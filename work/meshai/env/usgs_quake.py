"""USGS earthquake feed adapter (keyless, open API).

Phase 2.14 -- first net-new environmental adapter. Polls a USGS earthquake
GeoJSON summary feed, filters by magnitude and a geographic bounding box, and
emits one Event per qualifying quake. Standalone path (Rule 16); Central will
be the dual-source in v0.4.
"""

import json
import logging
import time
from typing import TYPE_CHECKING, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from meshai.notifications.events import Event, make_event

if TYPE_CHECKING:
    from ..config import USGSQuakeConfig

logger = logging.getLogger(__name__)


class USGSQuakeAdapter:
    """USGS earthquake GeoJSON feed polling.

    Feed format (FeatureCollection): each feature has a stable USGS id (e.g.
    "us6000abcd"), properties.mag / place / title / sig / time(ms), and
    geometry.coordinates [lon, lat, depth_km].
    """

    def __init__(self, config: "USGSQuakeConfig"):
        self._feed_url = config.feed_url
        self._min_magnitude = config.min_magnitude
        self._bbox = config.bbox or []  # [west, south, east, north]
        self._region = config.region or "magic_valley"
        self._tick_interval = config.tick_seconds or 300
        self._last_tick = 0.0
        self._events = []
        self._consecutive_errors = 0
        self._last_error = None
        self._is_loaded = False

    def tick(self) -> bool:
        """Execute one polling tick.

        Returns:
            True if data changed
        """
        now = time.time()
        if now - self._last_tick < self._tick_interval:
            return False
        self._last_tick = now
        return self._fetch()

    def _in_bbox(self, lat, lon) -> bool:
        """True if (lat, lon) is inside the configured bbox (or no bbox set)."""
        if not self._bbox or len(self._bbox) != 4:
            return True  # no geographic filter configured -> accept all
        west, south, east, north = self._bbox
        return (south <= lat <= north) and (west <= lon <= east)

    def _severity_for_mag(self, mag) -> str:
        """Magnitude -> severity bins: M<3.5 routine, 3.5-5 priority, >=5 immediate."""
        if mag is None:
            return "routine"
        if mag >= 5.0:
            return "immediate"
        if mag >= 3.5:
            return "priority"
        return "routine"

    def _fetch(self) -> bool:
        """Fetch the USGS earthquake GeoJSON feed and filter.

        Returns:
            True if the set of qualifying quake ids changed
        """
        headers = {"User-Agent": "MeshAI/1.0", "Accept": "application/json"}

        try:
            req = Request(self._feed_url, headers=headers)
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))

        except HTTPError as e:
            logger.warning(f"USGS quake HTTP error: {e.code}")
            self._last_error = f"HTTP {e.code}"
            self._consecutive_errors += 1
            return False

        except URLError as e:
            logger.warning(f"USGS quake connection error: {e.reason}")
            self._last_error = str(e.reason)
            self._consecutive_errors += 1
            return False

        except Exception as e:
            logger.warning(f"USGS quake fetch error: {e}")
            self._last_error = str(e)
            self._consecutive_errors += 1
            return False

        features = data.get("features", [])
        new_events = []
        now = time.time()

        for feature in features:
            try:
                props = feature.get("properties", {}) or {}
                geom = feature.get("geometry") or {}
                quake_id = feature.get("id")
                mag = props.get("mag")
                coords = geom.get("coordinates") or []

                # Filter: need id, magnitude, coords; magnitude threshold; bbox.
                if quake_id is None or mag is None or len(coords) < 2:
                    continue
                if mag < self._min_magnitude:
                    continue

                lon = coords[0]
                lat = coords[1]
                depth_km = coords[2] if len(coords) > 2 else None

                if lat is None or lon is None:
                    continue
                if not self._in_bbox(lat, lon):
                    continue

                place = props.get("place") or "Unknown location"
                title = props.get("title") or f"M{mag} - {place}"
                quake_time = props.get("time")  # epoch ms
                ts = quake_time / 1000.0 if quake_time else now

                new_events.append({
                    "source": "usgs_quake",
                    "event_id": quake_id,  # stable USGS id, e.g. "us6000abcd"
                    "event_type": "Earthquake",
                    "severity": self._severity_for_mag(mag),
                    "headline": title,
                    "magnitude": mag,
                    "place": place,
                    "depth_km": depth_km,
                    "sig": props.get("sig"),
                    "url": props.get("url"),
                    "region": self._region,
                    "lat": lat,
                    "lon": lon,
                    "quake_time": ts,
                    "expires": now + 86400,  # 24h TTL (matches the day-feed window)
                    "fetched_at": now,
                })
            except Exception:
                logger.exception("USGS quake feature parse error")
                continue

        old_ids = {e["event_id"] for e in self._events}
        new_ids = {e["event_id"] for e in new_events}
        changed = old_ids != new_ids

        self._events = new_events
        self._consecutive_errors = 0
        self._last_error = None
        self._is_loaded = True

        if changed:
            logger.info(f"USGS quakes updated: {len(new_events)} in region {self._region}")

        return changed

    def to_event(self, evt: dict) -> Optional["Event"]:
        """Translate a stored quake dict into a pipeline Event.

        Category is always earthquake_event; magnitude-binned severity is
        passed through. The stable USGS id is the group_key and sole
        inhibit_key.

        Args:
            evt: Internal event dict from get_events()

        Returns:
            Event instance, or None if the dict is missing its id, coords, or
            magnitude.
        """
        try:
            event_id = evt.get("event_id")
            if not event_id:
                return None

            lat = evt.get("lat")
            lon = evt.get("lon")
            if lat is None or lon is None:
                return None

            mag = evt.get("magnitude")
            if mag is None:
                return None

            severity = evt.get("severity", "routine")
            title = evt.get("headline") or f"M{mag} earthquake"

            summary_parts = [title]
            depth = evt.get("depth_km")
            if depth is not None:
                summary_parts.append(f"depth {round(depth, 1)} km")
            summary = " | ".join(summary_parts)[:300]

            return make_event(
                source="usgs_quake",
                category="earthquake_event",
                severity=severity,
                title=title,
                summary=summary,
                timestamp=evt.get("quake_time") or evt.get("fetched_at"),
                expires=evt.get("expires"),
                lat=lat,
                lon=lon,
                region=evt.get("region"),
                group_key=event_id,
                inhibit_keys=[event_id],
            )
        except Exception:
            logger.exception(f"USGS quake to_event failed for evt: {evt.get('event_id')}")
            return None

    def get_events(self) -> list:
        """Get current qualifying quake events."""
        return self._events

    @property
    def health_status(self) -> dict:
        """Get adapter health status."""
        return {
            "source": "usgs_quake",
            "is_loaded": self._is_loaded,
            "last_error": str(self._last_error) if self._last_error else None,
            "consecutive_errors": self._consecutive_errors,
            "event_count": len(self._events),
            "last_fetch": self._last_tick,
        }
