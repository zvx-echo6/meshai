"""NWS zone-geometry resolver with persistent SQLite cache.

Zones at api.weather.gov/zones/* are static (published zone boundaries that
change only when NWS redraws them — rare, O(years)).  We cache resolved
geometries in the shared meshai DB so repeated polls for the same alert don't
hit the NWS API on every tick.  A process-level in-memory dict provides a
hot-path shortcut inside a single run.

Public API
----------
    resolve_zones_geometry(zone_urls) -> dict | None
        Combine a list of NWS zone URLs into ONE GeoJSON MultiPolygon.

Internal helper exposed for monkeypatching in tests
----------------------------------------------------
    _fetch_zone(url) -> dict | None
        Single-zone HTTP fetch.  Factor it out so tests can inject geometry
        without touching the network at all.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from meshai.persistence import get_db

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# In-memory hot cache: url -> geometry dict or None
# (None = confirmed "this zone has no geometry"; still authoritative.)
# --------------------------------------------------------------------------
_mem_cache: dict[str, Optional[dict]] = {}

_NWS_USER_AGENT = "(meshai, ops@echo6.co)"
_FETCH_TIMEOUT = 8  # seconds

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS nws_zone_cache (
    url          TEXT PRIMARY KEY,
    geometry_json TEXT,
    fetched_at   REAL
)
"""


# --------------------------------------------------------------------------
# Internal helpers
# --------------------------------------------------------------------------

def _ensure_table(conn) -> None:
    """Create the cache table if it doesn't already exist (idempotent)."""
    conn.execute(_CREATE_TABLE_SQL)


def _fetch_zone(url: str) -> Optional[dict]:
    """Fetch a single NWS zone URL and return its .geometry (or None).

    This function is intentionally thin so tests can monkeypatch it without
    needing to stub urllib internals.  Any network/parse error returns None
    and is logged at DEBUG level (not ERROR — zone miss is non-fatal).
    """
    req = Request(
        url,
        headers={
            "User-Agent": _NWS_USER_AGENT,
            "Accept": "application/geo+json",
        },
    )
    try:
        with urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("geometry")  # may legitimately be None
    except (HTTPError, URLError, OSError) as exc:
        logger.debug("nws_zones: fetch failed for %s: %s", url, exc)
        return None
    except Exception as exc:
        logger.debug("nws_zones: unexpected error fetching %s: %s", url, exc)
        return None


def _load_cached(conn, url: str) -> tuple[bool, Optional[dict]]:
    """Return (cache_hit, geometry).  geometry may be None for known-null zones."""
    _ensure_table(conn)
    row = conn.execute(
        "SELECT geometry_json FROM nws_zone_cache WHERE url = ?",
        (url,),
    ).fetchone()
    if row is None:
        return False, None
    raw_json = row[0]
    if raw_json is None:
        return True, None  # cached null marker — zone has no geometry
    try:
        return True, json.loads(raw_json)
    except Exception:
        return True, None  # corrupt cache row — treat as null


def _store_cached(conn, url: str, geometry: Optional[dict]) -> None:
    """Persist a resolved geometry (or a null marker) for the given URL."""
    _ensure_table(conn)
    raw_json = json.dumps(geometry) if geometry is not None else None
    conn.execute(
        "INSERT OR REPLACE INTO nws_zone_cache(url, geometry_json, fetched_at)"
        " VALUES(?, ?, ?)",
        (url, raw_json, time.time()),
    )


def _get_zone_geometry(url: str) -> Optional[dict]:
    """Return the GeoJSON geometry for a single zone URL (multi-level cached).

    Resolution order:
        1. In-memory dict (hot path, no I/O)
        2. SQLite nws_zone_cache table
        3. HTTP fetch via NWS API → write-through to both caches
    """
    # 1. In-memory hit
    if url in _mem_cache:
        return _mem_cache[url]

    # 2. Persistent cache
    conn = get_db()
    found, geom = _load_cached(conn, url)
    if found:
        _mem_cache[url] = geom
        return geom

    # 3. Fetch from NWS (delegates to _fetch_zone — monkeypatchable)
    geom = _fetch_zone(url)
    _mem_cache[url] = geom
    _store_cached(conn, url, geom)
    return geom


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def resolve_zones_geometry(zone_urls: list) -> Optional[dict]:
    """Resolve a list of NWS zone URLs to ONE combined GeoJSON MultiPolygon.

    Fetches each zone's geometry (cached persistently — zones are static), and
    concatenates every zone's polygon(s) into a single MultiPolygon.  Returns
    None when the list is empty or NONE of the zones resolve (caller then keeps
    the alert's geometry as None → existing fail-closed behaviour).
    Fully resilient: any per-zone fetch/parse error skips that zone.

    Combination rules
    -----------------
    - Polygon  → its single coordinate ring-list appended as one MP entry
    - MultiPolygon → each of its coordinate ring-lists appended as MP entries
    - Anything else (e.g. Point, GeometryCollection) → skipped
    """
    if not zone_urls:
        return None

    combined: list = []
    for url in zone_urls:
        try:
            geom = _get_zone_geometry(url)
            if not geom:
                continue
            gtype = geom.get("type")
            coords = geom.get("coordinates")
            if not coords:
                continue
            if gtype == "Polygon":
                # A Polygon's coordinates is [outer_ring, *holes].
                # In a MultiPolygon each entry IS [outer_ring, *holes].
                combined.append(coords)
            elif gtype == "MultiPolygon":
                # A MultiPolygon's coordinates is [[ring_list], [ring_list], …]
                combined.extend(coords)
            # Unsupported geometry type → skip silently
        except Exception as exc:
            logger.debug("nws_zones: error resolving zone %s: %s", url, exc)

    if not combined:
        return None

    return {"type": "MultiPolygon", "coordinates": combined}
