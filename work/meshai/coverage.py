"""Universal coverage bounding-box derivation for MeshAI.

Bbox convention (used consistently throughout this module and the Phase 2+ wiring):
    bbox = [west, south, east, north]
         = [min_lon, min_lat, max_lon, max_lat]

This matches the order used by the USGS-quake native adapter (env/usgs_quake.py)
and the Roads511/WZDx config fields.  It is also the natural ArcGIS envelope order
(xmin, ymin, xmax, ymax).

Central-fed adapters (feed_source="central") ALWAYS return None from
resolve_adapter_coverage — Central governs their geographic scope entirely.

All functions here are pure (no I/O, no network, deterministic) so they can be
unit-tested without any runtime environment.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Geometry primitives
# ---------------------------------------------------------------------------


def bbox_is_valid(bbox) -> bool:
    """Return True iff *bbox* is a well-formed [W, S, E, N] bounding box.

    Rules:
    - Must be a sequence of exactly 4 numbers.
    - west < east (non-zero width).
    - south < north (non-zero height).
    - Latitudes in [-90, 90].
    - Longitudes in [-180, 180].
    """
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return False
    west, south, east, north = bbox
    try:
        west, south, east, north = float(west), float(south), float(east), float(north)
    except (TypeError, ValueError):
        return False
    if not (-180 <= west <= 180 and -180 <= east <= 180):
        return False
    if not (-90 <= south <= 90 and -90 <= north <= 90):
        return False
    if west >= east:
        return False
    if south >= north:
        return False
    return True


def point_in_bbox(lat: float, lon: float, bbox) -> bool:
    """Return True iff (lat, lon) lies inside *bbox* = [W, S, E, N]."""
    west, south, east, north = bbox
    return south <= lat <= north and west <= lon <= east


def bbox_intersects(a, b) -> bool:
    """Return True iff two [W, S, E, N] bounding boxes overlap (share any area).

    Boxes that only share an edge or a corner are considered to *not* intersect
    (strict inequality on all four sides).
    """
    # a overlaps b iff a's west is west of b's east AND a's east is east of b's west
    # AND a's south is south of b's north AND a's north is north of b's south.
    a_west, a_south, a_east, a_north = a
    b_west, b_south, b_east, b_north = b
    return (
        a_west < b_east and a_east > b_west
        and a_south < b_north and a_north > b_south
    )


def centroid(bbox) -> tuple[float, float]:
    """Return the (lat, lon) center of *bbox* = [W, S, E, N].

    Coordinates are rounded to 6 decimal places (≈0.11 m precision) so that
    values derived from high-precision Leaflet clicks never exceed external API
    decimal-place limits.
    """
    west, south, east, north = bbox
    return (round((south + north) / 2.0, 6), round((west + east) / 2.0, 6))


def grid_points(bbox, cols: int = 3, rows: int = 3) -> list[tuple[float, float]]:
    """Return a cols×rows grid of (lat, lon) sample points across *bbox*.

    Points are placed at the *centers* of each grid cell so none sit on the
    bounding-box edges.  Returned in row-major order (south to north, west to east).

    Used by the traffic adapter to distribute flow-query points across the
    coverage area.
    """
    west, south, east, north = bbox
    lon_step = (east - west) / cols
    lat_step = (north - south) / rows
    points = []
    for row in range(rows):
        lat = south + (row + 0.5) * lat_step
        for col in range(cols):
            lon = west + (col + 0.5) * lon_step
            points.append((round(lat, 6), round(lon, 6)))
    return points


def enclosing_bbox(areas) -> list:
    """Union bounding box [west, south, east, north] of a list of area dicts
    ({'west','south','east','north'}), or [] if empty/invalid. Used to widen
    adapter FETCH scope so multi-area / cross-state data is pulled; the Shapely
    gate narrows to the exact areas afterward."""
    boxes = []
    for a in areas or []:
        try:
            w, s, e, n = float(a["west"]), float(a["south"]), float(a["east"]), float(a["north"])
            boxes.append((w, s, e, n))
        except (KeyError, TypeError, ValueError):
            continue
    if not boxes:
        return []
    return [
        round(min(b[0] for b in boxes), 6),
        round(min(b[1] for b in boxes), 6),
        round(max(b[2] for b in boxes), 6),
        round(max(b[3] for b in boxes), 6),
    ]


def arcgis_envelope(bbox) -> dict:
    """Return ArcGIS FeatureServer spatial-filter query params for *bbox*.

    The returned dict is ready to merge into a FeatureServer query string:
        {"geometry": "xmin,ymin,xmax,ymax",
         "geometryType": "esriGeometryEnvelope",
         "spatialRel": "esriSpatialRelIntersects",
         "inSR": "4326"}

    xmin=west, ymin=south, xmax=east, ymax=north (standard ArcGIS convention).
    """
    west, south, east, north = bbox
    return {
        "geometry": f"{west},{south},{east},{north}",
        "geometryType": "esriGeometryEnvelope",
        "spatialRel": "esriSpatialRelIntersects",
        "inSR": "4326",
    }


# ---------------------------------------------------------------------------
# US state bbox table
# ---------------------------------------------------------------------------

# Approximate [west, south, east, north] bboxes per 2-letter US state code.
# Err slightly LARGE so border events aren't missed.  Values are not exact
# legal boundaries; they are generous bounding rectangles for intersection tests.
US_STATE_BBOXES: dict[str, list[float]] = {
    # Western / Mountain states — thorough
    "AK": [-180.0, 51.0, -130.0, 71.5],
    "AZ": [-114.9, 31.3, -109.0, 37.0],
    "CA": [-124.5, 32.5, -114.1, 42.0],
    "CO": [-109.1, 36.9, -102.0, 41.1],
    "HI": [-160.3, 18.9, -154.8, 22.3],
    "ID": [-117.3, 41.9, -111.0, 49.1],
    "MT": [-116.1, 44.3, -104.0, 49.1],
    "NM": [-109.1, 31.3, -103.0, 37.0],
    "NV": [-120.0, 35.0, -114.0, 42.1],
    "OR": [-124.6, 41.9, -116.5, 46.3],
    "UT": [-114.1, 36.9, -109.0, 42.1],
    "WA": [-124.7, 45.5, -116.9, 49.1],
    "WY": [-111.1, 40.9, -104.0, 45.1],

    # Pacific / Southwest
    "AS": [-171.1, -14.6, -168.1, -11.0],  # American Samoa
    "GU": [144.6, 13.2, 145.0, 13.7],      # Guam
    "MP": [145.0, 14.9, 146.1, 20.6],      # N. Mariana Islands
    "PR": [-67.3, 17.9, -65.6, 18.6],      # Puerto Rico
    "VI": [-65.1, 17.7, -64.6, 18.4],      # US Virgin Islands

    # Great Plains
    "KS": [-102.1, 36.9, -94.6, 40.1],
    "ND": [-104.1, 45.9, -96.6, 49.1],
    "NE": [-104.1, 39.9, -95.3, 43.1],
    "OK": [-103.1, 33.6, -94.4, 37.1],
    "SD": [-104.1, 42.4, -96.4, 45.9],
    "TX": [-106.7, 25.8, -93.5, 36.6],

    # Midwest
    "IA": [-96.7, 40.3, -90.1, 43.6],
    "IL": [-91.5, 36.9, -87.0, 42.5],
    "IN": [-88.1, 37.8, -84.8, 41.8],
    "MI": [-90.4, 41.7, -82.4, 48.3],
    "MN": [-97.3, 43.5, -89.5, 49.4],
    "MO": [-95.8, 35.9, -89.1, 40.6],
    "OH": [-84.8, 38.4, -80.5, 42.0],
    "WI": [-92.9, 42.5, -86.8, 47.1],

    # South
    "AL": [-88.5, 30.1, -84.9, 35.0],
    "AR": [-94.6, 33.0, -89.6, 36.5],
    "FL": [-87.6, 24.4, -80.0, 31.1],
    "GA": [-85.6, 30.4, -80.8, 35.0],
    "KY": [-89.6, 36.5, -81.9, 39.2],
    "LA": [-94.1, 28.9, -88.8, 33.0],
    "MS": [-91.7, 30.2, -88.1, 35.0],
    "NC": [-84.3, 33.9, -75.5, 36.6],
    "SC": [-83.4, 32.0, -78.5, 35.2],
    "TN": [-90.3, 34.9, -81.6, 36.7],
    "VA": [-83.7, 36.5, -75.2, 39.5],
    "WV": [-82.7, 37.2, -77.7, 40.6],

    # Northeast
    "CT": [-73.7, 41.0, -71.8, 42.1],
    "DC": [-77.1, 38.8, -76.9, 39.0],
    "DE": [-75.8, 38.4, -75.0, 39.9],
    "MA": [-73.5, 41.2, -69.9, 42.9],
    "MD": [-79.5, 37.9, -75.0, 39.7],
    "ME": [-71.1, 43.0, -66.9, 47.5],
    "NH": [-72.6, 42.7, -70.7, 45.3],
    "NJ": [-75.6, 38.9, -73.9, 41.4],
    "NY": [-79.8, 40.5, -71.9, 45.0],
    "PA": [-80.5, 39.7, -74.7, 42.3],
    "RI": [-71.9, 41.1, -71.1, 42.0],
    "VT": [-73.4, 42.7, -71.5, 45.1],
}


def states_for_bbox(bbox) -> list[str]:
    """Return sorted state codes whose bbox intersects the given coverage bbox.

    Uses bbox_intersects; a state whose bounding rectangle overlaps the coverage
    rectangle is included.  Deduped and sorted alphabetically.
    """
    result = []
    for code, state_bbox in US_STATE_BBOXES.items():
        if bbox_intersects(bbox, state_bbox):
            result.append(code)
    return sorted(set(result))


# ---------------------------------------------------------------------------
# Avalanche center bbox table
# ---------------------------------------------------------------------------

# Approximate [west, south, east, north] bboxes for US avalanche centers.
# IDs match what the AvalancheConfig.center_ids field expects.
# Err large — we want to include a center if there's any reasonable chance
# its forecast area overlaps the user's coverage box.
AVALANCHE_CENTER_BBOXES: dict[str, list[float]] = {
    # Idaho
    "SNFAC": [-116.5, 43.0, -113.5, 45.5],   # Sawtooth NF (Sun Valley / Stanley area)
    "PAC":   [-117.0, 43.5, -115.0, 44.8],   # Payette Avalanche Center (SW Idaho)
    "IPAC":  [-117.3, 46.5, -115.0, 49.1],   # Idaho Panhandle (N Idaho)

    # Montana
    "GNFAC": [-112.5, 44.0, -108.5, 46.5],   # Gallatin NF (SW Montana / NW Wyoming)
    "FAC":   [-115.5, 46.5, -112.5, 49.1],   # Flathead Avalanche Center (NW Montana)
    "WCMAC": [-114.5, 46.0, -111.5, 48.5],   # West Central Montana

    # Wyoming
    "BTAC":  [-111.5, 42.5, -109.0, 44.5],   # Bridger-Teton (Tetons / Jackson Hole)

    # Colorado
    "CAIC":  [-109.1, 36.9, -102.0, 41.1],   # Colorado Avalanche Information Center
    "CBAC":  [-108.0, 38.0, -105.5, 39.5],   # Crested Butte Avalanche Center

    # Utah
    "UAC":   [-114.1, 36.9, -109.0, 42.1],   # Utah Avalanche Center

    # Washington / Oregon
    "NWAC":  [-124.7, 45.5, -120.5, 49.1],   # Northwest Avalanche Center (WA + OR Cascades)
    "COAA":  [-122.5, 43.5, -120.0, 45.5],   # Central Oregon Avalanche Association

    # California
    "SAC":   [-121.0, 38.5, -119.0, 40.5],   # Sierra Avalanche Center (Lake Tahoe)
    "ESAC":  [-119.5, 37.0, -117.5, 38.8],   # Eastern Sierra Avalanche Center (Mammoth)

    # New Mexico / Arizona
    "NWRFC": [-108.5, 35.5, -105.5, 37.5],   # New Mexico (Taos area)
}


def avalanche_centers_for_bbox(bbox) -> list[str]:
    """Return sorted center IDs whose area intersects the given coverage bbox."""
    result = []
    for center_id, center_bbox in AVALANCHE_CENTER_BBOXES.items():
        if bbox_intersects(bbox, center_bbox):
            result.append(center_id)
    return sorted(set(result))


# ---------------------------------------------------------------------------
# Per-adapter resolver
# ---------------------------------------------------------------------------

# Adapters whose feed_source can be "central"; when it is, coverage is
# governed entirely by Central and we return None.
_CENTRAL_CAPABLE = frozenset({
    "fires", "nws", "swpc", "ducting", "avalanche", "usgs", "usgs_quake",
    "traffic", "roads511", "wzdx", "firms", "satpass",
})

# Adapters that are inherently global and have no meaningful geographic scope.
_GLOBAL_ADAPTERS = frozenset({"swpc"})


def resolve_adapter_coverage(
    adapter: str,
    coverage_bbox: list,
    feed_source: str = "native",
) -> dict | None:
    """Map one universal coverage bbox to an adapter's effective scope params.

    Args:
        adapter:       The adapter name (e.g. "nws", "fires", "satpass").
        coverage_bbox: The [west, south, east, north] coverage bbox from config.
        feed_source:   "native" or "central".  Central-fed adapters always
                       return None (Central governs coverage).

    Returns:
        None — when the caller should fall back to the adapter's own config:
            - feed_source == "central"
            - coverage_bbox is empty or invalid
            - adapter is global (swpc) or unknown

        dict — containing ONLY the keys relevant to that adapter:
            fires / wfigs / nicf:
                {"envelope": arcgis_envelope(bbox), "bbox": bbox}
            nws:
                {"areas": states_for_bbox(bbox), "bbox": bbox}
            wzdx:
                {"states": states_for_bbox(bbox), "bbox": bbox}
            usgs_quake / firms / roads511 / usgs:
                {"bbox": bbox}
            avalanche:
                {"center_ids": avalanche_centers_for_bbox(bbox)}
            traffic:
                {"points": grid_points(bbox)}
            satpass / ducting:
                {"centroid": centroid(bbox)}
    """
    # Central governs scope — never override.
    if feed_source == "central":
        return None

    # No valid bbox — caller falls back to adapter's own config.
    if not coverage_bbox or not bbox_is_valid(coverage_bbox):
        return None

    # Global adapters have no geographic scope.
    if adapter in _GLOBAL_ADAPTERS:
        return None

    # Defensive copy + precision cap: raw Leaflet clicks produce 14+ decimal
    # places which USGS (and others) reject.  6dp ≈ 0.11 m — more than enough.
    bbox = [round(float(c), 6) for c in coverage_bbox]

    if adapter in ("fires", "wfigs", "nicf"):
        return {
            "envelope": arcgis_envelope(bbox),
            "bbox": bbox,
        }

    if adapter == "nws":
        return {
            "areas": states_for_bbox(bbox),
            "bbox": bbox,
        }

    if adapter == "wzdx":
        return {
            "states": states_for_bbox(bbox),
            "bbox": bbox,
        }

    if adapter in ("usgs_quake", "firms", "roads511", "usgs"):
        return {"bbox": bbox}

    if adapter == "avalanche":
        return {"center_ids": avalanche_centers_for_bbox(bbox)}

    if adapter == "traffic":
        return {"points": grid_points(bbox)}

    if adapter in ("satpass", "ducting"):
        return {"centroid": centroid(bbox)}

    # Unknown adapter or inherently global — no coverage override.
    return None
