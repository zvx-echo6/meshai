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
    "aberdeen":           {"lat": 42.944098, "lon": -112.838381, "state": "ID"},
    "albion":             {"lat": 42.409808, "lon": -113.580438, "state": "ID"},
    "american falls":     {"lat": 42.782846, "lon": -112.854211, "state": "ID"},
    "ammon":              {"lat": 43.474999, "lon": -111.959631, "state": "ID"},
    "arbon valley":       {"lat": 42.88763, "lon": -112.589386, "state": "ID"},
    "arco":               {"lat": 43.631893, "lon": -113.301033, "state": "ID"},
    "arimo":              {"lat": 42.560385, "lon": -112.172927, "state": "ID"},
    "ashton":             {"lat": 44.073332, "lon": -111.448311, "state": "ID"},
    "athol":              {"lat": 47.947065, "lon": -116.707958, "state": "ID"},
    "avimor":             {"lat": 43.776181, "lon": -116.257108, "state": "ID"},
    "bancroft":           {"lat": 42.720241, "lon": -111.88301, "state": "ID"},
    "basalt":             {"lat": 43.314443, "lon": -112.165044, "state": "ID"},
    "bellevue":           {"lat": 43.467894, "lon": -114.254955, "state": "ID"},
    "bennington":         {"lat": 42.382259, "lon": -111.32098, "state": "ID"},
    "blackfoot":          {"lat": 43.1905, "lon": -112.3447, "state": "ID"},
    "blanchard":          {"lat": 48.0137, "lon": -116.996503, "state": "ID"},
    "bliss":              {"lat": 42.924284, "lon": -114.947516, "state": "ID"},
    "boise":              {"lat": 43.615, "lon": -116.2023, "state": "ID"},
    "bonners ferry":      {"lat": 48.6914, "lon": -116.3181, "state": "ID"},
    "buhl":               {"lat": 42.598362, "lon": -114.759536, "state": "ID"},
    "burley":             {"lat": 42.536, "lon": -113.7928, "state": "ID"},
    "caldwell":           {"lat": 43.6629, "lon": -116.6874, "state": "ID"},
    "cambridge":          {"lat": 44.571748, "lon": -116.678101, "state": "ID"},
    "carey":              {"lat": 43.312011, "lon": -113.941008, "state": "ID"},
    "cascade":            {"lat": 44.508761, "lon": -116.043627, "state": "ID"},
    "castleford":         {"lat": 42.520569, "lon": -114.871806, "state": "ID"},
    "challis":            {"lat": 44.505779, "lon": -114.228184, "state": "ID"},
    "chubbuck":           {"lat": 42.926182, "lon": -112.462537, "state": "ID"},
    "clark fork":         {"lat": 48.148019, "lon": -116.172988, "state": "ID"},
    "clifton":            {"lat": 42.187286, "lon": -112.004596, "state": "ID"},
    "coeur d'alene":      {"lat": 47.6777, "lon": -116.7805, "state": "ID"},
    "cottonwood":         {"lat": 46.051045, "lon": -116.349751, "state": "ID"},
    "council":            {"lat": 44.733195, "lon": -116.436837, "state": "ID"},
    "craigmont":          {"lat": 46.24174, "lon": -116.471344, "state": "ID"},
    "culdesac":           {"lat": 46.374896, "lon": -116.670097, "state": "ID"},
    "dalton gardens":     {"lat": 47.733412, "lon": -116.767873, "state": "ID"},
    "dayton":             {"lat": 42.111246, "lon": -111.984615, "state": "ID"},
    "deary":              {"lat": 46.800586, "lon": -116.557368, "state": "ID"},
    "declo":              {"lat": 42.519599, "lon": -113.628732, "state": "ID"},
    "dietrich":           {"lat": 42.912796, "lon": -114.266289, "state": "ID"},
    "donnelly":           {"lat": 44.733391, "lon": -116.086821, "state": "ID"},
    "dover":              {"lat": 48.259156, "lon": -116.609843, "state": "ID"},
    "downey":             {"lat": 42.428828, "lon": -112.123303, "state": "ID"},
    "driggs":             {"lat": 43.729865, "lon": -111.104319, "state": "ID"},
    "dubois":             {"lat": 44.171161, "lon": -112.228278, "state": "ID"},
    "eagle":              {"lat": 43.693423, "lon": -116.345989, "state": "ID"},
    "east hope":          {"lat": 48.240895, "lon": -116.28941, "state": "ID"},
    "eden":               {"lat": 42.605309, "lon": -114.209087, "state": "ID"},
    "emmett":             {"lat": 43.869228, "lon": -116.491336, "state": "ID"},
    "fairfield":          {"lat": 43.348045, "lon": -114.800826, "state": "ID"},
    "fernwood":           {"lat": 47.115099, "lon": -116.386484, "state": "ID"},
    "filer":              {"lat": 42.56789, "lon": -114.611471, "state": "ID"},
    "firth":              {"lat": 43.305788, "lon": -112.183454, "state": "ID"},
    "fort hall":          {"lat": 43.014547, "lon": -112.45787, "state": "ID"},
    "franklin":           {"lat": 42.009503, "lon": -111.802183, "state": "ID"},
    "fruitland":          {"lat": 44.020412, "lon": -116.922109, "state": "ID"},
    "garden city":        {"lat": 43.668297, "lon": -116.294389, "state": "ID"},
    "garden valley":      {"lat": 44.083404, "lon": -115.958464, "state": "ID"},
    "genesee":            {"lat": 46.551608, "lon": -116.928389, "state": "ID"},
    "georgetown":         {"lat": 42.479083, "lon": -111.363497, "state": "ID"},
    "glenns ferry":       {"lat": 42.949908, "lon": -115.308185, "state": "ID"},
    "gooding":            {"lat": 42.937048, "lon": -114.713188, "state": "ID"},
    "grace":              {"lat": 42.575182, "lon": -111.729771, "state": "ID"},
    "grand view":         {"lat": 42.985123, "lon": -116.09368, "state": "ID"},
    "grangeville":        {"lat": 45.925826, "lon": -116.121916, "state": "ID"},
    "greenleaf":          {"lat": 43.672607, "lon": -116.821443, "state": "ID"},
    "groveland":          {"lat": 43.223483, "lon": -112.37547, "state": "ID"},
    "hagerman":           {"lat": 42.816016, "lon": -114.897681, "state": "ID"},
    "hailey":             {"lat": 43.512674, "lon": -114.299499, "state": "ID"},
    "hammett":            {"lat": 42.944114, "lon": -115.465521, "state": "ID"},
    "hansen":             {"lat": 42.531365, "lon": -114.301177, "state": "ID"},
    "harrison":           {"lat": 47.469582, "lon": -116.808336, "state": "ID"},
    "hauser":             {"lat": 47.773694, "lon": -117.008, "state": "ID"},
    "hayden":             {"lat": 47.766, "lon": -116.7866, "state": "ID"},
    "hayden lake":        {"lat": 47.76446, "lon": -116.756097, "state": "ID"},
    "hazelton":           {"lat": 42.59548, "lon": -114.136614, "state": "ID"},
    "heyburn":            {"lat": 42.559982, "lon": -113.762067, "state": "ID"},
    "hidden springs":     {"lat": 43.716541, "lon": -116.259415, "state": "ID"},
    "hollister":          {"lat": 42.352911, "lon": -114.583846, "state": "ID"},
    "homedale":           {"lat": 43.615937, "lon": -116.939029, "state": "ID"},
    "horseshoe bend":     {"lat": 43.916085, "lon": -116.199236, "state": "ID"},
    "idaho city":         {"lat": 43.827844, "lon": -115.830474, "state": "ID"},
    "idaho falls":        {"lat": 43.4666, "lon": -112.034, "state": "ID"},
    "inkom":              {"lat": 42.796548, "lon": -112.254625, "state": "ID"},
    "iona":               {"lat": 43.527007, "lon": -111.930914, "state": "ID"},
    "irwin":              {"lat": 43.403359, "lon": -111.279513, "state": "ID"},
    "jerome":             {"lat": 42.7252, "lon": -114.5187, "state": "ID"},
    "juliaetta":          {"lat": 46.574737, "lon": -116.70808, "state": "ID"},
    "kamiah":             {"lat": 46.226796, "lon": -116.028303, "state": "ID"},
    "kellogg":            {"lat": 47.538, "lon": -116.1352, "state": "ID"},
    "kendrick":           {"lat": 46.614183, "lon": -116.661272, "state": "ID"},
    "ketchum":            {"lat": 43.687718, "lon": -114.380069, "state": "ID"},
    "kimberly":           {"lat": 42.534299, "lon": -114.369931, "state": "ID"},
    "kooskia":            {"lat": 46.141687, "lon": -115.973646, "state": "ID"},
    "kootenai":           {"lat": 48.311831, "lon": -116.517128, "state": "ID"},
    "kuna":               {"lat": 43.469607, "lon": -116.424153, "state": "ID"},
    "laclede":            {"lat": 48.167129, "lon": -116.751565, "state": "ID"},
    "lapwai":             {"lat": 46.403715, "lon": -116.804223, "state": "ID"},
    "lava hot springs":   {"lat": 42.620107, "lon": -112.009902, "state": "ID"},
    "lewiston":           {"lat": 46.4165, "lon": -117.0177, "state": "ID"},
    "lewisville":         {"lat": 43.695208, "lon": -112.013232, "state": "ID"},
    "lincoln":            {"lat": 43.51825, "lon": -111.969215, "state": "ID"},
    "mackay":             {"lat": 43.911996, "lon": -113.612728, "state": "ID"},
    "malad city":         {"lat": 42.189909, "lon": -112.249688, "state": "ID"},
    "marsing":            {"lat": 43.54636, "lon": -116.810422, "state": "ID"},
    "mccall":             {"lat": 44.9111, "lon": -116.0987, "state": "ID"},
    "mccammon":           {"lat": 42.648236, "lon": -112.189394, "state": "ID"},
    "melba":              {"lat": 43.373633, "lon": -116.531933, "state": "ID"},
    "menan":              {"lat": 43.721791, "lon": -111.992353, "state": "ID"},
    "meridian":           {"lat": 43.6121, "lon": -116.3915, "state": "ID"},
    "middleton":          {"lat": 43.711593, "lon": -116.615008, "state": "ID"},
    "montpelier":         {"lat": 42.3232, "lon": -111.298, "state": "ID"},
    "moreland":           {"lat": 43.21948, "lon": -112.437772, "state": "ID"},
    "moscow":             {"lat": 46.7324, "lon": -117.0002, "state": "ID"},
    "mountain home":      {"lat": 43.133, "lon": -115.6912, "state": "ID"},
    "mountain home afb":  {"lat": 43.049186, "lon": -115.86586, "state": "ID"},
    "moyie springs":      {"lat": 48.724746, "lon": -116.195421, "state": "ID"},
    "mud lake":           {"lat": 43.842855, "lon": -112.479504, "state": "ID"},
    "mullan":             {"lat": 47.468759, "lon": -115.796351, "state": "ID"},
    "nampa":              {"lat": 43.5407, "lon": -116.5635, "state": "ID"},
    "new meadows":        {"lat": 44.971335, "lon": -116.285195, "state": "ID"},
    "new plymouth":       {"lat": 43.970417, "lon": -116.818781, "state": "ID"},
    "newdale":            {"lat": 43.886385, "lon": -111.603888, "state": "ID"},
    "nezperce":           {"lat": 46.233582, "lon": -116.241418, "state": "ID"},
    "notus":              {"lat": 43.726863, "lon": -116.800432, "state": "ID"},
    "oakley":             {"lat": 42.24206, "lon": -113.883058, "state": "ID"},
    "oldtown":            {"lat": 48.182488, "lon": -117.018005, "state": "ID"},
    "orofino":            {"lat": 46.484943, "lon": -116.253027, "state": "ID"},
    "osburn":             {"lat": 47.505731, "lon": -116.000709, "state": "ID"},
    "paris":              {"lat": 42.227978, "lon": -111.402424, "state": "ID"},
    "parker":             {"lat": 43.958405, "lon": -111.759206, "state": "ID"},
    "parma":              {"lat": 43.786284, "lon": -116.942491, "state": "ID"},
    "paul":               {"lat": 42.605496, "lon": -113.784487, "state": "ID"},
    "payette":            {"lat": 44.080093, "lon": -116.926852, "state": "ID"},
    "pierce":             {"lat": 46.495335, "lon": -115.803292, "state": "ID"},
    "pinehurst":          {"lat": 47.536314, "lon": -116.231746, "state": "ID"},
    "plummer":            {"lat": 47.3344, "lon": -116.8856, "state": "ID"},
    "pocatello":          {"lat": 42.8713, "lon": -112.4455, "state": "ID"},
    "ponderay":           {"lat": 48.30478, "lon": -116.536645, "state": "ID"},
    "post falls":         {"lat": 47.718, "lon": -116.9516, "state": "ID"},
    "potlatch":           {"lat": 46.923493, "lon": -116.897713, "state": "ID"},
    "preston":            {"lat": 42.0963, "lon": -111.8766, "state": "ID"},
    "priest river":       {"lat": 48.18336, "lon": -116.884354, "state": "ID"},
    "rathdrum":           {"lat": 47.8121, "lon": -116.895, "state": "ID"},
    "rexburg":            {"lat": 43.826, "lon": -111.7897, "state": "ID"},
    "richfield":          {"lat": 43.05164, "lon": -114.155942, "state": "ID"},
    "rigby":              {"lat": 43.673587, "lon": -111.913525, "state": "ID"},
    "riggins":            {"lat": 45.420591, "lon": -116.317636, "state": "ID"},
    "ririe":              {"lat": 43.632494, "lon": -111.771717, "state": "ID"},
    "riverside":          {"lat": 43.196554, "lon": -112.435625, "state": "ID"},
    "roberts":            {"lat": 43.720488, "lon": -112.128868, "state": "ID"},
    "robie creek":        {"lat": 43.667649, "lon": -116.015203, "state": "ID"},
    "rockford":           {"lat": 43.189235, "lon": -112.530618, "state": "ID"},
    "rockford bay":       {"lat": 47.508637, "lon": -116.886536, "state": "ID"},
    "rockland":           {"lat": 42.573157, "lon": -112.87453, "state": "ID"},
    "rupert":             {"lat": 42.618936, "lon": -113.673967, "state": "ID"},
    "salmon":             {"lat": 45.1758, "lon": -113.8957, "state": "ID"},
    "sandpoint":          {"lat": 48.2766, "lon": -116.5535, "state": "ID"},
    "shelley":            {"lat": 43.379538, "lon": -112.126098, "state": "ID"},
    "shoshone":           {"lat": 42.936185, "lon": -114.404747, "state": "ID"},
    "silverton":          {"lat": 47.495681, "lon": -115.960513, "state": "ID"},
    "smelterville":       {"lat": 47.542423, "lon": -116.177448, "state": "ID"},
    "soda springs":       {"lat": 42.6543, "lon": -111.6047, "state": "ID"},
    "spirit lake":        {"lat": 47.965799, "lon": -116.869831, "state": "ID"},
    "st. anthony":        {"lat": 43.964839, "lon": -111.685049, "state": "ID"},
    "st. maries":         {"lat": 47.314589, "lon": -116.572235, "state": "ID"},
    "stanley":            {"lat": 44.216, "lon": -114.9311, "state": "ID"},
    "star":               {"lat": 43.702788, "lon": -116.491025, "state": "ID"},
    "sugar city":         {"lat": 43.87582, "lon": -111.751032, "state": "ID"},
    "sun valley":         {"lat": 43.683852, "lon": -114.334203, "state": "ID"},
    "swan valley":        {"lat": 43.442575, "lon": -111.324544, "state": "ID"},
    "teton":              {"lat": 43.887773, "lon": -111.672254, "state": "ID"},
    "tetonia":            {"lat": 43.814578, "lon": -111.158664, "state": "ID"},
    "troy":               {"lat": 46.737981, "lon": -116.773154, "state": "ID"},
    "twin falls":         {"lat": 42.563, "lon": -114.4609, "state": "ID"},
    "tyhee":              {"lat": 42.954001, "lon": -112.456199, "state": "ID"},
    "ucon":               {"lat": 43.593538, "lon": -111.959358, "state": "ID"},
    "victor":             {"lat": 43.60155, "lon": -111.110822, "state": "ID"},
    "wallace":            {"lat": 47.473578, "lon": -115.922542, "state": "ID"},
    "weippe":             {"lat": 46.37825, "lon": -115.938844, "state": "ID"},
    "weiser":             {"lat": 44.251, "lon": -116.969, "state": "ID"},
    "wendell":            {"lat": 42.77468, "lon": -114.70296, "state": "ID"},
    "weston":             {"lat": 42.036209, "lon": -111.977799, "state": "ID"},
    "wilder":             {"lat": 43.678388, "lon": -116.907585, "state": "ID"},
    "winchester":         {"lat": 46.240812, "lon": -116.624137, "state": "ID"},
    "worley":             {"lat": 47.400533, "lon": -116.919254, "state": "ID"},
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
