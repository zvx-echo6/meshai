"""TLE cache handler — consumes central.sat.tle.> and upserts sat_tles.

Central publishes ~190 TLEs every ~4h on CENTRAL_SAT stream, subject
central.sat.tle.{norad_id}. Envelope payload path:
    data.data.{norad_id, satellite_name, tle_line1, tle_line2, epoch}

Upsert rule: latest-wins on epoch — skip if cached epoch >= incoming.
Read-time staleness: callers exclude epoch older than 14 days.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from meshai.persistence import get_db

logger = logging.getLogger(__name__)

# Rows with epoch older than this are stale (no tombstone upstream).
STALE_DAYS = 14


def handle_tle(envelope: dict, subject: str,
               data: Optional[dict] = None,
               now: Optional[int] = None) -> Optional[str]:
    """Process a TLE update from Central.

    Always returns None — TLE updates are storage-only, never broadcast.
    """
    if not isinstance(envelope, dict):
        return None

    inner = envelope.get("data") or {}
    adapter = inner.get("adapter") or ""

    # Accept both sat_tles and sat_passes adapter (Central may tag either)
    d = inner.get("data") or {}

    norad_id = d.get("norad_id")
    if norad_id is None:
        return None
    try:
        norad_id = int(norad_id)
    except (TypeError, ValueError):
        return None

    name = d.get("satellite_name") or d.get("name") or f"SAT-{norad_id}"
    line1 = d.get("tle_line1") or d.get("line1")
    line2 = d.get("tle_line2") or d.get("line2")
    epoch = d.get("epoch")

    if not line1 or not line2 or not epoch:
        logger.debug("tle_handler: missing line1/line2/epoch for NORAD %s", norad_id)
        return None

    now = now if now is not None else int(time.time())

    try:
        conn = get_db()
    except Exception:
        logger.exception("tle_handler: persistence unavailable")
        return None

    # Upsert: latest-wins on epoch
    existing = conn.execute(
        "SELECT epoch FROM sat_tles WHERE norad_id = ?",
        (norad_id,),
    ).fetchone()

    if existing is not None and existing["epoch"] >= str(epoch):
        # Cached epoch is same or newer — skip
        return None

    conn.execute(
        "INSERT INTO sat_tles(norad_id, name, line1, line2, epoch, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(norad_id) DO UPDATE SET "
        "name=excluded.name, line1=excluded.line1, line2=excluded.line2, "
        "epoch=excluded.epoch, updated_at=excluded.updated_at",
        (norad_id, name, line1, line2, str(epoch), now),
    )

    return None  # storage-only, never broadcast


def get_fresh_tles(conn=None, max_age_days: int = STALE_DAYS) -> list[dict]:
    """Return all TLEs with epoch within max_age_days of now.

    Each dict has: norad_id, name, line1, line2, epoch, updated_at.
    """
    if conn is None:
        conn = get_db()
    # epoch is ISO string; compare lexicographically against cutoff
    import datetime
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(days=max_age_days)).isoformat()
    rows = conn.execute(
        "SELECT norad_id, name, line1, line2, epoch, updated_at "
        "FROM sat_tles WHERE epoch >= ? ORDER BY name",
        (cutoff,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_tle_by_norad(norad_id: int, conn=None) -> Optional[dict]:
    """Return a single TLE by NORAD ID, or None if missing/stale."""
    if conn is None:
        conn = get_db()
    import datetime
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(days=STALE_DAYS)).isoformat()
    row = conn.execute(
        "SELECT norad_id, name, line1, line2, epoch, updated_at "
        "FROM sat_tles WHERE norad_id = ? AND epoch >= ?",
        (norad_id, cutoff),
    ).fetchone()
    return dict(row) if row else None


def search_tle_by_name(query: str, conn=None, limit: int = 5) -> list[dict]:
    """Fuzzy search TLEs by name (case-insensitive LIKE match).

    Returns up to `limit` fresh results sorted by name.
    """
    if conn is None:
        conn = get_db()
    import datetime
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(days=STALE_DAYS)).isoformat()
    rows = conn.execute(
        "SELECT norad_id, name, line1, line2, epoch, updated_at "
        "FROM sat_tles WHERE name LIKE ? AND epoch >= ? "
        "ORDER BY name LIMIT ?",
        (f"%{query}%", cutoff, limit),
    ).fetchall()
    return [dict(r) for r in rows]
