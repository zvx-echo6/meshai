"""CustomAnnouncementScheduler -- user-crafted scheduled announcements.

The owner's words: "at xx:xx time every day / other day / specific day /
week / month, send the following message: 'literally anything I type in
this box in the GUI'". Free text only -- NO placeholders, NO data sources,
NO templating.

Modelled on ReminderScheduler's clock-slot tick loop
(notifications/reminders/__init__.py) -- same 60s tick, same timezone
localisation via `zoneinfo`, same `spacing_seconds` roll-call pacing between
consecutive dispatches -- but reading from the `custom_announcements` table
(persistence/migrations/v30.sql) instead of adapter_config, and dispatching
via ``Dispatcher.dispatch_scheduled_custom_broadcast()`` (the announcement's
OWN explicit channel list) instead of a toggle/region_routes path.

Recurrence kinds (schedule_kind column)
----------------------------------------
    daily         -- fires every day at time_of_day.
    interval_days -- fires every N days, counted from the LOCAL CALENDAR
                     DATE (in the announcement's own timezone) of
                     created_at. created_at's date is day 0; day N, 2N, 3N...
                     are eligible. Documented anchor choice: created_at
                     rather than "now" so the cadence is stable and doesn't
                     drift if the scheduler is down for a while -- an
                     announcement created on day 0 with interval_days=2
                     always lands on days 0, 2, 4, ... relative to its own
                     creation date, never renumbered by a restart.
    weekly        -- fires on days where dow_mask[local_weekday] is True.
                     dow_mask is Mon-first (index 0 = Monday .. 6 = Sunday),
                     matching Python's datetime.weekday().
    monthly       -- fires on day_of_month, CLAMPED to the last day of the
                     local month (calendar.monthrange) so day_of_month=31
                     fires on Feb 28 (or 29 in a leap year), Apr 30, etc.

Dedup (restart-safe, no double-send on a slot)
-----------------------------------------------
Every schedule_kind fires AT MOST ONCE per local calendar day (the
recurrence math above decides WHICH days are eligible; time_of_day is the
only slot on an eligible day). That makes "the concrete local datetime of
the slot" reducible to just the local CALENDAR DATE in the announcement's
own timezone -- there is never a second slot to disambiguate within a day.

The dedup key is therefore (announcement_id, local_date_str). It is checked
by comparing the LOCAL DATE of the stored `last_sent_at` (converted into the
announcement's timezone) against the local date of the slot under
consideration: if they match, the slot has already fired and is skipped.
`last_sent_at` is written through to SQLite immediately after a successful
send (before moving to the next row), so a restart mid-minute re-reads the
same persisted date on its next tick and will not re-fire a slot that
already went out -- there is no in-memory-only state a crash can lose.

(The remaining crash window -- a process dying in between a successful
`dispatch_scheduled_custom_broadcast()` call returning True and the
`UPDATE ... SET last_sent_at` write landing -- mirrors the exact same
after-dispatch-stamp pattern already used by ReminderScheduler and the
other scheduled dispatchers in this codebase; closing it fully would need a
durable pre-commit intent log, which nothing else here has either.)

Pacing
------
Reuses ReminderScheduler's `_space()` mechanism verbatim in spirit: a
shared `_last_dispatch_at` timestamp and a minimum gap between consecutive
SUCCESSFUL announcement dispatches, so N announcements eligible in the same
tick go out spaced apart rather than bursting. This is roll-call-level
pacing (announcement vs. announcement). Fan-out to the multiple channels
WITHIN one announcement is not separately throttled here -- each channel
send already goes through `RadioSendQueue`'s per-transport inter-packet
jitter at the connector layer (the same mechanism dispatch_scheduled_
fire_broadcast's multi-channel `plan` loop relies on), so a 5-target mixed
announcement is naturally paced without inventing a second mechanism.
"""
from __future__ import annotations

import asyncio
import calendar
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover (3.9+ only)
    ZoneInfo = None

from meshai.notifications.formatters._budget import budget_for, fit_to_budget

logger = logging.getLogger(__name__)


