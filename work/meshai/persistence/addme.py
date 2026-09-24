"""Persistence for the MeshCore !addme feature (v32).

Pure audit trail: WHO AIDA added as a contact on the sender's behalf, HOW it
resolved their pubkey (contacts roster vs. CoreScope), and WHEN. This table
is never consulted to decide whether to (re)add a contact -- the live
MeshCore contact list (``MeshCoreTransport.get_contacts()``) is the source
of truth for that -- it exists purely so the !addme flow is auditable.

Follows the same conn=None / lazy get_db() pattern as
``meshai/persistence/observer_locations.py``.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Optional


def record_addme_contact(
    pubkey: str,
    name: str,
    source: str,
    added_at: Optional[float] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Record (or update) provenance for one !addme-added contact."""
    if conn is None:
        from meshai.persistence import get_db
        conn = get_db()
    if added_at is None:
        added_at = time.time()
    conn.execute(
        "INSERT INTO addme_provenance(pubkey, name, source, added_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(pubkey) DO UPDATE SET "
        "name=excluded.name, source=excluded.source, added_at=excluded.added_at",
        (pubkey.lower(), name, source, added_at),
    )


def get_addme_contact(
    pubkey: str, conn: Optional[sqlite3.Connection] = None
) -> Optional[dict]:
    """Return the provenance row for *pubkey*, or None if never recorded."""
    if conn is None:
        from meshai.persistence import get_db
        conn = get_db()
    row = conn.execute(
        "SELECT pubkey, name, source, added_at FROM addme_provenance WHERE pubkey=?",
        (pubkey.lower(),),
    ).fetchone()
    return dict(row) if row else None


def list_addme_contacts(conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    """Return all !addme provenance rows, most recent first."""
    if conn is None:
        from meshai.persistence import get_db
        conn = get_db()
    rows = conn.execute(
        "SELECT pubkey, name, source, added_at FROM addme_provenance "
        "ORDER BY added_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]
