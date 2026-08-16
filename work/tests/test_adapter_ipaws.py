"""Tests for the FEMA IPAWS-OPEN EAS civil-alert adapter (env/ipaws.py),
its gating decider (gating/ipaws.py) and formatter (formatters/ipaws.py).

Covers: two-stage fetch + base_url rebuild, statefips coarse filter, en-US
filter, status filter, NWS/NOAA-sender weather exclusion, SAME same_codes fine
filter, severity mapping reuse, category derivation, polygon geometry, to_event
canonical dict, the ipaws_alerts dedup/Update/tombstone decider (its OWN table,
not nws_alerts), and a dry-run formatter render of a real Idaho alert.

Fixtures under tests/fixtures/ipaws/ are REAL captured IPAWS documents (Idaho
CEM w/ polygon, Oregon EVI evacuation) plus a synthetic index + a synthetic
NWS/NOAA weather CAP for the exclusion test.
"""
from __future__ import annotations

import pathlib
from unittest.mock import MagicMock, patch

import pytest

from meshai.config import IPAWSConfig
from meshai.env.ipaws import IPAWSAlertsAdapter, _norm_fips
from meshai.env.nws import map_cap_severity
from meshai.notifications.events import Event

FIX = pathlib.Path(__file__).parent / "fixtures" / "ipaws"


def _fx(name: str) -> bytes:
    return (FIX / name).read_bytes()


# URL suffix -> fixture bytes. Keyed by the trailing path so the same map works
# for direct FEMA and proxied (Conduit) base_urls.
_ROUTES = {
    "/feed": "feed.xml",
    "/eas/300130859542": "eas_idaho_cem.xml",
    "/eas/300130856756": "eas_oregon_evi.xml",
    "/eas/016weather": "eas_weather_noaa.xml",
    # 999999999999 (CA / statefips 06) intentionally has NO route: it must be
    # dropped by the coarse statefips gate BEFORE any stage-2 fetch.
}


class _FakeResp:
    def __init__(self, data: bytes):
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _make_urlopen(requested: list):
    """Return a fake urlopen that records requested URLs and serves fixtures by
    trailing-path suffix (raising for any URL not explicitly routed)."""
    def _urlopen(req, timeout=None):
        url = req.full_url
        requested.append(url)
        for suffix, fname in _ROUTES.items():
            if url.endswith(suffix):
                return _FakeResp(_fx(fname))
        raise AssertionError(f"unexpected stage-2 fetch: {url}")
    return _urlopen


def _config(**over) -> IPAWSConfig:
    cfg = IPAWSConfig()
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


# ============================================================
# helpers / severity / fips
# ============================================================

def test_defaults_disabled_and_native():
    cfg = IPAWSConfig()
    assert cfg.enabled is False          # HARD CONSTRAINT: ships disabled
    assert cfg.feed_source == "native"
    assert cfg.exclude_weather is True
    assert cfg.status_actual_only is True
    assert "16" in cfg.state_fips        # Idaho in default scope


def test_norm_fips():
    assert _norm_fips("16") == "16"
    assert _norm_fips("6") == "06"
    assert _norm_fips(6) == "06"
    assert _norm_fips("41") == "41"


def test_severity_mapping_reuses_nws():
    assert map_cap_severity("Extreme") == "immediate"
    assert map_cap_severity("Severe") == "priority"
    assert map_cap_severity("Moderate") == "routine"
    assert map_cap_severity("Minor") == "routine"
    assert map_cap_severity("Unknown") == "routine"


# ============================================================
# stage-2 URL rebuild (base_url honoured for BOTH stages)
# ============================================================

def test_stage2_url_rebuilds_from_absolute_fema_link():
    a = IPAWSAlertsAdapter(_config())
    abs_link = "https://apps.fema.gov/IPAWSOPEN_EAS_SERVICE/rest/eas/300130859542"
    assert a._stage2_url(abs_link) == (
        "https://apps.fema.gov/IPAWSOPEN_EAS_SERVICE/rest/eas/300130859542"
    )


