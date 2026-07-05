"""Tests for the native SGP4 satpass adapter (env.satpass).

The native adapter computes every observer for a satellite in ONE tick, so it
consolidates in-memory and gates synchronously via the SHARED
`satpass_handler.gate_consolidated_pass` — with NO `satpass_pending` buffer and
NO Central consumer/timer. These tests monkeypatch `compute_passes` and
`get_observers` (no SGP4 / no network) and seed a fresh `sat_tles` row, then
exercise: multi-observer consolidation, cross-tick dedup via `satpass_events`,
resilient empty cases, the precomposed aos→peak→los wire, and a guard that the
Central path still feeds the shared gate the correctly-merged consolidation.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from meshai.env.satpass import SatpassAdapter
from meshai.config import SatpassConfig
from meshai.central.pass_predictor import PassInfo
from meshai.central.tle_handler import upsert_tle
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
    monkeypatch.setattr("meshai.central.pass_predictor.compute_passes", fake)


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

    # Wire carries entry->exit region + aos->peak->los compass sweep.
    wire = evt["wire"]
    assert "boise→twin" in wire               # entry -> exit
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

    # Tick 2 (interval elapsed): same canonical pass must be suppressed.
    assert adapter.tick(now=T0 + 2000) is False
    assert adapter.get_events() == []


def test_second_tick_without_commit_is_not_deduped(monkeypatch):
    # Guard the semantics: dedup persists ONLY after the broadcast commits.
    _enable_satpass_db(dry_run=False)
    _seed_iss_tle()
    _patch_observers(monkeypatch, [_BOISE, _TWIN])
    _patch_predictor(monkeypatch, _two_observer_pass)

    adapter = _adapter()
    assert adapter.tick(now=T0 - 3600) is True
    # No commit fired -> last_broadcast_at still NULL -> re-stages next tick.
    assert adapter.tick(now=T0 + 2000) is True


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
# 5. SHARED-GATE EXTRACTION GUARD (Central path still feeds the gate right)
# ══════════════════════════════════════════════════════════════════════

def test_central_consolidate_feeds_shared_gate_merged(monkeypatch):
    """consolidate_satpass_pending must merge observers and hand the SAME
    shared gate a correctly-consolidated dict (earliest AOS / latest LOS /
    max-el observer / entry+exit / observer_list). Spying the gate proves the
    Central path routes through the extracted, source-agnostic function."""
    import meshai.central.satpass_handler as sh

    conn = get_db()
    cid = f"25544:{T0 // 3600}"
    # Two pending rows for the same canonical id (boise entry, twin exit+peak).
    rows = [
        (cid, "boise", "ISS", 25544, 40.0, T0, T0 + 300, "SW", "NW", "E", T0, T0 + 5),
        (cid, "twin", "ISS", 25544, 70.0, T0 + 60, T0 + 400, "W", "NE", "S", T0, T0 + 5),
    ]
    for r in rows:
        conn.execute(
            "INSERT OR REPLACE INTO satpass_pending("
            "consolidated_id, observer, sat_name, norad_id, max_elevation, "
            "aos_at, los_at, aos_compass, los_compass, peak_compass, "
            "received_at, due_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", r)

    captured = {}

    def _spy(consolidated, *, now):
        captured["c"] = consolidated
        captured["now"] = now
        return None  # short-circuit: no broadcast side effects

    monkeypatch.setattr(sh, "gate_consolidated_pass", _spy)

    result = sh.consolidate_satpass_pending(cid)
    assert result is None

    c = captured["c"]
    assert c["consolidated_id"] == cid
    assert c["norad_id"] == 25544
    assert c["max_elevation"] == 70.0              # twin (higher)
    assert c["aos_epoch"] == T0                     # boise earliest AOS
    assert c["los_epoch"] == T0 + 400               # twin latest LOS
    assert c["aos_compass"] == "SW"                 # boise
    assert c["los_compass"] == "NE"                 # twin
    assert c["peak_compass"] == "S"                 # twin (max-el observer)
    assert c["entry_observer"] == "boise"
    assert c["exit_observer"] == "twin"
    assert c["observer_list"] == "boise,twin"

    # Pending buffer is drained regardless of the gate's decision.
    left = conn.execute(
        "SELECT COUNT(*) AS n FROM satpass_pending WHERE consolidated_id=?",
        (cid,)).fetchone()
    assert left["n"] == 0
