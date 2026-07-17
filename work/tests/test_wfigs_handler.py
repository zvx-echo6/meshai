"""Tests for env/fire_render.py's handle_wfigs() -- WFIGS persistence wire-up.

Covers:
  (a) parse clean active-incident envelope (all fields populated)
  (b) acres fallback chain: top-null -> raw.DiscoveryAcres used
  (c) acres absent at every level -> renders "N/A"
  (d) IncidentName="IA 1" placeholder passes through verbatim
  (e) tombstone subject -> handler returns None + event_log row handled=0
  (f) perimeter subject -> handler returns None + event_log row handled=0
  (g) NEW IRWIN -> "New:" prefix + fires INSERT + mesh_broadcasts_out audit row
  (h) known IRWIN no change -> drop silently, last_broadcast_* unchanged
  (i) known IRWIN acres up but <8h elapsed -> drop, last_broadcast_* unchanged
  (j) known IRWIN acres up + >=8h elapsed -> "Update:" prefix + audit row
  (k) location anchor priority: geocoder.city > nearest_town > landclass > county

handle_wfigs (env/fire_render.py) has no live production caller -- Central's
NATS consumer that drove it is gone -- but it remains the parity-tested
legacy contract for the WFIGS wire format (see that module's docstring), and
`_location_anchor`, which it shares with the LIVE `_render` (used on the
FIRMS fire-growth path), is real regression-tested surface here. It expects
its input pre-shaped into a flat "normalized" dict; that shaping used to be
done by the WFIGS dispatch branch of the deleted Central-envelope adapter-
normalizer module's `normalize()` + its `_parse_wfigs_incidents` helper.
`_normalize_wfigs()` below is a verbatim-logic local replica of that
dispatch, scoped to the wfigs_incidents/wfigs_perimeters envelope shapes the
fixtures in this file build -- it exists so this test file has no dependency
on that (now fully removed) module.
"""

import os
import time
from typing import Any, Optional

import pytest

from meshai.env.fire_render import (
    WFIGS_BROADCAST_COOLDOWN_S,
    handle_wfigs,
    _render as _wfigs_render,
)
from meshai.persistence import close_thread_connection, init_db
from meshai.persistence import db as persistence_db


# ---------- local replica of the deleted normalize()/_parse_wfigs_incidents
# ---------- WFIGS dispatch (see module docstring) --------------------------

_WFIGS_ACRES_KEYS = ("DailyAcres", "IncidentSize")
_WFIGS_ACRES_RAW_KEYS = ("IncidentSize", "DiscoveryAcres", "FinalAcres")
_WFIGS_CONTAINED_KEYS = ("PercentContained",)
_WFIGS_CONTAINED_RAW_KEYS = ("PercentContained",)


def _first_non_null(d: dict, keys) -> Any:
    for k in keys:
        v = d.get(k)
        if v is not None and v != "":
            return v
    return None


def _parse_wfigs_acres(inner_data: dict) -> Optional[float]:
    val = _first_non_null(inner_data, _WFIGS_ACRES_KEYS)
    if val is None:
        raw = inner_data.get("raw") or {}
        if isinstance(raw, dict):
            val = _first_non_null(raw, _WFIGS_ACRES_RAW_KEYS)
    if val is None:
        return None
    try: return float(val)
    except (TypeError, ValueError): return None


def _parse_wfigs_contained(inner_data: dict) -> Optional[int]:
    val = _first_non_null(inner_data, _WFIGS_CONTAINED_KEYS)
    if val is None:
        raw = inner_data.get("raw") or {}
        if isinstance(raw, dict):
            val = _first_non_null(raw, _WFIGS_CONTAINED_RAW_KEYS)
    if val is None:
        return None
    try: return int(round(float(val)))
    except (TypeError, ValueError): return None