_TICK_SECONDS = 60.0

# Minimum gap between consecutive SUCCESSFUL announcement dispatches when
# more than one is eligible in the same tick. Matches the FirePacer /
# ReminderScheduler default so every scheduled-broadcast exit paces at the
# same rate.
_DEFAULT_SPACING_SECONDS = 60.0

_VALID_KINDS = frozenset({"daily", "interval_days", "weekly", "monthly"})


# ============================================================================
# Pure helpers -- no DB / dispatcher, easy to unit test directly.
# ============================================================================


def clamp_day_of_month(year: int, month: int, day: int) -> int:
    """Clamp `day` (1-31) to the last real day of `year`-`month`.

    31 -> 28 or 29 in February, 30 in Apr/Jun/Sep/Nov, unchanged elsewhere.
    """
    last = calendar.monthrange(year, month)[1]
    return min(max(1, int(day)), last)


def _localize(now: float, tz_name: str) -> datetime:
    dt_utc = datetime.fromtimestamp(now, tz=timezone.utc)
    if ZoneInfo is not None:
        try:
            return dt_utc.astimezone(ZoneInfo(tz_name))
        except Exception:
            logger.warning("custom_announcements: bad timezone %r; using UTC", tz_name)
    return dt_utc


def _local_date_str(epoch: float, tz_name: str) -> str:
    return _localize(epoch, tz_name).strftime("%Y-%m-%d")


def is_day_eligible(row: dict, local_dt: datetime) -> bool:
    """Does `local_dt`'s local calendar date match this announcement's
    recurrence pattern? (Ignores time-of-day -- caller checks that
    separately.) `row` is a mapping with the custom_announcements columns.
    """
    kind = row["schedule_kind"]
    if kind == "daily":
        return True

    if kind == "interval_days":
        interval = int(row["interval_days"] or 1)
        if interval <= 0:
            interval = 1
        tz_name = row["timezone"] or "America/Boise"
        created_at = row["created_at"]
        if created_at is None:
            return False
        anchor_date = _localize(float(created_at), tz_name).date()
        delta_days = (local_dt.date() - anchor_date).days
        return delta_days >= 0 and delta_days % interval == 0

    if kind == "weekly":
        mask = row["dow_mask"]
        if isinstance(mask, str):
            try:
                mask = json.loads(mask)
            except Exception:
                mask = None
        if not isinstance(mask, list) or len(mask) != 7:
            logger.warning(
                "custom_announcements: bad dow_mask %r for weekly "
                "announcement; treating as never-fire", mask)
            return False
        return bool(mask[local_dt.weekday()])

    if kind == "monthly":
        dom = row["day_of_month"]
        if dom is None:
            return False
        clamped = clamp_day_of_month(local_dt.year, local_dt.month, int(dom))
        return local_dt.day == clamped

    logger.warning("custom_announcements: unknown schedule_kind=%r", kind)
    return False


# ============================================================================
# Scheduler
# ============================================================================


