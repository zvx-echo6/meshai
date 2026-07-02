"""Tests for the fire-digest broadcast kill-switch (fires.digest_broadcast_enabled).

The scheduler wiring stays intact; only the mesh EMISSION is gated. Disabled by
default -> fire_slot() dispatches nothing. Flipping the flag True re-enables it
cleanly. Per-fire wfigs alerts are a separate path and are unaffected (covered in
test_wfigs_handler.py).
"""
from __future__ import annotations

import asyncio
import time

from meshai.persistence import get_db
from meshai.notifications.scheduled.fire_digest import FireDigestScheduler


class _RecordingDispatcher:
    def __init__(self):
        self.calls = []

    async def dispatch_scheduled_broadcast(self, *, text, source_event_table,
                                           source_event_pk):
        self.calls.append(
            {"text": text, "table": source_event_table, "pk": source_event_pk})
        return True


def _seed_active_fire(conn):
    now = int(time.time())
    conn.execute(
        "INSERT OR REPLACE INTO fires(irwin_id, incident_name, incident_type, "
        "current_acres, current_contained_pct, lat, lon, county, state, "
        "declared_at, last_event_at, tombstoned_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("GATE-01", "Gatekeeper Fire", "WF", 900, None, 43.6, -116.2,
         "Ada", "ID", now, now - 600, None),
    )


def test_fire_slot_disabled_by_default_dispatches_nothing():
    """With defaults (digest_broadcast_enabled=False) fire_slot emits nothing."""
    conn = get_db()
    _seed_active_fire(conn)  # a broadcast WOULD be produced if the gate were open
    now = int(time.time())
    dispatcher = _RecordingDispatcher()
    sched = FireDigestScheduler(dispatcher, clock=lambda: now)

    result = asyncio.run(sched.fire_slot(now, "06:00"))

    assert result is False
    assert dispatcher.calls == [], "digest broadcast must NOT be dispatched by default"


def test_fire_slot_dispatches_when_flag_enabled():
    """Flipping fires.digest_broadcast_enabled True cleanly re-enables emission."""
    from meshai.adapter_config import set_runtime_override
    conn = get_db()
    _seed_active_fire(conn)
    now = int(time.time())
    dispatcher = _RecordingDispatcher()
    sched = FireDigestScheduler(dispatcher, clock=lambda: now)

    set_runtime_override("fires", "digest_broadcast_enabled", True)
    try:
        result = asyncio.run(sched.fire_slot(now, "06:00"))
    finally:
        # Reset so the override doesn't leak into other tests in this process.
        set_runtime_override("fires", "digest_broadcast_enabled", False)

    assert result is True
    assert len(dispatcher.calls) == 1
    assert dispatcher.calls[0]["table"] == "fire_digest_broadcasts"
    assert "Gatekeeper Fire" in dispatcher.calls[0]["text"]
