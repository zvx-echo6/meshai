"""Environmental data store with tick-based adapter polling."""

import hashlib
import json
import logging
import time
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from ..config import EnvironmentalConfig
    from ..notifications.pipeline import EventBus

logger = logging.getLogger(__name__)


class EnvironmentalStore:
    """Cache and tick-driver for all environmental feed adapters."""

    def __init__(
        self,
        config: "EnvironmentalConfig",
        region_anchors: list = None,
        event_bus: Optional["EventBus"] = None,
    ):
        self._adapters = {}  # name -> adapter instance
        self._failed_adapters = {}  # name -> last_error string
        self._events = {}  # (source, event_id) -> event dict
        self._event_bus = event_bus  # Pipeline EventBus for emission
        self._swpc_status = {}  # Kp/SFI/scales snapshot
        self._ducting_status = {}  # tropo ducting assessment
        self._mesh_zones = config.nws_zones or []
        self._region_anchors = region_anchors or []

        # ── Received-delta gate (NATIVE-only) ────────────────────────────
        # Per-adapter set of item keys seen in PRIOR polls, plus the set of
        # adapters whose first poll has completed. The model the operator
        # demanded: a native adapter broadcasts an item ONLY when it was newly
        # RECEIVED from the API this poll — never by scanning an accumulated
        # backlog. So the FIRST poll for an adapter records every current key
        # as "seen" and emits NOTHING (that batch is pre-existing backlog, not
        # ours to announce); every later poll emits only keys not already
        # seen. In-memory only (per process) — a restart empties the sets, so
        # the next poll is a fresh "first poll" that re-seeds silently. This
        # makes broadcasting a backlog item structurally impossible on cold
        # start, restart, or after the adapter was disabled for days.
        self._seen: dict[str, set] = {}   # adapter name -> set of item keys
        self._seeded: set[str] = set()    # adapters past their first poll

        # Create adapter instances with error isolation
        self._register_adapter("nws", config.nws, ".nws", "NWSAlertsAdapter",
            lambda cfg: (cfg,))
        self._register_adapter("swpc", config.swpc, ".swpc", "SWPCAdapter",
            lambda cfg: (cfg,))
        self._register_adapter("ducting", config.ducting, ".ducting", "DuctingAdapter",
            lambda cfg: (cfg,))
        self._register_adapter("nifc", config.fires, ".fires", "NICFFiresAdapter",
            lambda cfg: (cfg, self._region_anchors))
        self._register_adapter("avalanche", config.avalanche, ".avalanche", "AvalancheAdapter",
            lambda cfg: (cfg,))
        self._register_adapter("usgs", config.usgs, ".usgs", "USGSStreamsAdapter",
            lambda cfg: (cfg,))
        self._register_adapter("usgs_quake", config.usgs_quake, ".usgs_quake", "USGSQuakeAdapter",
            lambda cfg: (cfg,))
        self._register_adapter("traffic", config.traffic, ".traffic", "TomTomTrafficAdapter",
            lambda cfg: (cfg,))
        self._register_adapter("roads511", config.roads511, ".roads511", "Roads511Adapter",
            lambda cfg: (cfg,))
        self._register_adapter("wzdx", config.wzdx, ".wzdx", "WZDxAdapter",
            lambda cfg: (cfg,))
        # Native satpass TLE fetcher (storage-only: populates sat_tles, emits
        # no events). Gated on satpass.feed_source=="native" like the rest.
        self._register_adapter("satpass_tle", config.satpass, ".tle_fetch", "TLEFetchAdapter",
            lambda cfg: (cfg,))
        # Native SGP4 pass predictor (broadcasts consolidated passes locally,
        # no Central dependency). SEPARATE from the satpass_tle fetcher above;
        # both are gated on satpass.enabled and feed_source=="native".
        self._register_adapter("satpass", config.satpass, ".satpass", "SatpassAdapter",
            lambda cfg: (cfg,))

        # FIRMS needs reference to NIFC adapter for cross-referencing
        if config.firms.enabled and config.firms.feed_source == "native":
            try:
                from .firms import FIRMSAdapter
                fires_adapter = self._adapters.get("nifc")
                self._firms = FIRMSAdapter(config.firms, self._region_anchors, fires_adapter)
                self._adapters["firms"] = self._firms
            except Exception as e:
                err_msg = f"{type(e).__name__}: {e}"
                logger.warning("Failed to initialize firms adapter: %s", err_msg)
                self._failed_adapters["firms"] = err_msg

        _central = [n for n in ("nws", "swpc", "ducting", "fires", "avalanche", "usgs", "usgs_quake", "traffic", "roads511", "wzdx", "firms", "satpass")
                    if getattr(getattr(config, n, None), "feed_source", "native") == "central"]
        if _central:
            logger.debug("Adapters sourced from Central (native skipped): %s", _central)
        if self._failed_adapters:
            logger.warning("Failed adapters: %s", list(self._failed_adapters.keys()))
        logger.info(f"EnvironmentalStore initialized with {len(self._adapters)} adapters")


    def _register_adapter(self, name: str, cfg, module_path: str, class_name: str, args_fn):
        """Register a single adapter with error isolation."""
        if not cfg.enabled or cfg.feed_source != "native":
            return
        try:
            module = __import__(f"meshai.env{module_path}", fromlist=[class_name])
            cls = getattr(module, class_name)
            self._adapters[name] = cls(*args_fn(cfg))
        except Exception as e:
            err_msg = f"{type(e).__name__}: {e}"
            logger.warning("Failed to initialize %s adapter: %s", name, err_msg)
            self._failed_adapters[name] = err_msg

    def refresh(self) -> bool:
        """Called every second from main loop. Ticks each adapter.

        Returns:
            True if any data changed
        """
        changed = False
        for name, adapter in self._adapters.items():
            try:
                if adapter.tick():
                    changed = True
                    self._ingest(name, adapter)
            except Exception as e:
                logger.warning("Env adapter %s error: %s", name, e)

        self._purge_expired()
        return changed

    def _seen_key(self, name: str, raw_evt: dict) -> str:
        """Derive a STABLE per-item key for the received-delta gate.

        Stability across polls is the whole point: the SAME real-world item
        must produce the SAME key every poll, or it would look "newly
        received" forever and re-broadcast on each tick. Preference order,
        most→least explicit id:
          1. external_id — the upstream feed's own stable id (e.g. WZDx)
          2. event_id    — the adapter's stable per-item id; every native raw
             event carries one (the store's own dedup already keys on it, and
             the adapters build it from natural ids: USGS quake id, 511 event
             id, NWS alert id, ``swpc_<scale><level>``, ``ducting_<tier>_<loc>``,
             ``avy_<center>_<zone>``, etc. — all stable across polls).
          3. content hash — last-resort fallback if an item somehow carries no
             id at all.
        Namespaced by adapter so two feeds never cross-contaminate.
        """
        ext = raw_evt.get("external_id")
        if ext:
            return f"{name}\x1eext:{ext}"
        eid = raw_evt.get("event_id")
        if eid:
            return f"{name}\x1eeid:{eid}"
        blob = json.dumps(raw_evt, sort_keys=True, default=str)
        return f"{name}\x1ehash:" + hashlib.sha1(blob.encode()).hexdigest()[:16]

    def _delta_emit(self, name: str, adapter, raw_evt: dict, force: bool = False):
        """Received-delta gate — emit ONLY items newly received THIS poll.

        - First poll for ``name`` (name not yet in ``self._seeded``): record
          the item's key as seen and emit NOTHING. That batch is the
          pre-existing backlog.
        - Later polls: emit only when the key is not already in the seen-set
          (it just appeared upstream = "just received"); then record it.
        - ``force=True`` (avalanche danger-level rise) re-emits an already-seen
          item on a legitimate CONTENT change — but is IGNORED on the first
          poll, so a restart still can never replay the backlog.

        This replaces the native path's reliance on the deciders'
        broadcast-state tables for the "is this new" decision. Legitimate
        content filtering (severity/threshold/impact, decider gates) still runs
        downstream in ``_emit_event``.
        """
        seen = self._seen.setdefault(name, set())
        key = self._seen_key(name, raw_evt)
        first_poll = name not in self._seeded

        if first_poll:
            seen.add(key)            # seed silently — this is backlog
            return
        if key in seen and not force:
            return                   # already received in a prior poll
        seen.add(key)

        if self._event_bus is not None and hasattr(adapter, "to_event"):
            self._emit_event(adapter, raw_evt)

    def _ingest(self, name: str, adapter):
        """Ingest data from an adapter after it ticks.

        Emission goes through the received-delta gate (``_delta_emit``): the
        adapter's FIRST data-bearing poll seeds the seen-set and broadcasts
        nothing; only items that newly appear on later polls are broadcast.
        ``self._events`` is still maintained for state/queries as before.
        """
        if name == "swpc":
            self._swpc_status = adapter.get_status()
            # Also ingest any alert events (R-scale >= 3)
            for evt in adapter.get_events():
                key = (evt["source"], evt["event_id"])
                self._events[key] = evt
                self._delta_emit(name, adapter, evt)
        elif name == "ducting":
            self._ducting_status = adapter.get_status()
            for evt in adapter.get_events():
                key = (evt["source"], evt["event_id"])
                self._events[key] = evt
                self._delta_emit(name, adapter, evt)
        elif name == "avalanche":
            # Avalanche: re-emit on danger_level rise (Update:) not just new
            # events. The rise is a legitimate CONTENT change, so it passes
            # `force=True` — but the received-delta gate still suppresses it on
            # the first poll (backlog stays silent).
            for evt in adapter.get_events():
                key = (evt["source"], evt["event_id"])
                prior = self._events.get(key)
                prior_level = prior.get("danger_level", -1) if prior else -1
                level_rose = (prior is not None) and (
                    evt.get("danger_level", -1) > prior_level)
                evt["_is_update"] = level_rose   # signal to to_event()
                self._delta_emit(name, adapter, evt, force=level_rose)
                self._events[key] = evt   # always update stored state
        else:
            for evt in adapter.get_events():
                key = (evt["source"], evt["event_id"])
                self._events[key] = evt
                self._delta_emit(name, adapter, evt)

        # First poll for this adapter is now complete: later polls may emit.
        self._seeded.add(name)

    def _emit_event(self, adapter, raw_evt: dict):
        """Convert raw event to pipeline Event and emit to bus.

        Phase-1 new-arch hook: if a gating decider is registered for the
        event's category (via meshai.notifications.gating.DECIDERS), it is
        called with event.data before the event reaches the bus.  The decider
        mirrors the Central-path gate so native and Central ingestion share
        identical broadcast decisions.

        Decider contract:
          - Returns GateResult.broadcast=False → suppress (event never emits)
          - Returns GateResult.broadcast=True  → apply data_patch, attach
            commit, then emit normally.
          - Exceptions in the decider are caught; event is silently suppressed
            to preserve the default-deny safety property.
        """
        try:
            event = adapter.to_event(raw_evt)
            if event is None:
                return  # adapter declined to emit (non-actionable reading)

            # ── New-arch decider hook ──────────────────────────────────────
            # Applied only when BOTH a decider is registered AND the category
            # has been explicitly cut over via MESHAI_CUTOVER_CATEGORIES.
            # When not cut over, the native adapter emits directly to the bus
            # (original pre-Phase-1 behavior); shadow_gate in consumer handles
            # dry-run comparison for the bake period.
            try:
                from meshai.notifications.gating import get_decider
                from meshai.notifications.cutover import is_cutover
                from meshai.notifications import clock as _clock
                decider = get_decider(event.category)
                if decider is not None and is_cutover(event.category):
                    if event.data is None:
                        event.data = {}
                    gate = decider(event.data, source=event.source,
                                   now=_clock.now())
                    if not gate.broadcast:
                        logger.debug(
                            "store: decider suppressed %s event %s: %s",
                            event.category, raw_evt.get("event_id", "?"),
                            gate.reason,
                        )
                        return
                    # Apply data_patch into event.data
                    event.data.update(gate.data_patch)
                    if gate.commit is not None:
                        event.data["_on_broadcast_committed"] = gate.commit
            except Exception as _gate_exc:
                logger.warning(
                    "store: decider failed for %s, suppressing: %s",
                    event.category, _gate_exc,
                )
                return  # default-deny on decider error

            self._event_bus.emit(event)
            logger.info(
                "Emitted %s event %s (%s) to pipeline bus",
                event.source,
                event.id,
                event.category,
            )
        except Exception as e:
            logger.warning("Failed to emit event to pipeline: %s", e)

    def _purge_expired(self):
        """Remove expired events."""
        now = time.time()
        expired = [
            k for k, v in self._events.items()
            if v.get("expires") and v["expires"] < now
        ]
        for k in expired:
            del self._events[k]

    def get_active(self, source: str = None) -> list:
        """Get active events, optionally filtered by source.

        Args:
            source: Filter to specific source (nws, swpc, etc.)

        Returns:
            List of event dicts sorted by fetched_at (newest first)
        """
        events = list(self._events.values())
        if source:
            events = [e for e in events if e["source"] == source]
        return sorted(events, key=lambda e: e.get("fetched_at", 0), reverse=True)

    def get_for_zones(self, zones: list) -> list:
        """Get events affecting specific NWS zones.

        Args:
            zones: List of UGC zone codes (e.g., ["IDZ016", "IDZ030"])

        Returns:
            List of events with overlapping zone coverage
        """
        zone_set = set(zones)
        return [
            e for e in self._events.values()
            if set(e.get("areas", [])) & zone_set
        ]

    def get_swpc_status(self) -> dict:
        """Get current SWPC space weather status."""
        return self._swpc_status

    def get_ducting_status(self) -> dict:
        """Get current tropospheric ducting status."""
        return self._ducting_status

    def get_rf_propagation(self) -> dict:
        """Combined HF + UHF propagation summary for dashboard/LLM."""
        return {
            "hf": self._swpc_status,
            "uhf_ducting": self._ducting_status,
        }

    def get_summary(self) -> str:
        """Compact text block for LLM context injection."""
        lines = []
        lines.append(f"### Current Conditions (as of {time.strftime('%H:%M:%S MT')}):")

        # NWS alerts
        nws = self.get_active(source="nws")
        if nws:
            lines.append(f"NWS: {len(nws)} active alert(s):")
            for a in nws[:3]:
                lines.append(f"  - {a['event_type']}: {a['headline'][:120]}")
        else:
            lines.append("NWS: No active alerts for mesh area.")

        # Space weather indices (raw - LLM interprets)
        s = self._swpc_status
        if s:
            kp = s.get("kp_current", "?")
            sfi = s.get("sfi", "?")
            r = s.get("r_scale", 0)
            g = s.get("g_scale", 0)
            lines.append(f"Space Weather: SFI {sfi}, Kp {kp}, R{r}/G{g}")
            warnings = s.get("active_warnings", [])
            if warnings:
                for w in warnings[:2]:
                    lines.append(f"  Warning: {w}")
        else:
            lines.append("Space Weather: Data not available.")

        # Tropospheric ducting (raw - LLM interprets)
        d = self._ducting_status
        if d:
            condition = d.get("condition", "unknown")
            gradient = d.get("min_gradient", "?")
            if condition == "normal":
                lines.append(f"Tropospheric: Normal (dM/dz {gradient} M-units/km)")
            else:
                thickness = d.get("duct_thickness_m", "?")
                lines.append(f"Tropospheric: {condition.replace('_', ' ').title()}")
                lines.append(f"  dM/dz: {gradient} M-units/km, duct ~{thickness}m thick")

        # Active fires
        fires = self.get_active(source="nifc")
        if fires:
            lines.append(f"Wildfires: {len(fires)} active")
            for f in fires[:2]:
                name = f.get("name", "Unknown")
                acres = f.get("acres", 0)
                pct = f.get("pct_contained", 0)
                dist = f.get("distance_km")
                lines.append(f"  - {name}: {int(acres):,} ac, {int(pct)}% contained" +
                            (f" ({int(dist)} km)" if dist else ""))

        # Avalanche advisories
        avy = self.get_active(source="avalanche")
        if avy:
            lines.append(f"Avalanche: {len(avy)} zone(s) with advisories")
            for a in avy[:2]:
                zone = a.get("zone_name", "Unknown")
                danger = a.get("danger_name", "Unknown")
                lines.append(f"  - {zone}: {danger}")

        # Stream gauges
        streams = self.get_active(source="usgs")
        if streams:
            lines.append(f"Stream Gauges: {len(streams)} readings")
            for s in streams[:2]:
                lines.append(f"  - {s['headline']}")

        # Traffic flow
        traffic = self.get_active(source="traffic")
        if traffic:
            lines.append(f"Traffic: {len(traffic)} corridors")
            for t in traffic[:2]:
                lines.append(f"  - {t['headline']}")

        # 511 road events
        roads = self.get_active(source="511")
        if roads:
            lines.append(f"Road Events: {len(roads)} active")
            for r in roads[:2]:
                lines.append(f"  - {r['headline'][:60]}")

        # Satellite hotspots
        hotspots = self.get_active(source="firms")
        if hotspots:
            new_ignitions = [h for h in hotspots if h.get("properties", {}).get("new_ignition")]
            lines.append(f"Satellite Hotspots: {len(hotspots)} detected")
            if new_ignitions:
                lines.append(f"  *** {len(new_ignitions)} POTENTIAL NEW IGNITION(S) ***")
            for h in hotspots[:2]:
                lines.append(f"  - {h['headline']}")

        return "\n".join(lines)


    def get_status(self) -> list:
        """Get status of all adapters including failed ones."""
        status = []
        for name, adapter in self._adapters.items():
            try:
                hs = adapter.health_status
                status.append({
                    "source": name,
                    "is_loaded": True,
                    "last_error": hs.get("last_error"),
                    "consecutive_errors": hs.get("consecutive_errors", 0),
                    "event_count": hs.get("event_count", 0),
                    "last_fetch": hs.get("last_fetch"),
                })
            except Exception:
                status.append({
                    "source": name,
                    "is_loaded": True,
                    "last_error": None,
                    "consecutive_errors": 0,
                    "event_count": 0,
                    "last_fetch": None,
                })
        for name, error in self._failed_adapters.items():
            status.append({
                "source": name,
                "is_loaded": False,
                "last_error": error,
                "consecutive_errors": 0,
                "event_count": 0,
                "last_fetch": None,
            })
        return status

    def get_source_health(self) -> list:
        """Get health status for all adapters."""
        return [a.health_status for a in self._adapters.values()]