class CustomAnnouncementScheduler:
    """Fires enabled custom_announcements rows at their configured slots."""

    def __init__(self, dispatcher, *,
                  clock=None, sleep=None,
                  tick_seconds: float = _TICK_SECONDS,
                  spacing_seconds: float = _DEFAULT_SPACING_SECONDS):
        self._dispatcher = dispatcher
        self._clock = clock or time.time
        self._sleep = sleep or asyncio.sleep
        self._tick = tick_seconds
        self._spacing = spacing_seconds
        self._task: Optional[asyncio.Task] = None
        self._stop: Optional[asyncio.Event] = None
        # Shared across every announcement -- the mesh is one shared medium,
        # same design as ReminderScheduler._last_dispatch_at.
        self._last_dispatch_at: Optional[float] = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            raise RuntimeError("CustomAnnouncementScheduler already running")
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="custom-announcement-scheduler")
        logger.info("CustomAnnouncementScheduler started; tick=%ss", self._tick)

    async def stop(self) -> None:
        if self._stop: self._stop.set()
        if self._task:
            try: await self._task
            except Exception: pass

    async def _run(self) -> None:
        while not (self._stop and self._stop.is_set()):
            try:
                await self.tick_once()
            except Exception:
                logger.exception("CustomAnnouncementScheduler tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._tick)
                return
            except asyncio.TimeoutError:
                pass

    async def tick_once(self, now: Optional[float] = None) -> int:
        """One pass over every enabled announcement. Returns count fired.

        Public so tests can drive single ticks deterministically."""
        now = now if now is not None else self._clock()
        try:
            from meshai.persistence import get_db
            conn = get_db()
        except Exception:
            return 0

        rows = conn.execute(
            "SELECT * FROM custom_announcements WHERE enabled = 1"
        ).fetchall()
        if not rows:
            return 0

        fired = 0
        for row in rows:
            try:
                if await self._maybe_fire(dict(row), now):
                    fired += 1
            except Exception:
                logger.exception(
                    "custom_announcements: tick failed for id=%s",
                    row["announcement_id"])
        return fired

    # ---- per-row ----------------------------------------------------

    async def _maybe_fire(self, row: dict, now: float) -> bool:
        tz_name = row["timezone"] or "America/Boise"
        local_dt = _localize(now, tz_name)

        if not is_day_eligible(row, local_dt):
            return False

        hh_mm = row["time_of_day"] or ""
        try:
            hh, mm = hh_mm.split(":")
            slot_min = int(hh) * 60 + int(mm)
        except Exception:
            logger.warning(
                "custom_announcements: bad time_of_day=%r for id=%s",
                hh_mm, row["announcement_id"])
            return False

        current_min = local_dt.hour * 60 + local_dt.minute
        tick_min = max(1, int(self._tick / 60))
        # Has the slot just passed (within the last tick)? Mirrors
        # ReminderScheduler._tick_clock's window check.
        if not (current_min - tick_min <= slot_min <= current_min):
            return False

        # Dedup: already sent today (this local calendar date)?
        today_str = local_dt.strftime("%Y-%m-%d")
        last_sent_at = row.get("last_sent_at")
        if last_sent_at is not None:
            if _local_date_str(float(last_sent_at), tz_name) == today_str:
                return False

        channels = row["channels"]
        if isinstance(channels, str):
            try:
                channels = json.loads(channels)
            except Exception:
                channels = []
        if not channels:
            logger.warning(
                "custom_announcements: id=%s has no channels; skipping",
                row["announcement_id"])
            return False

        message = row["message"] or ""
        wire = fit_to_budget(message, budget_for("custom_announcements"))
        if not wire:
            return False

        if not await self._space():
            return False  # stop() signalled -- abandon the roll-call

        slot_key = f"{today_str}T{hh_mm}"
        ok = False
        try:
            ok = bool(await self._dispatcher.dispatch_scheduled_custom_broadcast(
                text=wire,
                announcement_id=row["announcement_id"],
                slot_key=slot_key,
                channels=channels,
            ))
        except Exception:
            logger.exception(
                "custom_announcements: dispatch failed id=%s slot=%s",
                row["announcement_id"], slot_key)

        if ok:
            self._stamp_sent(row["announcement_id"], now)
            self._last_dispatch_at = self._clock()
        return ok

    async def _space(self) -> bool:
        """Wait out the inter-announcement pacing gap. Returns False if
        stop() fired while waiting (caller abandons the roll-call). Mirrors
        ReminderScheduler._space() exactly."""
        if self._spacing <= 0 or self._last_dispatch_at is None:
            return True
        remaining = self._spacing - (self._clock() - self._last_dispatch_at)
        if remaining <= 0:
            return True
        if self._stop is not None:
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=remaining)
                return False
            except asyncio.TimeoutError:
                return True
        await self._sleep(remaining)
        return True

    def _stamp_sent(self, announcement_id: int, now: float) -> None:
        try:
            from meshai.persistence import get_db
            conn = get_db()
        except Exception:
            return
        conn.execute(
            "UPDATE custom_announcements SET last_sent_at=?, updated_at=? "
            "WHERE announcement_id=?",
            (now, now, announcement_id),
        )
