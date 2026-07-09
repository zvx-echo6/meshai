"""Unit tests for native severity/category promotion (CHANGE 1) and
severity-floor drop observability (CHANGE 2).

CHANGE 1 — store._emit_event() must promote decider data_patch keys
  _severity_override and category onto event.severity / event.category so the
  toggle/matrix min_severity floor sees the correct value, not the adapter's
  raw value.

CHANGE 2 — Dispatcher must:
  (a) emit a WARN log when an event is dropped by the severity floor, and
  (b) increment + persist the severity_floor_dropped counter.
  Both the toggle path and the matrix path are tested.
"""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from meshai.config import (
    Config, EnvironmentalConfig, NotificationToggle,
)
from meshai.notifications.events import make_event, Event
from meshai.notifications.gating.base import GateResult
from meshai.notifications.pipeline.dispatcher import Dispatcher
from meshai.notifications.pipeline.bus import EventBus
from meshai.env.store import EnvironmentalStore
from meshai.persistence import close_thread_connection, init_db
from meshai.persistence import db as persistence_db


# ─────────────────────────────────────────────────────────────────────────────
# Shared fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """Isolated SQLite DB for each test."""
    p = str(tmp_path / "sev-promo-test.sqlite")
    monkeypatch.setenv("MESHAI_DB_PATH", p)
    persistence_db._initialised.clear()
    close_thread_connection()
    try:
        from meshai.adapter_config import adapter_config as _ac
        _ac.invalidate()
    except Exception:
        pass
    init_db()
    yield p
    close_thread_connection()
    persistence_db._initialised.discard(p)


# ─────────────────────────────────────────────────────────────────────────────
# CHANGE 1 — Promotion tests
# ─────────────────────────────────────────────────────────────────────────────


class _FakeAdapter:
    """Minimal adapter stub for _emit_event tests."""

    def __init__(self, source="wfigs", category="wildfire_incident",
                 severity="routine"):
        self._source = source
        self._category = category
        self._severity = severity

    def to_event(self, raw_evt: dict) -> Event:
        return make_event(
            source=self._source,
            category=self._category,
            severity=self._severity,
            title="Test fire",
        )


def _make_store_with_bus():
    """Build an EnvironmentalStore (no real adapters) + capture bus."""
    bus = EventBus()
    captured: list[Event] = []
    bus.subscribe(lambda e: captured.append(e))
    store = EnvironmentalStore(EnvironmentalConfig(), event_bus=bus)
    return store, captured


class TestSeverityPromotion:
    """_emit_event promotes _severity_override + category onto the Event."""

    def test_promotion_applied_when_override_present(self, db_path):
        """A decider patch with _severity_override='priority' and
        category='wildfire_declared' must be stamped onto the emitted Event."""
        store, captured = _make_store_with_bus()
        adapter = _FakeAdapter(severity="routine")
        raw_evt = {"source": "wfigs", "event_id": "fire_001"}

        gate = GateResult(
            broadcast=True,
            data_patch={
                "_severity_override": "priority",
                "category": "wildfire_declared",
            },
        )
        fake_decider = lambda data, *, source, now: gate

        with patch("meshai.notifications.gating.get_decider",
                   return_value=fake_decider), \
             patch("meshai.notifications.cutover.is_cutover",
                   return_value=False), \
             patch("meshai.notifications.cutover.NATIVE_ALWAYS_DECIDE",
                   {"wildfire_incident"}):
            store._emit_event(adapter, raw_evt)

        assert len(captured) == 1, "expected exactly one emitted event"
        ev = captured[0]
        assert ev.severity == "priority", (
            f"severity not promoted: got {ev.severity!r}"
        )
        assert ev.category == "wildfire_declared", (
            f"category not promoted: got {ev.category!r}"
        )

    def test_no_promotion_without_override(self, db_path):
        """An event whose decider patch carries NO _severity_override or
        category keys must leave event.severity and event.category unchanged."""
        store, captured = _make_store_with_bus()
        adapter = _FakeAdapter(severity="routine", category="wildfire_incident")
        raw_evt = {"source": "wfigs", "event_id": "fire_002"}

        gate = GateResult(
            broadcast=True,
            data_patch={"some_other_key": "value"},  # no override keys
        )
        fake_decider = lambda data, *, source, now: gate

        with patch("meshai.notifications.gating.get_decider",
                   return_value=fake_decider), \
             patch("meshai.notifications.cutover.is_cutover",
                   return_value=False), \
             patch("meshai.notifications.cutover.NATIVE_ALWAYS_DECIDE",
                   {"wildfire_incident"}):
            store._emit_event(adapter, raw_evt)

        assert len(captured) == 1
        ev = captured[0]
        assert ev.severity == "routine", (
            f"severity was unexpectedly changed: got {ev.severity!r}"
        )
        assert ev.category == "wildfire_incident", (
            f"category was unexpectedly changed: got {ev.category!r}"
        )

    def test_no_promotion_when_no_decider(self, db_path):
        """Without a registered decider the event passes through unchanged."""
        store, captured = _make_store_with_bus()
        adapter = _FakeAdapter(severity="routine", category="wildfire_incident")
        raw_evt = {"source": "wfigs", "event_id": "fire_003"}

        with patch("meshai.notifications.gating.get_decider",
                   return_value=None):
            store._emit_event(adapter, raw_evt)

        assert len(captured) == 1
        ev = captured[0]
        assert ev.severity == "routine"
        assert ev.category == "wildfire_incident"


