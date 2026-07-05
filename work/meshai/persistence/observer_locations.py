"""observer_locations accessors + config seeding (v23).

Ground-station coordinates for native satpass prediction. Follows the same
pattern as `persistence/curation.py`: a migration creates the table, a seed
routine populates it, and callers read via `get_observers()`.

Unlike curation's gauge_sites/town_anchors (seeded from Python constants),
observers are seeded from live config — `SatpassConfig.observers` is the
source of truth — so operators manage them in config.yaml / the dashboard
while the predictor reads the queryable table.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any, Optional

logger = logging.getLogger(__name__)


def upsert_observer(slug: str, name: str, lat: float, lon: float,
                    alt_m: float = 0.0, enabled: bool = True,
                    conn: Optional[sqlite3.Connection] = None) -> None:
    """Insert or update one observer row keyed on slug."""
    if conn is None:
        from meshai.persistence import get_db
        conn = get_db()
    conn.execute(
        "INSERT INTO observer_locations(slug, name, lat, lon, alt_m, enabled) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(slug) DO UPDATE SET "
        "name=excluded.name, lat=excluded.lat, lon=excluded.lon, "
        "alt_m=excluded.alt_m, enabled=excluded.enabled",
        (slug, name, float(lat), float(lon), float(alt_m),
         1 if enabled else 0),
    )


def get_observers(conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    """Return enabled observer rows as dicts (slug, name, lat, lon, alt_m)."""
    if conn is None:
        from meshai.persistence import get_db
        conn = get_db()
    rows = conn.execute(
        "SELECT slug, name, lat, lon, alt_m FROM observer_locations "
        "WHERE enabled=1 ORDER BY slug"
    ).fetchall()
    return [dict(r) for r in rows]


def seed_observers_from_config(satpass_config: Any,
                               conn: Optional[sqlite3.Connection] = None) -> int:
    """Upsert every observer defined in SatpassConfig.observers.

    Each config entry is a dict with keys: slug, name, lat, lon, and
    optional alt_m (default 0) and enabled (default True). Entries missing
    slug/name/lat/lon are skipped with a warning. Config is the source of
    truth, so this UPSERTs (not INSERT OR IGNORE) — edits in config
    propagate on the next startup. Returns the number of rows seeded.

    Intended to be called at startup after init_db(), where the loaded
    config object is available (see main.py `_init_components`).
    """
    observers = list(getattr(satpass_config, "observers", None) or [])
    if not observers:
        return 0
    if conn is None:
        from meshai.persistence import get_db
        conn = get_db()

    seeded = 0
    for obs in observers:
        try:
            slug = obs["slug"]
            name = obs["name"]
            lat = obs["lat"]
            lon = obs["lon"]
        except (KeyError, TypeError):
            logger.warning("observer_locations: skipping malformed observer entry: %r", obs)
            continue
        alt_m = obs.get("alt_m", 0.0)
        enabled = obs.get("enabled", True)
        upsert_observer(slug, name, lat, lon, alt_m, enabled, conn=conn)
        seeded += 1

    if seeded:
        logger.info("observer_locations: seeded %d observer(s) from config", seeded)
    return seeded
