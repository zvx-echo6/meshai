"""NIFC/WFIGS Wildfire perimeter adapter."""

import json
import logging
import time
from typing import TYPE_CHECKING
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlencode

if TYPE_CHECKING:
    from ..config import NICFFiresConfig

logger = logging.getLogger(__name__)


class NICFFiresAdapter:
    """WFIGS ArcGIS fire perimeter polling."""

    BASE_URL = "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/WFIGS_Interagency_Perimeters_Current/FeatureServer/0/query"

    def __init__(self, config: "NICFFiresConfig", region_anchors: list = None):
        self._state = config.state
        self._tick_interval = config.tick_seconds or 600
        self._last_tick = 0.0
        self._events = []
        self._consecutive_errors = 0
        self._last_error = None
        self._is_loaded = False
        # Region anchors for proximity calculation
        self._region_anchors = region_anchors or []

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

    def _fetch(self) -> bool:
        """Fetch fire perimeters from WFIGS.

        Returns:
            True if data changed
        """
        params = {
            "where": f"attr_POOState='{self._state}' AND attr_IncidentTypeCategory='WF'",
            "outFields": "attr_IncidentName,attr_IncidentSize,attr_PercentContained,attr_FireDiscoveryDateTime,attr_POOState,poly_GISAcres",
            "returnGeometry": "true",
            "f": "geojson",
        }

        url = f"{self.BASE_URL}?{urlencode(params)}"

        headers = {
            "User-Agent": "MeshAI/1.0",
            "Accept": "application/json",
        }

        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))

        except HTTPError as e:
            logger.warning(f"NIFC HTTP error: {e.code}")
            self._last_error = f"HTTP {e.code}"
            self._consecutive_errors += 1
            return False

        except URLError as e:
            logger.warning(f"NIFC connection error: {e.reason}")
            self._last_error = str(e.reason)
            self._consecutive_errors += 1
            return False

        except Exception as e:
            logger.warning(f"NIFC fetch error: {e}")
            self._last_error = str(e)
            self._consecutive_errors += 1
            return False

        # Parse response
        features = data.get("features", [])
        new_events = []
        now = time.time()

        for feature in features:
            props = feature.get("properties", {})
            geom = feature.get("geometry")

            name = props.get("attr_IncidentName", "Unknown Fire")
            acres = props.get("attr_IncidentSize") or props.get("poly_GISAcres") or 0
            pct_contained = props.get("attr_PercentContained") or 0

            # Compute centroid from polygon
            lat, lon = self._compute_centroid(geom)

            # Compute proximity to nearest anchor
            distance_km, nearest_anchor = self._nearest_anchor_distance(lat, lon)

            # Severity based on distance
            if distance_km is not None:
                if distance_km < 25:
                    severity = "warning"
                elif distance_km < 50:
                    severity = "watch"
                else:
                    severity = "advisory"
            else:
                severity = "advisory"

            # Format headline
            headline = f"{name} -- {int(acres):,} ac, {int(pct_contained)}% contained"
            if distance_km is not None and nearest_anchor:
                headline += f" ({int(distance_km)} km from {nearest_anchor})"

            event = {
                "source": "nifc",
                "event_id": f"nifc_{name.replace(' ', '_').lower()}_{self._state}",
                "event_type": "Wildfire",
                "severity": severity,
                "headline": headline,
                "name": name,
                "acres": acres,
                "pct_contained": pct_contained,
                "lat": lat,
                "lon": lon,
                "distance_km": distance_km,
                "nearest_anchor": nearest_anchor,
                "state": self._state,
                "expires": now + 21600,  # 6 hour TTL
                "fetched_at": now,
            }

            # Store polygon for map overlay
            if geom and geom.get("type") == "Polygon":
                event["polygon"] = geom.get("coordinates", [])

            new_events.append(event)

        # Check if data changed
        old_ids = {e["event_id"] for e in self._events}
        new_ids = {e["event_id"] for e in new_events}
        changed = old_ids != new_ids

        self._events = new_events
        self._consecutive_errors = 0
        self._last_error = None
        self._is_loaded = True

        if changed:
            logger.info(f"NIFC fires updated: {len(new_events)} active in {self._state}")

        return changed

    def _compute_centroid(self, geom) -> tuple:
        """Compute centroid from GeoJSON geometry."""
        if not geom:
            return (None, None)

        try:
            coords = geom.get("coordinates", [])
            geom_type = geom.get("type")

            if geom_type == "Polygon" and coords:
                # Use first ring
                ring = coords[0]
                if ring:
                    lat_sum = sum(c[1] for c in ring)
                    lon_sum = sum(c[0] for c in ring)
                    return (lat_sum / len(ring), lon_sum / len(ring))

            elif geom_type == "MultiPolygon" and coords:
                # Average all polygon centroids
                all_lats = []
                all_lons = []
                for polygon in coords:
                    if polygon:
                        ring = polygon[0]
                        if ring:
                            all_lats.append(sum(c[1] for c in ring) / len(ring))
                            all_lons.append(sum(c[0] for c in ring) / len(ring))
                if all_lats and all_lons:
                    return (sum(all_lats) / len(all_lats), sum(all_lons) / len(all_lons))

        except Exception:
            pass

        return (None, None)

    def _nearest_anchor_distance(self, lat, lon) -> tuple:
        """Find distance to nearest region anchor.

        Returns:
            (distance_km, anchor_name) or (None, None)
        """
        if lat is None or lon is None or not self._region_anchors:
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

    def get_events(self) -> list:
        """Get current fire events."""
        return self._events

    @property
    def health_status(self) -> dict:
        """Get adapter health status."""
        return {
            "source": "nifc",
            "is_loaded": self._is_loaded,
            "last_error": str(self._last_error) if self._last_error else None,
            "consecutive_errors": self._consecutive_errors,
            "event_count": len(self._events),
            "last_fetch": self._last_tick,
        }