# ─────────────────────────────────────────────────────────────────────────────
# CHANGE 2 — Floor observability: toggle path
# ─────────────────────────────────────────────────────────────────────────────


def _build_dispatcher_config(*, toggle_name="fire", min_severity="priority",
                               cold_start_grace=0):
    """Config with one toggle, no region filter, floor at min_severity."""
    cfg = Config()
    cfg.notifications.cold_start_grace_seconds = cold_start_grace
    cfg.notifications.toggles = {
        toggle_name: NotificationToggle(
            name=toggle_name, enabled=True,
            min_severity=min_severity,
            freshness_seconds=0,       # disabled — avoids stale-drop
            cooldown_seconds=0,
            severity_channels={
                "routine": ["mesh_broadcast"],
                "priority": ["mesh_broadcast"],
                "immediate": ["mesh_broadcast"],
            },
            broadcast_channel=1,
        ),
    }
    return cfg


def _mk_channel_factory():
    ch = MagicMock()
    ch.deliver = AsyncMock(return_value=True)
    return lambda rule, connector: ch


class TestToggleFloorObservability:
    """Toggle-path severity-floor drop emits WARN and increments counter."""

    def test_floor_drop_increments_counter(self, db_path, caplog):
        """Event severity='routine' below toggle min_severity='priority'
        must increment severity_floor_dropped and NOT deliver."""
        cfg = _build_dispatcher_config(min_severity="priority")
        d = Dispatcher(cfg, _mk_channel_factory())
        assert d._severity_floor_dropped == 0

        ev = make_event(
            source="wfigs", category="wildfire_incident",
            severity="routine",
            title="Below floor test",
        )
        with caplog.at_level(logging.WARNING, logger="meshai.pipeline.dispatcher"):
            asyncio.run(d._dispatch_toggles(ev))

        assert d._severity_floor_dropped == 1, (
            f"counter not incremented; got {d._severity_floor_dropped}"
        )

    def test_floor_drop_emits_warn_log(self, db_path, caplog):
        """WARN log must fire when event is below the toggle min_severity floor."""
        cfg = _build_dispatcher_config(min_severity="priority")
        d = Dispatcher(cfg, _mk_channel_factory())

        ev = make_event(
            source="wfigs", category="wildfire_incident",
            severity="routine",
            title="Below floor warn test",
        )
        with caplog.at_level(logging.WARNING, logger="meshai.pipeline.dispatcher"):
            asyncio.run(d._dispatch_toggles(ev))

        warn_records = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "severity-floor drop" in r.message
        ]
        assert warn_records, (
            "expected at least one 'severity-floor drop' WARNING log; got none. "
            f"Records: {[r.message for r in caplog.records]}"
        )

    def test_floor_drop_persists_to_db(self, db_path):
        """Counter write-through: severity_floor_dropped must land in dispatcher_state."""
        cfg = _build_dispatcher_config(min_severity="priority")
        d = Dispatcher(cfg, _mk_channel_factory())

        ev = make_event(
            source="wfigs", category="wildfire_incident", severity="routine",
            title="DB persist test",
        )
        asyncio.run(d._dispatch_toggles(ev))
        assert d._severity_floor_dropped == 1

        conn = persistence_db.get_db(db_path)
        row = conn.execute(
            "SELECT severity_floor_dropped FROM dispatcher_state"
        ).fetchone()
        assert row is not None
        assert row["severity_floor_dropped"] == 1

    def test_above_floor_does_not_increment(self, db_path):
        """Event at or above the floor must NOT increment severity_floor_dropped."""
        cfg = _build_dispatcher_config(min_severity="routine")
        d = Dispatcher(cfg, _mk_channel_factory())

        ev = make_event(
            source="wfigs", category="wildfire_incident", severity="routine",
            title="At-floor pass-through",
        )
        asyncio.run(d._dispatch_toggles(ev))
        assert d._severity_floor_dropped == 0, (
            "floor drop counter should stay 0 when event meets the floor"
        )

    def test_floor_drop_counter_survives_restart(self, db_path):
        """Counter value restored on a fresh Dispatcher reading from same DB."""
        cfg = _build_dispatcher_config(min_severity="priority")
        d1 = Dispatcher(cfg, _mk_channel_factory())

        ev = make_event(
            source="wfigs", category="wildfire_incident", severity="routine",
            title="Restart test",
        )
        asyncio.run(d1._dispatch_toggles(ev))
        assert d1._severity_floor_dropped == 1

        # Fresh Dispatcher, same DB.
        d2 = Dispatcher(cfg, _mk_channel_factory())
        assert d2._severity_floor_dropped == 1, (
            f"counter not restored after restart; got {d2._severity_floor_dropped}"
        )

    def test_dispatch_stats_includes_floor_dropped(self, db_path):
        """dispatch_stats() exposes severity_floor_dropped."""
        cfg = _build_dispatcher_config(min_severity="priority")
        d = Dispatcher(cfg, _mk_channel_factory())

        ev = make_event(
            source="wfigs", category="wildfire_incident", severity="routine",
            title="Stats test",
        )
        asyncio.run(d._dispatch_toggles(ev))
        stats = d.dispatch_stats()
        assert "severity_floor_dropped" in stats
        assert stats["severity_floor_dropped"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# CHANGE 2 — Floor observability: matrix path
# ─────────────────────────────────────────────────────────────────────────────


class TestMatrixFloorObservability:
    """Matrix per-cell severity floor drop emits WARN and increments counter."""

    def _cfg_with_matrix(self, *, cell_min_severity="priority") -> Config:
        """Config with a fire toggle + enabled region_routes matrix.
        The matrix cell for US-ID has min_severity=cell_min_severity.
        The toggle-level min_severity is 'routine' so the toggle floor
        passes; only the matrix per-cell floor may drop the event.
        """
        from meshai.config import RegionRouteMatrix
        cfg = Config()
        cfg.notifications.cold_start_grace_seconds = 0
        tog = NotificationToggle(
            name="fire", enabled=True,
            min_severity="routine",          # toggle floor passes; matrix cell drops
            freshness_seconds=0,
            cooldown_seconds=0,
            severity_channels={
                "routine": ["mesh_broadcast"],
                "priority": ["mesh_broadcast"],
            },
            broadcast_channel=1,
        )
        cfg.notifications.toggles = {"fire": tog}
        # Matrix: enabled, one cell for fire/US-ID
        cfg.notifications.region_routes = RegionRouteMatrix(
            mt_enabled=True, mc_enabled=True,   # v0.16.x per-transport split
            cells={
                "fire": {
                    "US-ID": {
                        "enabled": True,
                        "min_severity": cell_min_severity,
                        "mt": "1",
                        "mc": "",
                    }
                }
            },
        )
        return cfg

    def test_matrix_floor_drop_increments_counter(self, db_path, caplog):
        """Matrix per-cell floor drop (routine below priority) must
        increment severity_floor_dropped."""
        cfg = self._cfg_with_matrix(cell_min_severity="priority")
        d = Dispatcher(cfg, _mk_channel_factory())

        ev = make_event(
            source="wfigs", category="wildfire_incident",
            severity="routine", region="US-ID",
            title="Matrix floor test",
        )
        with caplog.at_level(logging.WARNING, logger="meshai.pipeline.dispatcher"):
            asyncio.run(d._dispatch_toggles(ev))

        assert d._severity_floor_dropped >= 1, (
            f"matrix floor drop not counted; got {d._severity_floor_dropped}"
        )
        warn_records = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "severity-floor drop" in r.message
        ]
        assert warn_records, "expected 'severity-floor drop' WARNING from matrix path"