def test_stage2_url_routes_through_proxy_base_url():
    # In prod base_url points at the Conduit proxy; the absolute FEMA link in
    # the feed must be rewritten to go through it.
    a = IPAWSAlertsAdapter(_config(base_url="http://100.64.0.12:8010/up/ipaws"))
    abs_link = "https://apps.fema.gov/IPAWSOPEN_EAS_SERVICE/rest/eas/300130859542"
    assert a._stage2_url(abs_link) == "http://100.64.0.12:8010/up/ipaws/eas/300130859542"


def test_stage2_url_none_when_no_eas_segment():
    a = IPAWSAlertsAdapter(_config())
    assert a._stage2_url("https://apps.fema.gov/somethingelse") is None
    assert a._stage2_url("") is None


# ============================================================
# category derivation
# ============================================================

def test_derive_category_same_codes():
    a = IPAWSAlertsAdapter(_config())
    assert a._derive_category("EVI", "Evacuation Immediate") == "emergency_evacuation"
    assert a._derive_category("CEM", "Civil Emergency Message") == "emergency_civil"
    assert a._derive_category("CAE", "Child Abduction Emergency") == "emergency_amber"
    assert a._derive_category("LEW", "Law Enforcement Warning") == "emergency_law"
    assert a._derive_category("TOE", "911 Telephone Outage Emergency") == "emergency_911_outage"
    assert a._derive_category("HMW", "Hazardous Materials Warning") == "emergency_hazmat"


def test_derive_category_keyword_fallback():
    a = IPAWSAlertsAdapter(_config())
    # Unknown SAME code -> keyword match on the event string.
    assert a._derive_category("ZZZ", "Mandatory Evacuation Ordered") == "emergency_evacuation"
    assert a._derive_category("", "AMBER Alert") == "emergency_amber"
    assert a._derive_category("", "Something Unclassifiable") == "emergency_civil"


# ============================================================
# polygon geometry
# ============================================================

def test_polygon_geometry_from_real_cem():
    a = IPAWSAlertsAdapter(_config())
    parsed = a._parse_cap(_fx("eas_idaho_cem.xml"), "16")
    geom = parsed["geometry"]
    assert geom is not None
    assert geom["type"] == "Polygon"
    ring = geom["coordinates"][0]
    assert ring[0] == ring[-1]           # closed ring
    # GeoJSON order is [lon, lat]; Boundary County ID is ~ -116, +48.
    assert ring[0][0] < -115 and ring[0][1] > 48


# ============================================================
# CAP parse / filters (via the private single-parse helper)
# ============================================================

def test_status_actual_only_drops_test():
    a = IPAWSAlertsAdapter(_config())
    xml = _fx("eas_idaho_cem.xml").replace(b"<status>Actual</status>",
                                           b"<status>Test</status>")
    assert a._parse_cap(xml, "16") is None


def test_weather_sender_excluded():
    a = IPAWSAlertsAdapter(_config())
    # NOAA-originated CAP must be dropped so we never double-broadcast weather.
    assert a._parse_cap(_fx("eas_weather_noaa.xml"), "16") is None


def test_weather_sender_kept_when_exclude_off():
    a = IPAWSAlertsAdapter(_config(exclude_weather=False))
    parsed = a._parse_cap(_fx("eas_weather_noaa.xml"), "16")
    assert parsed is not None
    assert parsed["sender"].endswith("noaa.gov")


def test_same_codes_fine_filter():
    # same_codes gate: keep only alerts whose area SAME is in the list.
    keep = IPAWSAlertsAdapter(_config(same_codes=["016021"]))
    assert keep._parse_cap(_fx("eas_idaho_cem.xml"), "16") is not None
    drop = IPAWSAlertsAdapter(_config(same_codes=["016099"]))
    assert drop._parse_cap(_fx("eas_idaho_cem.xml"), "16") is None


