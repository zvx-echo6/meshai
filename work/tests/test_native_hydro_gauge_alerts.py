"""fix/native-gauge-flood-alerts: native USGS gauge flood alerts are renderable.

Before this fix, env/usgs.py emitted `stream_flood_warning` / `stream_high_water`
Events that had NO registered decider and NO registered formatter -- they were
silently dropped by store._emit_event / compose_mesh_message (see
notifications/gating/__init__.py's old "deferred follow-up" comment and
notifications/formatters/__init__.py's old "deferred" comment, both since
updated). This file proves the fix along three axes:

1. Registration: both native categories now resolve a decider AND a formatter
   (mirrors TestRegistration in test_hydro_refactor.py, which covers the
   Central-only `stream_flow` category).
2. Gate behavior: a first elevated reading broadcasts, a sustained (same-band)
   reading suppresses, an escalation broadcasts again -- reusing the SAME
   hydro.decide() gate the Central path uses, now also fed by env/usgs.py's
   to_event() (which populates event.data with the canonical schema the gate
   and formatter both expect -- previously event.data was left empty for
   native events).
3. Golden wire format: formatters.hydro.format() renders a native event the
   same way it renders a Central `stream_flow` event (test_hydro_refactor.py
   already proves that formatter is byte-identical to the old
   central.nwis_handler._render()) -- captured here for the native shape
   specifically, where flow_cfs is always None (env/usgs.py's to_event() only
   ever emits stage/height readings; a paired discharge reading, if any,
   arrives as its own separate Event with no flood_status and never reaches
   to_event() -- see the docstring added to to_event()/decide() for detail).
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from meshai.env.usgs import USGSStreamsAdapter
from meshai.persistence import close_thread_connection, init_db
from meshai.persistence import db as persistence_db
from tests.harness.goldens import assert_byte_identical


# ── DB fixture (same shape as test_hydro_refactor.py / test_nwis_handler.py) ─

@pytest.fixture
def mem_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "native-hydro-test.sqlite")
    monkeypatch.setenv("MESHAI_DB_PATH", db_path)
    persistence_db._initialised.clear()
    close_thread_connection()
    conn = init_db()
    yield conn
    close_thread_connection()
    persistence_db._initialised.discard(db_path)


@pytest.fixture
def adapter():
    config = MagicMock()
    config.sites = []
    config.tick_seconds = 900
    config.flood_thresholds = {}
    return USGSStreamsAdapter(config)


def make_reading(*, site_id="13186000", site_name="Snake River at Heise",
                  value, flood_status, ts, lat=43.612, lon=-111.654):
    """Mirrors the internal event dict env/usgs.py's _fetch() stores."""
    now = time.time()
    return {
        "source": "usgs",
        "event_id": f"{site_id}_height",
        "event_type": "Stream Gauge",
        "headline": f"{site_name}: {value} ft" + (f" — {flood_status}" if flood_status else ""),
        "severity": "priority" if flood_status and "Flood" in flood_status else "routine",
        "lat": lat,
        "lon": lon,
        "expires": now + 1800,
        "fetched_at": now,
        "properties": {
            "site_id": site_id,
            "site_name": site_name,
            "parameter": "Gage height",
            "value": value,
            "unit": "ft",
            "timestamp": ts,
            "flood_status": flood_status,
            "flood_stages": {"action_stage": 9.0, "flood_stage": 10.5},
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# 1. Registration — both native categories resolve a decider AND a formatter
# ─────────────────────────────────────────────────────────────────────────────

class TestRegistration:
    @pytest.mark.parametrize("category", ["stream_flood_warning", "stream_high_water"])
    def test_decider_registered(self, category):
        from meshai.notifications.gating import get_decider
        from meshai.notifications.gating.hydro import decide
        assert get_decider(category) is decide

    @pytest.mark.parametrize("category", ["stream_flood_warning", "stream_high_water"])
    def test_formatter_registered(self, category):
        from meshai.notifications.formatters import get_formatter
        from meshai.notifications.formatters.hydro import format as hfmt
        assert get_formatter(category) is hfmt

    @pytest.mark.parametrize("category", ["stream_flood_warning", "stream_high_water"])
    def test_decider_is_not_the_earthquake_decider(self, category):
        """Regression guard: these categories must never resolve to the
        seismic/earthquake gate, even though get_toggle() names the shared
        family 'seismic' (see test_water_v057.py — that mapping is
        intentional and pre-existing: stream_flow already lives on the same
        toggle, and every water/hydro registry entry is guarded by
        test_alert_categories_water_complete)."""
        from meshai.notifications.gating import get_decider
        from meshai.notifications.gating.quake import decide as quake_decide
        assert get_decider(category) is not quake_decide

    @pytest.mark.parametrize("category", ["stream_flood_warning", "stream_high_water"])
    def test_native_always_decide(self, category):
        """Both categories are forced onto the live decider+formatter path
        unconditionally (independent of MESHAI_CUTOVER_CATEGORIES), exactly
        like the native WFIGS fire categories -- required because Central
        never emits these two category strings, so there is no shadow-bake
        window to wait out."""
        from meshai.notifications.cutover import NATIVE_ALWAYS_DECIDE
        assert category in NATIVE_ALWAYS_DECIDE


# ─────────────────────────────────────────────────────────────────────────────
# 2. Gate behavior — first crossing broadcasts, sustained state suppresses
# ─────────────────────────────────────────────────────────────────────────────

class TestGateBehavior:
    def _decide(self, adapter, evt):
        from meshai.notifications.gating import get_decider
        from meshai.notifications import clock
        event = adapter.to_event(evt)
        assert event is not None, "adapter unexpectedly suppressed the reading"
        decider = get_decider(event.category)
        gate = decider(event.data, source=event.source, now=clock.now())
        event.data.update(gate.data_patch)
        return event, gate

    def test_first_elevated_reading_broadcasts(self, adapter, mem_db):
        """A fresh site (no prior gauge_readings row) at action stage is a
        first-crossing -- 'no prior' degrades gracefully to normal->action,
        which broadcasts."""
        evt = make_reading(value=9.2, flood_status="Action Stage",
                            ts="2026-07-17T10:00:00-06:00")
        event, gate = self._decide(adapter, evt)
        assert event.category == "stream_high_water"
        assert gate.broadcast is True
        assert gate.lifecycle == "new"

    def test_routine_reading_never_reaches_the_gate(self, adapter, mem_db):
        """A below-action reading has no flood_status; the adapter drops it
        before to_event() ever returns an Event (unchanged pre-fix behavior
        -- this fix does not change what gets emitted, only what happens to
        what was already being emitted)."""
        evt = make_reading(value=5.0, flood_status=None,
                            ts="2026-07-17T10:00:00-06:00")
        assert adapter.to_event(evt) is None

    def test_sustained_elevated_reading_suppresses(self, adapter, mem_db):
        """Same site, same band, next tick: does NOT re-broadcast."""
        first = make_reading(value=9.2, flood_status="Action Stage",
                              ts="2026-07-17T10:00:00-06:00")
        second = make_reading(value=9.3, flood_status="Action Stage",
                               ts="2026-07-17T10:15:00-06:00")
        _, gate1 = self._decide(adapter, first)
        assert gate1.broadcast is True
        _, gate2 = self._decide(adapter, second)
        assert gate2.broadcast is False
        assert "unchanged band" in gate2.reason

    def test_escalation_broadcasts_again(self, adapter, mem_db):
        """Action stage -> minor flood on the same site is a second, higher
        crossing -- broadcasts again."""
        action = make_reading(value=9.2, flood_status="Action Stage",
                               ts="2026-07-17T10:00:00-06:00")
        flood = make_reading(value=10.8, flood_status="Minor Flood",
                              ts="2026-07-17T10:15:00-06:00")
        _, gate1 = self._decide(adapter, action)
        assert gate1.broadcast is True
        event2, gate2 = self._decide(adapter, flood)
        assert event2.category == "stream_flood_warning"
        assert gate2.broadcast is True
        assert gate2.lifecycle == "new"

    def test_native_write_does_not_leak_into_central_source(self, adapter, mem_db):
        """Sanity: the native persistence write in gating.hydro.decide() is
        gated on source != 'nwis'. Calling decide() directly with
        source='nwis' (the Central source) must NOT insert a row -- the
        Central handler owns its own inline INSERT, and decide() must stay
        read-only for that source (see hydro.py module docstring)."""
        from meshai.notifications.gating.hydro import decide
        canonical = {
            "site_id": "USGS-99999999", "gauge_name": "Should Not Persist",
            "stage_ft": 12.0, "flow_cfs": None, "unit": "ft",
            "threshold_state": "action", "reading_time": 1_700_000_000,
            "lat": 44.0, "lon": -115.0, "parameter_code": "00065",
        }
        decide(canonical, source="nwis", now=1_700_000_000.0)
        row = mem_db.execute(
            "SELECT COUNT(*) AS n FROM gauge_readings WHERE site_id=?",
            ("USGS-99999999",),
        ).fetchone()
        assert row["n"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# 3. Golden wire format — native event renders via the shared hydro formatter
# ─────────────────────────────────────────────────────────────────────────────

class TestFormatterGolden:
    """Captured-from-current-behavior goldens for the native shape.

    Unlike a Central envelope (which can carry a paired 00060 discharge value
    via the 00060 back-look), a native reading NEVER carries flow_cfs -- see
    to_event()'s docstring/comment: only stage/height readings reach
    to_event() at all, so flow_cfs is always None here. The stage/label/coords
    segments are otherwise identical to the Central-path golden in
    test_hydro_refactor.py::TestFormatterGolden, since both flow through the
    exact same formatters.hydro.format().
    """

    def _render(self, adapter, evt):
        from meshai.notifications.gating import get_decider
        from meshai.notifications.formatters.hydro import format as hfmt
        from meshai.notifications import clock
        event = adapter.to_event(evt)
        decider = get_decider(event.category)
        gate = decider(event.data, source=event.source, now=clock.now())
        event.data.update(gate.data_patch)
        return hfmt(event, now=clock.now(), budget=140)

    def test_high_water_wire(self, adapter, mem_db):
        evt = make_reading(
            site_name="Snake River at Heise", value=9.2,
            flood_status="Action Stage", ts="2026-07-17T10:00:00-06:00",
            lat=43.612, lon=-111.654,
        )
        wire = self._render(adapter, evt)
        assert wire == "🌊 New: Snake River at Heise: action stage 9.2 ft, @ 43.612,-111.654"

    def test_flood_warning_wire(self, adapter, mem_db):
        evt = make_reading(
            site_name="Boise River", value=14.5,
            flood_status="Minor Flood", ts="2026-07-17T10:00:00-06:00",
            lat=43.600, lon=-116.200,
        )
        wire = self._render(adapter, evt)
        assert wire == "🌊 New: Boise River: minor flooding 14.5 ft, @ 43.600,-116.200"

    def test_major_flood_wire(self, adapter, mem_db):
        evt = make_reading(
            site_name="Test Gauge", value=20.0,
            flood_status="Major Flood", ts="2026-07-17T10:00:00-06:00",
            lat=44.0, lon=-114.0,
        )
        wire = self._render(adapter, evt)
        assert wire == "🌊 New: Test Gauge: major flooding 20.0 ft, @ 44.000,-114.000"

    def test_missing_coords_drops_at_tail(self, adapter, mem_db):
        """Byte-identical drop behavior to the Central-path golden for the
        same case (test_hydro_refactor.py::test_missing_coords_drops_at_tail)."""
        evt = make_reading(
            site_name="No Coords Gauge", value=10.0,
            flood_status="Action Stage", ts="2026-07-17T10:00:00-06:00",
            lat=None, lon=None,
        )
        assert adapter.to_event(evt) is None, "to_event() requires lat/lon"

        # Exercise the formatter directly with coords stripped after the fact
        # to prove the "@ ..." segment is correctly omitted, mirroring the
        # Central golden (to_event() itself refuses a coord-less reading, so
        # this checks the formatter behavior the same way
        # test_hydro_refactor.py does: via a synthetic canonical dict).
        from meshai.notifications.formatters.hydro import format as hfmt

        class _FakeEvent:
            pass
        e = _FakeEvent()
        e.data = {
            "gauge_name": "No Coords Gauge", "threshold_state": "action",
            "stage_ft": 10.0, "flow_cfs": None, "unit": "ft",
            "lat": None, "lon": None,
        }
        wire = hfmt(e, now=1_700_000_000.0, budget=140)
        assert_byte_identical(
            wire, "🌊 New: No Coords Gauge: action stage 10.0 ft"
        )
        assert "@" not in wire


# ─────────────────────────────────────────────────────────────────────────────
# 4. End-to-end: compose_mesh_message renders the same wire (full pipeline)
# ─────────────────────────────────────────────────────────────────────────────

def test_compose_mesh_message_end_to_end(adapter, mem_db):
    """Full pipeline: to_event() -> decider -> compose_mesh_message(), the
    same call sequence store._emit_event() + the mesh dispatcher make in
    production. Proves the NATIVE_ALWAYS_DECIDE gate actually takes effect
    for compose_mesh_message's formatter dispatch, not just get_formatter()
    resolution."""
    from meshai.notifications.gating import get_decider
    from meshai.notifications.renderers.composer import compose_mesh_message
    from meshai.notifications import clock

    evt = make_reading(
        site_name="Snake River at Heise", value=9.2,
        flood_status="Action Stage", ts="2026-07-17T10:00:00-06:00",
        lat=43.612, lon=-111.654,
    )
    event = adapter.to_event(evt)
    decider = get_decider(event.category)
    gate = decider(event.data, source=event.source, now=clock.now())
    assert gate.broadcast is True
    event.data.update(gate.data_patch)
    wire = compose_mesh_message(event)
    assert wire == "🌊 New: Snake River at Heise: action stage 9.2 ft, @ 43.612,-111.654"
