"""Reminder-path output spacing.

The ReminderScheduler is the THIRD fire-broadcast exit, and the only one that
does not pass through the EventBus -- so FirePacer (which paces the Central
and native fire-event exits to <=1/60s) never sees it. Its tick is a ROLL-CALL:
every eligible row is its own broadcast, dispatched in a plain `for` loop.
Unpaced, N eligible fires => N back-to-back mesh transmissions.

These tests pin the spacing behavior:
  * N eligible fires do NOT emit back-to-back (spacing waited between sends),
  * the gap equals the configured spacing_seconds,
  * a LONE eligible fire is not delayed at all (nothing to pace against),
  * spacing_seconds=0 restores the old unpaced behavior (kill switch),
  * rows filtered out by terminate_when do not consume a spacing slot,
  * the `ok`-gated count + last_broadcast_at stamp semantics are UNCHANGED,
  * a failed dispatch does not arm the spacing gap (no packet went out).

Time is faked end-to-end (injected clock + sleep), so the suite pays no
wall-clock cost for a 60s default spacing.
"""
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
    if last_event_at is None:
        last_event_at = int(time.time())
    conn.execute(
        "INSERT OR REPLACE INTO fires(irwin_id, incident_name, incident_type, "
        "current_acres, current_contained_pct, lat, lon, county, state, "
        "declared_at, last_event_at, first_broadcast_at, last_broadcast_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (irwin_id, name, "WF", 500, current_contained_pct,
         42.5, -114.5, "Cassia", "ID",
         last_broadcast_at, last_event_at, last_broadcast_at, last_broadcast_at),
    )


def _enable_wfigs_reminders(spacing_seconds=None):
    """Enable wfigs reminders; optionally override spacing_seconds."""
    conn = get_db()
    for col in ("default_json", "value_json"):
        conn.execute(
            f"UPDATE adapter_config SET {col}='true' "
            "WHERE adapter='reminders_wfigs' AND key='enabled'"
        )
    if spacing_seconds is not None:
        for col in ("default_json", "value_json"):
            conn.execute(
                f"UPDATE adapter_config SET {col}=? "
                "WHERE adapter='reminders_wfigs' AND key='spacing_seconds'",
                (str(int(spacing_seconds)),),
            )
    from meshai.adapter_config import adapter_config as _ac
    _ac.invalidate()


class _FakeTime:
    """Injected clock + sleep. `sleep()` advances the clock instead of waiting,
    and records every gap it was asked to wait, so a test can assert on the
    exact spacing without any wall-clock cost."""

    def __init__(self, start=1_780_000_000.0):
        self.now = start
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def mock_dispatcher():
    d = MagicMock()
    d.dispatch_scheduled_broadcast = AsyncMock(return_value=True)
    d.dispatch_scheduled_fire_broadcast = AsyncMock(return_value=True)
    return d


def _sched(dispatcher, ft: _FakeTime) -> ReminderScheduler:
    return ReminderScheduler(dispatcher, clock=ft.clock, sleep=ft.sleep)


# ============================================================================
# The burst: N eligible fires must NOT go out back-to-back
# ============================================================================


def test_roll_call_of_n_fires_is_not_back_to_back(mock_dispatcher):
    """5 eligible fires => 5 sends, each separated by the configured spacing.

    This is THE regression guard: unpaced, this loop fired all 5 with zero
    gaps. Asserting on the send timestamps (not just the sleep calls) proves
    the gap is real from the dispatcher's point of view.
    """
    ft = _FakeTime()
    conn = get_db()
    _enable_wfigs_reminders(spacing_seconds=60)
    for i in range(5):
        _seed_fire(conn, irwin_id=f"BURST{i}",
                   last_broadcast_at=ft.now - 9 * 3600,
                   last_event_at=int(ft.now))

    sent_at: list[float] = []
    mock_dispatcher.dispatch_scheduled_fire_broadcast = AsyncMock(
        side_effect=lambda **kw: sent_at.append(ft.now) or True
    )

    fired = asyncio.run(_sched(mock_dispatcher, ft).tick_once())

    assert fired == 5, "all 5 eligible fires must still broadcast -- none dropped"
    assert len(sent_at) == 5
    gaps = [b - a for a, b in zip(sent_at, sent_at[1:])]
    assert gaps == [60.0, 60.0, 60.0, 60.0], (
        f"expected a 60s gap between every consecutive send, got {gaps}"
    )
    assert ft.sleeps == [60.0] * 4, (
        "spacing must be waited between sends (N-1 waits for N sends), "
        f"got {ft.sleeps}"
    )


