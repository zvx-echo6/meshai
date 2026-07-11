"""mesh_observations accessors (v28).

Durable backing store for MeshContext's passive mesh-traffic buffer.
Follows the same pattern as persistence/observer_locations.py: a migration
creates the table, MeshContext writes through to it on every observe() call
and loads recent rows back into its in-memory deque on startup so recap
context survives a container restart.

Fail-safe by design: every function here is expected to be called from
MeshContext, which wraps each call in try/except so a DB hiccup never
drops the in-memory (deque) observation path.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import Optional

logger = logging.getLogger(__name__)


def insert_observation(
    ts: float,
    transport: str,
    channel: Optional[int],
    is_dm: bool,
    sender_name: str,
    sender_id: str,
    text: str,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Write one observation through to SQLite."""
    if conn is None:
        from meshai.persistence import get_db
        conn = get_db()
    conn.execute(
        "INSERT INTO mesh_observations"
        "(ts, transport, channel, is_dm, sender_name, sender_id, text) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ts, transport, channel, 1 if is_dm else 0, sender_name, sender_id, text),
    )


def load_recent(
    max_age: int,
    limit: int,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    """Return up to `limit` observations newer than `max_age` seconds ago,
    oldest first (chronological -- ready to feed straight into the deque).
    """
    if conn is None:
        from meshai.persistence import get_db
        conn = get_db()
    cutoff = time.time() - max_age
    rows = conn.execute(
        "SELECT ts, transport, channel, is_dm, sender_name, sender_id, text "
        "FROM mesh_observations WHERE ts >= ? "
        "ORDER BY ts DESC LIMIT ?",
        (cutoff, limit),
    ).fetchall()
    out = [dict(r) for r in rows]
    out.reverse()  # newest-first -> chronological
    return out


def prune_older_than(
    max_age: int,
    conn: Optional[sqlite3.Connection] = None,
) -> int:
    """Delete rows older than max_age seconds. Returns rows deleted."""
    if conn is None:
        from meshai.persistence import get_db
        conn = get_db()
    cutoff = time.time() - max_age
    cur = conn.execute("DELETE FROM mesh_observations WHERE ts < ?", (cutoff,))
    return cur.rowcount if cur.rowcount is not None else 0