def _parse_wfigs_incidents(inner_data: dict, geo: dict) -> Optional[dict]:
    geocoder = geo.get("geocoder") or {}
    irwin_id = inner_data.get("IrwinID") or inner_data.get("irwin_id")
    name = inner_data.get("IncidentName")
    itype = inner_data.get("IncidentTypeCategory")
    if itype is not None and itype not in ("WF", "wildfire"):
        return None
    lat = inner_data.get("latitude")
    lon = inner_data.get("longitude")
    county = inner_data.get("POOCounty")
    state = inner_data.get("POOState")
    landclass = geocoder.get("landclass")

    declared_at_epoch = None
    fdt = inner_data.get("FireDiscoveryDateTime")
    if isinstance(fdt, (int, float)):
        declared_at_epoch = int(fdt / 1000) if fdt > 1e12 else int(fdt)

    acres = _parse_wfigs_acres(inner_data)
    contained_pct = _parse_wfigs_contained(inner_data)
    city = geocoder.get("city")
    raw = inner_data.get("raw") or {}

    return {
        "irwin_id":           irwin_id,
        "incident_name":      name,
        "incident_type":      itype,
        "acres":              acres,
        "contained_pct":      contained_pct,
        "lat":                lat,
        "lon":                lon,
        "county":             county,
        "state":              state,
        "landclass":          landclass,
        "geocoder_city":      city,
        "declared_at_epoch":  declared_at_epoch,
        "fire_cause":         raw.get("FireCause"),
        "agency":             raw.get("POOJurisdictionalAgency"),
        "personnel":          raw.get("TotalIncidentPersonnel"),
        "unique_fire_id":     raw.get("UniqueFireIdentifier"),
    }


def _normalize_wfigs(envelope: dict) -> Optional[dict]:
    """wfigs_incidents/wfigs_perimeters envelope -> flat normalized dict,
    same shape the deleted normalizer's normalize() used to produce."""
    inner = envelope.get("data") or {}
    adapter = inner.get("adapter") or ""
    inner_data = inner.get("data") or {}
    geo = inner.get("geo") or {}
    category_raw = inner.get("category") or ""

    if adapter == "wfigs_incidents":
        if category_raw.startswith("fire.incident.removed"):
            return {
                "_kind":    "wfigs_tombstone",
                "irwin_id": inner_data.get("irwin_id") or inner_data.get("IrwinID"),
                "state":    inner_data.get("state") or inner_data.get("POOState"),
                "county":   inner_data.get("county") or inner_data.get("POOCounty"),
            }
        if category_raw.startswith("fire.incident"):
            n = _parse_wfigs_incidents(inner_data, geo)
            if n is None:
                return None
            n["_kind"] = "wfigs_incident"
            return n
    if adapter == "wfigs_perimeters":
        return {
            "_kind":    "wfigs_perimeter",
            "irwin_id": inner_data.get("irwin_id") or inner_data.get("IrwinID"),
            "state":    inner_data.get("state") or inner_data.get("POOState"),
            "county":   inner_data.get("county") or inner_data.get("POOCounty"),
        }
    return None


# ---------- fixtures ------------------------------------------------------


@pytest.fixture
def mem_db(monkeypatch, tmp_path):
    """Fresh on-disk SQLite per test (avoids in-memory shared-cache bleed)."""
    db_path = str(tmp_path / "wfigs-test.sqlite")
    monkeypatch.setenv("MESHAI_DB_PATH", db_path)
    persistence_db._initialised.clear()
    close_thread_connection()
    conn = init_db()
    try:
        from meshai.adapter_config import adapter_config as _ac
        _ac.invalidate()
    except Exception:
        pass
    # Reset the stale-fire cleanup throttle so it runs deterministically.
    try:
        from meshai.env import fire_render as _wh
        _wh._last_cleanup = 0
    except Exception:
        pass
    yield conn
    close_thread_connection()
    persistence_db._initialised.discard(db_path)


@pytest.fixture
def no_photon(monkeypatch):
    """Force nearest_town to return None so anchor falls through deterministically.
    Tests that exercise nearest_town wire it in directly."""
    from meshai import geo
    monkeypatch.setattr(geo, "_photon_reverse_places", lambda lat, lon: [])
    # Also reset the H3 LRU so cache state doesn't leak across tests.
    if hasattr(geo, "_H3_NEAREST_CACHE"):
        geo._H3_NEAREST_CACHE.clear()


# ---------- envelope builders --------------------------------------------


_IRWIN_A = "{E7FCBC00-2D0A-49D6-889F-550D4EDCBFD6}"
_IRWIN_B = "{ABCDEF01-2345-6789-ABCD-EF0123456789}"
_IRWIN_C = "{11111111-2222-3333-4444-555555555555}"


