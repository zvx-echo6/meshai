"""v0.6-phase3 ReminderScheduler tests."""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from meshai.notifications.reminders import ReminderScheduler
from meshai.persistence import get_db


# ---------- helpers --------------------------------------------------------


def _seed_fire(conn, *, irwin_id, last_broadcast_at, current_contained_pct=10,
                last_event_at=None, name="Test Fire"):
    if last_event_at is None: last_event_at = int(time.time())
    conn.execute(
        "INSERT OR REPLACE INTO fires(irwin_id, incident_name, incident_type, "
        "current_acres, current_contained_pct, lat, lon, county, state, "
        "declared_at, last_event_at, first_broadcast_at, last_broadcast_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (irwin_id, name, "WF", 500, current_contained_pct,
         42.5, -114.5, "Cassia", "ID",
         last_broadcast_at, last_event_at, last_broadcast_at, last_broadcast_at),
    )


def _seed_swpc(conn, *, event_id, last_broadcast_at, event_type="swpc_kindex"):
    conn.execute(
        "INSERT OR REPLACE INTO swpc_events(event_id, event_type, severity_int, "
        "payload_json, occurred_at, first_seen_at, first_broadcast_at, "
        "last_broadcast_at) VALUES (?,?,?,?,?,?,?,?)",
        (event_id, event_type, 7, "{}", last_broadcast_at, last_broadcast_at,
         last_broadcast_at, last_broadcast_at),
    )


def _seed_work_zone(conn, *, external_id, last_broadcast_at, end_at=None,
                      sub_type="road_works"):
    conn.execute(
        "INSERT OR REPLACE INTO traffic_events(source, external_id, road, "
        "direction, county, state, sub_type, impact, first_seen_at, "
        "last_seen_at, first_broadcast_at, last_broadcast_at, end_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("itd_511", external_id, "I-84", "E", "Ada", "ID", sub_type,
         None, last_broadcast_at, last_broadcast_at,
         last_broadcast_at, last_broadcast_at, end_at),
    )


@pytest.fixture
def mock_dispatcher():
    d = MagicMock()
    d.dispatch_scheduled_broadcast = AsyncMock(return_value=True)
    return d


# ============================================================================
# Interval cadence (wfigs)
# ============================================================================


def test_wfigs_reminder_fires_past_cadence(mock_dispatcher):
    """A fire whose last_broadcast_at is past the 8h cadence emits Active:."""
    now = 1_780_000_000
    conn = get_db()
    _seed_fire(conn, irwin_id="F1", last_broadcast_at=now - 9 * 3600)

    sch = ReminderScheduler(mock_dispatcher, clock=lambda: now)
    fired = asyncio.run(sch.tick_once())
    assert fired == 1
    mock_dispatcher.dispatch_scheduled_broadcast.assert_called_once()
    args = mock_dispatcher.dispatch_scheduled_broadcast.call_args.kwargs
    assert "Active" in args["text"]
    assert args["source_event_table"] == "fires"
    assert args["source_event_pk"] == "F1"


def test_wfigs_reminder_skipped_within_cadence(mock_dispatcher):
    """A fire broadcast 1h ago is within the 8h cadence -> no reminder."""
    now = 1_780_000_000
    conn = get_db()
    _seed_fire(conn, irwin_id="F1", last_broadcast_at=now - 3600)

    sch = ReminderScheduler(mock_dispatcher, clock=lambda: now)
    fired = asyncio.run(sch.tick_once())
    assert fired == 0
    mock_dispatcher.dispatch_scheduled_broadcast.assert_not_called()


def test_wfigs_reminder_skipped_when_containment_100(mock_dispatcher):
    """containment_100 termination overrides the cadence."""
    now = 1_780_000_000
    conn = get_db()
    _seed_fire(conn, irwin_id="F1", last_broadcast_at=now - 9 * 3600,
                current_contained_pct=100)

    sch = ReminderScheduler(mock_dispatcher, clock=lambda: now)
    fired = asyncio.run(sch.tick_once())
    assert fired == 0
    mock_dispatcher.dispatch_scheduled_broadcast.assert_not_called()


def test_wfigs_reminder_skipped_when_last_event_age_24h(mock_dispatcher):
    """If last_event_at > 24h ago, treat the fire as gone -> no reminder."""
    now = 1_780_000_000
    conn = get_db()
    _seed_fire(conn, irwin_id="F1", last_broadcast_at=now - 9 * 3600,
                last_event_at=now - 48 * 3600)

    sch = ReminderScheduler(mock_dispatcher, clock=lambda: now)
    fired = asyncio.run(sch.tick_once())
    assert fired == 0


