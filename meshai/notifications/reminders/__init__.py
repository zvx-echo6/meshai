"""v0.6-phase3 ReminderScheduler.

Third broadcast type (after New: and Update:): Active: clock-driven
re-broadcasts of still-live events at human-scale cadences. Reads
reminder config from `adapter_config` (adapter='reminders_<name>'), reads
`reminder_enabled` from `adapter_meta`, scans the per-adapter persistence
tables on a fixed tick, and dispatches via the existing
`Dispatcher.dispatch_scheduled_broadcast()`.

Cadence kinds:
    "interval"  -- re-broadcast when (NOW - last_broadcast_at) >= cadence_value (seconds).
    "clock"     -- re-broadcast at the configured HH:MM slots in cadence_value,
                   gated by dow_mask (Mon..Sun) and timezone.

Termination conditions (per-adapter, JSON list of tokens):
    "tombstone"            -- row has a tombstone marker (per-handler semantics)
    "containment_100"      -- WFIGS: current_contained_pct >= 100
    "last_event_age_24h"   -- last_event_at older than 24h
    "end_date_passed"      -- traffic_events.end_at / swpc payload end_time in past

When a reminder fires:
  - "Active:" prefix is composed into the wire string via the adapter's
    existing renderer (or a minimal fallback).
  - dispatch_scheduled_broadcast() is called (cold-start grace honored,
    toggle path bypassed because reminders are clock-driven, not event-
    driven).
  - On successful delivery, `last_broadcast_at = NOW` is written via the
    adapter table's normal commit path. `first_broadcast_at` is NEVER
    touched here (only the handler sets it on first sight).

Quiet hours are NOT respected (Phase-2 deleted that concept).
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Default tick. The scheduler wakes every N seconds and decides what to
# fire. 60s is fine: interval reminders are 8h-scale; clock reminders
# tolerate up to a minute of lag.
_TICK_SECONDS = 60.0


# ============================================================================
# Public scheduler
# ============================================================================


class ReminderScheduler:
    """Fires Active: reminders for adapters where reminder_enabled=1."""

    def __init__(self, dispatcher, *,
                  clock=None, sleep=None,
                  tick_seconds: float = _TICK_SECONDS):
        self._dispatcher = dispatcher
        self._clock = clock or time.time
        self._sleep = sleep or asyncio.sleep
        self._tick = tick_seconds
        self._task: Optional[asyncio.Task] = None
        self._stop: Optional[asyncio.Event] = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            raise RuntimeError("ReminderScheduler already running")
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="reminder-scheduler")
        logger.info("ReminderScheduler started; tick=%ss", self._tick)

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
                logger.exception("ReminderScheduler tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._tick)
                return
            except asyncio.TimeoutError:
                pass

    async def tick_once(self) -> int:
        """One pass over every reminder-enabled adapter. Returns broadcasts
        fired. Public so tests can drive single ticks deterministically."""
        try:
            from meshai.persistence import get_db
            conn = get_db()
        except Exception:
            return 0

        enabled = [r["adapter"] for r in conn.execute(
            "SELECT adapter FROM adapter_meta WHERE reminder_enabled = 1"
        ).fetchall()]
        if not enabled:
            return 0

        fired = 0
        for adapter in enabled:
            try:
                fired += await self._tick_adapter(adapter)
            except Exception:
                logger.exception("reminder tick failed for adapter=%s", adapter)
        return fired

    # ---- per-adapter --------------------------------------------------

    async def _tick_adapter(self, adapter: str) -> int:
        """Look up the reminder config + query the adapter's table."""
        cfg = _ReminderConfig.load(adapter)
        if cfg is None:
            return 0

        now = self._clock()

        if cfg.cadence_kind == "interval":
            return await self._tick_interval(adapter, cfg, now)
        if cfg.cadence_kind == "clock":
            return await self._tick_clock(adapter, cfg, now)
        logger.debug("reminder %s: unknown cadence_kind=%r", adapter, cfg.cadence_kind)
        return 0

    async def _tick_interval(self, adapter: str, cfg: "_ReminderConfig", now: float) -> int:
        cadence = int(cfg.cadence_value) if cfg.cadence_value else 0
        if cadence <= 0:
            return 0

        targets = self._rows_for(adapter, now, cadence_seconds=cadence)
        fired = 0
        for r in targets:
            if self._terminated(adapter, r, cfg.terminate_when, now):
                continue
            wire = self._render(adapter, r, prefix="Active")
            if not wire:
                continue
            ok = await self._dispatch(adapter, r, wire)
            if ok:
                self._stamp_broadcast(adapter, r, now)
                fired += 1
        return fired

    async def _tick_clock(self, adapter: str, cfg: "_ReminderConfig", now: float) -> int:
        # cadence_value is a list of HH:MM strings.
        slots = list(cfg.cadence_value or [])
        if not slots:
            return 0
        # dow_mask: Mon=0..Sun=6 (Python weekday convention).
        dow_mask = cfg.dow_mask or [True] * 7
        tz_name = cfg.timezone or "UTC"
        # Localize "now" to the configured tz.
        try:
            from zoneinfo import ZoneInfo
            local_dt = datetime.fromtimestamp(now, tz=timezone.utc).astimezone(ZoneInfo(tz_name))
        except Exception:
            local_dt = datetime.fromtimestamp(now, tz=timezone.utc)
        if not dow_mask[local_dt.weekday()]:
            return 0
        # Find the most recent slot we re past today.
        current_local_min = local_dt.hour * 60 + local_dt.minute
        slot_minutes = []
        for s in slots:
            try:
                hh, mm = s.split(":")
                slot_minutes.append(int(hh) * 60 + int(mm))
            except Exception:
                continue
        # Has any slot just passed and we haven t fired today?
        # Simplest: fire iff there's a slot in (current - tick_minutes, current].
        tick_min = max(1, int(self._tick / 60))
        recent_slots = [s for s in slot_minutes
                         if current_local_min - tick_min <= s <= current_local_min]
        if not recent_slots:
            return 0
        # Targets: every row last broadcast more than ~24h ago (so daily
        # reminders don t double up if a slot fires twice on the same day).
        targets = self._rows_for(adapter, now, cadence_seconds=86400)
        fired = 0
        for r in targets:
            if self._terminated(adapter, r, cfg.terminate_when, now):
                continue
            wire = self._render(adapter, r, prefix="Active")
            if not wire: continue
            ok = await self._dispatch(adapter, r, wire)
            if ok:
                self._stamp_broadcast(adapter, r, now)
                fired += 1
        return fired

    # ---- table-specific helpers --------------------------------------

    def _rows_for(self, adapter: str, now: float,
                   *, cadence_seconds: int) -> list:
        try:
            from meshai.persistence import get_db
            conn = get_db()
        except Exception:
            return []
        cutoff = now - cadence_seconds
        if adapter == "wfigs":
            return list(conn.execute(
                "SELECT * FROM fires WHERE last_broadcast_at IS NOT NULL "
                "AND last_broadcast_at <= ?",
                (cutoff,),
            ).fetchall())
        if adapter == "swpc":
            return list(conn.execute(
                "SELECT * FROM swpc_events WHERE last_broadcast_at IS NOT NULL "
                "AND last_broadcast_at <= ?",
                (cutoff,),
            ).fetchall())
        if adapter == "itd_511_work_zone":
            return list(conn.execute(
                "SELECT * FROM traffic_events WHERE source='itd_511' "
                "AND sub_type='road_works' "
                "AND last_broadcast_at IS NOT NULL "
                "AND last_broadcast_at <= ?",
                (cutoff,),
            ).fetchall())
        return []

    def _terminated(self, adapter: str, row, terminate_when: list, now: float) -> bool:
        if not terminate_when: return False
        tokens = set(terminate_when)
        if adapter == "wfigs":
            if "containment_100" in tokens:
                c = row["current_contained_pct"]
                if c is not None and int(c) >= 100:
                    return True
            if "last_event_age_24h" in tokens:
                le = row["last_event_at"]
                if le is not None and (now - float(le)) > 86400:
                    return True
            # "tombstone" -- fires row has no flag; best-effort via event_log
            # would be expensive on each tick. Skip; deliberate scope-limit.
            return False
        if adapter == "swpc":
            if "end_date_passed" in tokens:
                # swpc_events payload_json may carry an end time. Best-effort
                # scan -- not all payloads include it. We treat its absence
                # as "not terminated".
                import json
                try:
                    payload = json.loads(row["payload_json"] or "{}")
                    end_raw = payload.get("end_time") or payload.get("end")
                    if end_raw:
                        from datetime import datetime as _dt
                        try:
                            end_epoch = int(_dt.fromisoformat(
                                str(end_raw).replace("Z", "+00:00")).timestamp())
                            if end_epoch < now:
                                return True
                        except Exception:
                            pass
                except Exception:
                    pass
            return False
        if adapter == "itd_511_work_zone":
            if "end_date_passed" in tokens:
                end_at = row["end_at"]
                if end_at is not None and float(end_at) < now:
                    return True
            return False
        return False

    def _render(self, adapter: str, row, *, prefix: str) -> str:
        """Best-effort Active: wire string per adapter family."""
        if adapter == "wfigs":
            name = row["incident_name"] or "(unnamed)"
            acres = row["current_acres"]
            cont = row["current_contained_pct"]
            acres_s = "N/A" if acres is None else f"{int(acres):,} ac"
            cont_s  = "?" if cont  is None else f"{int(cont)}%"
            anchor = " / ".join(p for p in (row["county"], row["state"]) if p) or "?"
            return f"🔥 {prefix}: {name}, {anchor}: {acres_s}, {cont_s} contained"
        if adapter == "swpc":
            return f"🌌 {prefix}: ongoing space weather event ({row['event_type']})"
        if adapter == "itd_511_work_zone":
            road = row["road"] or "road?"
            county = row["county"] or "?"
            return f"🚧 {prefix}: ongoing work zone on {road} ({county})"
        return ""

    async def _dispatch(self, adapter: str, row, wire: str) -> bool:
        """Send via dispatcher.dispatch_scheduled_broadcast (cold-start
        grace honored, no toggle-path freshness gating)."""
        table, pk = self._row_pk(adapter, row)
        try:
            return bool(await self._dispatcher.dispatch_scheduled_broadcast(
                text=wire, source_event_table=table, source_event_pk=pk,
            ))
        except Exception:
            logger.exception("reminder dispatch failed adapter=%s pk=%s", adapter, pk)
            return False

    def _row_pk(self, adapter: str, row) -> tuple[str, str]:
        if adapter == "wfigs":          return ("fires", str(row["irwin_id"]))
        if adapter == "swpc":           return ("swpc_events", str(row["event_id"]))
        if adapter == "itd_511_work_zone":
            return ("traffic_events", f"{row['source']}|{row['external_id']}")
        return ("?", "?")

    def _stamp_broadcast(self, adapter: str, row, now: float) -> None:
        try:
            from meshai.persistence import get_db
            conn = get_db()
        except Exception:
            return
        if adapter == "wfigs":
            conn.execute(
                "UPDATE fires SET last_broadcast_at=? WHERE irwin_id=?",
                (now, row["irwin_id"]),
            )
        elif adapter == "swpc":
            conn.execute(
                "UPDATE swpc_events SET last_broadcast_at=? WHERE event_id=?",
                (now, row["event_id"]),
            )
        elif adapter == "itd_511_work_zone":
            conn.execute(
                "UPDATE traffic_events SET last_broadcast_at=? "
                "WHERE source=? AND external_id=?",
                (now, row["source"], row["external_id"]),
            )