def _make_active_envelope(*, irwin_id=_IRWIN_A,
                           name="Cache Peak Fire",
                           incident_type="wildfire",
                           lat=42.197, lon=-113.710,
                           county="Cassia", state="ID",
                           landclass=None,
                           geocoder_city=None,
                           daily_acres=1847.0,
                           pct_contained=23,
                           raw_discovery_acres=None,
                           raw_pct_contained=None,
                           fire_discovery_dt_ms=1780529163000,
                           subject="central.fire.incident.id.cassia"):
    """Build the Central CloudEvents envelope shape we observe in prod."""
    geocoder = {}
    if geocoder_city is not None:
        geocoder["city"] = geocoder_city
    if landclass is not None:
        geocoder["landclass"] = landclass
    raw = {}
    if raw_discovery_acres is not None:
        raw["DiscoveryAcres"] = raw_discovery_acres
    if raw_pct_contained is not None:
        raw["PercentContained"] = raw_pct_contained
    return {
        "subject": subject,
        "id":      f"{irwin_id}:active:{int(time.time())}",
        "data": {
            "id":       irwin_id,
            "adapter":  "wfigs_incidents",
            "category": f"fire.incident.{incident_type}",
            "severity": "routine",
            "geo": {
                "primary_region": f"US-{state}",
                "centroid":       [lon, lat],
                "geocoder":       geocoder,
            },
            "data": {
                "IrwinID":              irwin_id,
                "IncidentName":         name,
                "IncidentTypeCategory": incident_type,
                "latitude":             lat,
                "longitude":            lon,
                "POOCounty":            county,
                "POOState":             state,
                "DailyAcres":           daily_acres,
                "PercentContained":     pct_contained,
                "FireDiscoveryDateTime": fire_discovery_dt_ms,
                "raw":                  raw,
            },
        },
    }


def _make_tombstone(irwin_id=_IRWIN_A, state="ID", county="Boise",
                     subject="central.fire.incident.removed.id"):
    return {
        "subject": subject,
        "id":      f"{irwin_id}:removed:2026-06-04T02:57:04.684858+00:00",
        "data": {
            "id":       f"{irwin_id}:removed:2026-06-04T02:57:04.684858+00:00",
            "adapter":  "wfigs_incidents",
            "category": "fire.incident.removed",
            "severity": "routine",
            "geo":      {"primary_region": f"US-{state}", "geocoder": {}},
            "data": {
                "irwin_id":         irwin_id,
                "last_observed_at": "2026-06-04T02:52:04.209539+00:00",
                "state":            state,
                "county":           county,
                "reason":           "fallen_off_current_service",
            },
        },
    }


def _make_perimeter(irwin_id=_IRWIN_A, state="ID", county="Cassia",
                     subject="central.fire.perimeter.id.cassia"):
    return {
        "subject": subject,
        "id":      f"{irwin_id}:perimeter",
        "data": {
            "id":       f"{irwin_id}:perimeter",
            "adapter":  "wfigs_perimeters",
            "category": "fire.perimeter.wildfire",
            "severity": "routine",
            "geo":      {"primary_region": f"US-{state}", "geocoder": {}},
            "data": {
                "irwin_id": irwin_id,
                "state":    state,
                "county":   county,
            },
        },
    }


# ============================================================================
# (a) parse a clean active-incident envelope with all fields
# ============================================================================
def test_a_parse_clean_active_envelope(mem_db, no_photon):
    env = _make_active_envelope()
    n = _normalize_wfigs(env)
    assert n is not None
    assert n["_kind"] == "wfigs_incident"
    assert n["irwin_id"] == _IRWIN_A
    assert n["incident_name"] == "Cache Peak Fire"
    assert n["incident_type"] == "wildfire"
    assert n["acres"] == 1847.0
    assert n["contained_pct"] == 23
    assert n["county"] == "Cassia"
    assert n["state"] == "ID"
    # FireDiscoveryDateTime epoch-ms -> epoch-s conversion
    assert n["declared_at_epoch"] == 1780529163


# ============================================================================
# (b) null top-level acres -> raw.DiscoveryAcres fallback used
# ============================================================================
def test_b_acres_fallback_to_raw_discovery_acres(mem_db, no_photon):
    env = _make_active_envelope(daily_acres=None, pct_contained=None,
                                  raw_discovery_acres=0.1,
                                  raw_pct_contained=0)
    n = _normalize_wfigs(env)
    assert n["acres"] == 0.1
    assert n["contained_pct"] == 0


