"""NASA FIRMS satellite fire hotspot adapter."""

import json
import logging
import time
from typing import TYPE_CHECKING, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from meshai.notifications.events import Event, make_event

if TYPE_CHECKING:
    from ..config import FIRMSConfig

logger = logging.getLogger(__name__)


class FIRMSAdapter:
    """NASA FIRMS satellite fire hotspot polling.

    Detects fire hotspots from satellite data (MODIS, VIIRS) typically
    hours before NIFC publishes official perimeters. Early warning.

    API: https://firms.modaps.eosdis.nasa.gov/api/area/csv/{MAP_KEY}/{SOURCE}/{BBOX}/{DAY_RANGE}
    """

    BASE_URL = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"

    def __init__(self, config: "FIRMSConfig", region_anchors: list = None, fires_adapter=None):
        self._map_key = config.map_key
        self._source = config.source or "VIIRS_SNPP_NRT"
        self._bbox = config.bbox  # [west, south, east, north]
        self._day_range = config.day_range or 1
        self._tick_interval = config.tick_seconds or 1800
        self._confidence_min = config.confidence_min or "nominal"
        self._proximity_km = config.proximity_km or 10.0  # km to match known fire

        self._last_tick = 0.0
        self._events = []
        self._consecutive_errors = 0
        self._last_error = None
        self._is_loaded = False

        # For cross-referencing
        self._region_anchors = region_anchors or []
        self._fires_adapter = fires_adapter  # NICFFiresAdapter for cross-ref

    def tick(self) -> bool:
        """Execute one polling tick.

        Returns:
            True if data changed
        """
        now = time.time()

        if now - self._last_tick < self._tick_interval:
            return False

        self._last_tick = now

        if not self._map_key:
            if not self._last_error:
                logger.warning("FIRMS: No MAP_KEY configured, skipping")
                self._last_error = "No MAP_KEY configured"
            return False

        if not self._bbox or len(self._bbox) != 4:
            if not self._last_error:
                logger.warning("FIRMS: No valid bbox configured, skipping")
                self._last_error = "No valid bbox configured"
            return False

        return self._fetch()

    def _fetch(self) -> bool:
        """Fetch fire hotspots from NASA FIRMS.

        Returns:
            True if data changed
        """
        # Format bbox as west,south,east,north
        bbox_str = ",".join(str(c) for c in self._bbox)

        url = f"{self.BASE_URL}/{self._map_key}/{self._source}/{bbox_str}/{self._day_range}"

        headers = {
            "User-Agent": "MeshAI/1.0",
            "Accept": "text/csv",
        }

        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=30) as resp:
                csv_data = resp.read().decode("utf-8")

        except HTTPError as e:
            if e.code == 401:
                logger.error("FIRMS: Invalid MAP_KEY, disabling adapter")
                self._last_error = "Invalid MAP_KEY"
                self._consecutive_errors = 999  # Disable
                return False
            logger.warning(f"FIRMS HTTP error: {e.code}")
            self._last_error = f"HTTP {e.code}"
            self._consecutive_errors += 1
            return False

        except URLError as e:
            logger.warning(f"FIRMS connection error: {e.reason}")
            self._last_error = str(e.reason)
            self._consecutive_errors += 1
            return False

        except Exception as e:
            logger.warning(f"FIRMS fetch error: {e}")
            self._last_error = str(e)
            self._consecutive_errors += 1
            return False

        # Parse CSV response
        new_events = self._parse_csv(csv_data)

        # Check if data changed
        old_ids = {e["event_id"] for e in self._events}
        new_ids = {e["event_id"] for e in new_events}
        changed = old_ids != new_ids

        self._events = new_events
        self._consecutive_errors = 0
        self._last_error = None
        self._is_loaded = True

        if changed:
            new_ignitions = sum(1 for e in new_events if e.get("properties", {}).get("new_ignition"))
            logger.info(f"FIRMS hotspots updated: {len(new_events)} total, {new_ignitions} potential new ignitions")

        return changed

    def _parse_csv(self, csv_data: str) -> list:
        """Parse FIRMS CSV response into events."""
        lines = csv_data.strip().split("\n")
        if len(lines) < 2:
            return []

        # Parse header
        header = lines[0].split(",")
        header_map = {col.strip().lower(): i for i, col in enumerate(header)}

        # Required columns
        lat_idx = header_map.get("latitude")
        lon_idx = header_map.get("longitude")
        conf_idx = header_map.get("confidence")
        frp_idx = header_map.get("frp")  # Fire Radiative Power
        acq_date_idx = header_map.get("acq_date")
        acq_time_idx = header_map.get("acq_time")
        bright_idx = header_map.get("bright_ti4") or header_map.get("brightness")

        if lat_idx is None or lon_idx is None:
            logger.warning("FIRMS CSV missing required columns")
            return []

        events = []
        now = time.time()

        # Confidence mapping
        conf_values = {"low": 1, "l": 1, "nominal": 2, "n": 2, "high": 3, "h": 3}
        min_conf = conf_values.get(self._confidence_min.lower(), 2)

        # Get known fire locations for cross-referencing
        known_fires = self._get_known_fires()

        for line in lines[1:]:
            cols = line.split(",")
            if len(cols) < max(filter(None, [lat_idx, lon_idx, conf_idx])) + 1:
                continue

            try:
                lat = float(cols[lat_idx])
                lon = float(cols[lon_idx])
            except (ValueError, IndexError):
                continue

            # Parse confidence
            conf_raw = cols[conf_idx].strip() if conf_idx is not None and conf_idx < len(cols) else "n"
            conf_value = conf_values.get(conf_raw.lower(), 2)

            # Filter by confidence
            if conf_value < min_conf:
                continue

            # Parse FRP (fire radiative power in MW)
            frp = None
            if frp_idx is not None and frp_idx < len(cols):
                try:
                    frp = float(cols[frp_idx])
                except ValueError:
                    pass

            # Parse brightness temperature
            brightness = None
            if bright_idx is not None and bright_idx < len(cols):
                try:
                    brightness = float(cols[bright_idx])
                except ValueError:
                    pass

            # Parse acquisition datetime
            acq_date = cols[acq_date_idx].strip() if acq_date_idx is not None and acq_date_idx < len(cols) else ""
            acq_time = cols[acq_time_idx].strip() if acq_time_idx is not None and acq_time_idx < len(cols) else ""

            # Create unique ID from position and time
            event_id = f"firms_{lat:.4f}_{lon:.4f}_{acq_date}_{acq_time}"

            # Check if near known fire
            near_fire, fire_name, distance_to_fire = self._check_near_known_fire(lat, lon, known_fires)

            # Determine severity
            if not near_fire:
                # Potential new ignition
                severity = "routine"
                new_ignition = True
                headline = f"NEW HOTSPOT detected"
            else:
                # Near known fire
                severity = "routine"
                new_ignition = False
                headline = f"Hotspot near {fire_name}"

            # Bump severity for high FRP
            if frp is not None and frp > 100:
                if severity == "routine":
                    severity = "routine"
                elif severity == "routine":
                    severity = "priority"
                headline += f" ({int(frp)} MW)"

            # Compute proximity to region anchors
            distance_km, nearest_anchor = self._nearest_anchor_distance(lat, lon)

            if distance_km is not None and nearest_anchor:
                headline += f" ({int(distance_km)} km from {nearest_anchor})"

            event = {
                "source": "firms",
                "event_id": event_id,
                "event_type": "Fire Hotspot",
                "severity": severity,
                "headline": headline,
                "lat": lat,
                "lon": lon,
                "expires": now + 21600,  # 6 hour TTL
                "fetched_at": now,
                "properties": {
                    "new_ignition": new_ignition,
                    "confidence": conf_raw,
                    "frp": frp,
                    "brightness": brightness,
                    "acq_date": acq_date,
                    "acq_time": acq_time,
                    "near_fire": fire_name if near_fire else None,
                    "distance_to_fire_km": distance_to_fire,
                    "distance_km": distance_km,
                    "nearest_anchor": nearest_anchor,
                },
            }

            events.append(event)

        return events

    def _get_known_fires(self) -> list:
        """Get known fire locations from NIFC adapter."""
        if not self._fires_adapter:
            return []

        fires = self._fires_adapter.get_events()
        return [
            {
                "name": f.get("name", "Unknown"),
                "lat": f.get("lat"),
                "lon": f.get("lon"),
            }
            for f in fires
            if f.get("lat") is not None and f.get("lon") is not None
        ]

    def _check_near_known_fire(self, lat: float, lon: float, known_fires: list) -> tuple:
        """Check if hotspot is near a known fire.

        Returns:
            (is_near, fire_name, distance_km)
        """
        if not known_fires:
            return (False, None, None)

        from ..geo import haversine_distance

        for fire in known_fires:
            fire_lat = fire.get("lat")
            fire_lon = fire.get("lon")
            if fire_lat is None or fire_lon is None:
                continue

            # haversine_distance returns miles, convert to km
            dist_miles = haversine_distance(lat, lon, fire_lat, fire_lon)
            dist_km = dist_miles * 1.60934

            if dist_km <= self._proximity_km:
                return (True, fire.get("name"), dist_km)

        return (False, None, None)

    def _nearest_anchor_distance(self, lat: float, lon: float) -> tuple:
        """Find distance to nearest region anchor.

        Returns:
            (distance_km, anchor_name) or (None, None)
        """
        if not self._region_anchors:
            return (None, None)

        from ..geo import haversine_distance

        min_dist = float("inf")
        nearest_name = None

        for anchor in self._region_anchors:
            anchor_lat = anchor.get("lat") if isinstance(anchor, dict) else getattr(anchor, "lat", None)
            anchor_lon = anchor.get("lon") if isinstance(anchor, dict) else getattr(anchor, "lon", None)
            anchor_name = anchor.get("name") if isinstance(anchor, dict) else getattr(anchor, "name", "Unknown")

            if anchor_lat is None or anchor_lon is None:
                continue

            # haversine_distance returns miles, convert to km
            dist_miles = haversine_distance(lat, lon, anchor_lat, anchor_lon)
            dist_km = dist_miles * 1.60934

            if dist_km < min_dist:
                min_dist = dist_km
                nearest_name = anchor_name

        if min_dist < float("inf"):
            return (min_dist, nearest_name)

        return (None, None)

    def to_event(self, evt: dict) -> Optional["Event"]:
        """Translate a stored FIRMS event dict into a pipeline Event.

        Args:
            evt: Internal event dict from get_events()

        Returns:
            Event instance ready for EventBus emission, or None if
            the dict is missing required fields (lat/lon).
        """
        try:
            lat = evt.get("lat")
            lon = evt.get("lon")
            if lat is None or lon is None:
                return None  # Can't make a useful Event without coords

            props = evt.get("properties", {}) or {}
            is_new_ignition = bool(props.get("new_ignition", False))
            category = "new_ignition" if is_new_ignition else "wildfire_proximity"

            severity = evt.get("severity", "routine")

            title = evt.get("headline", "") or "Fire Hotspot"

            # Build a richer summary including FRP, confidence, distance
            summary_parts = [title]
            if props.get("frp") is not None:
                summary_parts.append(f"FRP {int(props['frp'])} MW")
            if props.get("confidence"):
                summary_parts.append(f"conf {props['confidence']}")
            if props.get("distance_km") is not None and props.get("nearest_anchor"):
                summary_parts.append(
                    f"{int(props['distance_km'])} km from {props['nearest_anchor']}"
                )
            summary = " | ".join(summary_parts)[:300]

            spatial_key = f"firms:{round(lat, 2):.2f}:{round(lon, 2):.2f}"

            return make_event(
                source="firms",
                category=category,
                severity=severity,
                title=title,
                summary=summary,
                timestamp=evt.get("fetched_at"),
                expires=evt.get("expires"),
                region=props.get("nearest_anchor"),
                lat=lat,
                lon=lon,
                group_key=spatial_key,
                inhibit_keys=[spatial_key],
            )
        except Exception:
            logger.exception(f"FIRMS to_event failed for evt: {evt.get('event_id')}")
            return None

    def get_events(self) -> list:
        """Get current hotspot events."""
        return self._events

    def get_new_ignitions(self) -> list:
        """Get only potential new ignitions (not near known fires)."""
        return [e for e in self._events if e.get("properties", {}).get("new_ignition")]

    @property
    def health_status(self) -> dict:
        """Get adapter health status."""
        new_ignitions = len(self.get_new_ignitions())
        return {
            "source": "firms",
            "is_loaded": self._is_loaded,
            "last_error": str(self._last_error) if self._last_error else None,
            "consecutive_errors": self._consecutive_errors,
            "event_count": len(self._events),
            "new_ignitions": new_ignitions,
            "last_fetch": self._last_tick,
        }