def test_parse_cap_fields_idaho_cem():
    a = IPAWSAlertsAdapter(_config())
    p = a._parse_cap(_fx("eas_idaho_cem.xml"), "16")
    assert p["source"] == "ipaws"
    assert p["event_id"] == "AS-ID-de78a02d-8bf9-4528-8f84-fd417bbcaa5b"
    assert p["same_code"] == "CEM"
    assert p["category"] == "emergency_civil"
    assert p["cap_severity"] == "Extreme"
    assert p["msgType"] == "Update"
    assert "016021" in p["area_same_codes"]
    assert p["area_desc"] == "Boundary County"
    assert "evacuation" in p["description"].lower()


# ============================================================
# two-stage fetch (mocked HTTP) — coarse statefips gate
# ============================================================

def test_fetch_coarse_statefips_filter_and_base_url():
    requested = []
    a = IPAWSAlertsAdapter(_config())  # default state_fips includes 16 & 41, not 06
    with patch("meshai.env.ipaws.urlopen", _make_urlopen(requested)):
        changed = a._fetch()
    assert changed is True
    events = a.get_events()
    ids = {e["same_code"] for e in events}
    assert ids == {"CEM", "EVI"}         # Idaho CEM + Oregon EVI kept
    # CA (statefips 06) was dropped BEFORE stage-2: its eas URL was never fetched.
    assert not any("999999999999" in u for u in requested)
    # stage-1 + two in-scope stage-2 fetches only.
    assert sum(1 for u in requested if "/eas/" in u) == 2


def test_fetch_routes_all_stages_through_proxy_base_url():
    requested = []
    a = IPAWSAlertsAdapter(_config(base_url="http://100.64.0.12:8010/up/ipaws"))
    with patch("meshai.env.ipaws.urlopen", _make_urlopen(requested)):
        a._fetch()
    assert all(u.startswith("http://100.64.0.12:8010/up/ipaws") for u in requested)


def test_coverage_derived_state_fips_overrides_config():
    # A coverage dict (bbox->state_fips) narrows scope; here only Idaho (16),
    # so the Oregon (41) EVI is dropped by the coarse gate.
    requested = []
    a = IPAWSAlertsAdapter(_config(), coverage={"state_fips": ["16"]})
    with patch("meshai.env.ipaws.urlopen", _make_urlopen(requested)):
        a._fetch()
    assert {e["same_code"] for e in a.get_events()} == {"CEM"}


# ============================================================
# to_event canonical dict
# ============================================================

def test_to_event_canonical_dict():
    a = IPAWSAlertsAdapter(_config())
    raw = a._parse_cap(_fx("eas_oregon_evi.xml"), "41")
    ev = a.to_event(raw)
    assert isinstance(ev, Event)
    assert ev.source == "ipaws"
    assert ev.category == "emergency_evacuation"
    assert ev.severity == "immediate"            # Extreme -> immediate
    assert ev.group_key == raw["event_id"]
    d = ev.data
    assert d["cap_id"] == raw["event_id"]
    assert d["same_code"] == "EVI"
    assert d["category"] == "emergency_evacuation"
    assert d["geometry"] is not None
    assert d["geometry"]["type"] == "Polygon"
    assert d["geocoder"]["county"] == d["area_desc"]


def test_health_status_shape():
    a = IPAWSAlertsAdapter(_config())
    hs = a.health_status
    assert hs["source"] == "ipaws"
    assert set(hs) >= {"is_loaded", "last_error", "consecutive_errors",
                       "event_count", "last_fetch"}


# ============================================================
# gating decider — ipaws_alerts table (NOT nws_alerts)
# ============================================================

def _canonical(**over):
    base = {
        "cap_id": "IPAWS-TEST-1",
        "msgType": "Alert",
        "references": [],
        "event": "Civil Emergency Message",
        "area_desc": "Boundary County",
        "geocoder": {"city": None, "county": "Boundary County", "state": None},
        "cap_severity": "Extreme",
        "expires_at": 1_800_000_000,
        "description": "test",
        "category": "emergency_civil",
        "headline": "Test civil alert",
    }
    base.update(over)
    return base


