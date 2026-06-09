"""Environmental data store with tick-based adapter polling."""

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
        self._events = {}  # (source, event_id) -> event dict
        self._event_bus = event_bus  # Pipeline EventBus for emission
        self._swpc_status = {}  # Kp/SFI/scales snapshot
        self._ducting_status = {}  # tropo ducting assessment
        self._mesh_zones = config.nws_zones or []
        self._region_anchors = region_anchors or []

        # Create adapter instances based on config
        if config.nws.enabled and config.nws.feed_source == "native":
            from .nws import NWSAlertsAdapter
            self._adapters["nws"] = NWSAlertsAdapter(config.nws)

        if config.swpc.enabled and config.swpc.feed_source == "native":
            from .swpc import SWPCAdapter
            self._adapters["swpc"] = SWPCAdapter(config.swpc)

        if config.ducting.enabled and config.ducting.feed_source == "native":
            from .ducting import DuctingAdapter
            self._adapters["ducting"] = DuctingAdapter(config.ducting)

        if config.fires.enabled and config.fires.feed_source == "native":
            from .fires import NICFFiresAdapter
            self._adapters["nifc"] = NICFFiresAdapter(config.fires, self._region_anchors)

        if config.avalanche.enabled and config.avalanche.feed_source == "native":
            from .avalanche import AvalancheAdapter
            self._adapters["avalanche"] = AvalancheAdapter(config.avalanche)

        if config.usgs.enabled and config.usgs.feed_source == "native":
            from .usgs import USGSStreamsAdapter
            self._adapters["usgs"] = USGSStreamsAdapter(config.usgs)

        if config.usgs_quake.enabled and config.usgs_quake.feed_source == "native":
            from .usgs_quake import USGSQuakeAdapter
            self._adapters["usgs_quake"] = USGSQuakeAdapter(config.usgs_quake)

        if config.traffic.enabled and config.traffic.feed_source == "native":
            from .traffic import TomTomTrafficAdapter
            self._adapters["traffic"] = TomTomTrafficAdapter(config.traffic)

        if config.roads511.enabled and config.roads511.feed_source == "native":
            from .roads511 import Roads511Adapter
            self._adapters["roads511"] = Roads511Adapter(config.roads511)

        # FIRMS needs reference to NIFC adapter for cross-referencing
        if config.firms.enabled and config.firms.feed_source == "native":
            from .firms import FIRMSAdapter
            fires_adapter = self._adapters.get("nifc")
            self._firms = FIRMSAdapter(config.firms, self._region_anchors, fires_adapter)
            self._adapters["firms"] = self._firms

        _central = [n for n in ("nws", "swpc", "ducting", "fires", "avalanche", "usgs", "usgs_quake", "traffic", "roads511", "firms")
                    if getattr(getattr(config, n, None), "feed_source", "native") == "central"]
        if _central:
            logger.debug("Adapters sourced from Central (native skipped): %s", _central)
        logger.info(f"EnvironmentalStore initialized with {len(self._adapters)} adapters")

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

    def _ingest(self, name: str, adapter):
        """Ingest data from an adapter after it ticks."""
        if name == "swpc":
            self._swpc_status = adapter.get_status()
            # Also ingest any alert events (R-scale >= 3)
            for evt in adapter.get_events():
                key = (evt["source"], evt["event_id"])
                is_new = key not in self._events
                self._events[key] = evt
                if is_new and self._event_bus and hasattr(adapter, "to_event"):
                    self._emit_event(adapter, evt)
        elif name == "ducting":
            self._ducting_status = adapter.get_status()
            for evt in adapter.get_events():
                key = (evt["source"], evt["event_id"])
                is_new = key not in self._events
                self._events[key] = evt
                if is_new and self._event_bus and hasattr(adapter, "to_event"):
                    self._emit_event(adapter, evt)
        elif name == "avalanche":
            # Avalanche: re-emit on danger_level rise (Update:) not just new events.
            for evt in adapter.get_events():
                key = (evt["source"], evt["event_id"])
                prior = self._events.get(key)
                is_new = prior is None
                prior_level = prior.get("danger_level", -1) if prior else -1
                level_rose = (not is_new) and (evt.get("danger_level", -1) > prior_level)

                if (is_new or level_rose) and self._event_bus and hasattr(adapter, "to_event"):
                    evt["_is_update"] = level_rose   # signal to to_event()
                    self._emit_event(adapter, evt)

                self._events[key] = evt   # always update stored state
        else:
            for evt in adapter.get_events():
                key = (evt["source"], evt["event_id"])
                is_new = key not in self._events
                self._events[key] = evt
                if is_new and self._event_bus and hasattr(adapter, "to_event"):
                    self._emit_event(adapter, evt)

    def _emit_event(self, adapter, raw_evt: dict):
        """Convert raw event to pipeline Event and emit to bus."""
        try:
            event = adapter.to_event(raw_evt)
            if event is None:
                return  # adapter declined to emit (non-actionable reading)
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

    def get_source_health(self) -> list:
        """Get health status for all adapters."""
        return [a.health_status for a in self._adapters.values()]