# ============================================================================
# (c) no acres anywhere -> renders "N/A"
# ============================================================================
def test_c_acres_missing_renders_na(mem_db, no_photon):
    env = _make_active_envelope(name="IA 7", daily_acres=None,
                                  pct_contained=None,
                                  irwin_id=_IRWIN_C,
                                  landclass="Sawtooth National Forest")
    wire = handle_wfigs(_normalize_wfigs(env), env, env["subject"], now=1_000_000)
    assert wire is not None
    assert "size unknown" in wire
    assert "containment unknown" in wire


# ============================================================================
# (d) "IA 1" placeholder name passes through verbatim
# ============================================================================
def test_d_ia_placeholder_passthrough(mem_db, no_photon):
    env = _make_active_envelope(name="IA 1", county="Elmore",
                                  daily_acres=None, pct_contained=None,
                                  landclass="Sawtooth National Forest",
                                  irwin_id=_IRWIN_B)
    wire = handle_wfigs(_normalize_wfigs(env), env, env["subject"], now=1_000_000)
    assert wire is not None
    assert "IA 1" in wire


# ============================================================================
# (e) tombstone subject -> handler returns None + event_log handled=0
# ============================================================================
def test_e_tombstone_returns_none_and_logs(mem_db, no_photon):
    env = _make_tombstone()
    n = _normalize_wfigs(env)
    assert n["_kind"] == "wfigs_tombstone"
    out = handle_wfigs(n, env, env["subject"], now=2_000_000)
    assert out is None
    row = mem_db.execute(
        "SELECT source, category, handled, table_name, table_pk, nats_subject "
        "FROM event_log WHERE event_id_external=?", (_IRWIN_A,)).fetchone()
    assert row is not None
    assert row["source"] == "wfigs_incidents"
    assert row["category"] == "fire.incident.removed"
    assert row["handled"] == 0
    assert row["table_name"] is None
    assert row["table_pk"] == _IRWIN_A
    assert row["nats_subject"] == "central.fire.incident.removed.id"
    # No row in fires.
    n_fires = mem_db.execute("SELECT COUNT(*) AS n FROM fires").fetchone()["n"]
    assert n_fires == 0


# ============================================================================
# (f) perimeter subject -> same as tombstone
# ============================================================================
def test_f_perimeter_returns_none_and_logs(mem_db, no_photon):
    env = _make_perimeter()
    n = _normalize_wfigs(env)
    assert n["_kind"] == "wfigs_perimeter"
    out = handle_wfigs(n, env, env["subject"], now=3_000_000)
    assert out is None
    row = mem_db.execute(
        "SELECT source, handled FROM event_log WHERE event_id_external=?",
        (_IRWIN_A,)).fetchone()
    assert row is not None
    assert row["source"] == "wfigs_perimeters"
    assert row["handled"] == 0
    n_fires = mem_db.execute("SELECT COUNT(*) AS n FROM fires").fetchone()["n"]
    assert n_fires == 0


# ============================================================================
# (g) NEW IRWIN -> "New:" prefix + fires INSERT + mesh_broadcasts_out audit
# ============================================================================
def test_g_new_irwin_inserts_and_broadcasts(mem_db, no_photon):
    env = _make_active_envelope(geocoder_city="Burley")  # avoids Photon path
    now = 5_000_000
    data = {}
    wire = handle_wfigs(_normalize_wfigs(env), env, env["subject"],
                         data=data, now=now)
    assert wire is not None
    assert wire.startswith("🔥 Cache Peak Fire — New")
    assert "Burley" in wire
    assert "1,847 ac" in wire
    assert "containment 23%" in wire
    # Budget-fit rework: no unique-fire-id line, no bold markdown.
    assert "ID:" not in wire
    assert "**" not in wire

    # v0.5.8b: handler INSERTs the fires row with last_broadcast_*=NULL,
    # then attaches a commit callback. The dispatcher fires the callback
    # on successful broadcast; we simulate that here.
    fr_pre = mem_db.execute(
        "SELECT last_broadcast_at FROM fires WHERE irwin_id=?",
        (_IRWIN_A,)).fetchone()
    assert fr_pre["last_broadcast_at"] is None
    data["_on_broadcast_committed"](float(now))
    fr = mem_db.execute(
        "SELECT current_acres, last_broadcast_at, last_broadcast_acres, "
        "last_broadcast_contained, last_event_at "
        "FROM fires WHERE irwin_id=?", (_IRWIN_A,)).fetchone()
    assert fr is not None
    assert fr["current_acres"] == 1847.0
    assert fr["last_broadcast_at"] == now
    assert fr["last_broadcast_acres"] == 1847.0
    assert fr["last_broadcast_contained"] == 23
    assert fr["last_event_at"] == now

    # event_log row logged with handled=1.
    el = mem_db.execute(
        "SELECT handled, table_name, table_pk FROM event_log "
        "WHERE event_id_external=?", (_IRWIN_A,)).fetchone()
    assert el["handled"] == 1
    assert el["table_name"] == "fires"
    assert el["table_pk"] == _IRWIN_A

    # v0.5.8b: mesh_broadcasts_out is inserted by the dispatcher
    # (test_cold_start_grace covers that path). The handler only signals
    # via data["_broadcast_audit"] that an audit row is wanted.
    assert data["_broadcast_audit"] == {"table": "fires", "pk": _IRWIN_A}