def test_spacing_seconds_is_honored_from_config(mock_dispatcher):
    """The gap tracks adapter_config, so it is tunable without a deploy."""
    ft = _FakeTime()
    conn = get_db()
    _enable_wfigs_reminders(spacing_seconds=15)
    for i in range(3):
        _seed_fire(conn, irwin_id=f"CFG{i}",
                   last_broadcast_at=ft.now - 9 * 3600,
                   last_event_at=int(ft.now))

    fired = asyncio.run(_sched(mock_dispatcher, ft).tick_once())

    assert fired == 3
    assert ft.sleeps == [15.0, 15.0]


def test_spacing_zero_disables_pacing(mock_dispatcher):
    """spacing_seconds=0 is the documented kill switch -> no waits at all."""
    ft = _FakeTime()
    conn = get_db()
    _enable_wfigs_reminders(spacing_seconds=0)
    for i in range(4):
        _seed_fire(conn, irwin_id=f"OFF{i}",
                   last_broadcast_at=ft.now - 9 * 3600,
                   last_event_at=int(ft.now))

    fired = asyncio.run(_sched(mock_dispatcher, ft).tick_once())

    assert fired == 4
    assert ft.sleeps == []


# ============================================================================
# No added latency when there is nothing to pace against
# ============================================================================


def test_single_eligible_fire_is_not_delayed(mock_dispatcher):
    """A lone fire has nothing to pace against -> ZERO added latency.

    Spacing must never become a fixed startup tax on the first broadcast.
    """
    ft = _FakeTime()
    conn = get_db()
    _enable_wfigs_reminders(spacing_seconds=60)
    _seed_fire(conn, irwin_id="LONE", last_broadcast_at=ft.now - 9 * 3600,
               last_event_at=int(ft.now))

    t_before = ft.now
    fired = asyncio.run(_sched(mock_dispatcher, ft).tick_once())

    assert fired == 1
    assert ft.sleeps == [], "a lone reminder must not wait for anything"
    assert ft.now == t_before, "no simulated time may pass before a lone send"
    mock_dispatcher.dispatch_scheduled_fire_broadcast.assert_called_once()


def test_elapsed_gap_is_credited_not_re_waited(mock_dispatcher):
    """If the spacing gap already elapsed on its own, do not wait again.

    Spacing is measured from the last delivery, so a fire arriving well after
    the previous one goes out immediately.
    """
    ft = _FakeTime()
    conn = get_db()
    _enable_wfigs_reminders(spacing_seconds=60)
    _seed_fire(conn, irwin_id="ELAPSED", last_broadcast_at=ft.now - 9 * 3600,
               last_event_at=int(ft.now))

    sch = _sched(mock_dispatcher, ft)
    # Pretend we delivered something 10 minutes ago -- far past the 60s gap.
    sch._last_dispatch_at = ft.now - 600

    fired = asyncio.run(sch.tick_once())

    assert fired == 1
    assert ft.sleeps == [], "an already-elapsed gap must not be re-waited"


# ============================================================================
# Spacing must not change WHAT is broadcast -- only its timing
# ============================================================================


def test_terminated_rows_do_not_consume_a_spacing_slot(mock_dispatcher):
    """Rows killed by terminate_when never transmit, so they must not add gaps.

    Seeds 2 sendable fires around 2 that terminate (tombstoned / 100% contained).
    Correct behavior: 2 sends, exactly 1 gap between them.
    """
    ft = _FakeTime()
    conn = get_db()
    _enable_wfigs_reminders(spacing_seconds=60)
    old = ft.now - 9 * 3600
    _seed_fire(conn, irwin_id="LIVE1", last_broadcast_at=old,
               last_event_at=int(ft.now))
    _seed_fire(conn, irwin_id="CONTAINED", last_broadcast_at=old,
               current_contained_pct=100, last_event_at=int(ft.now))
    _seed_fire(conn, irwin_id="LIVE2", last_broadcast_at=old,
               last_event_at=int(ft.now))
    _seed_fire(conn, irwin_id="TOMB", last_broadcast_at=old,
               last_event_at=int(ft.now))
    conn.execute("UPDATE fires SET tombstoned_at=? WHERE irwin_id='TOMB'",
                 (ft.now,))

    fired = asyncio.run(_sched(mock_dispatcher, ft).tick_once())

    assert fired == 2, "only the 2 non-terminated fires may broadcast"
    assert ft.sleeps == [60.0], (
        "2 sends => exactly 1 gap; a skipped row must not burn a spacing slot"
    )
    pks = {c.kwargs["source_event_pk"]
           for c in mock_dispatcher.dispatch_scheduled_fire_broadcast.call_args_list}
    assert pks == {"LIVE1", "LIVE2"}


