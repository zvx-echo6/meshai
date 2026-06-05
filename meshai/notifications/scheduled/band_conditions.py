"""v0.5.11 band-conditions scheduled broadcaster.

Computes HF propagation ratings 3x/day (06:00, 14:00, 22:00 Mountain Time
by default) and broadcasts a multi-line summary to the mesh. Data source
priority:

    1. Recent SWPC readings persisted in swpc_events (last 6h) -- preferred
       because meshai already ingests these and the local model lets us
       compute without an external HTTP call.
    2. HamQSL.com solarxml.php fallback -- canonical industry source used
       by Ham Radio Toolbox + most propagation apps.
    3. Silent skip -- if both fail, no broadcast (a row is still inserted
       into band_conditions_broadcasts with source='skipped_no_data' so
       the accounting trail is visible).

Distinct from event-driven adapters in two ways:
    - Scheduled by the clock, not triggered by incoming envelopes.
    - The universal freshness gate (v0.5.9 GAMMA) does NOT apply --
      scheduled broadcasts are intentionally periodic, not reactive.
The cold-start grace (v0.5.8b) DOES apply via the dispatcher -- if meshai
just started, the first scheduled broadcast within the grace window is
suppressed for consistency with the event-driven adapters.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None   # pragma: no cover (3.9+ only)

import httpx

logger = logging.getLogger(__name__)

# Window for "fresh" SWPC data. If the latest swpc_kindex (or whatever we
# need) is older than this, fall through to HamQSL.
_SWPC_FRESHNESS_S = 6 * 3600

# Multi-line wire format -- emoji + headline per slot, then 4 band rows.
# Color codes per Matt: 🟢 Good, 🟡 Fair, 🔴 Poor.
_RATING_EMOJI = {"Good": "🟢", "Fair": "🟡", "Poor": "🔴"}

# Slot -> (emoji, headline) tuple. Hour selection is based on the SLOT, not
# the actual fire time, so a slightly-late firing still gets the right
# headline.
_SLOT_LABEL = {
    "06:00": ("☀️", "Day Propagation"),
    "14:00": ("🌞", "Day Propagation"),
    "22:00": ("🌙", "Night Propagation"),
}

# Band order in the wire string. The 4 row labels match the HamQSL grouping
# convention so users coming from Ham Radio Toolbox recognise the format.
_BAND_ORDER = ["80-40m", "30-20m", "17-15m", "12-10m"]

# HamQSL endpoint. Public, no auth.
_HAMQSL_URL = "https://www.hamqsl.com/solarxml.php"
_HAMQSL_TIMEOUT_S = 5


# ========================================================================
# Public API
# ========================================================================


def is_day_slot(hh_mm: str) -> bool:
    """06:00 and 14:00 broadcast 'day' band ratings; 22:00 broadcasts night."""
    return hh_mm in ("06:00", "14:00")


def compute_band_ratings(now: Optional[int] = None,
                          hh_mm: str = "14:00",
                          allow_hamqsl: bool = True,
                          _http_get: Optional[Callable] = None,
                          ) -> Optional[tuple[dict, str]]:
    """Return ((ratings_by_band, source) tuple, or None if no data.

    Source = 'swpc_local' when computed from persisted swpc_events, or
    'hamqsl_fallback' when fetched from HamQSL.com.
    """
    now = now if now is not None else int(time.time())

    # 1. Try local SWPC.
    state = _load_swpc_state(now=now)
    if state is not None:
        ratings = _heuristic_ratings(state, day=is_day_slot(hh_mm))
        if ratings:
            return ratings, "swpc_local"

    # 2. HamQSL fallback.
    if allow_hamqsl:
        try:
            ratings = _fetch_hamqsl(day=is_day_slot(hh_mm), _http_get=_http_get)
        except Exception:
            logger.exception("HamQSL.com fallback failed; silent skip")
            ratings = None
        if ratings:
            return ratings, "hamqsl_fallback"

    return None


def format_band_conditions_wire(ratings: dict, hh_mm: str) -> str:
    """Multi-line wire string -- headline + 4 band rows."""
    emoji, headline = _SLOT_LABEL.get(hh_mm, ("🌞", "Day Propagation"))
    lines = [f"{emoji} {headline}", "📡 Band Conditions:"]
    for band in _BAND_ORDER:
        rating = ratings.get(band, "Poor")
        rating_emoji = _RATING_EMOJI.get(rating, "🔴")
        lines.append(f"{band}: {rating_emoji} {rating}")
    return "\n".join(lines)


def slot_epoch(now_dt: datetime, hh_mm: str, tz_name: str = "America/Boise") -> int:
    """Return the epoch-second timestamp of `hh_mm` on the date of `now_dt`,
    interpreted in the requested timezone. Used as the dedup key for
    band_conditions_broadcasts(scheduled_for).
    """
    h, m = (int(x) for x in hh_mm.split(":"))
    if ZoneInfo is not None:
        tz = ZoneInfo(tz_name)
        local = now_dt.astimezone(tz)
        slot = local.replace(hour=h, minute=m, second=0, microsecond=0)
        return int(slot.timestamp())
    # Fallback: assume UTC (tests can monkeypatch).
    slot = now_dt.replace(hour=h, minute=m, second=0, microsecond=0)
    return int(slot.timestamp())


# ========================================================================
# Internal: SWPC reading -> ratings
# ========================================================================


def _load_swpc_state(now: int) -> Optional[dict]:
    """Pull the latest Kp + SFI from swpc_events. Returns None when readings
    are missing or older than _SWPC_FRESHNESS_S."""
    try:
        from meshai.persistence import get_db
        conn = get_db()
    except Exception:
        return None

    cutoff = now - _SWPC_FRESHNESS_S

    state = {}

    # Latest Kp from swpc_kindex.
    row = conn.execute(
        "SELECT payload_json, occurred_at FROM swpc_events "
        "WHERE event_type='swpc_kindex' AND occurred_at >= ? "
        "ORDER BY occurred_at DESC LIMIT 1",
        (cutoff,)).fetchone()
    if row and row["payload_json"]:
        try:
            payload = json.loads(row["payload_json"])
            kp = payload.get("kp_index") or payload.get("kp") or payload.get("value")
            if isinstance(kp, (int, float)):
                state["kp"] = float(kp)
        except Exception: pass

    # Latest SFI from swpc_alerts. Look for F10.7 in the payload.
    row = conn.execute(
        "SELECT payload_json, occurred_at FROM swpc_events "
        "WHERE event_type='swpc_alerts' AND occurred_at >= ? "
        "  AND (payload_json LIKE '%F10.7%' OR payload_json LIKE '%solar_flux%' "
        "       OR payload_json LIKE '%SFI%' OR payload_json LIKE '%sfi%') "
        "ORDER BY occurred_at DESC LIMIT 1",
        (cutoff,)).fetchone()
    if row and row["payload_json"]:
        try:
            payload = json.loads(row["payload_json"])
            for k in ("F10.7", "f10_7", "f107", "solar_flux", "sfi", "SFI"):
                v = payload.get(k)
                if isinstance(v, (int, float)):
                    state["sfi"] = float(v); break
        except Exception: pass

    # Need at least Kp to do anything useful locally. If SFI is missing,
    # caller still falls back; we return None to signal "incomplete".
    if "kp" not in state or "sfi" not in state:
        return None
    return state


def _heuristic_ratings(state: dict, day: bool) -> dict:
    """Per Matt's spec: per-band rating from Kp + SFI."""
    kp = state.get("kp", 9.0)
    sfi = state.get("sfi", 50.0)

    out = {}

    # 80-40m: night band primarily.
    if not day:
        if kp < 4 and sfi > 70: out["80-40m"] = "Good"
        elif kp < 5:            out["80-40m"] = "Fair"
        else:                   out["80-40m"] = "Poor"
    else:
        # Daytime 80-40m is usually Fair to Poor (absorption).
        if kp < 4: out["80-40m"] = "Fair"
        else:      out["80-40m"] = "Poor"

    # 30-20m: day-strong band; tolerates higher Kp than upper bands.
    if day:
        if sfi > 120 and kp < 4: out["30-20m"] = "Good"
        elif 80 <= sfi <= 120 and kp < 6: out["30-20m"] = "Fair"
        elif sfi < 80 or kp >= 6: out["30-20m"] = "Poor"
        else: out["30-20m"] = "Fair"
    else:
        # Night 20m can be Good if SFI high + Kp low (gray-line).
        if sfi > 110 and kp < 4: out["30-20m"] = "Good"
        elif kp < 5: out["30-20m"] = "Fair"
        else: out["30-20m"] = "Poor"

    # 17-15m: day-only band most of the time.
    if day:
        if sfi > 120: out["17-15m"] = "Good"
        elif 90 <= sfi <= 120 and kp < 5: out["17-15m"] = "Fair"
        else: out["17-15m"] = "Poor"
    else:
        # Night upper bands typically Poor unless aurora/Es event.
        out["17-15m"] = "Poor"

    # 12-10m: day-only solar-max band.
    if day:
        if sfi > 140: out["12-10m"] = "Good"
        elif 110 <= sfi <= 140: out["12-10m"] = "Fair"
        else: out["12-10m"] = "Poor"
    else:
        out["12-10m"] = "Poor"

    return out


# ========================================================================
# Internal: HamQSL.com fallback
# ========================================================================


def _fetch_hamqsl(day: bool, _http_get: Optional[Callable] = None) -> Optional[dict]:
    """Pull HamQSL solarxml.php and parse calculatedconditions."""
    if _http_get is None:
        def _http_get(url, timeout):
            with httpx.Client(timeout=timeout) as c:
                return c.get(url)
    try:
        resp = _http_get(_HAMQSL_URL, _HAMQSL_TIMEOUT_S)
    except Exception:
        return None
    if getattr(resp, "status_code", 0) != 200:
        return None

    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError:
        return None

    cond = root.find(".//calculatedconditions")
    if cond is None: return None

    want_time = "day" if day else "night"
    out = {}
    # HamQSL uses names like "80m-40m", "30m-20m", "17m-15m", "12m-10m".
    for band in cond.findall("band"):
        name = (band.get("name") or "").lower().replace("m", "")
        # Normalise to our 4-row labels.
        key = None
        if "80" in name and "40" in name: key = "80-40m"
        elif "30" in name and "20" in name: key = "30-20m"
        elif "17" in name and "15" in name: key = "17-15m"
        elif "12" in name and "10" in name: key = "12-10m"
        if not key: continue
        t = (band.get("time") or "").lower()
        if t != want_time: continue
        text = (band.text or "").strip()
        if text not in _RATING_EMOJI: continue
        out[key] = text

    return out if out else None


# ========================================================================
# Persistence helpers (band_conditions_broadcasts rows)
# ========================================================================


def record_slot_attempt(slot_epoch_s: int, *, source: str,
                         ratings: Optional[dict] = None,
                         sent_at: Optional[int] = None) -> Optional[int]:
    """Insert (or ignore) a row in band_conditions_broadcasts. Returns the
    broadcast_id on success, None when the UNIQUE(scheduled_for) constraint
    blocks a duplicate firing (the slot already ran)."""
    try:
        from meshai.persistence import get_db
        conn = get_db()
    except Exception:
        return None
    rj = json.dumps(ratings) if ratings else None
    cur = conn.execute(
        "INSERT OR IGNORE INTO band_conditions_broadcasts(sent_at, "
        "scheduled_for, ratings_json, source) VALUES (?,?,?,?)",
        (sent_at, slot_epoch_s, rj, source),
    )
    return int(cur.lastrowid) if cur.rowcount > 0 else None


# ========================================================================
# Scheduler -- async loop firing once per slot
# ========================================================================


class BandConditionsScheduler:
    """Fires band-conditions broadcasts at configured local times."""

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
        self._tz_name = tz_name or getattr(
            config.notifications, "band_conditions_tz", "America/Boise")
        self._logger = logging.getLogger("meshai.scheduled.band_conditions")

    def _enabled(self) -> bool:
        return bool(getattr(self._config.notifications,
                             "band_conditions_enabled", True))

    def _schedule(self) -> list:
        sched = getattr(self._config.notifications,
                         "band_conditions_schedule",
                         ["06:00", "14:00", "22:00"])
        return [s for s in sched if isinstance(s, str) and ":" in s]

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            raise RuntimeError("BandConditionsScheduler already running")
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(),
                                            name="band-conditions-scheduler")
        self._logger.info(
            "Band conditions scheduler started: enabled=%s schedule=%s tz=%s",
            self._enabled(), self._schedule(), self._tz_name)

    async def stop(self) -> None:
        if self._stop_event: self._stop_event.set()
        if self._task: await self._task

    async def _run(self) -> None:
        while not (self._stop_event and self._stop_event.is_set()):
            if not self._enabled():
                await self._sleep(60); continue
            now = self._clock()
            now_dt = datetime.fromtimestamp(now, tz=timezone.utc)
            target_epoch, target_hh_mm = self._next_slot(now_dt)
            wait_s = max(1, target_epoch - int(now))
            try: await self._sleep(min(wait_s, 3600))
            except asyncio.CancelledError: break
            now2 = int(self._clock())
            if now2 >= target_epoch:
                await self.fire_slot(target_epoch, target_hh_mm)

    def _next_slot(self, now_dt: datetime) -> tuple[int, str]:
        """Return the (epoch, hh_mm) of the next future slot. Searches today's
        slots first; if all past, picks tomorrow's first slot."""
        schedule = sorted(set(self._schedule()))
        if not schedule:
            # Fall back to noon tomorrow if config is broken.
            tomorrow = now_dt + timedelta(days=1)
            return slot_epoch(tomorrow, "12:00", self._tz_name), "12:00"
        today_now = int(now_dt.timestamp())
        for hh_mm in schedule:
            ep = slot_epoch(now_dt, hh_mm, self._tz_name)
            if ep > today_now: return ep, hh_mm
        # All today's slots are past; pick tomorrow's first.
        tomorrow = now_dt + timedelta(days=1)
        return slot_epoch(tomorrow, schedule[0], self._tz_name), schedule[0]

    async def fire_slot(self, slot_epoch_s: int, hh_mm: str) -> bool:
        """Compute + broadcast for this slot. Returns True on broadcast,
        False on suppression (grace), None on silent skip (no data)."""
        result = compute_band_ratings(now=int(self._clock()), hh_mm=hh_mm)
        if result is None:
            record_slot_attempt(slot_epoch_s, source="skipped_no_data")
            self._logger.info(
                "band-conditions: silent skip for %s (no SWPC + HamQSL down)",
                hh_mm)
            return False
        ratings, source = result

        # Pre-insert the slot row (UNIQUE constraint handles dedup).
        bcast_id = record_slot_attempt(slot_epoch_s, source=source,
                                         ratings=ratings,
                                         sent_at=int(self._clock()))
        if bcast_id is None:
            # Slot already broadcast (UNIQUE constraint blocked the INSERT).
            self._logger.info(
                "band-conditions: slot %s already broadcast; skipping dup",
                hh_mm)
            return False

        wire = format_band_conditions_wire(ratings, hh_mm)
        try:
            success = await self._dispatcher.dispatch_scheduled_broadcast(
                text=wire,
                source_event_table="band_conditions_broadcasts",
                source_event_pk=str(bcast_id),
            )
        except Exception:
            self._logger.exception(
                "band-conditions: dispatcher raised; row stays in table")
            success = False
        return bool(success)