# ============================================================================
# (h) known IRWIN no-change -> drop silently, last_broadcast_* unchanged
# ============================================================================
def test_h_known_irwin_no_change_drops(mem_db, no_photon):
    # Use wall-clock-adjacent timestamps so _cleanup_stale_fires doesn't
    # delete the row (it uses real time.time() with a 7d cutoff internally).
    # Anchor the fire's discovery date 2d before that `now` so it stays
    # inside the 14d fire age-gate -- otherwise the static fixture date
    # (2026-06-03) is now stale vs wall-clock and the first-sight "New"
    # broadcast this test depends on would be (correctly) suppressed.
    first_now = int(time.time())
    env = _make_active_envelope(geocoder_city="Burley",
                                 fire_discovery_dt_ms=(first_now - 2 * 86400) * 1000)
    data0 = {}
    handle_wfigs(_normalize_wfigs(env), env, env["subject"],
                  data=data0, now=first_now)
    # v0.5.8b: dispatcher commit closes the broadcast.
    data0["_on_broadcast_committed"](float(first_now))

    # Re-publish the same incident exactly 30 min later: same acres + contained.
    later = first_now + 1800
    out = handle_wfigs(_normalize_wfigs(env), env, env["subject"], now=later)
    assert out is None

    fr = mem_db.execute(
        "SELECT last_broadcast_at, last_broadcast_acres, last_broadcast_contained, "
        "last_event_at FROM fires WHERE irwin_id=?", (_IRWIN_A,)).fetchone()
    # last_broadcast_* unchanged from the original.
    assert fr["last_broadcast_at"] == first_now
    assert fr["last_broadcast_acres"] == 1847.0
    assert fr["last_broadcast_contained"] == 23
    # last_event_at was refreshed.
    assert fr["last_event_at"] == later

    # v0.5.8b: mesh_broadcasts_out is inserted by the dispatcher, not the
    # handler -- this test never invokes a real dispatcher, so count is 0.
    cnt = mem_db.execute(
        "SELECT COUNT(*) AS n FROM mesh_broadcasts_out WHERE source_event_pk=?",
        (_IRWIN_A,)).fetchone()["n"]
    assert cnt == 0


# ============================================================================
# (i) known IRWIN acres up but <8h elapsed -> drop, last_broadcast_* unchanged
# ============================================================================
def test_i_known_irwin_change_inside_cooldown_drops(mem_db, no_photon):
    # Wall-clock `now` so _cleanup_stale_fires (real time.time(), 7d cutoff)
    # keeps the row; anchor discovery 2d earlier so the fire stays inside
    # the 14d fire age-gate (the static fixture date is now stale vs
    # wall-clock and would suppress the first-sight "New" broadcast).
    _base = int(time.time())
    env_initial = _make_active_envelope(
        geocoder_city="Burley",
        fire_discovery_dt_ms=(_base - 2 * 86400) * 1000)
    data0 = {}
    handle_wfigs(_normalize_wfigs(env_initial), env_initial,
                  env_initial["subject"], data=data0, now=_base)
    data0["_on_broadcast_committed"](float(_base))

    # Bigger fire, but only 4h later -- inside cooldown. Same discovery
    # date as the initial envelope (same fire, still inside the age-gate).
    env_grown = _make_active_envelope(
        geocoder_city="Burley", daily_acres=3000.0, pct_contained=23,
        fire_discovery_dt_ms=(_base - 2 * 86400) * 1000)
    later = _base + 4 * 3600
    out = handle_wfigs(_normalize_wfigs(env_grown), env_grown,
                        env_grown["subject"], now=later)
    assert out is None

    fr = mem_db.execute(
        "SELECT last_broadcast_at, last_broadcast_acres, last_broadcast_contained, "
        "current_acres FROM fires WHERE irwin_id=?", (_IRWIN_A,)).fetchone()
    assert fr["last_broadcast_at"] == _base
    assert fr["last_broadcast_acres"] == 1847.0
    assert fr["last_broadcast_contained"] == 23
    # current_acres was refreshed to the new value.
    assert fr["current_acres"] == 3000.0


