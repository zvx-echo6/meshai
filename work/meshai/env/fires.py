"""NIFC/WFIGS Wildfire adapter — perimeter + incident-point fusion.

Fetches both the WFIGS perimeter layer (fires with mapped perimeters) and the
WFIGS incident-locations point layer (the IRWIN superset, includes fires without
a perimeter yet) and merges them by IrwinID.  The point layer is the authoritative
superset; the perimeter layer supplies geometry (polygon + centroid) and validated
acreage when a perimeter exists.

Result: non-perimeter fires surface without double-broadcasting (deduplicated by
IrwinID through the same gating.fire.decide + fires table path as before).
"""

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


def _cfg_str(config, attr: str, default: str) -> str:
    """Read a string config field, falling back to `default` if absent,
    empty, or not a real string (e.g. an unconfigured test mock)."""
    value = getattr(config, attr, None)
    return value if isinstance(value, str) and value else default


class NICFFiresAdapter:
    """WFIGS ArcGIS fire perimeter + incident-point polling (merged by IrwinID)."""

    BASE_URL = "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/WFIGS_Interagency_Perimeters_Current/FeatureServer/0/query"
    POINTS_URL = "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/WFIGS_Incident_Locations_Current/FeatureServer/0/query"

    def __init__(self, config: "NICFFiresConfig", region_anchors: list = None, coverage: dict = None):
        self._state = config.state
        self._feed_url = _cfg_str(config, "feed_url", self.BASE_URL)
        self._points_url = _cfg_str(config, "points_url", self.POINTS_URL)
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
        """Build WFIGS ArcGIS perimeter query parameters (GeoJSON).

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

    def _build_points_query_params(self) -> dict:
        """Build WFIGS ArcGIS incident-locations point query parameters (ArcGIS JSON).

        Point-layer field names carry NO attr_ prefix (different service schema).
        Uses the same coverage envelope as the perimeter query when coverage is set,
        falling back to a state WHERE clause otherwise.
        """
        out_fields = (
            "IrwinID,IncidentName,IncidentSize,PercentContained,"
            "FireDiscoveryDateTime,POOState,POOCounty,InitialLatitude,"
            "InitialLongitude,UniqueFireIdentifier,IncidentTypeCategory"
        )
        if self._coverage is not None:
            params = {
                "where": "IncidentTypeCategory='WF'",
                "outFields": out_fields,
                "returnGeometry": "true",
                "f": "json",
            }
            params.update(self._coverage["envelope"])
        else:
            params = {
                "where": f"POOState='{self._state}' AND IncidentTypeCategory='WF'",
                "outFields": out_fields,
                "returnGeometry": "true",
                "f": "json",
            }
        return params

    def _fetch(self) -> bool:
        """Fetch and merge fire perimeters + incident-point locations from WFIGS.

        (a) Fetches the perimeter layer (GeoJSON) — builds perimeters_by_irwin.
        (b) Fetches the point layer (ArcGIS JSON) — the authoritative superset.
        (c) Merges: iterates the point set; per-fire uses perimeter geometry
            (polygon + centroid) when available, point coordinates otherwise.

        Resilience: each HTTP call is independently try/except'd. If the point
        layer fails, falls back to perimeter-only (today's behaviour). If the
        perimeter layer fails, uses point-only (no polygon). Consecutive error
        counter is bumped only when BOTH layers fail (nothing new to return).

        Returns:
            True if data changed
        """
        headers = {
            "User-Agent": "MeshAI/1.0",
            "Accept": "application/json",
        }

        # ── (a) Perimeter layer fetch ────────────────────────────────────────
        perim_features = []
        perim_ok = False
        try:
            params = self._build_query_params()
            url = f"{self._feed_url}?{urlencode(params)}"
            req = Request(url, headers=headers)
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            perim_features = data.get("features", [])
            perim_ok = True
        except HTTPError as e:
            logger.warning(f"NIFC perimeter HTTP error: {e.code}")
        except URLError as e:
            logger.warning(f"NIFC perimeter connection error: {e.reason}")
        except Exception as e:
            logger.warning(f"NIFC perimeter fetch error: {e}")

        # Build perimeters_by_irwin: irwin_id -> {lat, lon, polygon, acres, pct_contained}
        perimeters_by_irwin: dict = {}
        for feature in perim_features:
            try:
                props = feature.get("properties", {})
                geom = feature.get("geometry")

                irwin_id = (
                    props.get("attr_IrwinID")
                    or props.get("attr_UniqueFireIdentifier")
                )
                if not irwin_id:
                    continue

                lat, lon = self._compute_centroid(geom)

                # Store polygon for map overlay (Polygon type only, same as original)
                polygon = None
                if geom and geom.get("type") == "Polygon":
                    polygon = geom.get("coordinates", [])

                acres = props.get("attr_IncidentSize") or props.get("poly_GISAcres") or 0
                pct_contained = props.get("attr_PercentContained") or 0

                perimeters_by_irwin[irwin_id] = {
                    "lat": lat,
                    "lon": lon,
                    "polygon": polygon,
                    "acres": acres,
                    "pct_contained": pct_contained,
                }
            except Exception as e:
                logger.warning(f"NIFC perimeter parse error for feature: {e}")

        # ── (b) Points layer fetch ───────────────────────────────────────────
        point_features = []
        points_ok = False
        try:
            params = self._build_points_query_params()
            url = f"{self._points_url}?{urlencode(params)}"
            req = Request(url, headers=headers)
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            # ArcGIS JSON format: data["features"][].attributes + .geometry.x/.y
            point_features = data.get("features", [])
            points_ok = True
        except HTTPError as e:
            logger.warning(f"NIFC points HTTP error: {e.code}")
        except URLError as e:
            logger.warning(f"NIFC points connection error: {e.reason}")
        except Exception as e:
            logger.warning(f"NIFC points fetch error: {e}")

        # If BOTH layers failed, bump error counter and bail unchanged
        if not perim_ok and not points_ok:
            self._last_error = "both perimeter and points fetch failed"
            self._consecutive_errors += 1
            return False

        # ── (c) Merge: points are the authoritative superset ─────────────────
        # If point layer failed, fall back to perimeter-only by synthesising
        # point-style entries from the perimeter features already parsed.
        if not points_ok:
            logger.warning("NIFC points fetch failed — falling back to perimeter-only")
            point_features = _perim_features_as_point_stubs(perim_features)

        new_events = []
        now = time.time()

        for feature in point_features:
            try:
                # ArcGIS JSON: attrs in .attributes, geometry is {x: lon, y: lat}
                attrs = feature.get("attributes", {})
                pt_geom = feature.get("geometry")  # {"x": lon, "y": lat} or None

                name = attrs.get("IncidentName") or "Unknown Fire"

                # Derive irwin_id: prefer real IRWIN GUID, then unique fire id,
                # then fall through to event_id (computed below).
                irwin_id_raw = (
                    attrs.get("IrwinID")
                    or attrs.get("UniqueFireIdentifier")
                )

                # State for event_id construction (coverage mode uses fire's own state)
                if self._coverage is not None:
                    event_id_state = (attrs.get("POOState") or "").strip() or self._state
                else:
                    event_id_state = self._state

                event_id = f"nifc_{name.replace(' ', '_').lower()}_{event_id_state}"

                irwin_id = irwin_id_raw or event_id

                # ── Merge with perimeter data ────────────────────────────────
                perim = perimeters_by_irwin.get(irwin_id)
                if perim:
                    # Perimeter geometry preferred: use centroid + polygon
                    lat = perim["lat"]
                    lon = perim["lon"]
                    polygon = perim["polygon"]
                    # Perimeter acres/pct preferred; fall back to point values
                    acres = perim["acres"] or attrs.get("IncidentSize") or 0
                    pct_contained = (
                        perim["pct_contained"]
                        if perim["pct_contained"] is not None
                        else (attrs.get("PercentContained") or 0)
                    )
                else:
                    # Point-only: coordinates from geometry, fallback to InitialLat/Lon
                    if pt_geom:
                        lat = pt_geom.get("y") or attrs.get("InitialLatitude")
                        lon = pt_geom.get("x") or attrs.get("InitialLongitude")
                    else:
                        lat = attrs.get("InitialLatitude")
                        lon = attrs.get("InitialLongitude")
                    polygon = None
                    acres = attrs.get("IncidentSize") or 0
                    pct_contained = attrs.get("PercentContained") or 0

                # Compute proximity to nearest anchor
                distance_km, nearest_anchor = self._nearest_anchor_distance(lat, lon)

                # Severity based on distance
                if distance_km is not None:
                    severity = "priority" if distance_km < 25 else "routine"
                else:
                    severity = "routine"

                # Format headline
                headline = f"{name} -- {int(acres):,} ac, {int(pct_contained)}% contained"
                if distance_km is not None and nearest_anchor:
                    headline += f" ({int(distance_km)} km from {nearest_anchor})"

                declared_at_epoch = self._parse_discovery_epoch(
                    attrs.get("FireDiscoveryDateTime"))

                county = attrs.get("POOCounty") or None

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
                    "county": county,
                    "lat": lat,
                    "lon": lon,
                    "distance_km": distance_km,
                    "nearest_anchor": nearest_anchor,
                    "state": self._state,
                    "expires": now + 21600,  # 6 hour TTL
                    "fetched_at": now,
                }

                # Store polygon for map overlay (only when perimeter geometry present)
                if polygon:
                    event["polygon"] = polygon

                new_events.append(event)

            except Exception as e:
                logger.warning(f"NIFC merge error for feature: {e}")

        # Change detection — include acres + containment so fire growth is visible
        def _change_sig(e):
            try:
                acres = int(round(float(e.get("acres") or 0)))
            except (TypeError, ValueError):
                acres = 0
            try:
                pct = int(float(e.get("pct_contained") or 0))
            except (TypeError, ValueError):
                pct = 0
            return (e["event_id"], acres, pct)

        old_ids = {_change_sig(e) for e in self._events}
        new_ids = {_change_sig(e) for e in new_events}
        changed = old_ids != new_ids

        self._events = new_events
        self._consecutive_errors = 0
        self._last_error = None
        self._is_loaded = True

        if changed:
            loc = "the coverage area" if self._coverage is not None else self._state
            perim_count = len(perimeters_by_irwin)
            point_count = len(point_features)
            logger.info(
                f"NIFC fires updated: {len(new_events)} active in {loc} "
                f"(merged {point_count} point(s) + {perim_count} perimeter(s))"
            )

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
        """Translate a stored NIFC/WFIGS fire into a pipeline Event.

        Every active fire with a reported size maps to a single
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


# ── Fallback: synthesise point-stub dicts from perimeter GeoJSON features ──────
# Used when the points fetch fails; lets the perimeter-only path continue to
# work through the unified merge loop without a separate code path.

def _perim_features_as_point_stubs(perim_features: list) -> list:
    """Convert perimeter GeoJSON features to minimal ArcGIS-point-style dicts.

    Used ONLY when the points fetch fails; keeps the merge loop unified.
    The stub carries attrs under ``attributes`` and no ``geometry`` (lat/lon
    come from the perimeter dict via perimeters_by_irwin lookup in the caller).
    """
    stubs = []
    for f in perim_features:
        props = f.get("properties", {})
        stubs.append({
            "attributes": {
                "IrwinID": props.get("attr_IrwinID"),
                "UniqueFireIdentifier": props.get("attr_UniqueFireIdentifier"),
                "IncidentName": props.get("attr_IncidentName"),
                "IncidentSize": props.get("attr_IncidentSize") or props.get("poly_GISAcres"),
                "PercentContained": props.get("attr_PercentContained"),
                "FireDiscoveryDateTime": props.get("attr_FireDiscoveryDateTime"),
                "POOState": props.get("attr_POOState"),
                "POOCounty": None,
                "InitialLatitude": None,
                "InitialLongitude": None,
                "UniqueFireIdentifier": props.get("attr_UniqueFireIdentifier"),
                "IncidentTypeCategory": "WF",
            },
            "geometry": None,
        })
    return stubs
