"""Native SGP4 satellite-pass adapter — predicts + broadcasts locally.

The final piece of native satpass: unlike the Central path (which receives
one per-observer envelope at a time, buffers them in `satpass_pending`, and
relies on the Central consumer's timer to fire `consolidate_satpass_pending`),
this adapter computes ALL observers for a satellite in a SINGLE `tick()`. That
lets it consolidate across observers in-memory and gate synchronously — so it
needs NEITHER the `satpass_pending` buffer NOR the Central consumer/timer,
both of which are OFF in standalone (feed_source="native") mode.

Flow per tick (slow poll, ~15 min; passes are computed over `window_hours`):
    1. Resolve target satellites from the shared `sat_tles` table (populated
       by env.tle_fetch). Resolution rule:
         - `norad_ids` configured  -> exactly those NORAD ids, fresh only.
         - `norad_ids` empty        -> ALL fresh TLEs (which are precisely the
           sats env.tle_fetch pulled from the configured `tle_groups` +
           `norad_ids`). This differs from the CENTRAL broadcast filter, where
           empty norad_ids means "broadcast nothing" — here empty means
           "predict for everything the fetcher stocked". Broadcast volume is
           still bounded by the gate's rate cap + dry-run default.
    2. For each satellite x each enabled observer, run the SGP4 predictor
       (`central.pass_predictor.compute_passes`).
    3. Group every (observer, PassInfo) by the observer-independent canonical
       id `{norad}:{aos_epoch//3600}` and consolidate across observers exactly
       like `consolidate_satpass_pending`: earliest AOS, latest LOS, the
       max-elevation observer supplies max_elevation + peak_compass, and the
       entry/exit observers are the earliest-AOS / latest-LOS stations.
    4. Hand each consolidated pass to the SHARED, source-agnostic
       `satpass_handler.gate_consolidated_pass`, which dedups vs
       `satpass_events`, applies the rate cap + dry-run, upserts the event
       row, and attaches the deferred commit. Staged results feed
       `get_events()` / `to_event()`.

Dedup persists across ticks through `satpass_events`: the gate upserts the row
(with last_broadcast_at NULL) at stage time, and the commit closure — carried
on the emitted Event's `data["_on_broadcast_committed"]` and fired by the
dispatcher after a successful send — sets last_broadcast_at. The next tick's
gate sees that timestamp and suppresses the same canonical pass.

Resilient: no observers / no fresh TLEs / a bad TLE ⇒ log + skip, update
health, never crash.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from meshai.notifications.events import Event, make_event

if TYPE_CHECKING:
    from ..config import SatpassConfig

logger = logging.getLogger(__name__)

# Slow poll cadence. Passes are computed over `window_hours`, so re-running
# more often than this only re-confirms already-gated passes (cheap, but
# pointless). Overridable via SatpassConfig.pass_refresh_seconds if present.
DEFAULT_POLL_SECONDS = 900

# Broadcast imminence lead. Satpass is NOT an API-delta feed — it computes the
# SAME future passes every poll, so a pure "new since last poll" gate would
# seed them all as backlog and then never broadcast. A pass's "newly received"
# analog is IMMINENCE: it becomes broadcast-worthy only once its AOS first
# enters this near-term lead window (and is still in the future). Combined with
# the store's received-delta seen-set (keyed on the pass canonical id), each
# pass then emits exactly once as it crosses into imminence, and the
# first-poll-seed suppresses any startup burst. `window_hours` still governs
# how far ahead we PREDICT (the schedule); this only governs the BROADCAST
# trigger. Overridable via SatpassConfig.broadcast_lead_seconds.
DEFAULT_BROADCAST_LEAD_SECONDS = 3600


class SatpassAdapter:
    """Native SGP4 pass predictor — consolidates in-memory, gates synchronously."""

    def __init__(self, config: "SatpassConfig"):
        self._config = config
        self._interval = int(
            getattr(config, "pass_refresh_seconds", DEFAULT_POLL_SECONDS)
            or DEFAULT_POLL_SECONDS)
        self._window_h = int(getattr(config, "window_hours", 24) or 24)
        self._min_el = float(getattr(config, "min_elevation_deg", 10.0))
        self._lead_s = int(
            getattr(config, "broadcast_lead_seconds", DEFAULT_BROADCAST_LEAD_SECONDS)
            or DEFAULT_BROADCAST_LEAD_SECONDS)
        self._norad_ids = self._parse_norad_ids(
            getattr(config, "norad_ids", None) or [])

        self._last_tick = 0.0
        self._last_error: Optional[str] = None
        self._consecutive_errors = 0
        self._is_loaded = False
        self._events: list[dict] = []  # staged pass events for get_events()

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def _parse_norad_ids(raw) -> list[int]:
        """Coerce a config norad_ids value to a sorted unique int list.

        Accepts a real list (ints or GUI strings) OR a single comma/space
        separated string (as some GUI paths persist it). The string case
        MUST be split first — iterating a bare string would walk its
        CHARACTERS, turning "25544,33591" into garbage single-digit ids and
        silently defeating the norad filter (a cause of non-listed sats
        leaking into predictions).
        """
        if isinstance(raw, str):
            raw = raw.replace(",", " ").split()
        return sorted({int(x) for x in raw if str(x).strip().isdigit()})

    def _resolve_tles(self) -> list[dict]:
        """Resolve the target TLE set from `sat_tles` (fresh only).

        `norad_ids` configured -> exactly those; empty -> all fresh.

        When `norad_ids` is configured the result is ALSO post-filtered to
        that set: this is belt-and-suspenders so that only configured NORADs
        are ever predicted, regardless of which lookup path ran. Without a
        configured list, the fetcher's `tle_groups` (e.g. "weather") stock
        sat_tles with GOES/METEOR/FENGYUN etc.; the strict post-filter
        guarantees those never leak into predictions once a list is set.
        """
        from meshai.central.tle_handler import get_fresh_tles, get_tle_by_norad

        if self._norad_ids:
            allowed = set(self._norad_ids)
            out: list[dict] = []
            for nid in self._norad_ids:
                tle = get_tle_by_norad(nid)
                if tle is not None and int(tle["norad_id"]) in allowed:
                    out.append(tle)
            return out
        return get_fresh_tles()

    @staticmethod
    def _consolidate(cid: str, recs: list[dict]) -> dict:
        """Merge per-observer records for one canonical pass.

        Mirrors `consolidate_satpass_pending`: earliest AOS, latest LOS, the
        max-elevation observer supplies max_elevation + peak_compass, and the
        entry/exit observers are the earliest-AOS / latest-LOS stations.
        """
        by_aos = sorted(recs, key=lambda r: r["aos_epoch"])
        by_los = sorted(recs, key=lambda r: r["los_epoch"])
        entry = by_aos[0]
        exit_ = by_los[-1]
        best = max(recs, key=lambda r: r["max_elevation"])
        return {
            "consolidated_id": cid,
            "norad_id": best["norad_id"],
            "sat_name": best["sat_name"],
            "max_elevation": best["max_elevation"],
            "aos_epoch": entry["aos_epoch"],
            "los_epoch": exit_["los_epoch"],
            "aos_compass": entry["aos_compass"],
            "los_compass": exit_["los_compass"],
            "peak_compass": best["peak_compass"],
            # Entry/exit carry the FRIENDLY observer name for the wire's region
            # parenthetical; observer_list stays slugs for the audit column.
            "entry_observer": entry.get("observer_label") or entry["observer"],
            "exit_observer": exit_.get("observer_label") or exit_["observer"],
            "observer_list": ",".join(r["observer"] for r in by_aos),
        }

    def _compute_staged(self, now_epoch: int) -> list[dict]:
        """Predict → consolidate → gate. Returns staged event dicts."""
        from meshai.central import satpass_handler as sh
        from meshai.central.pass_predictor import compute_passes
        from meshai.persistence.observer_locations import get_observers

        observers = get_observers()
        if not observers:
            logger.debug("satpass native: no observers configured; skipping")
            return []

        tles = self._resolve_tles()
        if not tles:
            logger.debug("satpass native: no fresh TLEs available; skipping")
            return []

        now_dt = datetime.fromtimestamp(now_epoch, tz=timezone.utc)

        # canonical_id -> [per-observer record]
        groups: dict[str, list[dict]] = {}
        for tle in tles:
            norad = tle["norad_id"]
            name = tle["name"]
            l1 = tle["line1"]
            l2 = tle["line2"]
            for obs in observers:
                try:
                    passes = compute_passes(
                        l1, l2, obs["lat"], obs["lon"],
                        obs.get("alt_m", 0.0) or 0.0,
                        self._window_h, self._min_el, now_dt)
                except Exception as e:
                    logger.warning(
                        "satpass native: compute_passes failed for NORAD %s "
                        "obs %s: %s", norad, obs.get("slug"), e)
                    continue
                for p in passes:
                    aos_epoch = int(p.aos_time.timestamp())
                    los_epoch = int(p.los_time.timestamp())
                    cid = sh._canonical_id(norad, aos_epoch)
                    groups.setdefault(cid, []).append({
                        "norad_id": norad,
                        "sat_name": name,
                        "observer": obs["slug"],
                        "observer_label": obs.get("name") or obs["slug"],
                        "max_elevation": p.max_elevation,
                        "aos_epoch": aos_epoch,
                        "los_epoch": los_epoch,
                        "aos_compass": sh._azimuth_to_compass(p.azimuth_at_aos),
                        "los_compass": sh._azimuth_to_compass(p.azimuth_at_los),
                        "peak_compass": sh._azimuth_to_compass(p.azimuth_at_peak),
                    })

        staged: list[dict] = []
        for cid, recs in groups.items():
            consolidated = self._consolidate(cid, recs)
            # IMMINENCE gate — applied on the CONSOLIDATED pass (earliest AOS
            # across observers), so multi-observer consolidation is never
            # fragmented. A pass is broadcast-worthy only once its AOS is both
            # in the FUTURE and within the near-term lead window. Far-future
            # passes are predicted (on the schedule) but not staged until they
            # become imminent; a past AOS is never staged. This is satpass's
            # "just received" signal — the store's received-delta seen-set then
            # emits each pass exactly once as it crosses into the window, and
            # the first-poll-seed prevents any startup burst.
            aos = consolidated["aos_epoch"]
            if aos <= now_epoch or aos > now_epoch + self._lead_s:
                continue
            try:
                result = sh.gate_consolidated_pass(consolidated, now=now_epoch)
            except Exception as e:
                logger.warning("satpass native: gate failed for %s: %s", cid, e)
                continue
            if result is None:
                continue
            wire, data = result
            staged.append({
                "source": "satpass",
                "event_id": cid,
                "wire": wire,
                "data": data,
                "severity": data.get("_severity_override", "routine"),
                "sat_name": consolidated["sat_name"],
                "norad_id": consolidated["norad_id"],
                "aos_epoch": consolidated["aos_epoch"],
                "los_epoch": consolidated["los_epoch"],
                # Purge from the store's in-memory map once the pass is over.
                "expires": consolidated["los_epoch"],
                "fetched_at": now_epoch,
            })
        return staged

    # -- polling --------------------------------------------------------------

    def tick(self, now: Optional[float] = None) -> bool:
        """One slow prediction pass. Returns True if any pass was staged.

        Resilient: any failure logs + updates health and returns False without
        propagating out of tick().
        """
        now = now if now is not None else time.time()
        if now - self._last_tick < self._interval:
            return False
        self._last_tick = now

        try:
            staged = self._compute_staged(int(now))
        except Exception as e:
            self._last_error = str(e)
            self._consecutive_errors += 1
            logger.warning("satpass native: tick failed: %s", e)
            self._is_loaded = True
            return False

        self._events = staged
        self._last_error = None
        self._consecutive_errors = 0
        self._is_loaded = True
        return bool(staged)

    def get_events(self) -> list:
        """Return staged consolidated pass events (dicts)."""
        return list(self._events)

    def to_event(self, evt: dict) -> Optional["Event"]:
        """Translate a staged pass into a precomposed pipeline Event.

        The gate already rendered the LoRa wire (aos→peak→los) and attached the
        `_on_broadcast_committed` closure onto `data`; passing that same dict as
        the Event's `data` makes the commit ride to the dispatcher, which fires
        it after a successful send so `satpass_events.last_broadcast_at` is set
        and the pass won't re-broadcast next tick.
        """
        try:
            wire = evt.get("wire")
            data = evt.get("data")
            if not wire or data is None:
                return None
            event_id = evt.get("event_id")
            return make_event(
                source="satpass",
                category="sat_pass",
                severity=evt.get("severity", "routine"),
                title=wire,        # precomposed: composer passes it verbatim
                summary=wire,
                timestamp=evt.get("fetched_at"),
                expires=evt.get("expires"),
                group_key=event_id,
                inhibit_keys=[event_id] if event_id else [],
                data=data,         # carries _meshai_precomposed + commit hook
            )
        except Exception:
            logger.exception("satpass native: to_event failed")
            return None

    @property
    def health_status(self) -> dict:
        return {
            "source": "satpass",
            "is_loaded": self._is_loaded,
            "last_error": self._last_error,
            "consecutive_errors": self._consecutive_errors,
            "event_count": len(self._events),
            "last_fetch": self._last_tick,
        }