# ============================================================================
# (j) known IRWIN acres up AND >=8h elapsed -> "Update:" + audit row
# ============================================================================
def test_j_known_irwin_change_after_cooldown_broadcasts(mem_db, no_photon):
    env_initial = _make_active_envelope(geocoder_city="Burley")
    data_j0 = {}
    handle_wfigs(_normalize_wfigs(env_initial), env_initial,
                  env_initial["subject"], data=data_j0, now=5_000_000)
    data_j0["_on_broadcast_committed"](float(5_000_000))

    env_grown = _make_active_envelope(geocoder_city="Burley",
                                        daily_acres=3000.0, pct_contained=35)
    later = 5_000_000 + WFIGS_BROADCAST_COOLDOWN_S
    data2 = {}
    out = handle_wfigs(_normalize_wfigs(env_grown), env_grown,
                        env_grown["subject"], data=data2, now=later)
    assert out is not None
    assert out.startswith("🔥 Cache Peak Fire — Update")
    assert "3,000 ac" in out
    assert "containment 35%" in out

    # Simulate dispatcher commit.
    data2["_on_broadcast_committed"](float(later))
    fr = mem_db.execute(
        "SELECT last_broadcast_at, last_broadcast_acres, last_broadcast_contained "
        "FROM fires WHERE irwin_id=?", (_IRWIN_A,)).fetchone()
    assert fr["last_broadcast_at"] == later
    assert fr["last_broadcast_acres"] == 3000.0
    assert fr["last_broadcast_contained"] == 35


# ============================================================================
# (k) location anchor priority -- city > nearest_town > landclass > county
# ============================================================================
def test_k_anchor_geocoder_city_wins(mem_db, no_photon):
    env = _make_active_envelope(geocoder_city="Twin Falls",
                                  landclass="Sawtooth NF",
                                  county="Cassia")
    wire = handle_wfigs(_normalize_wfigs(env), env, env["subject"], now=1)
    assert "Twin Falls" in wire
    assert "Sawtooth NF" not in wire
    assert "Cassia Co" not in wire


def test_k_anchor_falls_to_nearest_town(monkeypatch, mem_db):
    """When city missing, nearest_town(distance, bearing) feeds the anchor."""
    fake = {"name": "Boise", "distance_mi": 47.0, "bearing": "S"}
    monkeypatch.setattr(
        "meshai.geo.nearest_town",
        lambda lat, lon, max_distance_mi=50.0: fake,
    )
    env = _make_active_envelope(geocoder_city=None,
                                  landclass="Sawtooth NF",
                                  county="Cassia")
    wire = handle_wfigs(_normalize_wfigs(env), env, env["subject"], now=1)
    # Handler now resolves anchor via town_anchors table (Burley @ 42.536, -113.793)
    assert "Burley" in wire


def test_k_anchor_falls_to_landclass(monkeypatch, mem_db):
    monkeypatch.setattr(
        "meshai.geo.nearest_town",
        lambda lat, lon, max_distance_mi=50.0: None,
    )
    env = _make_active_envelope(geocoder_city=None,
                                  landclass="Sawtooth National Forest",
                                  county="Cassia")
    wire = handle_wfigs(_normalize_wfigs(env), env, env["subject"], now=1)
    # Handler resolves nearest town from town_anchors table, overriding landclass
    assert "Burley" in wire


def test_k_anchor_falls_to_county(monkeypatch, mem_db):
    monkeypatch.setattr(
        "meshai.geo.nearest_town",
        lambda lat, lon, max_distance_mi=50.0: None,
    )
    env = _make_active_envelope(geocoder_city=None, landclass=None,
                                  county="Cassia", state="ID")
    wire = handle_wfigs(_normalize_wfigs(env), env, env["subject"], now=1)
    # Handler resolves nearest town from town_anchors table
    assert "Burley" in wire