# ============================================================================
# Config helper
# ============================================================================


class _ReminderConfig:
    """Snapshot of the reminders_<adapter> config rows."""
    __slots__ = ("cadence_kind", "cadence_value", "channels", "terminate_when",
                  "dow_mask", "timezone")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    @classmethod
    def load(cls, adapter: str) -> Optional["_ReminderConfig"]:
        """Pull reminders_<adapter> rows from adapter_config."""
        try:
            from meshai.adapter_config import adapter_config
            ac_adapter = f"reminders_{adapter}"
            ck = _safe_get(adapter_config, ac_adapter, "cadence_kind")
            if ck is None:
                return None
            return cls(
                cadence_kind=ck,
                cadence_value=_safe_get(adapter_config, ac_adapter, "cadence_value"),
                channels=_safe_get(adapter_config, ac_adapter, "channels") or [],
                terminate_when=_safe_get(adapter_config, ac_adapter, "terminate_when") or [],
                dow_mask=_safe_get(adapter_config, ac_adapter, "dow_mask"),
                timezone=_safe_get(adapter_config, ac_adapter, "timezone"),
            )
        except Exception:
            logger.exception("reminders: config load failed for %s", adapter)
            return None


def _safe_get(adapter_config, adapter: str, key: str) -> Any:
    try:
        return getattr(getattr(adapter_config, adapter), key)
    except AttributeError:
        return None
    except Exception:
        return None
