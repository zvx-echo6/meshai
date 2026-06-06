"""v0.7-fire-tracker-4 fire-digest scheduled broadcaster.

Twice-daily (default 06:00 + 18:00 Mountain) summary of active fires +
the last 24 h of growth / spotting events, rendered by the LLM into a
terse mesh wire and broadcast via dispatcher.dispatch_scheduled_broadcast.

Modeled after band_conditions.py (cf. v0.5.11 scheduled broadcaster).
"""
from __future__ import annotations

import asyncio
import json
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
# Data + prompt
# ===========================================================================


def _gather_fire_context(now: int, *, window_h: int = 24):
    """Build the LLM-facing data block for the digest prompt."""
    from meshai.persistence import get_db
    conn = get_db()
    cutoff = now - window_h * 3600
    fires = conn.execute(
        "SELECT irwin_id, incident_name, current_acres, "
        "current_contained_pct, lat, lon, state, county, last_pass_at "
        "FROM fires "
        "WHERE tombstoned_at IS NULL "
        "ORDER BY COALESCE(current_acres, 0) DESC LIMIT 20",
    ).fetchall()
    if not fires:
        return None

    fire_blocks: list[str] = []
    for f in fires:
        passes = conn.execute(
            "SELECT pass_id, drift_mi_from_prev, drift_direction, "
            "drift_mi_per_hour FROM fire_passes "
            "WHERE irwin_id=? AND pass_ended_at >= ? "
            "ORDER BY pass_ended_at DESC LIMIT 4",
            (f["irwin_id"], cutoff),
        ).fetchall()
        growth_summary = "no recent passes"
        if passes:
            drifts = [
                f"{p['drift_mi_from_prev']:.1f}mi {p['drift_direction']}"
                for p in passes
                if p["drift_mi_from_prev"] is not None
                and p["drift_direction"] is not None
            ]
            if drifts:
                growth_summary = "drift " + ", ".join(drifts)
            else:
                growth_summary = f"{len(passes)} pass(es), no drift recorded"

        spot_count = conn.execute(
            "SELECT COUNT(*) FROM event_log "
            "WHERE source='firms' AND category LIKE 'wildfire_spotting%'",
        ).fetchone()[0]

        anchor = (f"{f['county']}/{f['state']}"
                   if f["county"] and f["state"] else "ID")
        fire_blocks.append(
            f"- {f['incident_name'] or '(unnamed)'} "
            f"({f['current_acres'] or 0:.0f} ac, "
            f"{f['current_contained_pct'] or 0}% contained, {anchor}); "
            f"{growth_summary}"
        )
    if not fire_blocks:
        return None
    return "\n".join(fire_blocks)


def _build_prompt(context_block: str, *, max_chars: int) -> str:
    return (
        f"You are a wildfire radio dispatcher writing a single-message "
        f"summary for mesh-radio operators in Idaho. You have data on "
        f"{context_block.count(chr(10)) + 1} active fires. "
        f"Write ONE message of <= {max_chars} characters that names the "
        f"top fires, includes any movement direction/speed, and notes "
        f"any spotting or possible new fires. Be terse: this is "
        f"bandwidth-constrained mesh radio. No markdown, no bullet "
        f"points, no greeting, no sign-off. Plain text only.\n\n"
        f"DATA:\n{context_block}"
    )


def _terse_fallback(context_block: str, *, max_chars: int) -> str:
    """Used when the LLM call fails or no LLM is configured."""
    lines = context_block.splitlines()
    fires_n = len(lines)
    head = f"Fires today ({fires_n}): "
    body_parts: list[str] = []
    for line in lines[:3]:
        # line shape: "- <name> (<acres> ac, <pct>% contained, anchor); ..."
        if line.startswith("- "):
            line = line[2:]
        # Trim to "<name> <acres>ac"
        head_part = line.split("(", 1)
        if len(head_part) == 2:
            name = head_part[0].strip()
            rest = head_part[1].split(",", 1)[0]
            body_parts.append(f"{name} {rest}")
        else:
            body_parts.append(line[:40])
    body = "; ".join(body_parts)
    if fires_n > 3:
        body += f"; +{fires_n - 3} more"
    out = head + body
    return out[:max_chars]


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


async def _llm_render(prompt: str, llm_backend, *, max_chars: int) -> Optional[str]:
    """Call the LLM backend and return the trimmed/cleaned wire string."""
    if llm_backend is None:
        return None
    try:
        text = await llm_backend.generate(
            messages=[{"role": "user", "content": prompt}],
            system_prompt="",
            max_tokens=512,
        )
    except Exception:
        logger.exception("fire_digest: LLM call failed")
        return None
    if not text:
        return None
    # Strip markdown the LLM may have added even though prompted not to.
    try:
        from meshai.chunker import strip_markdown
        text = strip_markdown(text)
    except Exception:
        pass
    # Replace newlines with spaces (a digest is a single-line wire).
    text = " ".join(text.split())
    return text[:max_chars]


async def render_digest(*, llm_backend, now: Optional[int] = None,
                          max_chars: Optional[int] = None) -> tuple[str, str]:
    """Build the digest wire string. Returns (wire, source). source is
    'llm' on success, 'fallback_terse' on LLM failure, 'no_fires' if
    there are no active fires (wire is empty in that case)."""
    now = now if now is not None else int(time.time())
    cap = (max_chars if max_chars is not None
            else int(adapter_config.fires.digest_max_chars))
    ctx = _gather_fire_context(now)
    if ctx is None:
        return "", "no_fires"
    prompt = _build_prompt(ctx, max_chars=cap)
    wire = await _llm_render(prompt, llm_backend, max_chars=cap)
    if wire:
        return wire, "llm"
    return _terse_fallback(ctx, max_chars=cap), "fallback_terse"


# ===========================================================================
# Scheduler
# ===========================================================================


class FireDigestScheduler:
    """Fires fire-digest broadcasts at configured local times."""

    def __init__(self, dispatcher, llm_backend, *,
                  clock: Optional[Callable[[], float]] = None,
                  sleep: Optional[Callable[[float], Any]] = None):
        self._dispatcher = dispatcher
        self._llm = llm_backend
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

    async def fire_slot(self, slot_epoch_s: int, hh_mm: str) -> bool:
        """Build + broadcast for the given slot. Returns True on broadcast."""
        wire, source = await render_digest(llm_backend=self._llm,
                                              now=int(self._clock()))
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
