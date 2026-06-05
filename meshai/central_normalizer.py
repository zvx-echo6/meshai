"""Meshai-side Central-envelope normalizer.

Central is a faithful firehose — it preserves upstream payloads verbatim
(per Central v0.10.0 §README "Central takes it all and gives it all").
Per-adapter shape normalization is the consumer's job. This module is
where that lives.

First adapter wired: state_511_atis (Castle Rock ATIS feeds — the source
for Idaho 511 work_zone / closure events). Other adapters will be added
as their renderer formats are approved.

Design: `normalize(envelope) -> dict | None` returns a flat, render-ready
dict whose shape is described in NORMALIZED_KEYS. Adapter-specific
extraction lives in private parsers dispatched off `inner.adapter`. The
output dict is pure-data; formatting is the renderer's job.
"""

import json
import logging
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------- shared normalized output shape --------------------------------

NORMALIZED_KEYS = (
    "source",        # str        -- inner.adapter
    "road",          # str | None
    "direction",     # str | None -- 'northbound'/'southbound'/'eastbound'/
                     #                'westbound'/'both'/'unknown'
    "mile_start",    # int | None
    "mile_end",      # int | None
    "description",   # str | None -- upstream prose, cleaned
    "sub_type",      # str | None -- friendly: 'construction work', 'incident', ...
    "impact",        # str | None -- 'full_closure'/'partial'/'unknown'
    "ends_at",       # datetime | None (UTC) -- parsed from description if absent structurally
    "town",          # str | None -- _enriched.geocoder.city or .name
    "distance_mi",   # int | None -- haversine from event coords to town
    "bearing",       # str | None -- 'N'/'NE'/.../'NW'
)


# ---------- direction normalization ---------------------------------------

_DIR_MAP = {
    "north": "northbound", "northbound": "northbound", "nb": "northbound",
    "south": "southbound", "southbound": "southbound", "sb": "southbound",
    "east":  "eastbound",  "eastbound":  "eastbound",  "eb": "eastbound",
    "west":  "westbound",  "westbound":  "westbound",  "wb": "westbound",
    "both":  "both",       "both directions": "both",
    "unknown": "unknown",  "": "unknown",
}


def _norm_direction(raw: Optional[str]) -> Optional[str]:
    if raw is None: return None
    s = str(raw).strip().lower()
    return _DIR_MAP.get(s, "unknown")


# ---------- sub_type → friendly label -------------------------------------

_SUBTYPE_MAP = {
    "roadConstruction": "road construction",
    "longTermRoadConstruction": "road construction",
    "constructionWork": "construction work",
    "bridgeConstruction": "bridge construction",
    "bridgeMaintenanceOperations": "bridge maintenance",
    "bridgeInspectionWork": "bridge inspection",
    "pavingOperations": "paving",
    "pavementMarkingOperations": "pavement marking",  # also w/ trailing space
    "emergencyRepairs": "emergency repairs",
    "utilityWork": "utility work",
    "guardrailRepairs": "guardrail repairs",
    "workOnTheShoulder": "shoulder work",
    "brushControl": "brush control",
    "flaggingOperation": "flagging",
    "singleLineTraffic:AlternatingDirections": "alternating one-way",
}


def _norm_sub_type(raw: Optional[str]) -> Optional[str]:
    if not raw: return None
    s = str(raw).strip()
    if s in _SUBTYPE_MAP:
        return _SUBTYPE_MAP[s]
    # Trailing-space variants
    if s.strip() in _SUBTYPE_MAP:
        return _SUBTYPE_MAP[s.strip()]
    # Fallback: camelCase split, lowercase, drop colon-suffix
    s = s.split(":", 1)[0]
    parts = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)", s) or [s]
    return " ".join(p.lower() for p in parts)


# ---------- description parsers (state_511_atis-style) --------------------

