"""wzdx per-region DAILY work-zone count summary scheduler (Part 3).

Fires ONCE PER DAY at a configurable local time (``adapter_config.wzdx.
summary_time`` / ``summary_tz``, defaults "07:00" / "America/Boise"). For
EACH coverage region with >=1 active in-coverage work zone, broadcasts ONE
line:

    "🚧 <Region Name>: <N> active work zones — DM AIDA for details"

Data source: the coalesced ``traffic_events(source='wzdx')`` rows written by
the native WZDx adapter (env/wzdx.py) via the incident gating decider
(notifications/gating/incident.py) — one row per PHYSICAL work zone (Part 1
coalescing). A row counts toward EVERY coverage region its (lat, lon)
resolves to (event_region_names over areas_from_config(config.coverage)); a
region with zero matching rows is skipped (no line emitted).

Routing: each region's line is dispatched through
``Dispatcher.dispatch_scheduled_roads_broadcast`` (Part 3's "roads"-family
counterpart to ``dispatch_scheduled_fire_broadcast``), so it lands on the
SAME channels a live work-zone event for that region would (region_routes
matrix "roads" cells, falling back to the `roads` toggle default).

Deliberately NO change-detection / throttle / dedup table (per spec): a
fixed daily slot is the whole mechanism. The clock-driven shape (next-slot
loop, `fire_slot`) mirrors ``BandConditionsScheduler`` in band_conditions.py
so operators reading one scheduler understand the other. A public
``fire_once(now=None)`` lets tests drive a single pass deterministically
without the sleep loop, analogous to ``fire_slot``/``tick_once`` on the
other schedulers.
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
    ZoneInfo = None   # pragma: no cover (3.9+ only)

from meshai.adapter_config import adapter_config
from meshai.notifications.scheduled.band_conditions import slot_epoch

logger = logging.getLogger(__name__)


# ========================================================================
# Public API — pure helpers (no DB / dispatcher), easy to unit test.
# ========================================================================


def format_wzdx_summary_line(region_name: str, count: int) -> str:
    """One <=140-char broadcast line for a region's active work-zone count."""
    return f"🚧 {region_name}: {count} active work zones — DM AIDA for details"


def count_active_work_zones_by_region(rows: list, areas: list) -> dict:
    """Group coalesced wzdx rows by coverage region name -> count.

    ``rows`` are sqlite3.Row-like objects (or dicts) with at least
    ``lat``/``lon`` keys. A row is counted in EVERY region its point
    resolves to (event_region_names over the named coverage areas) — a
    zone straddling two regions' bboxes counts in both. Rows that resolve
    to no named region are simply not counted anywhere (never raises,
    never invents a region).
    """
    from meshai.coverage_area import event_region_names
    from meshai.notifications.events import make_event

    counts: dict = {}
    # Representative (lat, lon) per region — the FIRST zone seen for that
    # region — so the caller can re-derive the same region deterministically
    # when dispatching (event_region_names(same point, areas) == region).
    rep_point: dict = {}
    for row in rows:
        try:
            lat = row["lat"] if not isinstance(row, dict) else row.get("lat")
            lon = row["lon"] if not isinstance(row, dict) else row.get("lon")
        except (IndexError, KeyError):
            lat = lon = None
        if lat is None or lon is None:
            continue
        ev = make_event(source="wzdx", category="work_zone", severity="routine",
                         lat=float(lat), lon=float(lon))
        for region in event_region_names(ev, areas) or []:
            counts[region] = counts.get(region, 0) + 1
            rep_point.setdefault(region, (float(lat), float(lon)))
    return counts, rep_point


# ========================================================================
# Scheduler — async loop firing once per day
# ========================================================================


