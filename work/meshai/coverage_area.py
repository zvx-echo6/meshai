"""Geographic coverage area primitives.

Attribution: MonitoringArea, build_geom_json, and classify_geom_areas are
ported verbatim from central/src/central/monitoring_area.py (v0.14.0+).
The data source is adapted: Central loads boxes from asyncpg; here they
come from meshai config (plain dicts/floats).  The Shapely algorithm and
geom-json normalization are byte-identical to Central's implementation.

Uses shapely>=2.0 (manylinux wheels bundle GEOS — no apt package required
on python:3.11-slim-bookworm).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from shapely.geometry import box as _shapely_box
from shapely.geometry import shape

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# MonitoringArea — frozen bbox dataclass (ported from central)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MonitoringArea:
    """Bounding box that events must intersect to pass the coverage gate."""

    north: float
    south: float
    east: float
    west: float
    name: str | None = None  # P1: optional label; unnamed areas never contribute to region tagging

    def as_box(self):
        # shapely box(minx, miny, maxx, maxy) -> (west, south, east, north)
        return _shapely_box(self.west, self.south, self.east, self.north)


# ---------------------------------------------------------------------------
# build_geom_json — geo-dict → GeoJSON string (ported from central)
# ---------------------------------------------------------------------------

def build_geom_json(geo_data: dict[str, Any] | None) -> str | None:
    """Build a GeoJSON string from an event's geo dict.

    Priority (matches Central's v0.9.8+ rule):
      1. full ``geometry`` dict (wins over everything)
      2. ``bbox`` [minlon, minlat, maxlon, maxlat] — rendered as a 4-corner Polygon
      3. ``centroid`` [lon, lat] — rendered as a Point

    Returns None if no usable shape is present.
    Ported verbatim from central.monitoring_area.build_geom_json.
    """
    if not geo_data:
        return None

    geometry = geo_data.get("geometry")
    if geometry:
        return json.dumps(geometry)

    bbox = geo_data.get("bbox")
    centroid = geo_data.get("centroid")

    if bbox and len(bbox) == 4:
        min_lon, min_lat, max_lon, max_lat = bbox
        return json.dumps({
            "type": "Polygon",
            "coordinates": [[
                [min_lon, min_lat],
                [max_lon, min_lat],
                [max_lon, max_lat],
                [min_lon, max_lat],
                [min_lon, min_lat],
            ]],
        })
    if centroid and len(centroid) == 2:
        return json.dumps({
            "type": "Point",
            "coordinates": centroid,
        })

    return None


# ---------------------------------------------------------------------------
# classify_geom_areas — set-union Shapely intersection (ported from central)
# ---------------------------------------------------------------------------

def classify_geom_areas(
    geom_json: str | None,
    areas: list[MonitoringArea],
) -> str:
    """Classify a GeoJSON string against a list of monitoring areas (set union).

    Returns one of:
      'null-geom'     -- no geometry; always kept (fail-open)
      'no-area'       -- empty area list; keep everything
      'in-bounds'     -- geometry intersects AT LEAST ONE area; keep
      'out-of-bounds' -- geometry lies entirely outside every area; drop
      'invalid-geom'  -- geometry could not be evaluated; kept (fail-open)

    An event is kept if it intersects ANY configured area (set union).
    Uses intersects() so border-straddlers and points-on-edge are kept.
    The filter must never drop an event because of a parse failure.
    Ported verbatim from central.monitoring_area.classify_geom_areas (v0.14.0).
    """
    if geom_json is None:
        return "null-geom"
    if not areas:
        return "no-area"
    try:
        geom = shape(json.loads(geom_json))
        for area in areas:
            if geom.intersects(area.as_box()):
                return "in-bounds"
        return "out-of-bounds"
    except Exception:
        return "invalid-geom"


# ---------------------------------------------------------------------------
# areas_from_config — meshai config → MonitoringArea list
# ---------------------------------------------------------------------------

def areas_from_config(coverage_cfg) -> list[MonitoringArea]:
    """Build a MonitoringArea list from meshai config.Coverage.

    Uses ``coverage_cfg.areas`` (list of dicts with name/west/south/east/north)
    if non-empty.  Falls back to synthesising a single area from the legacy
    ``coverage_cfg.bbox`` ([w, s, e, n]) when ``areas`` is empty, so existing
    single-bbox deployments need no config migration.

    Returns an empty list when neither source is populated, which causes
    classify_geom_areas to return 'no-area' and keep everything (pre-feature
    default — identical to Central's NULL-bbox behaviour).
    """
    areas_list = getattr(coverage_cfg, "areas", None) or []
    if areas_list:
        result: list[MonitoringArea] = []
        for a in areas_list:
            if isinstance(a, dict):
                try:
                    result.append(MonitoringArea(
                        north=float(a["north"]),
                        south=float(a["south"]),
                        east=float(a["east"]),
                        west=float(a["west"]),
                        name=(a.get("name") or None),
                    ))
                except (KeyError, TypeError, ValueError) as exc:
                    logger.warning("Skipping malformed coverage area %r: %s", a, exc)
        return result

    # Fall back to the legacy single-bbox field (no name — legacy compat)
    bbox = getattr(coverage_cfg, "bbox", None) or []
    if bbox and len(bbox) == 4:
        west, south, east, north = (float(v) for v in bbox)
        return [MonitoringArea(north=north, south=south, east=east, west=west, name=None)]

    return []


# ---------------------------------------------------------------------------
# _event_geom_json — shared geometry extraction helper (used by gate + tagger)
# ---------------------------------------------------------------------------

def _event_geom_json(event: Any) -> str | None:
    """Extract a GeoJSON string from an event using the standard priority chain.

    Priority (mirrors Central's archive chain):
      1. event.data["geometry"] — full GeoJSON dict (richest, preferred)
      2. event.data["bbox"]     — [west, south, east, north]
      3. (event.lon, event.lat) — Point centroid [lon, lat] in GeoJSON order

    Returns None when no geometry can be constructed.
    Shared between classify_event_areas (gate) and event_region_names (tagger)
    so both operate on identical geometry; gate behaviour is byte-identical.
    """
    data: dict[str, Any] = getattr(event, "data", None) or {}
    geo: dict[str, Any] = {}

    geometry = data.get("geometry")
    if geometry:
        geo["geometry"] = geometry
    else:
        bbox = data.get("bbox")
        if bbox:
            geo["bbox"] = bbox
        else:
            lat = getattr(event, "lat", None)
            lon = getattr(event, "lon", None)
            if lat is not None and lon is not None:
                geo["centroid"] = [lon, lat]  # GeoJSON coordinate order: [lon, lat]

    return build_geom_json(geo if geo else None)


# ---------------------------------------------------------------------------
# event_in_areas — convenience wrapper over build_geom_json + classify
# ---------------------------------------------------------------------------

def classify_event_areas(event: Any, areas: list[MonitoringArea]) -> str:
    """Classify an event against configured areas, returning the 3-state result.

    Extracts the event's geometry (same priority chain as ``event_in_areas``)
    and returns the raw ``classify_geom_areas`` verdict so callers can
    distinguish "out-of-bounds" from "can't be located":

      'null-geom'     -- no geometry / bbox / centroid at all
      'invalid-geom'  -- geometry present but Shapely could not evaluate it
      'no-area'       -- areas list empty (coverage effectively off)
      'in-bounds'     -- intersects at least one configured area
      'out-of-bounds' -- lies entirely outside every configured area

    The coverage gate uses this to fail CLOSED (drop) on 'null-geom' /
    'invalid-geom' for weather-alert categories, while other adapters keep
    the fail-OPEN behaviour.

    Geometry extraction priority (mirrors Central's archive chain):
      1. event.data["geometry"] — full GeoJSON dict (richest, preferred)
      2. event.data["bbox"]     — [west, south, east, north]
      3. (event.lon, event.lat) — Point centroid [lon, lat] in GeoJSON order
    """
    geom_json = _event_geom_json(event)
    return classify_geom_areas(geom_json, areas)


# ---------------------------------------------------------------------------
# matching_area_names — additive region-name collector (P1 tagger)
# ---------------------------------------------------------------------------

def matching_area_names(geom_json: str | None, areas: list[MonitoringArea]) -> list[str]:
    """Return names of every NAMED area whose bbox the geometry intersects.

    - Returns names in config order, deduped (first occurrence wins).
    - Unnamed areas (name is None or empty) never contribute.
    - null/parse-error geometry -> [] (never raises).
    - Empty areas list -> [].
    - Does NOT short-circuit on first match — all matching named areas are
      collected (additive, not a gate).  classify_geom_areas is unchanged.
    """
    if not geom_json or not areas:
        return []
    try:
        geom = shape(json.loads(geom_json))
    except Exception:
        return []
    seen: set[str] = set()
    result: list[str] = []
    for area in areas:
        if not area.name:
            continue
        try:
            if geom.intersects(area.as_box()):
                if area.name not in seen:
                    seen.add(area.name)
                    result.append(area.name)
        except Exception:
            pass
    return result


def observer_region_names(event: Any, areas: list[MonitoringArea]) -> list[str]:
    """Region names for an event tagged by its satpass observer coordinates.

    For each observer slug in event.data['observer_list'], look up its lat/lon
    via get_observers() and collect every NAMED area whose bbox contains it.
    Union, config order, deduped. Returns [] when there's no observer_list, no
    resolvable coords, or no observer falls in any named area. Fail-open on any
    error (return []).
    """
    data = getattr(event, "data", None) or {}
    ol = data.get("observer_list")
    if not ol or not areas:
        return []
    slugs = [s.strip() for s in str(ol).split(",") if s.strip()]
    if not slugs:
        return []
    try:
        from meshai.persistence.observer_locations import get_observers
        coords = {o["slug"]: (o["lat"], o["lon"]) for o in get_observers()}
    except Exception:
        return []
    out: list[str] = []
    for s in slugs:
        c = coords.get(s)
        if not c:
            continue
        try:
            lat, lon = float(c[0]), float(c[1])
        except (TypeError, ValueError):
            continue
        for a in areas:
            if a.name and a.name not in out and a.south <= lat <= a.north and a.west <= lon <= a.east:
                out.append(a.name)
    return out


def event_region_names(event: Any, areas: list[MonitoringArea]) -> list[str]:
    """Return region names for an event by matching its geometry against named areas.

    Primary path: extracts geometry via _event_geom_json (same chain as the
    coverage gate) and delegates to matching_area_names.
    Fallback path: when geometry yields no names (e.g. satpass events have no
    polygon), tries observer_region_names() which maps satpass observer
    coordinates to named coverage areas.
    Returns [] when no geometry, no named areas, or parse error.
    """
    names = matching_area_names(_event_geom_json(event), areas)
    if not names:
        names = observer_region_names(event, areas)
    return names


def event_in_areas(event: Any, areas: list[MonitoringArea]) -> bool:
    """Return True if the event's geometry intersects any configured area.

    Back-compat bool wrapper over ``classify_event_areas``. Retains the
    original fail-OPEN semantics (matching Central):
      'null-geom'  (no geometry at all)   → True  (keep)
      'invalid-geom' (parse/Shapely error) → True  (keep)
      'no-area'    (areas list empty)      → True  (keep)
      'in-bounds'                          → True  (keep)
      'out-of-bounds'                      → False (drop)
    """
    return classify_event_areas(event, areas) != "out-of-bounds"