def test_decider_first_sighting_broadcasts(_isolate_meshai_db):
    from meshai.notifications.gating.ipaws import decide
    r = decide(_canonical(), source="ipaws", now=1000.0)
    assert r.broadcast is True
    assert r.lifecycle == "new"
    assert r.data_patch.get("_ipaws_prefix") == ""


def test_decider_dedup_window(_isolate_meshai_db):
    from meshai.notifications.gating.ipaws import decide
    r1 = decide(_canonical(), source="ipaws", now=1000.0)
    r1.commit(1000.0)
    # within window -> suppress
    r2 = decide(_canonical(), source="ipaws", now=1000.0 + 60)
    assert r2.broadcast is False
    assert r2.lifecycle == "suppress"
    # past the 3h dedup window -> re-broadcast with Active prefix
    r3 = decide(_canonical(), source="ipaws", now=1000.0 + 10800 + 1)
    assert r3.broadcast is True
    assert r3.data_patch.get("_ipaws_prefix") == "Active"


def test_decider_tombstone_suppresses(_isolate_meshai_db):
    from meshai.notifications.gating.ipaws import decide
    for mt in ("Cancel", "Expire"):
        r = decide(_canonical(msgType=mt), source="ipaws", now=1000.0)
        assert r.broadcast is False
        assert r.lifecycle == "tombstone"


def test_decider_uses_own_table_not_nws(_isolate_meshai_db):
    from meshai.notifications.gating.ipaws import decide
    from meshai.persistence import get_db
    decide(_canonical(), source="ipaws", now=1000.0)
    conn = get_db()
    ipaws_n = conn.execute("SELECT COUNT(*) FROM ipaws_alerts").fetchone()[0]
    nws_n = conn.execute("SELECT COUNT(*) FROM nws_alerts").fetchone()[0]
    assert ipaws_n == 1
    assert nws_n == 0


# ============================================================
# formatter (dry-run render — NO transmit)
# ============================================================

def test_formatter_renders_civil_alert():
    """Idaho CEM headline ("Wildfire Immediate Evacuation Alert") names the GO
    phase explicitly ("Immediate Evacuation"), so line 1 leads with GO instead
    of the raw CAP event string — the whole point of this feature. Category
    is emergency_civil (normally ⚠️), but a detected GO forces the alarm
    emoji regardless of category — a confirmed "leave now" must never render
    with the softer warning icon."""
    from meshai.notifications.formatters.ipaws import format as ipaws_format
    a = IPAWSAlertsAdapter(_config())
    raw = a._parse_cap(_fx("eas_idaho_cem.xml"), "16")
    ev = a.to_event(raw)
    ev.data["_ipaws_prefix"] = ""
    wire = ipaws_format(ev, now=1000.0, budget=200)
    assert len(wire) <= 200
    lines = wire.split("\n")
    assert lines[0].startswith("🚨")                 # GO overrides civil's ⚠️ -> alarm emoji
    assert "GO" in lines[0] and "Leave now" in lines[0]
    assert "Boundary County" in wire
    assert "evacuation" in wire.lower()             # headline carried the signal


def test_formatter_evacuation_emoji():
    """Oregon EVI has no phase language in its headline, but its CMAMtext
    parameter ("Level 3 GO NOW evacuation notice...") does, and that text is
    now parsed and preferred for line 3 — so GO must win here too."""
    from meshai.notifications.formatters.ipaws import format as ipaws_format
    a = IPAWSAlertsAdapter(_config())
    raw = a._parse_cap(_fx("eas_oregon_evi.xml"), "41")
    ev = a.to_event(raw)
    wire = ipaws_format(ev, now=1000.0, budget=200)
    assert wire.startswith("🚨")                     # evacuation -> alarm emoji
    lines = wire.split("\n")
    assert "GO" in lines[0] and "Leave now" in lines[0]
    assert "Level 3 GO NOW" in wire                  # CMAMtext carried into line 3


