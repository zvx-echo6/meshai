"""v0.7-fire-tracker-4 fire-digest scheduled broadcaster.

Twice-daily (default 06:00 + 18:00 Mountain) deterministic summary of
active fires, broadcast via dispatcher.dispatch_scheduled_broadcast.

Modeled after band_conditions.py (cf. v0.5.11 scheduled broadcaster).
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # pragma: no cover

from meshai.adapter_config import adapter_config

logger = logging.getLogger("meshai.scheduled.fire_digest")


# ===========================================================================
# Slot epoch -- HH:MM local -> UNIX epoch (UTC)
# ===========================================================================


def _slot_epoch(now_dt: datetime, hh_mm: str, tz_name: str) -> int:
    """Convert HH:MM in `tz_name` on now_dt's local date to UNIX epoch."""
    h, m = hh_mm.split(":")
    if ZoneInfo is None:
        # Fall back: treat as UTC.
        local = now_dt.replace(hour=int(h), minute=int(m),
                                 second=0, microsecond=0,
                                 tzinfo=timezone.utc)
    else:
        tz = ZoneInfo(tz_name)
        local = now_dt.astimezone(tz).replace(
            hour=int(h), minute=int(m), second=0, microsecond=0,
        )
    return int(local.astimezone(timezone.utc).timestamp())


# ===========================================================================
# Deterministic renderer
# ===========================================================================


def _get_anchor(lat, lon) -> str:
    """Get location anchor for a fire using nearest_town from central_normalizer."""
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return ""
    try:
        from meshai.central_normalizer import nearest_town
        max_mi = float(adapter_config.wfigs.anchor_max_mi)
        nt = nearest_town(lat, lon, max_distance_mi=max_mi)
    except Exception:
        return ""
    if nt and nt.get("name"):
        town = nt["name"].title()
        d = nt.get("distance_mi")
        bearing = nt.get("bearing")
        if isinstance(d, (int, float)):
            if d < 1:
                return f"near {town}"
            return f"{int(round(d))} mi {bearing or ''} of {town}".strip()
        return f"near {town}"
    return ""


async def render_digest(*, now: Optional[int] = None) -> tuple[str, str]:
    """Build the digest wire string deterministically.

    Returns (wire, source). source is 'deterministic' on success,
    'no_fires' if there are no active fires (wire is empty).
    """
    from meshai.persistence import get_db

    now = now if now is not None else int(time.time())
    conn = get_db()

    cutoff = now - 7 * 86400
    rows = conn.execute(
        "SELECT incident_name, current_acres, current_contained_pct, county, state "
        "FROM fires WHERE last_event_at >= ? "
        "AND tombstoned_at IS NULL "
        "AND (current_contained_pct IS NULL OR current_contained_pct < 100) "
        "ORDER BY last_event_at DESC",
        (cutoff,),
    ).fetchall()

    if not rows:
        return "", "no_fires"

    total = len(rows)
    top = rows[:2]

    header = f"\U0001f525 Fire Digest \u2014 {total} active wildfire(s)"

    fire_lines: list[str] = []
    for row in top:
        name = row["incident_name"] or "(unnamed)"
        county = row["county"]
        state = row["state"]
        if county and state:
            fire_lines.append(f"{name} in {county} Co, {state}")
        elif state:
            fire_lines.append(f"{name} in {state}")
        else:
            fire_lines.append(name)

    # Assemble within the universal mesh budget; trim fire lines, never the tail
    budget = int(adapter_config.fires.digest_max_chars)
    shown: list[str] = []
    for line in fire_lines:
        # Estimate tail for budget check
        est_remaining = total - len(shown) - 1
        if est_remaining == 1:
            est_tail = "There is 1 additional wildfire. DM me for the full list."
        elif est_remaining > 1:
            est_tail = f"There are {est_remaining} additional wildfires. DM me for the full list."
        else:
            est_tail = ""
        parts = [header] + shown + [line]
        if est_tail:
            parts.append(est_tail)
        candidate = "\n".join(parts)
        if len(candidate.encode("utf-8")) <= budget:
            shown.append(line)
        else:
            break

    # Compute tail AFTER budget loop with actual shown count
    remaining = total - len(shown)
    if remaining == 1:
        tail = "There is 1 additional wildfire. DM me for the full list."
    elif remaining > 1:
        tail = f"There are {remaining} additional wildfires. DM me for the full list."
    else:
        tail = ""

    parts = [header] + shown
    if tail:
        parts.append(tail)
    wire = "\n".join(parts)

    return wire, "deterministic"


# ===========================================================================
# Broadcast
# ===========================================================================


