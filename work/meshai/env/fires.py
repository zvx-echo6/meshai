"""NIFC/WFIGS Wildfire perimeter adapter."""

import json
import logging
import time
from typing import TYPE_CHECKING, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlencode

from meshai.notifications.events import Event, make_event

if TYPE_CHECKING:
    from ..config import NICFFiresConfig

logger = logging.getLogger(__name__)


class NICFFiresAdapter:
    """WFIGS ArcGIS fire perimeter polling."""

    BASE_URL = "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/WFIGS_Interagency_Perimeters_Current/FeatureServer/0/query"

    def __init__(self, config: "NICFFiresConfig", region_anchors: list = None, coverage: dict = None):
        self._state = config.state
        self._tick_interval = config.tick_seconds or 600
        self._last_tick = 0.0
        self._events = []
        self._consecutive_errors = 0
        self._last_error = None
        self._is_loaded = False
        # Region anchors for proximity calculation
        self._region_anchors = region_anchors or []
        # Coverage bbox — when set, drives spatial filter instead of single-state WHERE
        self._coverage = coverage

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

    def _build_query_params(self) -> dict:
        """Build WFIGS ArcGIS query parameters.

        When self._coverage is set, switches to an envelope spatial filter spanning
        the full coverage bbox (which may cross multiple states); the single-state
        attr_POOState WHERE clause is dropped in favour of the geometry filter.
        When self._coverage is None, falls back to the original single-state WHERE.
        """
        out_fields = (
            "attr_IncidentName,attr_IncidentSize,attr_PercentContained,"
            "attr_FireDiscoveryDateTime,attr_POOState,poly_GISAcres,"
            "attr_IrwinID,attr_UniqueFireIdentifier"
        )
        if self._coverage is not None:
            params = {
                "where": "attr_IncidentTypeCategory='WF'",
                "outFields": out_fields,
                "returnGeometry": "true",
                "f": "geojson",
            }
            # Merge the ArcGIS envelope keys from the coverage dict
            params.update(self._coverage["envelope"])
        else:
            params = {
                "where": f"attr_POOState='{self._state}' AND attr_IncidentTypeCategory='WF'",
                "outFields": out_fields,
                "returnGeometry": "true",
                "f": "geojson",
            }
        return params

    def _fetch(self) -> bool:
        """Fetch fire perimeters from WFIGS.

        Returns:
            True if data changed
        """
        params = self._build_query_params()

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
                    severity = "priority"
                elif distance_km < 50:
                    severity = "routine"
                else:
                    severity = "routine"
            else:
                severity = "routine"

            # Format headline
            headline = f"{name} -- {int(acres):,} ac, {int(pct_contained)}% contained"
            if distance_km is not None and nearest_anchor:
                headline += f" ({int(distance_km)} km from {nearest_anchor})"

            # In coverage mode fires can span multiple states, so use the fire's own
            # POOState for the event_id suffix. This keeps Idaho fire keys identical to
            # the single-state path (attr_POOState="US-ID" == self._state for Idaho).
            # Fall back to self._state when the field is absent or blank.
            if self._coverage is not None:
                event_id_state = (props.get("attr_POOState") or "").strip() or self._state
            else:
                event_id_state = self._state

            event_id = f"nifc_{name.replace(' ', '_').lower()}_{event_id_state}"

            # Canonical WFIGS identity + discovery date the Phase-3 fire decider
            # (notifications/gating/fire.py::decide) reads off Event.data. Prefer
            # the real IRWIN GUID; fall back to the unique fire id, then to the
            # stable adapter event_id so a fire is always gate-able by the fires
            # table even when WFIGS omits the id fields.
            irwin_id = (
                props.get("attr_IrwinID")
                or props.get("attr_UniqueFireIdentifier")
                or event_id
            )
            declared_at_epoch = self._parse_discovery_epoch(
                props.get("attr_FireDiscoveryDateTime"))

            event = {
                "source": "nifc",
                "event_id": event_id,
                "event_type": "Wildfire",
                "severity": severity,
                "headline": headline,
                "name": name,
                "acres": acres,
                "pct_contained": pct_contained,
                # Canonical keys the decider consumes (mirrored into Event.data
                # by to_event) + reused by store cold-start seeding.
                "irwin_id": irwin_id,
                "contained_pct": pct_contained,
                "declared_at_epoch": declared_at_epoch,
                "county": None,  # WFIGS perimeter layer carries no county field
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
            loc = "the coverage area" if self._coverage is not None else self._state
            logger.info(f"NIFC fires updated: {len(new_events)} active in {loc}")

        return changed

    @staticmethod
    def _parse_discovery_epoch(raw) -> Optional[int]:
        """WFIGS FireDiscoveryDateTime (epoch MILLISECONDS) -> epoch seconds.

        Guards None/blank/garbage; WFIGS reports ms since epoch, so values with
        13+ digits are divided by 1000. Returns None when unparseable so the
        decider's age-gate fails OPEN (announces) exactly as for a dateless fire.
        """
        if raw is None or raw == "":
            return None
        try:
            val = int(float(raw))
        except (TypeError, ValueError):
            return None
        if val <= 0:
            return None
        # >= ~ year 2001 in ms (1e12) -> treat as milliseconds.
        if val >= 1_000_000_000_000:
            val //= 1000
        return val

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

    def to_event(self, evt: dict) -> Optional["Event"]:
        """Translate a stored NIFC/WFIGS fire perimeter into a pipeline Event.

        Every active perimeter with a reported size maps to a single
        wildfire_incident category; the adapter's proximity-based severity
        (priority when near a region anchor, else routine) is passed through
        unchanged. Severity tiering is delegated to the pipeline Inhibitor.

        Args:
            evt: Internal event dict from get_events()

        Returns:
            Event instance ready for EventBus emission, or None if the dict is
            missing its centroid, event_id, or a reported acreage.
        """
        try:
            lat = evt.get("lat")
            lon = evt.get("lon")
            if lat is None or lon is None:
                return None  # No centroid -- can't make a useful Event

            event_id = evt.get("event_id")
            if not event_id:
                return None  # No stable identity to group/inhibit on

            acres = evt.get("acres")
            if not acres:
                return None  # No reported size -- low-signal, do not emit

            severity = evt.get("severity", "routine")
            name = evt.get("name") or "Wildfire"

            # Summary: size, containment, and proximity to nearest anchor.
            summary_parts = [name]
            try:
                summary_parts.append(f"{int(acres):,} ac")
            except (TypeError, ValueError):
                pass
            pct = evt.get("pct_contained")
            if pct is not None:
                try:
                    summary_parts.append(f"{int(pct)}% contained")
                except (TypeError, ValueError):
                    pass
            anchor = evt.get("nearest_anchor")
            dist = evt.get("distance_km")
            if anchor and dist is not None:
                summary_parts.append(f"{int(dist)} km from {anchor}")
            summary = " | ".join(summary_parts)[:300]

            # event_id is already the stable "nifc_{name}_{state}" key. Re-polls
            # of the same incident coalesce on this group_key; using it as the
            # sole inhibit_key lets the pipeline Inhibitor suppress lower-severity
            # re-emissions while a higher-severity one is active (severity tiering
            # delegated to the Inhibitor).
            # Canonical WFIGS schema on Event.data — the exact keys the Phase-3
            # decider (gating/fire.py::decide) and formatter (formatters/fire.py)
            # read. `_kind="wfigs_incident"` routes decide() into the active-fire
            # New/Update/suppress state machine (without it, decide suppresses).
            data = {
                "_kind": "wfigs_incident",
                "irwin_id": evt.get("irwin_id"),
                "incident_name": name,
                "acres": acres,
                "contained_pct": evt.get("contained_pct"),
                "declared_at_epoch": evt.get("declared_at_epoch"),
                "lat": lat,
                "lon": lon,
                "county": evt.get("county"),
                "state": evt.get("state"),
            }

            return make_event(
                source="nifc",
                category="wildfire_incident",
                severity=severity,
                title=name,
                summary=summary,
                timestamp=evt.get("fetched_at"),
                expires=evt.get("expires"),
                lat=lat,
                lon=lon,
                group_key=event_id,
                inhibit_keys=[event_id],
                data=data,
            )
        except Exception:
            logger.exception(f"NIFC to_event failed for evt: {evt.get('event_id')}")
            return None

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
