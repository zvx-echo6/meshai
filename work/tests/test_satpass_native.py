"""Tests for the native SGP4 satpass adapter (env.satpass).

The native adapter computes every observer for a satellite in ONE tick, so it
consolidates in-memory and gates synchronously via
`env.satellite.pass_format.gate_consolidated_pass` — with no buffer table and
no consumer/timer. These tests monkeypatch `compute_passes` and
`get_observers` (no SGP4 / no network) and seed a fresh `sat_tles` row, then
exercise: multi-observer consolidation, cross-tick dedup via `satpass_events`,
resilient empty cases, and the precomposed aos→peak→los wire.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from meshai.env.satpass import SatpassAdapter
from meshai.config import SatpassConfig
from meshai.env.satellite.pass_predictor import PassInfo
from meshai.env.satellite.tle_store import upsert_tle
from meshai.persistence import get_db


# A valid ISS TLE so _resolve_tles / get_tle_by_norad has real data to return.
ISS_L1 = "1 25544U 98067A   26182.50000000  .00016717  00000-0  10270-3 0  9008"
ISS_L2 = "2 25544  51.6400 208.9163 0007417  17.6777  85.6621 15.54225995 12345"

# An hour-aligned base so both observers' AOS land in the same hour bucket
# (same canonical id) and therefore consolidate into ONE pass.
T0 = (1783000000 // 3600) * 3600 + 100  # aligned + 100s

_BOISE = {"slug": "boise", "name": "Boise", "lat": 43.6, "lon": -116.2, "alt_m": 0.0}
_TWIN = {"slug": "twin", "name": "Twin Falls", "lat": 42.5, "lon": -114.4, "alt_m": 0.0}


# ── helpers ──────────────────────────────────────────────────────────────

def _dt(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def _pass(aos: int, los: int, max_el: float,
          az_aos: float, az_los: float, az_peak: float) -> PassInfo:
    peak = (aos + los) // 2
    return PassInfo(
        aos_time=_dt(aos), los_time=_dt(los), peak_time=_dt(peak),
        max_elevation=max_el,
        azimuth_at_aos=az_aos, azimuth_at_los=az_los, azimuth_at_peak=az_peak,
    )


def _seed_iss_tle():
    """Seed a FRESH ISS TLE into sat_tles so the adapter resolves it."""
    conn = get_db()
    fresh = datetime.now(timezone.utc).isoformat()
    upsert_tle(conn, 25544, "ISS (ZARYA)", ISS_L1, ISS_L2, fresh)


def _enable_satpass_db(dry_run=False, max_per_hour=100, norad_ids=None):
    """Set satpass adapter_config in the test DB (the gate reads it)."""
    from meshai.adapter_config import invalidate_cache
    conn = get_db()
    conn.execute("UPDATE adapter_config SET value_json='true' "
                 "WHERE adapter='satpass' AND key='enabled'")
    conn.execute("UPDATE adapter_config SET value_json=? "
                 "WHERE adapter='satpass' AND key='dry_run'",
                 (json.dumps(dry_run),))
    conn.execute("UPDATE adapter_config SET value_json=? "
                 "WHERE adapter='satpass' AND key='max_broadcasts_per_hour'",
                 (json.dumps(max_per_hour),))
    if norad_ids is not None:
        conn.execute("UPDATE adapter_config SET value_json=? "
                     "WHERE adapter='satpass' AND key='norad_ids'",
                     (json.dumps(norad_ids),))
    invalidate_cache()


def _adapter(**overrides) -> SatpassAdapter:
    cfg = SatpassConfig(enabled=True, feed_source="native",
                        norad_ids=[25544], min_elevation_deg=10.0,
                        window_hours=24, **overrides)
    return SatpassAdapter(cfg)


def _patch_predictor(monkeypatch, fake):
    monkeypatch.setattr("meshai.env.satellite.pass_predictor.compute_passes", fake)


def _patch_observers(monkeypatch, observers):
    monkeypatch.setattr(
        "meshai.persistence.observer_locations.get_observers",
        lambda *a, **k: list(observers))


# Two observers, same pass: boise rises first (entry), twin sets last (exit)
# AND twin has the higher max elevation (so it supplies max_el + peak).
def _two_observer_pass(l1, l2, lat, lon, alt, window_h, min_el, now):
    if abs(lat - _BOISE["lat"]) < 0.1:
        return [_pass(T0, T0 + 300, 40.0, az_aos=225, az_los=300, az_peak=90)]
    if abs(lat - _TWIN["lat"]) < 0.1:
        return [_pass(T0 + 60, T0 + 400, 70.0, az_aos=270, az_los=45, az_peak=180)]
    return []


# ══════════════════════════════════════════════════════════════════════
# 1. MULTI-OBSERVER CONSOLIDATION
# ══════════════════════════════════════════════════════════════════════

def test_two_observers_consolidate_to_one_broadcast(monkeypatch):
    _enable_satpass_db(dry_run=False)
    _seed_iss_tle()
    _patch_observers(monkeypatch, [_BOISE, _TWIN])
    _patch_predictor(monkeypatch, _two_observer_pass)

    adapter = _adapter()
    changed = adapter.tick(now=T0 - 3600)  # any now; predictor ignores it

    assert changed is True
    staged = adapter.get_events()
    assert len(staged) == 1, "two observers, one pass -> ONE consolidated event"

    evt = staged[0]
    cid = f"25544:{T0 // 3600}"
    assert evt["event_id"] == cid

    # satpass_events row proves the merge: earliest AOS, latest LOS,
    # max-elevation from the higher observer, both observers recorded.
    row = get_db().execute(
        "SELECT observer, max_elevation, aos_at, los_at FROM satpass_events "
        "WHERE event_id=?", (cid,)).fetchone()
    assert row is not None
    assert row["max_elevation"] == 70.0            # twin (higher)
    assert row["aos_at"] == T0                      # boise (earliest AOS)
    assert row["los_at"] == T0 + 400                # twin (latest LOS)
    assert "boise" in row["observer"] and "twin" in row["observer"]

    # Wire carries entry->exit region (FRIENDLY names) + aos->peak->los sweep.
    wire = evt["wire"]
    assert "(Boise→Twin Falls)" in wire       # friendly entry -> exit
    assert "SW→S→NE" in wire             # aos -> peak(twin) -> los


# ══════════════════════════════════════════════════════════════════════
# 2. CROSS-TICK DEDUP (satpass_events remembers after commit)
# ══════════════════════════════════════════════════════════════════════

def test_second_tick_does_not_rebroadcast_after_commit(monkeypatch):
    _enable_satpass_db(dry_run=False)
    _seed_iss_tle()
    _patch_observers(monkeypatch, [_BOISE, _TWIN])
    _patch_predictor(monkeypatch, _two_observer_pass)

    adapter = _adapter()

    # Tick 1: stages the pass. Simulate a successful mesh send by firing the
    # commit closure the gate attached (this is what the dispatcher does).
    assert adapter.tick(now=T0 - 3600) is True
    staged = adapter.get_events()
    assert len(staged) == 1
    commit = staged[0]["data"]["_on_broadcast_committed"]
    commit(float(T0))  # marks satpass_events.last_broadcast_at

    # Tick 2 (interval elapsed): the pass is STILL imminent (AOS 30 min out,
    # inside the 60-min lead) so the imminence gate would stage it — but the
    # satpass_events commit from tick 1 must suppress the re-broadcast.
    assert adapter.tick(now=T0 - 1800) is False
    assert adapter.get_events() == []


def test_second_tick_without_commit_is_not_deduped(monkeypatch):
    # Guard the semantics: dedup persists ONLY after the broadcast commits.
    _enable_satpass_db(dry_run=False)
    _seed_iss_tle()
    _patch_observers(monkeypatch, [_BOISE, _TWIN])
    _patch_predictor(monkeypatch, _two_observer_pass)

    adapter = _adapter()
    assert adapter.tick(now=T0 - 3600) is True
    # No commit fired -> last_broadcast_at still NULL. The pass is still
    # imminent at T0-1800 (AOS 30 min out) so it re-stages next tick.
    assert adapter.tick(now=T0 - 1800) is True


# ══════════════════════════════════════════════════════════════════════
# 3. RESILIENT EMPTY CASES (never crash, yield nothing)
# ══════════════════════════════════════════════════════════════════════

def test_no_observers_yields_nothing(monkeypatch):
    _enable_satpass_db(dry_run=False)
    _seed_iss_tle()
    _patch_observers(monkeypatch, [])
    _patch_predictor(monkeypatch, _two_observer_pass)

    adapter = _adapter()
    assert adapter.tick(now=T0) is False
    assert adapter.get_events() == []
    assert adapter.health_status["is_loaded"] is True


def test_no_fresh_tles_yields_nothing(monkeypatch):
    _enable_satpass_db(dry_run=False)
    # Do NOT seed sat_tles -> get_tle_by_norad returns None.
    _patch_observers(monkeypatch, [_BOISE, _TWIN])
    _patch_predictor(monkeypatch, _two_observer_pass)

    adapter = _adapter()
    assert adapter.tick(now=T0) is False
    assert adapter.get_events() == []


def test_below_min_elevation_yields_nothing(monkeypatch):
    _enable_satpass_db(dry_run=False)
    _seed_iss_tle()
    _patch_observers(monkeypatch, [_BOISE, _TWIN])
    # Predictor filters by min_el internally; a too-low pass => empty list.
    _patch_predictor(monkeypatch, lambda *a, **k: [])

    adapter = _adapter()
    assert adapter.tick(now=T0) is False
    assert adapter.get_events() == []


# ══════════════════════════════════════════════════════════════════════
# 4. PRECOMPOSED WIRE renders aos->peak->los through format_pass
# ══════════════════════════════════════════════════════════════════════

def test_emitted_event_renders_aos_peak_los(monkeypatch):
    from meshai.notifications.renderers.composer import compose_mesh_message

    _enable_satpass_db(dry_run=False)
    _seed_iss_tle()
    _patch_observers(monkeypatch, [_BOISE, _TWIN])
    _patch_predictor(monkeypatch, _two_observer_pass)

    adapter = _adapter()
    adapter.tick(now=T0 - 3600)
    evt = adapter.get_events()[0]

    event = adapter.to_event(evt)
    assert event is not None
    assert event.category == "sat_pass"
    assert event.severity == "immediate"           # max_el 70 -> immediate
    assert event.data.get("_meshai_precomposed") is True
    assert callable(event.data.get("_on_broadcast_committed"))

    # Precomposed: composer returns the wire verbatim, with the peak point
    # rendered between aos and los.
    composed = compose_mesh_message(event)
    assert composed == evt["wire"]
    assert "SW→S→NE" in composed          # aos -> peak -> los
    assert composed.startswith("\U0001F6F0")        # satellite emoji


# ══════════════════════════════════════════════════════════════════════
# 6. IMMINENCE BROADCAST TRIGGER (satpass's "just received" analog)
# ══════════════════════════════════════════════════════════════════════
#
# Satpass is NOT an API-delta feed — it recomputes the SAME future passes
# every poll. Its "newly received" signal is IMMINENCE: a pass is staged for
# broadcast only once its AOS is both in the FUTURE and within the near-term
# lead window (default 60 min). A far-future pass is predicted (on schedule)
# but not staged; a past AOS is never staged. Combined with the store's
# received-delta seen-set, each pass then emits exactly once as it crosses in.

# The fixed mock pass sits at AOS=T0 (both observers consolidate to one).

def test_far_future_pass_not_staged(monkeypatch):
    _enable_satpass_db(dry_run=False)
    _seed_iss_tle()
    _patch_observers(monkeypatch, [_BOISE, _TWIN])
    _patch_predictor(monkeypatch, _two_observer_pass)

    adapter = _adapter()  # default broadcast_lead_seconds = 3600 (60 min)
    # AOS=T0 is 4 hours ahead of now -> well outside the 60-min lead window.
    assert adapter.tick(now=T0 - 4 * 3600) is False
    assert adapter.get_events() == []


def test_imminent_pass_is_staged(monkeypatch):
    _enable_satpass_db(dry_run=False)
    _seed_iss_tle()
    _patch_observers(monkeypatch, [_BOISE, _TWIN])
    _patch_predictor(monkeypatch, _two_observer_pass)

    adapter = _adapter()
    # AOS=T0 is 30 min ahead of now -> inside the 60-min lead window.
    assert adapter.tick(now=T0 - 1800) is True
    staged = adapter.get_events()
    assert len(staged) == 1
    assert staged[0]["event_id"] == f"25544:{T0 // 3600}"


def test_past_aos_never_staged(monkeypatch):
    _enable_satpass_db(dry_run=False)
    _seed_iss_tle()
    _patch_observers(monkeypatch, [_BOISE, _TWIN])
    _patch_predictor(monkeypatch, _two_observer_pass)

    adapter = _adapter()
    # now is 10 min AFTER AOS=T0 -> the pass is in the past, never staged.
    assert adapter.tick(now=T0 + 600) is False
    assert adapter.get_events() == []


def test_configurable_lead_window(monkeypatch):
    _enable_satpass_db(dry_run=False)
    _seed_iss_tle()
    _patch_observers(monkeypatch, [_BOISE, _TWIN])
    _patch_predictor(monkeypatch, _two_observer_pass)

    # With a 3-hour lead, an AOS 2 hours out is now imminent.
    adapter = _adapter(broadcast_lead_seconds=3 * 3600)
    assert adapter.tick(now=T0 - 2 * 3600) is True
    assert len(adapter.get_events()) == 1


# ══════════════════════════════════════════════════════════════════════
# 7. NORAD_IDS FILTER IS STRICT (non-listed sats never predicted)
# ══════════════════════════════════════════════════════════════════════

# A non-ISS TLE (a stand-in for a GOES/METEOR/FENGYUN weather sat the fetcher
# stocks via tle_groups). Different NORAD so a filter break would surface as a
# second consolidated pass with a different canonical id.
OTHER_L1 = "1 43226U 18022A   26182.50000000  .00000000  00000-0  00000-0 0  9999"
OTHER_L2 = "2 43226   0.0300 100.0000 0001000  90.0000 270.0000  1.00270000 12345"


def _seed_other_tle():
    conn = get_db()
    fresh = datetime.now(timezone.utc).isoformat()
    upsert_tle(conn, 43226, "GOES-17", OTHER_L1, OTHER_L2, fresh)


def test_norad_ids_filter_excludes_non_listed_sats(monkeypatch):
    _enable_satpass_db(dry_run=False)
    _seed_iss_tle()     # 25544 — configured
    _seed_other_tle()   # 43226 — fresh but NOT in norad_ids
    _patch_observers(monkeypatch, [_BOISE, _TWIN])
    # Predictor would return a pass for ANY satellite (keyed on observer lat),
    # so if 43226 leaked through it would produce a second staged pass.
    _patch_predictor(monkeypatch, _two_observer_pass)

    adapter = _adapter()  # norad_ids=[25544]
    assert adapter.tick(now=T0 - 1800) is True
    staged = adapter.get_events()
    # Only the configured NORAD is predicted, despite 43226 being fresh.
    assert len(staged) == 1
    assert staged[0]["norad_id"] == 25544


def test_parse_norad_ids_handles_comma_string():
    # A GUI-persisted comma string must NOT be char-iterated into garbage ids.
    assert SatpassAdapter._parse_norad_ids("25544, 33591") == [25544, 33591]
    assert SatpassAdapter._parse_norad_ids([25544, "33591"]) == [25544, 33591]
    assert SatpassAdapter._parse_norad_ids([]) == []


# ══════════════════════════════════════════════════════════════════════
# 8. observer_list FLOWS INTO THE GATE'S data DICT (region-tagging fix)
# ══════════════════════════════════════════════════════════════════════
#
# gate_consolidated_pass() builds the event `data` dict that rides all the
# way to the dispatcher via to_event(). Satpass events carry no lat/lon/
# geometry, so coverage_area.observer_region_names() reading
# event.data["observer_list"] is the ONLY path that can region-tag a
# satpass event for the region_routes matrix. These tests pin that the
# gate actually populates it (and stays safe when it can't).

def test_gate_consolidated_pass_data_contains_observer_list():
    """The (wire, data) tuple returned by gate_consolidated_pass() must
    carry the same comma-joined observer_list already written to the
    satpass_events audit column."""
    _enable_satpass_db(dry_run=False)
    from meshai.env.satellite import pass_format as sh

    consolidated = {
        "consolidated_id": "25544:900000",
        "norad_id": 25544,
        "sat_name": "ISS (ZARYA)",
        "max_elevation": 70.0,
        "aos_epoch": 1_000_000,
        "los_epoch": 1_000_300,
        "aos_compass": "SW",
        "los_compass": "NE",
        "peak_compass": "S",
        "entry_observer": "Boise",
        "exit_observer": "Twin Falls",
        "observer_list": "boise,twin",
    }
    result = sh.gate_consolidated_pass(consolidated, now=0)
    assert result is not None
    _, data = result
    assert data["observer_list"] == "boise,twin"


def test_gate_consolidated_pass_missing_observer_list_is_defensive():
    """A `consolidated` dict with no observer_list must not raise, and the
    resulting data["observer_list"] must be falsy (never a garbage value
    that would resolve to a bogus region)."""
    _enable_satpass_db(dry_run=False)
    from meshai.env.satellite import pass_format as sh

    consolidated = {
        "consolidated_id": "25544:900001",
        "norad_id": 25544,
        "sat_name": "ISS (ZARYA)",
        "max_elevation": 40.0,
        "aos_epoch": 2_000_000,
        "los_epoch": 2_000_300,
        "aos_compass": "SW",
        "los_compass": "NE",
        "peak_compass": "S",
        # entry_observer / exit_observer / observer_list all deliberately absent
    }
    result = sh.gate_consolidated_pass(consolidated, now=0)
    assert result is not None
    _, data = result
    assert not data.get("observer_list")

    # And observer_region_names() must treat that as "no region", not raise.
    from meshai.coverage_area import MonitoringArea, observer_region_names
    from meshai.notifications.events import make_event

    event = make_event(source="satpass", category="sat_pass", severity="routine",
                        title="Pass", data=data)
    areas = [MonitoringArea(north=44.0, south=42.0, east=-113.0, west=-117.0,
                            name="SW Idaho")]
    assert observer_region_names(event, areas) == []
