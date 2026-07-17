"""v0.6-4 curation accessors + seed routines.

Both tables (gauge_sites, town_anchors) follow the same pattern as
adapter_config: created by a migration, seeded from Python data on first
boot, then runtime reads from SQLite via cached accessors.

gauge_sites replaces idaho_gauge_sites.IDAHO_CURATED_SITES.
town_anchors replaces a hardcoded _TOWN_COORDS table that used to live in
the (since-deleted) Central-envelope adapter-normalizer module.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ============================================================================
# Seed data (the dict that used to live in handler code)
# ============================================================================

# Idaho stream-gauge sites originally from
# meshai/central/idaho_gauge_sites.py:IDAHO_CURATED_SITES (v0.5.12).
_GAUGE_SITES_SEED: dict[str, dict[str, Any]] = {
    "USGS-13139510": {
        "gauge_name":        "Big Lost River near Mackay",
        "lat": 43.910, "lon": -113.620,
        "action_ft": 5.5, "flood_minor_ft": 7.0,
        "flood_moderate_ft": None, "flood_major_ft": None,
    },
    "USGS-13186000": {
        "gauge_name":        "Snake River at Heise",
        "lat": 43.612, "lon": -111.654,
        "action_ft": 12.0, "flood_minor_ft": 14.0,
        "flood_moderate_ft": 16.0, "flood_major_ft": None,
    },
    "USGS-13037500": {
        "gauge_name":        "Snake River at Idaho Falls",
        "lat": 43.500, "lon": -112.034,
        "action_ft": 8.5, "flood_minor_ft": 10.0,
        "flood_moderate_ft": None, "flood_major_ft": None,
    },
    "USGS-13135500": {
        "gauge_name":        "Big Wood River near Hailey",
        "lat": 43.533, "lon": -114.318,
        "action_ft": 6.0, "flood_minor_ft": 7.5,
        "flood_moderate_ft": None, "flood_major_ft": None,
    },
    "USGS-13205000": {
        "gauge_name":        "Boise River near Boise",
        "lat": 43.690, "lon": -116.200,
        "action_ft": 8.0, "flood_minor_ft": 10.5,
        "flood_moderate_ft": None, "flood_major_ft": None,
    },
    "USGS-13247500": {
        "gauge_name":        "Payette River at Banks",
        "lat": 44.080, "lon": -116.130,
        "action_ft": 10.0, "flood_minor_ft": 12.0,
        "flood_moderate_ft": None, "flood_major_ft": None,
    },
    "USGS-13057000": {
        "gauge_name":        "Henrys Fork near Rexburg",
        "lat": 43.831, "lon": -111.781,
        "action_ft": 9.0, "flood_minor_ft": 10.5,
        "flood_moderate_ft": None, "flood_major_ft": None,
    },
    "USGS-13162225": {
        "gauge_name":        "Salmon Falls Creek near San Jacinto",
        "lat": 42.180, "lon": -114.850,
        "action_ft": 8.0, "flood_minor_ft": 10.0,
        "flood_moderate_ft": None, "flood_major_ft": None,
    },
    "USGS-13083000": {
        "gauge_name":        "Bear River near Border WY/ID",
        "lat": 42.214, "lon": -111.045,
        "action_ft": 6.0, "flood_minor_ft": 8.0,
        "flood_moderate_ft": None, "flood_major_ft": None,
    },
}

# Idaho + neighbor towns, originally from a hardcoded _TOWN_COORDS table in
# the (since-deleted) Central-envelope adapter-normalizer module.
_TOWN_ANCHORS_SEED: dict[str, dict[str, Any]] = {
    "boise":          {"lat": 43.6150, "lon": -116.2023, "state": "ID"},
    "meridian":       {"lat": 43.6121, "lon": -116.3915, "state": "ID"},
    "nampa":          {"lat": 43.5407, "lon": -116.5635, "state": "ID"},
    "caldwell":       {"lat": 43.6629, "lon": -116.6874, "state": "ID"},
    "idaho falls":    {"lat": 43.4666, "lon": -112.0340, "state": "ID"},
    "pocatello":      {"lat": 42.8713, "lon": -112.4455, "state": "ID"},
    "twin falls":     {"lat": 42.5630, "lon": -114.4609, "state": "ID"},
    "coeur d'alene":  {"lat": 47.6777, "lon": -116.7805, "state": "ID"},
    "lewiston":       {"lat": 46.4165, "lon": -117.0177, "state": "ID"},
    "moscow":         {"lat": 46.7324, "lon": -117.0002, "state": "ID"},
    "sandpoint":      {"lat": 48.2766, "lon": -116.5535, "state": "ID"},
    "post falls":     {"lat": 47.7180, "lon": -116.9516, "state": "ID"},
    "hayden":         {"lat": 47.7660, "lon": -116.7866, "state": "ID"},
    "rathdrum":       {"lat": 47.8121, "lon": -116.8950, "state": "ID"},
    "plummer":        {"lat": 47.3344, "lon": -116.8856, "state": "ID"},
    "kellogg":        {"lat": 47.5380, "lon": -116.1352, "state": "ID"},
    "bonners ferry":  {"lat": 48.6914, "lon": -116.3181, "state": "ID"},
    "rexburg":        {"lat": 43.8260, "lon": -111.7897, "state": "ID"},
    "blackfoot":      {"lat": 43.1905, "lon": -112.3447, "state": "ID"},
    "burley":         {"lat": 42.5360, "lon": -113.7928, "state": "ID"},
    "jerome":         {"lat": 42.7252, "lon": -114.5187, "state": "ID"},
    "mountain home":  {"lat": 43.1330, "lon": -115.6912, "state": "ID"},
    "stanley":        {"lat": 44.2160, "lon": -114.9311, "state": "ID"},
    "salmon":         {"lat": 45.1758, "lon": -113.8957, "state": "ID"},
    "mccall":         {"lat": 44.9111, "lon": -116.0987, "state": "ID"},
    "weiser":         {"lat": 44.2510, "lon": -116.9690, "state": "ID"},
    "soda springs":   {"lat": 42.6543, "lon": -111.6047, "state": "ID"},
    "preston":        {"lat": 42.0963, "lon": -111.8766, "state": "ID"},
    "montpelier":     {"lat": 42.3232, "lon": -111.2980, "state": "ID"},
}


# ============================================================================
# Caches
# ============================================================================

_LOCK = threading.Lock()
_gauge_cache: Optional[dict[str, dict[str, Any]]] = None
_town_cache: Optional[dict[str, tuple[float, float]]] = None


def invalidate_curation_cache() -> None:
    """Drop the in-memory caches. Called by the REST API on POST/PUT/DELETE."""
    global _gauge_cache, _town_cache
    with _LOCK:
        _gauge_cache = None
        _town_cache = None


def _load_gauge_cache() -> dict[str, dict[str, Any]]:
    global _gauge_cache
    if _gauge_cache is not None:
        return _gauge_cache
    try:
        from meshai.persistence import get_db
        conn = get_db()
        rows = conn.execute(
            "SELECT site_id, gauge_name, lat, lon, action_ft, flood_minor_ft, "
            "flood_moderate_ft, flood_major_ft FROM gauge_sites WHERE enabled=1"
        ).fetchall()
        cache = {r["site_id"]: {
            "gauge_name": r["gauge_name"],
            "lat": r["lat"], "lon": r["lon"],
            "action_ft": r["action_ft"],
            "flood_minor_ft": r["flood_minor_ft"],
            "flood_moderate_ft": r["flood_moderate_ft"],
            "flood_major_ft": r["flood_major_ft"],
        } for r in rows}
    except Exception:
        logger.exception("curation: gauge_sites cache load failed; using empty")
        cache = {}
    with _LOCK:
        _gauge_cache = cache
    return cache


def _load_town_cache() -> dict[str, tuple[float, float]]:
    global _town_cache
    if _town_cache is not None:
        return _town_cache
    try:
        from meshai.persistence import get_db
        conn = get_db()
        rows = conn.execute(
            "SELECT name, lat, lon FROM town_anchors WHERE enabled=1"
        ).fetchall()
        cache = {r["name"].lower(): (r["lat"], r["lon"]) for r in rows}
    except Exception:
        logger.exception("curation: town_anchors cache load failed; using empty")
        cache = {}
    with _LOCK:
        _town_cache = cache
    return cache


# ============================================================================
# Lookups (called from handlers)
# ============================================================================


def lookup_gauge_site(site_id: str) -> Optional[dict[str, Any]]:
    """Return the row dict for `site_id` (canonical 'USGS-...' form) or None."""
    cache = _load_gauge_cache()
    return cache.get(site_id)


def lookup_town_anchor(name: str) -> Optional[tuple[float, float]]:
    """Return (lat, lon) for the lowercased town name, or None."""
    if not name: return None
    cache = _load_town_cache()
    return cache.get(name.strip().lower())


# ============================================================================
# Seed routines (called from init_db after migrations)
# ============================================================================


def seed_gauge_sites(conn: sqlite3.Connection) -> int:
    """INSERT OR IGNORE one row per _GAUGE_SITES_SEED entry. Idempotent."""
    now = time.time()
    inserted = 0
    for site_id, spec in _GAUGE_SITES_SEED.items():
        cur = conn.execute(
            "INSERT OR IGNORE INTO gauge_sites("
            "site_id, gauge_name, lat, lon, action_ft, flood_minor_ft, "
            "flood_moderate_ft, flood_major_ft, enabled, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (site_id, spec["gauge_name"], spec["lat"], spec["lon"],
             spec.get("action_ft"), spec.get("flood_minor_ft"),
             spec.get("flood_moderate_ft"), spec.get("flood_major_ft"),
             1, now),
        )
        if cur.rowcount > 0:
            inserted += 1
    if inserted:
        logger.info("curation: seeded %d gauge_sites rows", inserted)
    return inserted


def seed_town_anchors(conn: sqlite3.Connection) -> int:
    """INSERT OR IGNORE one row per _TOWN_ANCHORS_SEED entry. Idempotent."""
    now = time.time()
    inserted = 0
    for name, spec in _TOWN_ANCHORS_SEED.items():
        cur = conn.execute(
            "INSERT OR IGNORE INTO town_anchors("
            "name, lat, lon, state, enabled, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (name, spec["lat"], spec["lon"], spec.get("state"), 1, now),
        )
        if cur.rowcount > 0:
            inserted += 1
    if inserted:
        logger.info("curation: seeded %d town_anchors rows", inserted)
    return inserted
