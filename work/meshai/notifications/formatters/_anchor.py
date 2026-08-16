"""Shared location anchor resolver — Phase-2+.

resolve_anchor(lat, lon, *, max_mi) -> Optional[dict]

Returns {town: str, distance_mi: int, bearing: str} or None.

Priority:
  1. Curated ``town_anchors`` SQLite table (GUI-managed, haversine nearest).
  2. Photon reverse geocoder via geo.nearest_town() fallback.

The function is PURE (no side-effects beyond the LRU cache inside
nearest_town) and safe to call from any formatter.  Both the incident
formatter (Phase 2) and the WFIGS fire formatter (Phase 3) use it.

Haversine and bearing implementations are local copies so the module
has no runtime dependency on meshai.geo at import time (avoids the
circular import chain formatters → geo → persistence → formatters).
"""
from __future__ import annotations

import logging
import math
from typing import Optional

logger = logging.getLogger(__name__)


# ── Geometry helpers (mirrors meshai.geo exactly) ──────────────────────────


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 3958.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _bearing_compass(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> str:
    """Compass bearing from town (lat2, lon2) to event (lat1, lon1).

    Result is the direction the event lies relative to the town, so
    '8 mi N of Plummer' means the event is north of the town.
    Mirrors meshai.geo._bearing_compass and env.fire_render._location_anchor.
    """
    phi1, phi2 = math.radians(lat2), math.radians(lat1)
    dl = math.radians(lon1 - lon2)
    x = math.sin(dl) * math.cos(phi2)
    y = (math.cos(phi1) * math.sin(phi2)
         - math.sin(phi1) * math.cos(phi2) * math.cos(dl))
    brng = (math.degrees(math.atan2(x, y)) + 360) % 360
    points = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return points[int((brng + 22.5) // 45) % 8]


# ── Public API ────────────────────────────────────────────────────────────


def resolve_anchor(
    lat: float, lon: float, *, max_mi: float = 100.0
) -> Optional[dict]:
    """Find the nearest town to (lat, lon) within *max_mi* miles.

    Priority
    --------
    1. Curated ``town_anchors`` SQLite table — haversine nearest row with
       ``lat IS NOT NULL AND lon IS NOT NULL``.  GUI-managed and always
       tried first so curated entries beat the Photon fallback.
    2. ``geo.nearest_town()`` Photon reverse-geocoder — same H3-cached
       Photon call used by the WZDx/state_511_atis town-selection chain.

    Parameters
    ----------
    lat, lon : float
        Event coordinates.
    max_mi : float
        Maximum anchor distance in miles.  Default 100.

    Returns
    -------
    dict with keys ``town`` (str), ``distance_mi`` (int), ``bearing`` (str)
    — or ``None`` when no town is within *max_mi*.
    """
    if lat is None or lon is None:
        return None
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None

    # ── 1. Curated town_anchors table ────────────────────────────────────
    try:
        from meshai.persistence import get_db
        rows = get_db().execute(
            "SELECT name, lat, lon FROM town_anchors "
            "WHERE lat IS NOT NULL AND lon IS NOT NULL AND enabled = 1"
        ).fetchall()
        best = None
        best_d = float("inf")
        for row in rows:
            d = _haversine_miles(lat, lon, float(row["lat"]), float(row["lon"]))
            if d < best_d:
                best_d = d
                best = row
        if best is not None and best_d <= max_mi:
            bearing = _bearing_compass(lat, lon, float(best["lat"]), float(best["lon"]))
            return {
                "town":        best["name"].title(),
                "distance_mi": int(round(best_d)),
                "bearing":     bearing,
            }
    except Exception:
        logger.debug("resolve_anchor: town_anchors lookup failed, falling back to Photon")

    # ── 2. Photon nearest_town fallback ──────────────────────────────────
    try:
        from meshai.geo import nearest_town
        nt = nearest_town(lat, lon, max_distance_mi=max_mi)
        if nt and nt.get("name"):
            return {
                "town":        str(nt["name"]),
                "distance_mi": nt.get("distance_mi"),
                "bearing":     nt.get("bearing"),
            }
    except Exception:
        logger.debug("resolve_anchor: nearest_town fallback also failed")

    return None