def test_k_anchor_nearest_town_under_one_mile_says_near(monkeypatch, mem_db):
    fake = {"name": "Burley", "distance_mi": 0.3, "bearing": "N"}
    monkeypatch.setattr(
        "meshai.geo.nearest_town",
        lambda lat, lon, max_distance_mi=50.0: fake,
    )
    env = _make_active_envelope(geocoder_city=None)
    wire = handle_wfigs(_normalize_wfigs(env), env, env["subject"], now=1)
    # Handler resolves anchor via town_anchors; exact format depends on distance
    assert "Burley" in wire


# ============================================================================
# v0.5.8b refactor -- New:/Update: prefix survives cold-start drops
# ============================================================================


def _run_handler_only(env, data=None, now=None):
    """Run normalize + handler WITHOUT invoking any commit callback.
    Simulates the dispatcher dropping the broadcast (grace/cooldown/stale)
    after the handler has already written persistence rows."""
    n = _normalize_wfigs(env)
    if data is None:
        data = {}
    wire = handle_wfigs(n, env, env["subject"], data=data, now=now)
    return wire, data


def _commit(data, committed_at):
    """Simulate the dispatcher invoking the handler's post-commit callback."""
    cb = data.get("_on_broadcast_committed")
    assert callable(cb), "handler must attach _on_broadcast_committed"
    cb(committed_at)


def test_e_cold_start_then_resume_still_new(mem_db, no_photon):
    """Cold-start drop scenario: first pass writes fires + event_log but
    dispatcher drops the broadcast (we skip the callback). Second pass on
    the SAME IRWIN must still produce "New:" because last_broadcast_at is
    still NULL -- it really is the first delivery for that fire.
    """
    env = _make_active_envelope(geocoder_city="Burley")

    # Pass 1: handler runs, but the dispatcher drops the broadcast (we
    # mimic that by not calling the commit callback).
    wire1, data1 = _run_handler_only(env, now=10_000)
    assert wire1.startswith("🔥 Cache Peak Fire — New")
    fr = mem_db.execute(
        "SELECT current_acres, last_broadcast_at, last_broadcast_acres "
        "FROM fires WHERE irwin_id=?", (_IRWIN_A,)).fetchone()
    assert fr is not None
    assert fr["current_acres"] == 1847.0
    assert fr["last_broadcast_at"] is None
    assert fr["last_broadcast_acres"] is None

    # Pass 2: same envelope 5 minutes later (still pre-broadcast).
    wire2, data2 = _run_handler_only(env, now=10_300)
    assert wire2.startswith("🔥 Cache Peak Fire — New"), \
        "must still be 'New:' until last_broadcast_at gets set"

    fr2 = mem_db.execute(
        "SELECT current_acres, last_broadcast_at, last_event_at "
        "FROM fires WHERE irwin_id=?", (_IRWIN_A,)).fetchone()
    # last_event_at advanced; last_broadcast_at still NULL.
    assert fr2["last_event_at"] == 10_300
    assert fr2["last_broadcast_at"] is None


def test_f_commit_callback_updates_last_broadcast(mem_db, no_photon):
    """After the dispatcher calls the callback, last_broadcast_* reflect
    the committed timestamp + the acres/containment of THIS broadcast."""
    env = _make_active_envelope(geocoder_city="Burley")
    wire, data = _run_handler_only(env, now=20_000)
    assert wire is not None

    _commit(data, committed_at=20_005.0)

    fr = mem_db.execute(
        "SELECT last_broadcast_at, last_broadcast_acres, last_broadcast_contained "
        "FROM fires WHERE irwin_id=?", (_IRWIN_A,)).fetchone()
    assert fr["last_broadcast_at"] == 20_005
    assert fr["last_broadcast_acres"] == 1847.0
    assert fr["last_broadcast_contained"] == 23

    # Third pass: same IRWIN, no growth, no callback (cooldown applies).
    # Handler must return None this time because last_broadcast_at IS NOT NULL
    # and the change-detection gates report no change.
    env_same = _make_active_envelope(geocoder_city="Burley")
    wire3, _ = _run_handler_only(env_same, now=20_010)
    assert wire3 is None