def test_stamp_and_count_semantics_unchanged_on_success(mock_dispatcher):
    """The post-success last_broadcast_at stamp still lands, still uses the
    tick's `now` (NOT the post-spacing send time) -- unchanged semantics."""
    ft = _FakeTime()
    conn = get_db()
    _enable_wfigs_reminders(spacing_seconds=60)
    for i in range(2):
        _seed_fire(conn, irwin_id=f"STAMP{i}",
                   last_broadcast_at=ft.now - 9 * 3600,
                   last_event_at=int(ft.now))
    tick_now = ft.now

    fired = asyncio.run(_sched(mock_dispatcher, ft).tick_once())

    assert fired == 2
    for i in range(2):
        row = conn.execute(
            "SELECT last_broadcast_at FROM fires WHERE irwin_id=?",
            (f"STAMP{i}",),
        ).fetchone()
        assert row["last_broadcast_at"] == tick_now, (
            "stamp must still use the tick's `now`, not the delayed send time"
        )


def test_failed_dispatch_does_not_stamp_or_arm_the_gap(mock_dispatcher):
    """A dispatch returning False sent no packet: it must not stamp
    last_broadcast_at, must not count, and must not open a spacing gap."""
    ft = _FakeTime()
    conn = get_db()
    _enable_wfigs_reminders(spacing_seconds=60)
    _seed_fire(conn, irwin_id="FAIL1", last_broadcast_at=ft.now - 9 * 3600,
               last_event_at=int(ft.now))
    mock_dispatcher.dispatch_scheduled_fire_broadcast = AsyncMock(
        return_value=False
    )

    fired = asyncio.run(_sched(mock_dispatcher, ft).tick_once())

    assert fired == 0
    assert ft.sleeps == [], "a send that never landed must not arm the gap"
    row = conn.execute(
        "SELECT last_broadcast_at FROM fires WHERE irwin_id='FAIL1'"
    ).fetchone()
    assert row["last_broadcast_at"] == ft.now - 9 * 3600, (
        "a failed dispatch must not stamp last_broadcast_at"
    )


def test_nothing_is_dropped_in_a_large_roll_call(mock_dispatcher):
    """Spacing spreads a roll-call out; it never drops a fire."""
    ft = _FakeTime()
    conn = get_db()
    _enable_wfigs_reminders(spacing_seconds=60)
    n = 15
    for i in range(n):
        _seed_fire(conn, irwin_id=f"MANY{i:02d}",
                   last_broadcast_at=ft.now - 9 * 3600,
                   last_event_at=int(ft.now))

    fired = asyncio.run(_sched(mock_dispatcher, ft).tick_once())

    assert fired == n
    assert mock_dispatcher.dispatch_scheduled_fire_broadcast.call_count == n
    assert ft.sleeps == [60.0] * (n - 1)
    pks = {c.kwargs["source_event_pk"]
           for c in mock_dispatcher.dispatch_scheduled_fire_broadcast.call_args_list}
    assert pks == {f"MANY{i:02d}" for i in range(n)}


# ============================================================================
# Shutdown must not be held hostage by a long roll-call
# ============================================================================


def test_stop_interrupts_the_spacing_wait(mock_dispatcher):
    """A 15-fire roll-call at 60s spacing holds tick_once() for ~14 minutes,
    and stop() awaits the tick task -- so the spacing wait MUST abort when the
    stop event is set, or shutdown hangs.
    """
    conn = get_db()
    _enable_wfigs_reminders(spacing_seconds=60)
    now = 1_780_000_000
    for i in range(5):
        _seed_fire(conn, irwin_id=f"STOP{i}", last_broadcast_at=now - 9 * 3600,
                   last_event_at=now)

    async def _run() -> int:
        sch = ReminderScheduler(mock_dispatcher, clock=lambda: now)
        # Simulate the running state start() sets up, then ask it to stop.
        # The spacing wait races the stop event, so it must return promptly
        # rather than sleeping out the full 60s gap.
        sch._stop = asyncio.Event()
        sch._stop.set()
        return await asyncio.wait_for(sch.tick_once(), timeout=5.0)

    fired = asyncio.run(_run())

    # The first fire has nothing to pace against and goes out; the roll-call is
    # then abandoned at the first spacing wait because stop was signalled.
    assert fired == 1
