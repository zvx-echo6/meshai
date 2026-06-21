"""Danger-zone correlator (v0.8).

Subscribes to the notification EventBus and, for hazard events with a
location, flags infrastructure nodes that fall inside the hazard's threat
radius and (optionally) alerts the operator. Distance math is MILES
end-to-end (haversine_distance returns miles).

EventBus handlers are SYNC; async delivery is fire-and-forget via
asyncio.create_task guarded by try/except RuntimeError (mirrors the
pipeline tee in notifications/pipeline/__init__.py).

Ships gated: cfg.enabled defaults False; cfg.dry_run defaults True. dry_run
is provably send-free — it logs and returns BEFORE any channel is built.
"""

import asyncio
import logging
import time
from typing import Optional

from meshai.geo import haversine_distance
from meshai.notifications import categories
from meshai.notifications.events import make_event, make_payload_from_event
from meshai.notifications import channels
from meshai.config import NotificationRuleConfig

logger = logging.getLogger(__name__)

# NWS event_type substrings that indicate a winter/snow hazard.
_SNOW_MARKERS = ("snow", "winter", "blizzard", "ice storm", "freezing")


class DangerZoneCorrelator:
    """Correlates located hazard events against infrastructure nodes."""

    def __init__(self, config, data_store, connector):
        self.config = config
        self.data_store = data_store
        self.connector = connector
        # (node_num, family) -> last alert epoch
        self._last: dict[tuple[int, str], float] = {}

    def _resolve_family(self, event) -> Optional[str]:
        """Return the danger_zones sub-config name for this event, or None."""
        fam = categories.get_toggle(event.category)
        if fam not in ("fire", "weather", "avalanche", "seismic"):
            return None
        if fam == "weather":
            et = (event.data.get("event_type") or event.title or "").lower()
            if any(m in et for m in _SNOW_MARKERS):
                return "snow"
            return "weather"
        if fam == "seismic":
            if str(event.category).startswith("stream"):
                return "flood"
            return "seismic"
        return fam  # fire | avalanche

    def _fire_radius_and_centroid(self, event):
        """For fire events, look up spread_radius_mi + centroid from the fires
        table by irwin_id (= event.group_key). Returns (radius_mi, lat, lon)
        with lat/lon falling back to the event's own coords. Non-fire -> (0, None, None)."""
        irwin_id = event.group_key
        if not irwin_id:
            return 0.0, event.lat, event.lon
        from meshai.persistence import get_db
        try:
            row = get_db().execute(
                "SELECT spread_radius_mi, current_centroid_lat, current_centroid_lon, "
                "lat, lon, current_acres FROM fires WHERE irwin_id = ?",
                (irwin_id,),
            ).fetchone()
        except Exception:
            logger.exception("danger_zone: fires lookup failed for %s", irwin_id)
            return 0.0, event.lat, event.lon
        if row is None:
            return 0.0, event.lat, event.lon
        radius = row["spread_radius_mi"] if row["spread_radius_mi"] is not None else 0.0
        haz_lat = row["current_centroid_lat"] if row["current_centroid_lat"] is not None else (
            row["lat"] if row["lat"] is not None else event.lat)
        haz_lon = row["current_centroid_lon"] if row["current_centroid_lon"] is not None else (
            row["lon"] if row["lon"] is not None else event.lon)
        return float(radius), haz_lat, haz_lon

    def _fire_acres(self, event) -> Optional[float]:
        """Acres for a fire event: prefer the fires DB row, fall back to event.data."""
        irwin_id = event.group_key
        if irwin_id:
            from meshai.persistence import get_db
            try:
                row = get_db().execute(
                    "SELECT current_acres FROM fires WHERE irwin_id = ?",
                    (irwin_id,),
                ).fetchone()
                if row is not None and row["current_acres"] is not None:
                    return float(row["current_acres"])
            except Exception:
                pass
        v = event.data.get("acres")
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def handle(self, event) -> None:
        """Sync EventBus handler. Never raises (bus isolates, but be safe)."""
        try:
            self._handle(event)
        except Exception:
            logger.exception("danger_zone correlator failed on event %s", getattr(event, "category", "?"))

    def _handle(self, event) -> None:
        cfg = self.config.danger_zones
        if not cfg.enabled:
            return
        if self.data_store is None:
            return

        # Active hazards only.
        if event.category == "wildfire_closed":
            return

        fam = self._resolve_family(event)
        if fam is None:
            return
        sub = getattr(cfg, fam, None)
        if sub is None or not sub.enabled:
            return

        # Snow is tabled pending a snowfall + elevation pipeline: a generic
        # winter-weather warning over a whole zone is not a per-node hazard
        # without elevation/snowfall modeling. Skip snow entirely; general
        # (non-snow) weather still correlates. (snow config kept for schema/GUI.)
        if fam == "snow":
            return

        # Fire radius + acres; non-fire is a point hazard at the event location.
        if fam == "fire":
            radius_mi, haz_lat, haz_lon = self._fire_radius_and_centroid(event)
            min_acres = sub.min_acres or 0.0
            if min_acres > 0:
                acres = self._fire_acres(event)
                if acres is None or acres < min_acres:
                    return
        else:
            radius_mi, haz_lat, haz_lon = 0.0, event.lat, event.lon

        if haz_lat is None or haz_lon is None:
            return  # cannot correlate without a location

        effective_mi = radius_mi + (sub.buffer_mi or cfg.default_buffer_mi)

        # Snapshot nodes BEFORE any async scheduling.
        try:
            nodes = self.data_store.get_nodes_by_roles(set(cfg.monitor_roles))
        except Exception:
            logger.exception("danger_zone: get_nodes_by_roles failed")
            return

        now = time.time()
        cooldown_s = cfg.cooldown_minutes * 60
        hazard_title = event.title or event.summary or event.category

        for node in nodes:
            if node.latitude is None or node.longitude is None:
                continue
            dist_mi = haversine_distance(haz_lat, haz_lon, node.latitude, node.longitude)
            if dist_mi > effective_mi:
                continue

            key = (node.node_num, fam)
            last = self._last.get(key)
            if last is not None and (now - last) < cooldown_s:
                continue

            summary = (
                f"DANGER ZONE: {node.short_name or node.node_id_hex} "
                f"({node.role}) {dist_mi:.1f} mi from {hazard_title}"
            )
            ev = make_event(
                source="danger_zone",
                category="infra_danger_zone",
                severity="priority",
                title=f"Infra node in danger zone: {node.short_name or node.node_id_hex}",
                summary=summary,
                lat=node.latitude,
                lon=node.longitude,
            )

            if cfg.dry_run:
                logger.info(
                    "[danger_zone dry-run] would alert %s (%s) %.1f mi from %s",
                    node.short_name or node.node_id_hex, node.role, dist_mi, hazard_title,
                )
                self._last[key] = now
                continue

            if cfg.delivery_type == "none":
                self._last[key] = now
                continue

            rule = NotificationRuleConfig(
                name="danger_zone",
                delivery_type=cfg.delivery_type,
                broadcast_channel=cfg.broadcast_channel or 0,
                node_ids=list(cfg.node_ids),
                webhook_url=cfg.webhook_url,
                webhook_headers=dict(cfg.webhook_headers),
            )
            try:
                ch = channels.create_channel(rule, self.connector)
            except Exception:
                logger.exception("danger_zone: create_channel failed")
                continue

            try:
                asyncio.create_task(ch.deliver(make_payload_from_event(ev), rule))
                self._last[key] = now
            except RuntimeError:
                logger.warning(
                    "danger_zone: no running event loop; skipping async deliver for %s",
                    node.short_name or node.node_id_hex,
                )