# "from MM (93) to MM (89)"  →  (93, 89)
# "near MM (495)"            →  (495, None)
# "at MM (60)"               →  (60, None)
_MM_RE = re.compile(
    r"(?:from\s+)?MM\s*\(?(\d+)\)?(?:\s*to\s+MM\s*\(?(\d+)\)?)?",
    re.IGNORECASE,
)


def _parse_mile_posts(description: str) -> tuple[Optional[int], Optional[int]]:
    if not description: return None, None
    m = _MM_RE.search(description)
    if not m: return None, None
    try:
        start = int(m.group(1))
    except (TypeError, ValueError):
        return None, None
    end = None
    if m.group(2):
        try: end = int(m.group(2))
        except (TypeError, ValueError): end = None
    return start, end


# "5/29/2026 10:00 AM to 5/29/2026 3:00 PM"  →  datetime(2026, 5, 29, 15, 0, tzinfo=UTC)
# (we treat the parsed time as local America/Boise but for the short
#  format renderer Boise-relative is what users actually want anyway).
_DATERANGE_RE = re.compile(
    r"(\d{1,2}/\d{1,2}/\d{4})\s+(\d{1,2}:\d{2})\s+(AM|PM)\s+to\s+"
    r"(\d{1,2}/\d{1,2}/\d{4})\s+(\d{1,2}:\d{2})\s+(AM|PM)",
    re.IGNORECASE,
)


def _parse_ends_at(description: str) -> Optional[datetime]:
    if not description: return None
    m = _DATERANGE_RE.search(description)
    if not m: return None
    end_date, end_time, end_ampm = m.group(4), m.group(5), m.group(6).upper()
    try:
        dt = datetime.strptime(f"{end_date} {end_time} {end_ampm}", "%m/%d/%Y %I:%M %p")
    except ValueError:
        return None
    return dt   # naive; renderer treats as local


# ---------- description cleanup -------------------------------------------

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _clean_description(raw: Optional[str]) -> Optional[str]:
    if not raw: return None
    s = _HTML_TAG_RE.sub(" ", str(raw))
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


# ---------- distance / bearing --------------------------------------------