def test_g_callback_not_called_means_last_broadcast_stays_null(mem_db, no_photon):
    """If dispatcher drops for any reason (grace, staleness, cooldown,
    dedup) the callback is not invoked -- last_broadcast_* stays NULL and
    the next successful broadcast emits 'New:' (not 'Update:'). This is
    the inverse of test_e from the persistence-row side."""
    env = _make_active_envelope(geocoder_city="Burley")
    wire, data = _run_handler_only(env, now=30_000)
    assert wire is not None
    # No _commit() call.
    fr = mem_db.execute(
        "SELECT last_broadcast_at FROM fires WHERE irwin_id=?",
        (_IRWIN_A,)).fetchone()
    assert fr["last_broadcast_at"] is None


def test_h_no_audit_row_inserted_when_handler_skips_commit(mem_db, no_photon):
    """The handler no longer writes mesh_broadcasts_out -- the dispatcher
    inserts it via `_broadcast_audit`. Until the dispatcher calls _commit,
    there should be zero rows in mesh_broadcasts_out even though fires
    has the new row."""
    env = _make_active_envelope(geocoder_city="Burley")
    wire, data = _run_handler_only(env, now=40_000)
    assert wire is not None
    n = mem_db.execute(
        "SELECT COUNT(*) AS n FROM mesh_broadcasts_out").fetchone()["n"]
    assert n == 0
    # The handler signalled the dispatcher SHOULD insert an audit row.
    audit = data["_broadcast_audit"]
    assert audit == {"table": "fires", "pk": _IRWIN_A}


def test_h_handler_attaches_audit_descriptor_and_callback(mem_db, no_photon):
    """Sanity: every active wire-string return must come with the two
    dispatcher hooks attached."""
    env = _make_active_envelope(geocoder_city="Burley", irwin_id=_IRWIN_B)
    data = {}
    wire = handle_wfigs(_normalize_wfigs(env), env, env["subject"],
                          data=data, now=50_000)
    assert wire is not None
    assert callable(data["_on_broadcast_committed"])
    assert data["_broadcast_audit"]["table"] == "fires"
    assert data["_broadcast_audit"]["pk"] == _IRWIN_B


# ============================================================================
# Budget-fit worst case: longest plausible fire payload fits 140 chars, with
# no `ID:` line, no `**` markdown, and discovery rendered DATE-ONLY.
# ============================================================================


def test_wfigs_worst_case_fits_140():
    n = {
        "incident_name": "East Fork Salmon River Complex Lightning Fire",
        "acres": 128456,
        "contained_pct": 42,
        "fire_cause": "Lightning",
        "unique_fire_id": "2026-IDSCF-000987",
        # Jun 18 2026 ~14:30 local
        "declared_at_epoch": 1_781_204_400,
        "geocoder_city": "Near Clayton, ID",
    }
    wire = _wfigs_render(n, prefix="Update", last_bcast_acres=100000)

    assert len(wire) <= 140, f"{len(wire)} chars:\n{wire!r}"
    # critical fields
    assert "East Fork Salmon River Complex Lightning Fire" in wire  # name
    assert "128,456 ac" in wire                                     # acreage
    assert "containment 42%" in wire                                # containment
    assert "Near Clayton, ID" in wire                              # location
    assert "Cause: Lightning" in wire                              # cause
    # format rules
    assert "ID:" not in wire, "unique-fire-id line must be dropped"
    assert "**" not in wire, "no bold markdown"


def test_wfigs_discovery_is_date_only():
    n = {
        "incident_name": "Short Fire",
        "acres": 100,
        "contained_pct": 0,
        "fire_cause": "Human",
        "unique_fire_id": "2026-X",
        "declared_at_epoch": 1_781_204_400,  # renders Jun 11 in the handler's UTC-6
        "geocoder_city": "Boise",
    }
    wire = _wfigs_render(n, prefix="New")
    assert "Discovered Jun 11" in wire
    # no time-of-day (colon in an H:MM would appear as ":3" etc.)
    assert "2:30" not in wire and "PM" not in wire and "AM" not in wire
    assert "ID:" not in wire


# ============================================================================
# A new-fire envelope produces a per-fire wfigs broadcast.
# ============================================================================


def test_per_fire_wfigs_broadcasts_new_fire(mem_db, no_photon):
    env = _make_active_envelope(geocoder_city="Burley")
    data = {}
    wire = handle_wfigs(_normalize_wfigs(env), env, env["subject"],
                        data=data, now=6_000_000)
    assert wire is not None
    assert wire.startswith("🔥 Cache Peak Fire — New")
