"""WZDx (Work Zone Data Exchange) field-mapping/parsing helpers.

Relocated from the deleted Central-envelope adapter-normalizer module (a
per-adapter Central-envelope shaper with no live production caller, removed
in chore/ripout-2e-geo-normalizer) — this module now owns the FHWA WZDx v4 +
work-zone-description parsing logic. Its sole consumer is
`meshai.env.wzdx.WZDxAdapter._parse_feature`, so it lives right next to the
adapter that uses it rather than in a generic normalizer module.

`_parse_wzdx_federal(inner_data, geo) -> dict` is the entry point: it reads
either the raw WZDx `core_details.*` nesting or a flattened envelope and
returns a flat, render-ready dict (see the keys built at the bottom of
`_parse_wzdx_federal`) — road, direction, mile posts, folded sub_type,
impact, ends_at, and a town/distance/bearing anchor resolved via
`meshai.geo.nearest_town`.
"""

import re
from datetime import datetime
from typing import Optional

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


# ---------- description parsers --------------------------------------------

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


# ---------- description cleanup -------------------------------------------

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _clean_description(raw: Optional[str]) -> Optional[str]:
    if not raw: return None
    s = _HTML_TAG_RE.sub(" ", str(raw))
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


# ---------- uninformative road-name detection ------------------------------

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


# ---------- entry point: wzdx federal -------------------------------------

def _parse_wzdx_federal(inner_data: dict, geo: dict) -> dict:
    """Normalize a wzdx-adapter envelope (FHWA WZDx federal spec).

    Some feeds flatten the upstream payload in practice (the FHWA-spec
    `core_details.*` nesting is not always preserved), so this defensively
    checks nested keys too via the `field()` helper below.

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

    # --- town fallback chain: geocoder.city, else Photon nearest_town ----
    enriched = (inner_data.get("_enriched") or {}).get("geocoder") or {}
    town = (enriched.get("city") or "").strip() or None
    distance_mi: Optional[int] = None
    bearing: Optional[str] = None
    from meshai.geo import nearest_town, _compute_distance_bearing
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
