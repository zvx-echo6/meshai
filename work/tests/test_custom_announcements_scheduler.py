"""CustomAnnouncementScheduler -- recurrence math, dedup, pacing.

Mirrors the shape of tests/test_fire_reminder_pacing.py and
tests/test_wzdx_summary_region_routing.py: a fake injected clock/sleep so
the suite pays no wall-clock cost, and a mock dispatcher whose
dispatch_scheduled_custom_broadcast is asserted on directly.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from meshai.notifications.scheduled.custom_announcements import (
    CustomAnnouncementScheduler,
    clamp_day_of_month,
    is_day_eligible,
)
from meshai.persistence import get_db


# ---------- helpers --------------------------------------------------------


_TZ = "America/Boise"  # UTC-7 (MST, no DST edge in our fixed test dates)


def _epoch_for_local(y, m, d, hh, mm, tz_name=_TZ) -> float:
    from zoneinfo import ZoneInfo
    dt = datetime(y, m, d, hh, mm, tzinfo=ZoneInfo(tz_name))
    return dt.timestamp()


def _insert_announcement(
    conn, *, name="Test", message="hello mesh", schedule_kind="daily",
    time_of_day="08:00", interval_days=None, dow_mask=None,
    day_of_month=None, tz_name=_TZ, channels=None, enabled=1,
    created_at=None, last_sent_at=None,
) -> int:
    now = created_at if created_at is not None else time.time()
    if channels is None:
        channels = [{"transport": "meshtastic", "channel": 2}]
    cur = conn.execute(
        "INSERT INTO custom_announcements "
        "(name, message, schedule_kind, time_of_day, interval_days, "
        "dow_mask, day_of_month, timezone, channels, enabled, "
        "last_sent_at, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (name, message, schedule_kind, time_of_day, interval_days,
         json.dumps(dow_mask) if dow_mask is not None else None,
         day_of_month, tz_name, json.dumps(channels), enabled,
         last_sent_at, now, now),
    )
    return cur.lastrowid


@pytest.fixture
def mock_dispatcher():
    d = MagicMock()
    d.dispatch_scheduled_custom_broadcast = AsyncMock(return_value=True)
    return d


def _sched(dispatcher, *, clock=None, sleep=None, spacing_seconds=60.0):
    return CustomAnnouncementScheduler(
        dispatcher, clock=clock, sleep=sleep, spacing_seconds=spacing_seconds,
    )


# ============================================================================
# clamp_day_of_month
# ============================================================================


def test_clamp_day_of_month_february_non_leap():
    assert clamp_day_of_month(2026, 2, 31) == 28


def test_clamp_day_of_month_february_leap():
    assert clamp_day_of_month(2024, 2, 31) == 29


def test_clamp_day_of_month_thirty_day_month():
    assert clamp_day_of_month(2026, 4, 31) == 30


def test_clamp_day_of_month_unaffected_when_in_range():
    assert clamp_day_of_month(2026, 1, 15) == 15


# ============================================================================
# Recurrence kinds -- each fires on the right day
# ============================================================================


def test_daily_fires_every_day(mock_dispatcher):
    conn = get_db()
    now = _epoch_for_local(2026, 8, 17, 8, 0)  # Monday
    _insert_announcement(conn, schedule_kind="daily", time_of_day="08:00")

    fired = asyncio.run(_sched(mock_dispatcher, clock=lambda: now).tick_once())
    assert fired == 1


def test_interval_days_fires_on_multiple_of_interval_from_creation():
    """interval_days=2, created on day 0 -> fires day 0, 2, 4... not day 1, 3."""
    conn = get_db()
    anchor = _epoch_for_local(2026, 8, 10, 8, 0)  # Monday, day 0
    row_id = _insert_announcement(
        conn, schedule_kind="interval_days", interval_days=2,
        time_of_day="08:00", created_at=anchor,
    )
    row = dict(conn.execute(
        "SELECT * FROM custom_announcements WHERE announcement_id=?", (row_id,)
    ).fetchone())

    from zoneinfo import ZoneInfo
    day0 = datetime.fromtimestamp(anchor, tz=timezone.utc).astimezone(ZoneInfo(_TZ))
    day1 = day0.replace(day=day0.day + 1)
    day2 = day0.replace(day=day0.day + 2)

    assert is_day_eligible(row, day0) is True
    assert is_day_eligible(row, day1) is False
    assert is_day_eligible(row, day2) is True


def test_weekly_fires_only_on_masked_days():
    """dow_mask Mon-first: only Wed (index 2) true."""
    conn = get_db()
    mask = [False, False, True, False, False, False, False]
    row_id = _insert_announcement(
        conn, schedule_kind="weekly", dow_mask=mask, time_of_day="08:00",
    )
    row = dict(conn.execute(
        "SELECT * FROM custom_announcements WHERE announcement_id=?", (row_id,)
    ).fetchone())

    from zoneinfo import ZoneInfo
    tue = datetime(2026, 8, 18, 8, 0, tzinfo=ZoneInfo(_TZ))  # Tuesday
    wed = datetime(2026, 8, 19, 8, 0, tzinfo=ZoneInfo(_TZ))  # Wednesday

    assert is_day_eligible(row, tue) is False
    assert is_day_eligible(row, wed) is True


def test_monthly_fires_on_day_of_month():
    conn = get_db()
    row_id = _insert_announcement(
        conn, schedule_kind="monthly", day_of_month=15, time_of_day="08:00",
    )
    row = dict(conn.execute(
        "SELECT * FROM custom_announcements WHERE announcement_id=?", (row_id,)
    ).fetchone())

    from zoneinfo import ZoneInfo
    the_14th = datetime(2026, 8, 14, 8, 0, tzinfo=ZoneInfo(_TZ))
    the_15th = datetime(2026, 8, 15, 8, 0, tzinfo=ZoneInfo(_TZ))

    assert is_day_eligible(row, the_14th) is False
    assert is_day_eligible(row, the_15th) is True


def test_monthly_day_31_clamps_in_february_non_leap():
    """The headline requirement: day_of_month=31 fires on Feb 28 in a non-leap year."""
    conn = get_db()
    row_id = _insert_announcement(
        conn, schedule_kind="monthly", day_of_month=31, time_of_day="09:00",
    )
    row = dict(conn.execute(
        "SELECT * FROM custom_announcements WHERE announcement_id=?", (row_id,)
    ).fetchone())

    from zoneinfo import ZoneInfo
    feb28_2026 = datetime(2026, 2, 28, 9, 0, tzinfo=ZoneInfo(_TZ))  # 2026 is not leap
    feb27_2026 = datetime(2026, 2, 27, 9, 0, tzinfo=ZoneInfo(_TZ))

    assert is_day_eligible(row, feb28_2026) is True
    assert is_day_eligible(row, feb27_2026) is False


def test_monthly_day_31_clamps_in_february_leap_year():
    conn = get_db()
    row_id = _insert_announcement(
        conn, schedule_kind="monthly", day_of_month=31, time_of_day="09:00",
    )
    row = dict(conn.execute(
        "SELECT * FROM custom_announcements WHERE announcement_id=?", (row_id,)
    ).fetchone())

    from zoneinfo import ZoneInfo
    feb29_2024 = datetime(2024, 2, 29, 9, 0, tzinfo=ZoneInfo(_TZ))  # 2024 IS leap
    assert is_day_eligible(row, feb29_2024) is True


def test_monthly_day_31_fires_via_full_tick_in_february():
    """End-to-end: tick_once actually dispatches on Feb 28 for day_of_month=31."""
    conn = get_db()
    now = _epoch_for_local(2026, 2, 28, 9, 0)
    _insert_announcement(
        conn, schedule_kind="monthly", day_of_month=31, time_of_day="09:00",
    )
    d = MagicMock()
    d.dispatch_scheduled_custom_broadcast = AsyncMock(return_value=True)

    fired = asyncio.run(_sched(d, clock=lambda: now).tick_once())
    assert fired == 1
    d.dispatch_scheduled_custom_broadcast.assert_called_once()


# ============================================================================
# Disabled announcements never fire
# ============================================================================


def test_disabled_announcement_never_fires(mock_dispatcher):
    conn = get_db()
    now = _epoch_for_local(2026, 8, 17, 8, 0)
    _insert_announcement(conn, schedule_kind="daily", time_of_day="08:00", enabled=0)

    fired = asyncio.run(_sched(mock_dispatcher, clock=lambda: now).tick_once())
    assert fired == 0
    mock_dispatcher.dispatch_scheduled_custom_broadcast.assert_not_called()


# ============================================================================
# Dedup: never send the same slot twice, restart-safe
# ============================================================================


def test_dedup_prevents_double_send_in_same_slot(mock_dispatcher):
    """Two ticks within the same minute window must not double-fire."""
    conn = get_db()
    now = _epoch_for_local(2026, 8, 17, 8, 0)
    _insert_announcement(conn, schedule_kind="daily", time_of_day="08:00")

    sched = _sched(mock_dispatcher, clock=lambda: now)
    fired1 = asyncio.run(sched.tick_once())
    fired2 = asyncio.run(sched.tick_once())

    assert fired1 == 1
    assert fired2 == 0
    assert mock_dispatcher.dispatch_scheduled_custom_broadcast.call_count == 1


def test_dedup_survives_a_simulated_restart():
    """A brand-new scheduler instance (simulating a process restart) reads
    the persisted last_sent_at and still refuses to double-send the slot
    that already went out."""
    conn = get_db()
    now = _epoch_for_local(2026, 8, 17, 8, 0)
    _insert_announcement(conn, schedule_kind="daily", time_of_day="08:00")

    d1 = MagicMock()
    d1.dispatch_scheduled_custom_broadcast = AsyncMock(return_value=True)
    fired1 = asyncio.run(_sched(d1, clock=lambda: now).tick_once())
    assert fired1 == 1

    # Simulate restart: a fresh scheduler instance, same DB, ticking again
    # a few seconds later within the same slot window.
    d2 = MagicMock()
    d2.dispatch_scheduled_custom_broadcast = AsyncMock(return_value=True)
    fired2 = asyncio.run(_sched(d2, clock=lambda: now + 5).tick_once())
    assert fired2 == 0
    d2.dispatch_scheduled_custom_broadcast.assert_not_called()


def test_dedup_allows_the_next_days_slot(mock_dispatcher):
    conn = get_db()
    day1 = _epoch_for_local(2026, 8, 17, 8, 0)
    day2 = _epoch_for_local(2026, 8, 18, 8, 0)
    _insert_announcement(conn, schedule_kind="daily", time_of_day="08:00")

    fired1 = asyncio.run(_sched(mock_dispatcher, clock=lambda: day1).tick_once())
    fired2 = asyncio.run(_sched(mock_dispatcher, clock=lambda: day2).tick_once())

    assert fired1 == 1
    assert fired2 == 1


# ============================================================================
# Message truncation at the budget
# ============================================================================


def test_message_is_truncated_to_budget(mock_dispatcher):
    conn = get_db()
    now = _epoch_for_local(2026, 8, 17, 8, 0)
    long_message = "x" * 500
    _insert_announcement(
        conn, schedule_kind="daily", time_of_day="08:00", message=long_message,
    )

    asyncio.run(_sched(mock_dispatcher, clock=lambda: now).tick_once())

    call = mock_dispatcher.dispatch_scheduled_custom_broadcast.call_args
    sent_text = call.kwargs["text"]
    assert len(sent_text) <= 140
    assert sent_text != long_message


# ============================================================================
# Pacing between multiple announcements firing in the same tick
# ============================================================================


class _FakeTime:
    def __init__(self, start):
        self.now = start
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_multiple_announcements_in_one_tick_are_spaced_not_burst():
    conn = get_db()
    ft = _FakeTime(_epoch_for_local(2026, 8, 17, 8, 0))
    for i in range(3):
        _insert_announcement(
            conn, name=f"A{i}", schedule_kind="daily", time_of_day="08:00",
        )
    sent_at: list[float] = []
    d = MagicMock()
    d.dispatch_scheduled_custom_broadcast = AsyncMock(
        side_effect=lambda **kw: sent_at.append(ft.now) or True
    )

    fired = asyncio.run(
        _sched(d, clock=ft.clock, sleep=ft.sleep, spacing_seconds=60.0).tick_once()
    )

    assert fired == 3
    gaps = [b - a for a, b in zip(sent_at, sent_at[1:])]
    assert gaps == [60.0, 60.0]


def test_single_announcement_fire_is_not_delayed():
    conn = get_db()
    ft = _FakeTime(_epoch_for_local(2026, 8, 17, 8, 0))
    _insert_announcement(conn, schedule_kind="daily", time_of_day="08:00")
    d = MagicMock()
    d.dispatch_scheduled_custom_broadcast = AsyncMock(return_value=True)

    fired = asyncio.run(
        _sched(d, clock=ft.clock, sleep=ft.sleep, spacing_seconds=60.0).tick_once()
    )

    assert fired == 1
    assert ft.sleeps == []