class WZDxSummaryScheduler:
    """Fires the per-region wzdx daily work-zone count summary."""

    def __init__(self, config, dispatcher, *,
                  clock: Optional[Callable[[], float]] = None,
                  sleep: Optional[Callable[[float], Any]] = None,
                  tz_name: Optional[str] = None):
        self._config = config
        self._dispatcher = dispatcher
        self._clock = clock or time.time
        self._sleep = sleep or asyncio.sleep
        self._task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._tz_name = tz_name or str(adapter_config.wzdx.summary_tz)
        self._logger = logging.getLogger("meshai.scheduled.wzdx_summary")

    def _enabled(self) -> bool:
        return bool(adapter_config.wzdx.summary_enabled)

    def _summary_time(self) -> str:
        hh_mm = str(adapter_config.wzdx.summary_time or "07:00")
        return hh_mm if ":" in hh_mm else "07:00"

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            raise RuntimeError("WZDxSummaryScheduler already running")
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(),
                                            name="wzdx-summary-scheduler")
        self._logger.info(
            "WZDx summary scheduler started: enabled=%s time=%s tz=%s",
            self._enabled(), self._summary_time(), self._tz_name)

    async def stop(self) -> None:
        if self._stop_event: self._stop_event.set()
        if self._task: await self._task

    async def _run(self) -> None:
        while not (self._stop_event and self._stop_event.is_set()):
            if not self._enabled():
                await self._sleep(60); continue
            now = self._clock()
            now_dt = datetime.fromtimestamp(now, tz=timezone.utc)
            target_epoch = self._next_slot(now_dt)
            wait_s = max(1, target_epoch - int(now))
            try: await self._sleep(min(wait_s, 3600))
            except asyncio.CancelledError: break
            now2 = int(self._clock())
            if now2 >= target_epoch:
                await self.fire_slot(target_epoch)

    def _next_slot(self, now_dt: datetime) -> int:
        """Return the epoch of the next future daily slot."""
        hh_mm = self._summary_time()
        today_now = int(now_dt.timestamp())
        ep = slot_epoch(now_dt, hh_mm, self._tz_name)
        if ep > today_now:
            return ep
        tomorrow = now_dt + timedelta(days=1)
        return slot_epoch(tomorrow, hh_mm, self._tz_name)

    async def fire_slot(self, slot_epoch_s: int) -> int:
        """Compute + broadcast the daily summary for this slot. Returns the
        number of region lines dispatched."""
        return await self.fire_once(now=slot_epoch_s)

    async def fire_once(self, now: Optional[float] = None) -> int:
        """One pass: query coalesced active wzdx rows, group by region,
        dispatch one line per region-with-zones. Public so tests can drive
        it deterministically without the sleep loop (mirrors fire_slot/
        tick_once on the other schedulers). Returns the number of region
        lines dispatched (0 when there are no regions with >=1 active zone,
        or when coverage areas / rows are unavailable)."""
        now = now if now is not None else self._clock()
        now_int = int(now)

        try:
            from meshai.persistence import get_db
            conn = get_db()
        except Exception:
            self._logger.warning("wzdx-summary: DB unavailable; skipping")
            return 0

        try:
            rows = conn.execute(
                "SELECT road, lat, lon, sub_type, impact, end_at "
                "FROM traffic_events "
                "WHERE source='wzdx' AND (end_at IS NULL OR end_at >= ?)",
                (now_int,),
            ).fetchall()
        except Exception:
            self._logger.exception("wzdx-summary: query failed; skipping")
            return 0

        if not rows:
            return 0

        try:
            from meshai.coverage_area import areas_from_config
            areas = areas_from_config(getattr(self._config, "coverage", None))
        except Exception:
            self._logger.exception("wzdx-summary: coverage areas load failed; skipping")
            return 0

        if not areas:
            return 0

        counts, rep_point = count_active_work_zones_by_region(rows, areas)
        if not counts:
            return 0

        dispatched = 0
        for region, n in counts.items():
            if n < 1:
                continue
            line = format_wzdx_summary_line(region, n)
            rep_lat, rep_lon = rep_point.get(region, (None, None))
            pk = f"wzdx_summary_{region}_{now_int}"
            try:
                ok = await self._dispatcher.dispatch_scheduled_roads_broadcast(
                    text=line,
                    source_event_pk=pk,
                    lat=rep_lat, lon=rep_lon, region=region,
                )
            except Exception:
                self._logger.exception(
                    "wzdx-summary: dispatch raised for region=%s", region)
                ok = False
            if ok:
                dispatched += 1
        return dispatched