def test_wfigs_reminder_stamps_last_broadcast_at(mock_dispatcher):
    now = 1_780_000_000
    conn = get_db()
    _seed_fire(conn, irwin_id="F1", last_broadcast_at=now - 9 * 3600)
    sch = ReminderScheduler(mock_dispatcher, clock=lambda: now)
    asyncio.run(sch.tick_once())
    row = conn.execute("SELECT last_broadcast_at, first_broadcast_at FROM fires WHERE irwin_id='F1'").fetchone()
    assert row["last_broadcast_at"] == now
    # first_broadcast_at preserved
    assert row["first_broadcast_at"] == now - 9 * 3600


def test_reminder_disabled_adapter_does_nothing(mock_dispatcher):
    """When adapter_meta.reminder_enabled=0 for wfigs, no reminders fire."""
    now = 1_780_000_000
    conn = get_db()
    _seed_fire(conn, irwin_id="F1", last_broadcast_at=now - 9 * 3600)
    conn.execute("UPDATE adapter_meta SET reminder_enabled=0 WHERE adapter='wfigs'")
    sch = ReminderScheduler(mock_dispatcher, clock=lambda: now)
    fired = asyncio.run(sch.tick_once())
    assert fired == 0


# ============================================================================
# SWPC interval cadence
# ============================================================================


def test_swpc_reminder_fires(mock_dispatcher):
    now = 1_780_000_000
    conn = get_db()
    _seed_swpc(conn, event_id="S1", last_broadcast_at=now - 10 * 3600)
    sch = ReminderScheduler(mock_dispatcher, clock=lambda: now)
    fired = asyncio.run(sch.tick_once())
    assert fired == 1
    args = mock_dispatcher.dispatch_scheduled_broadcast.call_args.kwargs
    assert "Active" in args["text"]
    assert args["source_event_table"] == "swpc_events"


# ============================================================================
# itd_511_work_zone clock cadence
# ============================================================================


def test_work_zone_reminder_fires_at_clock_slot(mock_dispatcher):
    """At the 08:00 Mountain slot, an active work zone broadcasts Active:."""
    # 08:00 America/Boise on a Monday. Compute UTC equivalent.
    from datetime import datetime
    from zoneinfo import ZoneInfo
    # Pick a Monday at exactly 08:00 Mountain.
    local = datetime(2026, 6, 8, 8, 0, 0, tzinfo=ZoneInfo("America/Boise"))
    now = int(local.timestamp())
    conn = get_db()
    _seed_work_zone(conn, external_id="WZ1", last_broadcast_at=now - 48 * 3600)
    sch = ReminderScheduler(mock_dispatcher, clock=lambda: now, tick_seconds=60)
    fired = asyncio.run(sch.tick_once())
    assert fired == 1
    args = mock_dispatcher.dispatch_scheduled_broadcast.call_args.kwargs
    assert "Active" in args["text"]
    assert args["source_event_table"] == "traffic_events"


def test_work_zone_reminder_skipped_when_end_date_passed(mock_dispatcher):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    local = datetime(2026, 6, 8, 8, 0, 0, tzinfo=ZoneInfo("America/Boise"))
    now = int(local.timestamp())
    conn = get_db()
    _seed_work_zone(conn, external_id="WZ1",
                      last_broadcast_at=now - 48 * 3600,
                      end_at=now - 3600)   # end_at in the past
    sch = ReminderScheduler(mock_dispatcher, clock=lambda: now, tick_seconds=60)
    fired = asyncio.run(sch.tick_once())
    assert fired == 0


def test_work_zone_reminder_skipped_outside_slot(mock_dispatcher):
    """At 14:00 Mountain (no slot), no reminder fires."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    local = datetime(2026, 6, 8, 14, 0, 0, tzinfo=ZoneInfo("America/Boise"))
    now = int(local.timestamp())
    conn = get_db()
    _seed_work_zone(conn, external_id="WZ1", last_broadcast_at=now - 48 * 3600)
    sch = ReminderScheduler(mock_dispatcher, clock=lambda: now, tick_seconds=60)
    fired = asyncio.run(sch.tick_once())
    assert fired == 0
