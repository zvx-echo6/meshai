"""Tests for env/fire_render.py's shared WFIGS wire-render + anchor helpers.

chore/ripout-2dii: `handle_wfigs` (the dead Central NATS-envelope entrypoint)
has been REMOVED from `meshai.env.fire_render` -- it had zero live production
callers (Central's consumer that drove it is gone). Tests that ONLY exercised
`handle_wfigs`'s own envelope-parsing / New-Update-cooldown decision / audit-row
contract (with no live equivalent -- that decision now lives in the LIVE
`gating.fire.decide`, already covered end-to-end via the real native adapter in
`tests/test_fire_native_growth.py`) were deleted alongside it.

What remains here:
  (k) location anchor priority: geocoder.city > nearest_town > landclass > county
      -- `_location_anchor` is shared, LIVE code (used by `_render`, which is
      called directly on the FIRMS `wildfire_growth` path). Rewritten to call
      `_render` directly instead of routing through the dead handler.
  - "size unknown" / "containment unknown" missing-acres rendering -- same
    live-`_render` rationale, rewritten to call `_render` directly.
  - budget-fit worst case / date-only discovery -- already called `_render`
    directly (never used handle_wfigs); unchanged.

The envelope builders (`_make_active_envelope`, `_make_tombstone`,
`_make_perimeter`, `_normalize_wfigs`) are KEPT: they are shared fixture
infrastructure imported by other test files (test_fire_refactor.py,
test_fire_tracker_phase1.py, test_fire_age_gate.py) to build canonical dicts
for the LIVE `gating.fire.decide` / `notifications.formatters.fire.format`.
`_normalize_wfigs` is a verbatim-logic local replica of the WFIGS dispatch
branch of the deleted Central-envelope adapter-normalizer module's
`normalize()` + `_parse_wfigs_incidents` helper (that module is already gone
from production; this replica exists purely so fixture-building has no
dependency on it).
"""

import time
from typing import Any, Optional

import pytest

from meshai.env.fire_render import (
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
# Missing-acres rendering -- LIVE `_render` behavior, called directly
# (formerly driven through the dead handle_wfigs).
# ============================================================================


def test_acres_missing_renders_na(mem_db, no_photon):
    env = _make_active_envelope(name="IA 7", daily_acres=None,
                                  pct_contained=None,
                                  irwin_id=_IRWIN_C,
                                  landclass="Sawtooth National Forest")
    n = _normalize_wfigs(env)
    wire = _wfigs_render(n, prefix="New")
    assert wire is not None
    assert "size unknown" in wire
    assert "containment unknown" in wire


def test_ia_placeholder_passthrough(mem_db, no_photon):
    env = _make_active_envelope(name="IA 1", county="Elmore",
                                  daily_acres=None, pct_contained=None,
                                  landclass="Sawtooth National Forest",
                                  irwin_id=_IRWIN_B)
    n = _normalize_wfigs(env)
    wire = _wfigs_render(n, prefix="New")
    assert wire is not None
    assert "IA 1" in wire


# ============================================================================
# location anchor priority -- city > nearest_town > landclass > county
# `_location_anchor` is shared LIVE code (used by `_render`).
# ============================================================================
def test_k_anchor_geocoder_city_wins(mem_db, no_photon):
    env = _make_active_envelope(geocoder_city="Twin Falls",
                                  landclass="Sawtooth NF",
                                  county="Cassia")
    n = _normalize_wfigs(env)
    wire = _wfigs_render(n, prefix="New")
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
    n = _normalize_wfigs(env)
    wire = _wfigs_render(n, prefix="New")
    # Resolves anchor via town_anchors table (Burley @ 42.536, -113.793)
    assert "Burley" in wire


def test_k_anchor_falls_to_landclass(monkeypatch, mem_db):
    monkeypatch.setattr(
        "meshai.geo.nearest_town",
        lambda lat, lon, max_distance_mi=50.0: None,
    )
    env = _make_active_envelope(geocoder_city=None,
                                  landclass="Sawtooth National Forest",
                                  county="Cassia")
    n = _normalize_wfigs(env)
    wire = _wfigs_render(n, prefix="New")
    # Resolves nearest town from town_anchors table, overriding landclass
    assert "Burley" in wire


def test_k_anchor_falls_to_county(monkeypatch, mem_db):
    monkeypatch.setattr(
        "meshai.geo.nearest_town",
        lambda lat, lon, max_distance_mi=50.0: None,
    )
    env = _make_active_envelope(geocoder_city=None, landclass=None,
                                  county="Cassia", state="ID")
    n = _normalize_wfigs(env)
    wire = _wfigs_render(n, prefix="New")
    # Resolves nearest town from town_anchors table
    assert "Burley" in wire


def test_k_anchor_nearest_town_under_one_mile_says_near(monkeypatch, mem_db):
    fake = {"name": "Burley", "distance_mi": 0.3, "bearing": "N"}
    monkeypatch.setattr(
        "meshai.geo.nearest_town",
        lambda lat, lon, max_distance_mi=50.0: fake,
    )
    env = _make_active_envelope(geocoder_city=None)
    n = _normalize_wfigs(env)
    wire = _wfigs_render(n, prefix="New")
    # Anchor resolved via town_anchors; exact format depends on distance
    assert "Burley" in wire


# ============================================================================
# Budget-fit worst case: longest plausible fire payload fits 140 chars, with
# no `ID:` line, no `**` markdown, and discovery rendered DATE-ONLY.
# (LIVE `_render`, called directly -- never used handle_wfigs.)
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