def test_formatter_full_three_line_go_rendering():
    """Full 3-line render for a real GO alert (Oregon EVI): phase-led line 1,
    area+expiry line 2, CMAMtext line 3 — exact text, not just substrings."""
    from meshai.notifications.formatters.ipaws import format as ipaws_format
    a = IPAWSAlertsAdapter(_config())
    raw = a._parse_cap(_fx("eas_oregon_evi.xml"), "41")
    ev = a.to_event(raw)
    ev.data["_ipaws_prefix"] = ""
    wire = ipaws_format(ev, now=1000.0, budget=200)
    lines = wire.split("\n")
    assert lines[0] == "🚨 GO — Leave now"
    assert lines[1] == "Jackson County · Until 8:50 AM MDT"
    assert lines[2] == (
        "Wildfire Alert- Level 3 GO NOW evacuation notice is UPGRADED for JAC-126"
    )


def test_formatter_no_phase_fallback_unchanged():
    """When no READY/SET/GO language is present anywhere in headline/CMAMtext/
    description, line 1 keeps the CURRENT raw-event-string behaviour exactly —
    the safe fallback this feature must never break."""
    from meshai.notifications.formatters.ipaws import format as ipaws_format
    from meshai.notifications.events import make_event

    canonical = _canonical(
        event="Civil Emergency Message",
        headline="Boil water advisory issued for the district",
        description="A water main break has contaminated the supply.",
        parameters={},
        expires_at=None,
    )
    ev = make_event(
        source="ipaws",
        category="emergency_civil",
        severity="priority",
        title=canonical["headline"],
        summary=canonical["headline"],
        body=canonical["description"],
        data=canonical,
    )
    ev.data["_ipaws_prefix"] = ""
    wire = ipaws_format(ev, now=1000.0, budget=200)
    lines = wire.split("\n")
    assert lines[0] == "⚠️ Civil Emergency Message"    # unchanged fallback (no phase)
    assert lines[1] == "Boundary County"
    assert lines[2] == "Boil water advisory issued for the district"


# ============================================================
# coverage bbox -> state_fips
# ============================================================

def test_coverage_state_fips_for_bbox():
    from meshai.coverage import state_fips_for_bbox, resolve_adapter_coverage
    # An Idaho bbox must resolve to include FIPS 16.
    idaho_bbox = [-117.0, 42.0, -111.0, 49.0]
    fips = state_fips_for_bbox(idaho_bbox)
    assert "16" in fips
    resolved = resolve_adapter_coverage("ipaws", idaho_bbox, "native")
    assert "16" in resolved["state_fips"]
    assert resolved["bbox"] == [round(c, 6) for c in idaho_bbox]


# ============================================================
# stage-2 failure negative cache (no re-hammering FEMA)
# ============================================================

def test_stage2_forbidden_is_negative_cached():
    """A 403 on a stage-2 detail URL is remembered so the next poll tick does
    NOT re-fetch it, while a sibling URL that succeeds is fetched every pass."""
    from urllib.error import HTTPError

    counts: dict[str, int] = {}

    def _urlopen(req, timeout=None):
        url = req.full_url
        counts[url] = counts.get(url, 0) + 1
        if url.endswith("/feed"):
            return _FakeResp(_fx("feed.xml"))
        if url.endswith("/eas/300130859542"):        # Idaho CEM -> forbidden
            raise HTTPError(url, 403, "Forbidden", {}, None)
        if url.endswith("/eas/300130856756"):        # Oregon EVI -> ok
            return _FakeResp(_fx("eas_oregon_evi.xml"))
        raise AssertionError(f"unexpected fetch: {url}")

    a = IPAWSAlertsAdapter(_config())
    with patch("meshai.env.ipaws.urlopen", _urlopen):
        a._fetch()
        a._fetch()

    cem = next(u for u in counts if u.endswith("/eas/300130859542"))
    evi = next(u for u in counts if u.endswith("/eas/300130856756"))
    assert counts[cem] == 1     # 403 -> negative-cached, not re-fetched
    assert counts[evi] == 2     # success -> fetched on each pass