# Small lookup of Idaho (+ a few neighbor) towns -> (lat, lon). Used to
# render "X mi <bearing> of <town>" when the reverse-geocoder picked a
# city we know. Built from the geocoder.city values seen across 60-sample
# probe + major Idaho cities the operator's mesh is most likely to care
# about. Misses fall through silently: distance_mi/bearing stay None.
_TOWN_COORDS: dict[str, tuple[float, float]] = {
    "boise":          (43.6150, -116.2023),
    "meridian":       (43.6121, -116.3915),
    "nampa":          (43.5407, -116.5635),
    "caldwell":       (43.6629, -116.6874),
    "idaho falls":    (43.4666, -112.0340),
    "pocatello":      (42.8713, -112.4455),
    "twin falls":     (42.5630, -114.4609),
    "coeur d'alene":  (47.6777, -116.7805),
    "lewiston":       (46.4165, -117.0177),
    "moscow":         (46.7324, -117.0002),
    "sandpoint":      (48.2766, -116.5535),
    "post falls":     (47.7180, -116.9516),
    "hayden":         (47.7660, -116.7866),
    "rathdrum":       (47.8121, -116.8950),
    "plummer":        (47.3344, -116.8856),
    "kellogg":        (47.5380, -116.1352),
    "bonners ferry":  (48.6914, -116.3181),
    "rexburg":        (43.8260, -111.7897),
    "blackfoot":      (43.1905, -112.3447),
    "burley":         (42.5360, -113.7928),
    "jerome":         (42.7252, -114.5187),
    "mountain home":  (43.1330, -115.6912),
    "stanley":        (44.2160, -114.9311),
    "salmon":         (45.1758, -113.8957),
    "mccall":         (44.9111, -116.0987),
    "weiser":         (44.2510, -116.9690),
    "soda springs":   (42.6543, -111.6047),
    "preston":        (42.0963, -111.8766),
    "montpelier":     (42.3232, -111.2980),
}


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 3958.8  # Earth radius in miles
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _bearing_compass(lat1: float, lon1: float, lat2: float, lon2: float) -> str:
    """Compass bearing FROM (lat2, lon2) TO (lat1, lon1) -- i.e., 'event is
    <bearing> of town'. We orient so the event's bearing relative to the
    town reads naturally ("8 mi N of Plummer" = event is north of Plummer)."""
    phi1, phi2 = math.radians(lat2), math.radians(lat1)
    dl = math.radians(lon1 - lon2)
    x = math.sin(dl) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dl)
    brng = (math.degrees(math.atan2(x, y)) + 360) % 360
    points = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return points[int((brng + 22.5) // 45) % 8]


def _compute_distance_bearing(
    event_lat: Optional[float], event_lon: Optional[float], town: Optional[str]
) -> tuple[Optional[int], Optional[str]]:
    if event_lat is None or event_lon is None or not town:
        return None, None
    key = str(town).strip().lower()
    coords = _TOWN_COORDS.get(key)
    if coords is None:
        return None, None
    tlat, tlon = coords
    d = _haversine_miles(event_lat, event_lon, tlat, tlon)
    b = _bearing_compass(event_lat, event_lon, tlat, tlon)
    return int(round(d)), b


# ---------- road-name normalization ---------------------------------------

# SB/NB/EB/WB tokens inside a road name (e.g. "I-15 SB Off Ramp") collapse
# to a single cardinal letter ("I-15 S Off Ramp") for tighter mesh output.
_CARDINAL_TOKEN_RE = re.compile(r"\b(SB|NB|EB|WB)\b")
_CARDINAL_MAP = {"SB": "S", "NB": "N", "EB": "E", "WB": "W"}


def normalize_road_name(raw: Optional[str]) -> Optional[str]:
    """Tighten a raw roadway_name for mesh output:
      'I-15 SB Off Ramp' -> 'I-15 S Off Ramp'
      'US-95 NB'         -> 'US-95 N'
    Returns None for empty / None input.
    """
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    return _CARDINAL_TOKEN_RE.sub(lambda m: _CARDINAL_MAP[m.group(1)], s)


# Uninformative road names (Exit-only ramps with no parent route prefix
# visible) get dropped so the renderer leads with the town instead.
_UNINFORMATIVE_ROAD_RE = re.compile(
    r"^Exit\s+\d+.*\b(On|Off)\s+Ramp$",
    re.IGNORECASE,
)


def _is_uninformative_road(road: Optional[str]) -> bool:
    if not road:
        return False
    return bool(_UNINFORMATIVE_ROAD_RE.match(str(road).strip()))


# ---------- nearest_town: Photon /reverse + H3 cache ----------------------

# Photon is reachable from CT108 at this Tailscale address (verified
# 2026-06-04). It's the same Echo6-local Photon instance that backs Central's
# NaviBackend reverse-geocoder. Photon takes osm_tag=place (KEY only, not
# key:value with comma-list -- that returns 0 features -- per probe).
PHOTON_BASE_URL = "http://100.64.0.24:2322"
PHOTON_TIMEOUT_S = 2.0
PHOTON_RADIUS_KM = 80     # ≈ 50 miles
PHOTON_LIMIT = 10
# OSM place classes we accept as "town". Suburb included for metro coverage;
# locality is rare but valid for tiny rural places.
_TOWN_OSM_VALUES = frozenset({"city", "town", "village", "hamlet", "suburb", "locality"})


# Process-lifetime LRU cache keyed by H3 cell (resolution 7 ≈ 5km hexagons).
# Cells don't move and Photon's reverse output for a coord is stable, so
# entries never expire within a process lifetime. Cap at 10k entries.
_H3_CACHE_RESOLUTION = 7
_H3_CACHE_MAX = 10_000
_h3_cache: "OrderedDict[str, Optional[dict]]" = OrderedDict()


def _h3_cell(lat: float, lon: float) -> Optional[str]:
    try:
        import h3   # local import: keep module-import-time h3-free
        return h3.latlng_to_cell(lat, lon, _H3_CACHE_RESOLUTION)
    except Exception:
        # Fallback: coarse-grain by rounding coords (~1.1 km per 0.01 deg).
        return f"fallback:{round(lat, 2)},{round(lon, 2)}"


def _photon_reverse_places(lat: float, lon: float) -> list[dict]:
    """Call Photon /reverse with osm_tag=place. Return raw feature list."""
    qs = urllib.parse.urlencode({
        "lat":     f"{lat:.6f}",
        "lon":     f"{lon:.6f}",
        "radius":  PHOTON_RADIUS_KM,
        "osm_tag": "place",
        "limit":   PHOTON_LIMIT,
    })
    url = f"{PHOTON_BASE_URL}/reverse?{qs}"
    try:
        with urllib.request.urlopen(url, timeout=PHOTON_TIMEOUT_S) as resp:
            body = resp.read()
        d = json.loads(body)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            json.JSONDecodeError, ConnectionError) as e:
        logger.debug("Photon /reverse failed (%s) for %.4f,%.4f", e, lat, lon)
        return []
    feats = d.get("features") or []
    return feats if isinstance(feats, list) else []


def nearest_town(lat: float, lon: float, max_distance_mi: float = 50.0) -> Optional[dict]:
    """Return the nearest populated place to (lat, lon) within max_distance_mi.

    Result shape: {name: str, distance_mi: int (rounded), bearing: str}
    where bearing is an 8-point compass (N/NE/E/SE/S/SW/W/NW) of the event
    location relative to the town -- i.e. "8 mi N of Plummer" means the
    event is N of the town. Returns None if no town within range or if
    Photon is unreachable.

    Calls Photon /reverse?osm_tag=place at PHOTON_BASE_URL. Results are
    H3-cell-cached (resolution 7 ≈ 5 km cells) so the second event near
    the same town is free.
    """
    if lat is None or lon is None:
        return None
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None

    cell = _h3_cell(lat, lon)
    if cell is not None and cell in _h3_cache:
        # LRU touch
        _h3_cache.move_to_end(cell)
        cached = _h3_cache[cell]
        if cached is None or cached.get("distance_mi", 999) <= max_distance_mi:
            return cached

    feats = _photon_reverse_places(lat, lon)
    candidates: list[tuple[float, dict]] = []
    for f in feats:
        p = f.get("properties") or {}
        # Only accept proper populated places.
        if p.get("osm_key") != "place" or p.get("osm_value") not in _TOWN_OSM_VALUES:
            continue
        coords = (f.get("geometry") or {}).get("coordinates")
        if not (isinstance(coords, list) and len(coords) >= 2):
            continue
        tlon, tlat = coords[0], coords[1]
        try:
            tlat, tlon = float(tlat), float(tlon)
        except (TypeError, ValueError):
            continue
        d_mi = _haversine_miles(lat, lon, tlat, tlon)
        if d_mi > max_distance_mi:
            continue
        name = p.get("name")
        if not name:
            continue
        candidates.append((d_mi, {
            "name":        str(name),
            "distance_mi": int(round(d_mi)),
            "bearing":     _bearing_compass(lat, lon, tlat, tlon),
        }))

    if not candidates:
        if cell is not None:
            _h3_cache[cell] = None
            _h3_cache.move_to_end(cell)
            while len(_h3_cache) > _H3_CACHE_MAX:
                _h3_cache.popitem(last=False)
        return None

    candidates.sort(key=lambda kv: kv[0])
    result = candidates[0][1]
    if cell is not None:
        _h3_cache[cell] = result
        _h3_cache.move_to_end(cell)
        while len(_h3_cache) > _H3_CACHE_MAX:
            _h3_cache.popitem(last=False)
    return result


# ---------- per-adapter parsers -------------------------------------------

def _parse_state_511_atis(inner_data: dict, geo: dict) -> dict:
    desc = _clean_description(inner_data.get("description"))
    mile_start, mile_end = _parse_mile_posts(desc or "")
    ends_at = _parse_ends_at(desc or "")
    is_full = bool(inner_data.get("is_full_closure"))
    impact = "full_closure" if is_full else "partial"
    enriched = (inner_data.get("_enriched") or {}).get("geocoder") or {}

    # Road name normalization + uninformative drop.
    road = normalize_road_name(inner_data.get("roadway_name"))
    if _is_uninformative_road(road):
        road = None

    # Coordinates: prefer flat lat/lon, fall back to geo.centroid.
    event_lat = inner_data.get("latitude")
    event_lon = inner_data.get("longitude")
    if event_lat is None and geo.get("centroid"):
        try: event_lon, event_lat = geo["centroid"][0], geo["centroid"][1]
        except (IndexError, TypeError): pass

    # Town selection (Matt's locked plan, post-parse-everything decision):
    #   PRIMARY:   _enriched.geocoder.city  (Navi/Photon already chose it for us)
    #   SECONDARY: nearest_town(lat, lon) -- direct Photon nearest-place hit
    #   TERTIARY:  None  -- renderer drops the town segment
    # NEVER fall back to _enriched.geocoder.name -- that's nearest-feature
    # data (forest-service road numbers, generic street names) not town data.
    town = (enriched.get("city") or "").strip() or None
    distance_mi: Optional[int] = None
    bearing: Optional[str] = None
    if town:
        distance_mi, bearing = _compute_distance_bearing(event_lat, event_lon, town)
    else:
        # SECONDARY: ask Photon directly for the nearest populated place.
        nt = nearest_town(event_lat, event_lon) if event_lat is not None else None
        if nt:
            town        = nt.get("name")
            distance_mi = nt.get("distance_mi")
            bearing     = nt.get("bearing")

    return {
        "source":      "state_511_atis",
        "road":        road,
        "direction":   _norm_direction(inner_data.get("direction")),
        "mile_start":  mile_start,
        "mile_end":    mile_end,
        "description": desc,
        "sub_type":    _norm_sub_type(inner_data.get("event_sub_type")),
        "impact":      impact,
        "ends_at":     ends_at,
        "town":        town,
        "distance_mi": distance_mi,
        "bearing":     bearing,
    }


# ---------- wzdx federal vocabulary maps ----------------------------------

# FHWA WZDx v4 + custom-feed vocabulary observed in the wild. Unknown values
# fall through to lowercased + hyphens→spaces (see _norm_wzdx_sub_type).
_WZDX_WORK_TYPE_MAP: dict[str, Optional[str]] = {
    # WZDx v4 spec types_of_work.type_name enum:
    "maintenance":               "maintenance",
    "minor-road-defect-repair":  "minor repair",
    "roadside-work":             "roadside work",
    "overhead-work":             "overhead work",
    "below-road-work":           "subsurface work",
    "barrier-work":              "barrier work",
    "surface-work":              "surface work",
    "painting":                  "painting",
    "roadway-relocation":        "roadway relocation",
    "roadway-creation":          "new construction",
    # Common informal values seen in upstream feeds (ID, WA):
    "road-work":                 "road work",
    "paving":                    "paving",
    "bridge-construction":       "bridge construction",
    "bridge-maintenance":        "bridge maintenance",
    "utility-work":              "utility work",
    "road-construction":         "road construction",
    "construction":              "construction",
    "emergency-repairs":         "emergency repairs",
    # event_type values (drop the too-generic ones):
    "work-zone":                 None,
    "detour":                    "detour",
}


# vehicle_impact taxonomy (WZDx v4). Maps to mesh-friendly phrase.
# Returns None for values the renderer should drop entirely.
_WZDX_IMPACT_MAP: dict[str, Optional[str]] = {
    "all-lanes-closed":     "all lanes closed",
    "some-lanes-closed":    "lanes reduced",
    "alternating-one-way":  "one-way alternating",
    "unknown":              None,
    "all-lanes-open":       None,   # informational only; nothing to do
}


def _norm_wzdx_sub_type(raw) -> Optional[str]:
    if not raw: return None
    s = str(raw).strip().lower()
    if not s: return None
    if s in _WZDX_WORK_TYPE_MAP:
        return _WZDX_WORK_TYPE_MAP[s]
    # Unknown value — keep lowercased, hyphens → spaces, single-line.
    return re.sub(r"\s+", " ", s.replace("-", " ")).strip() or None


# ---------- per-adapter parser: wzdx federal ------------------------------

def _parse_wzdx_federal(inner_data: dict, geo: dict) -> dict:
    """Normalize a wzdx-adapter envelope (FHWA WZDx federal spec).

    Central flattens the upstream payload in practice (the FHWA-spec
    `core_details.*` nesting is not preserved), but we defensively check
    nested keys too so any future Central change doesn't silently regress.

    sub_type uses types_of_work[0].type_name when present, else event_type,
    each normalized via _WZDX_WORK_TYPE_MAP. impact_phrase is folded INTO
    the sub_type slot for the renderer (so the description-slot reads e.g.
    'lanes reduced, paving' or 'one-way alternating' or 'road work').
    'all lanes closed' is set on impact='full_closure' so the renderer's
    existing full-closure promotion handles it -- avoids double-printing.
    """
    cd = inner_data.get("core_details")
    if not isinstance(cd, dict): cd = {}
    def field(key):
        v = cd.get(key)
        if v is None or (isinstance(v, str) and not v.strip()):
            v = inner_data.get(key)
        return v

    # --- road (raw, verbatim per Matt's spec) -----------------------------
    road_names = field("road_names")
    road = None
    if isinstance(road_names, list) and road_names:
        road = str(road_names[0]).strip() or None
    elif isinstance(road_names, str) and road_names.strip():
        road = road_names.strip()
    if _is_uninformative_road(road):
        road = None

    # --- direction --------------------------------------------------------
    direction = _norm_direction(field("direction"))

    # --- sub_type (types_of_work[0] | event_type) -------------------------
    work_type: Optional[str] = None
    tow = field("types_of_work")
    if isinstance(tow, list) and tow:
        first = tow[0]
        if isinstance(first, dict):
            work_type = _norm_wzdx_sub_type(first.get("type_name"))
        elif isinstance(first, str):
            work_type = _norm_wzdx_sub_type(first)
    if not work_type:
        work_type = _norm_wzdx_sub_type(field("event_type"))

    # --- vehicle_impact ---------------------------------------------------
    vi_raw = (inner_data.get("vehicle_impact") or cd.get("vehicle_impact") or "")
    impact_phrase: Optional[str] = _WZDX_IMPACT_MAP.get(str(vi_raw).strip().lower())
    is_full_closure = (str(vi_raw).strip().lower() == "all-lanes-closed")

    # Fold impact_phrase + work_type into the renderer's sub_type slot.
    # For full-closure, exclude impact_phrase here -- the renderer prepends
    # "all lanes closed" itself via the impact='full_closure' branch.
    parts: list[str] = []
    if impact_phrase and not is_full_closure:
        parts.append(impact_phrase)
    if work_type:
        parts.append(work_type)
    sub_type = ", ".join(parts) if parts else None
    impact = "full_closure" if is_full_closure else "partial"

    # --- ends_at: structured end_date ISO-8601 ---------------------------
    ends_at: Optional[datetime] = None
    end_date = inner_data.get("end_date") or cd.get("end_date")
    if end_date:
        try:
            s = str(end_date).replace("Z", "+00:00")
            ends_at = datetime.fromisoformat(s)
            # Strip tzinfo so _format_end_short compares naive-to-naive.
            if ends_at.tzinfo is not None:
                ends_at = ends_at.astimezone().replace(tzinfo=None)
        except Exception:
            ends_at = None

    # --- mile_start/_end: regex on description, fall back to structured --
    desc = _clean_description(field("description"))
    mile_start, mile_end = _parse_mile_posts(desc or "")
    if mile_start is None:
        ms = inner_data.get("road_mile_post_start")
        if ms is not None:
            try: mile_start = int(ms)
            except (TypeError, ValueError): pass
    if mile_end is None:
        me = inner_data.get("road_mile_post_end")
        if me is not None:
            try: mile_end = int(me)
            except (TypeError, ValueError): pass

    # --- coordinates -----------------------------------------------------
    event_lat = inner_data.get("latitude")
    event_lon = inner_data.get("longitude")
    if event_lat is None and geo.get("centroid"):
        try: event_lon, event_lat = geo["centroid"][0], geo["centroid"][1]
        except (IndexError, TypeError): pass

    # --- town fallback chain (same as state_511_atis) --------------------
    enriched = (inner_data.get("_enriched") or {}).get("geocoder") or {}
    town = (enriched.get("city") or "").strip() or None
    distance_mi: Optional[int] = None
    bearing: Optional[str] = None
    if town:
        distance_mi, bearing = _compute_distance_bearing(event_lat, event_lon, town)
    elif event_lat is not None:
        nt = nearest_town(event_lat, event_lon)
        if nt:
            town        = nt.get("name")
            distance_mi = nt.get("distance_mi")
            bearing     = nt.get("bearing")

    return {
        "source":      "wzdx",
        "road":        road,
        "direction":   direction,
        "mile_start":  mile_start,
        "mile_end":    mile_end,
        "description": desc,
        "sub_type":    sub_type,
        "impact":      impact,
        "ends_at":     ends_at,
        "town":        town,
        "distance_mi": distance_mi,
        "bearing":     bearing,
    }



# ---------- WFIGS incidents (wildfire+prescribed) -------------------------

# IncidentName values like "IA 1", "IA 27" are auto-numbered Initial-Attack
# placeholders that WFIGS issues before a fire gets a proper name. We pass
# them through verbatim per Matt's call -- they at least signal "new fire
# in <county>" even without an interesting name.
_WFIGS_ACRES_KEYS = ("DailyAcres", "IncidentSize")
_WFIGS_ACRES_RAW_KEYS = ("DiscoveryAcres", "FinalAcres")
_WFIGS_CONTAINED_KEYS = ("PercentContained",)
_WFIGS_CONTAINED_RAW_KEYS = ("PercentContained",)


def _first_non_null(d: dict, keys) -> Any:
    """Return d[k] for the first k in keys with a non-null value, else None."""
    for k in keys:
        v = d.get(k)
        if v is not None and v != "":
            return v
    return None


def _parse_wfigs_acres(inner_data: dict) -> Optional[float]:
    """Acres fallback chain: top-level DailyAcres/IncidentSize -> raw.* -> None."""
    val = _first_non_null(inner_data, _WFIGS_ACRES_KEYS)
    if val is None:
        raw = inner_data.get("raw") or {}
        if isinstance(raw, dict):
            val = _first_non_null(raw, _WFIGS_ACRES_RAW_KEYS)
    if val is None:
        return None
    try: return float(val)
    except (TypeError, ValueError): return None


def _parse_wfigs_contained(inner_data: dict) -> Optional[int]:
    """Containment fallback chain: top-level PercentContained -> raw.* -> None."""
    val = _first_non_null(inner_data, _WFIGS_CONTAINED_KEYS)
    if val is None:
        raw = inner_data.get("raw") or {}
        if isinstance(raw, dict):
            val = _first_non_null(raw, _WFIGS_CONTAINED_RAW_KEYS)
    if val is None:
        return None
    try: return int(round(float(val)))
    except (TypeError, ValueError): return None


def _parse_wfigs_incidents(inner_data: dict, geo: dict) -> dict:
    """Normalize a WFIGS-incidents payload into a flat render-ready dict.

    Field shapes per Central v0.10.0 guide (see /OneDrive/.../wfigs-investigation.md):
      Top-level (incident): IrwinID, IncidentName, IncidentTypeCategory,
        latitude, longitude, FireDiscoveryDateTime (epoch-ms), POOState,
        POOCounty, DailyAcres, IncidentSize, PercentContained.
      Nested raw dict (97-key): DiscoveryAcres, FinalAcres, PercentContained
        (often the place where real values live in early season when the
        top-level fields haven't populated yet).
      _enriched.geocoder.landclass: optional ("Sawtooth National Forest", etc).

    Returns the normalized dict. Caller layers on "_kind": "wfigs_incident".
    """
    geocoder = geo.get("geocoder") or {}
    irwin_id = inner_data.get("IrwinID") or inner_data.get("irwin_id")
    name = inner_data.get("IncidentName")
    itype = inner_data.get("IncidentTypeCategory")
    lat = inner_data.get("latitude")
    lon = inner_data.get("longitude")
    county = inner_data.get("POOCounty")
    state = inner_data.get("POOState")
    landclass = geocoder.get("landclass")

    # FireDiscoveryDateTime is epoch-ms in WFIGS; convert to epoch-s.
    declared_at_epoch = None
    fdt = inner_data.get("FireDiscoveryDateTime")
    if isinstance(fdt, (int, float)):
        # Heuristic: anything >1e12 is ms (post-2001 in ms is ~1.4e12).
        declared_at_epoch = int(fdt / 1000) if fdt > 1e12 else int(fdt)

    acres = _parse_wfigs_acres(inner_data)
    contained_pct = _parse_wfigs_contained(inner_data)

    # Geocoder-side anchor enrichment for the renderer.
    city = geocoder.get("city")

    return {
        "irwin_id":           irwin_id,
        "incident_name":      name,
        "incident_type":      itype,
        "acres":              acres,
        "contained_pct":      contained_pct,
        "lat":                lat,
        "lon":                lon,
        "county":             county,
        "state":              state,
        "landclass":          landclass,
        "geocoder_city":      city,
        "declared_at_epoch":  declared_at_epoch,
    }


# ---------- public entry point --------------------------------------------

def normalize(envelope: dict) -> Optional[dict]:
    """Normalize a Central CloudEvents envelope into a flat render-ready dict.

    Returns None if the adapter has no normalizer wired yet (caller falls
    back to the existing meshai title path).
    """
    if not isinstance(envelope, dict): return None
    inner = envelope.get("data") or {}
    adapter = inner.get("adapter") or ""
    inner_data = inner.get("data") or {}
    geo = inner.get("geo") or {}

    if adapter == "state_511_atis":
        return _parse_state_511_atis(inner_data, geo)
    if adapter == "wzdx":
        return _parse_wzdx_federal(inner_data, geo)

    # v0.5.8 WFIGS dispatch -- incidents + tombstones + perimeters.
    # The handler downstream uses _kind to route to change-detection
    # (active incidents) or to event_log-only logging (tombstones,
    # perimeters). Tombstones carry only irwin_id + state + county;
    # perimeters share the IrwinID with their parent incident.
    category_raw = inner.get("category") or ""
    if adapter == "wfigs_incidents":
        if category_raw.startswith("fire.incident.removed"):
            return {
                "_kind":    "wfigs_tombstone",
                "irwin_id": inner_data.get("irwin_id") or inner_data.get("IrwinID"),
                "state":    inner_data.get("state") or inner_data.get("POOState"),
                "county":   inner_data.get("county") or inner_data.get("POOCounty"),
            }
        if category_raw.startswith("fire.incident"):
            n = _parse_wfigs_incidents(inner_data, geo)
            n["_kind"] = "wfigs_incident"
            return n
    if adapter == "wfigs_perimeters":
        return {
            "_kind":    "wfigs_perimeter",
            "irwin_id": inner_data.get("irwin_id") or inner_data.get("IrwinID"),
            "state":    inner_data.get("state") or inner_data.get("POOState"),
            "county":   inner_data.get("county") or inner_data.get("POOCounty"),
        }

    # Other adapters await per-adapter parsers; return None to defer.
    return None