def _record_slot_attempt(slot_epoch_s: int, *,
                          sent_at: int,
                          summary: Optional[str],
                          source: str) -> Optional[int]:
    """Insert into fire_digest_broadcasts. Returns rowid on insert, None
    if the slot was already broadcast (UNIQUE PK collision)."""
    try:
        from meshai.persistence import get_db
        conn = get_db()
    except Exception:
        return None
    cur = conn.execute(
        "INSERT OR IGNORE INTO fire_digest_broadcasts(slot_epoch, "
        "sent_at, summary, source) VALUES (?,?,?,?)",
        (slot_epoch_s, sent_at, summary, source),
    )
    return int(cur.lastrowid) if cur.rowcount > 0 else None


# ===========================================================================
# Scheduler
# ===========================================================================


class FireDigestScheduler:
    """Fires fire-digest broadcasts at configured local times."""

    def __init__(self, dispatcher, *,
                  clock: Optional[Callable[[], float]] = None,
                  sleep: Optional[Callable[[float], Any]] = None):
        self._dispatcher = dispatcher
        self._clock = clock or time.time
        self._sleep = sleep or asyncio.sleep
        self._task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._logger = logger

    def _enabled(self) -> bool:
        try:
            return bool(adapter_config.fires.digest_enabled)
        except Exception:
            return False

    def _schedule(self) -> list[str]:
        try:
            sched = adapter_config.fires.digest_schedule
        except Exception:
            sched = ["06:00", "18:00"]
        if not isinstance(sched, list):
            sched = ["06:00", "18:00"]
        return [s for s in sched if isinstance(s, str) and ":" in s]

    def _tz_name(self) -> str:
        try:
            return str(adapter_config.fires.digest_timezone)
        except Exception:
            return "America/Boise"

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            raise RuntimeError("FireDigestScheduler already running")
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(),
                                          name="fire-digest-scheduler")
        self._logger.info(
            "Fire digest scheduler started: enabled=%s schedule=%s tz=%s",
            self._enabled(), self._schedule(), self._tz_name())

    async def stop(self) -> None:
        if self._stop_event:
            self._stop_event.set()
        if self._task:
            await self._task

    async def _run(self) -> None:
        while not (self._stop_event and self._stop_event.is_set()):
            if not self._enabled():
                await self._sleep(60); continue
            now = self._clock()
            now_dt = datetime.fromtimestamp(now, tz=timezone.utc)
            target_epoch, target_hh_mm = self._next_slot(now_dt)
            wait_s = max(1, target_epoch - int(now))
            try:
                await self._sleep(min(wait_s, 3600))
            except asyncio.CancelledError:
                break
            now2 = int(self._clock())
            if now2 >= target_epoch:
                await self.fire_slot(target_epoch, target_hh_mm)

    def _next_slot(self, now_dt: datetime) -> tuple[int, str]:
        schedule = sorted(set(self._schedule()))
        if not schedule:
            tomorrow = now_dt + timedelta(days=1)
            return _slot_epoch(tomorrow, "12:00", self._tz_name()), "12:00"
        today_now = int(now_dt.timestamp())
        for hh_mm in schedule:
            ep = _slot_epoch(now_dt, hh_mm, self._tz_name())
            if ep > today_now:
                return ep, hh_mm
        tomorrow = now_dt + timedelta(days=1)
        return _slot_epoch(tomorrow, schedule[0], self._tz_name()), schedule[0]

    def _broadcast_enabled(self) -> bool:
        try:
            return bool(adapter_config.fires.digest_broadcast_enabled)
        except Exception:
            return False

    async def fire_slot(self, slot_epoch_s: int, hh_mm: str) -> bool:
        """Build + broadcast for the given slot. Returns True on broadcast."""
        # Kill-switch on the actual mesh emission. Disabled by default: the
        # scheduler wiring stays intact (so flipping the flag to True cleanly
        # re-enables it) but nothing is dispatched. Per-fire wfigs alerts are a
        # separate path and are unaffected.
        if not self._broadcast_enabled():
            self._logger.info(
                "fire-digest: broadcast disabled "
                "(fires.digest_broadcast_enabled=False); skipping slot %s", hh_mm)
            return False
        wire, source = await render_digest(now=int(self._clock()))
        if source == "no_fires":
            self._logger.info(
                "fire-digest: silent skip for %s (no active fires)", hh_mm)
            _record_slot_attempt(slot_epoch_s,
                                  sent_at=int(self._clock()),
                                  summary=None,
                                  source="skipped_no_fires")
            return False
        bcast_id = _record_slot_attempt(slot_epoch_s,
                                          sent_at=int(self._clock()),
                                          summary=wire,
                                          source=source)
        if bcast_id is None:
            self._logger.info(
                "fire-digest: slot %s already broadcast; skipping dup",
                hh_mm)
            return False
        try:
            success = await self._dispatcher.dispatch_scheduled_broadcast(
                text=wire,
                source_event_table="fire_digest_broadcasts",
                source_event_pk=str(bcast_id),
            )
        except Exception:
            self._logger.exception(
                "fire-digest: dispatcher raised; row stays in table")
            success = False
        return bool(success)
