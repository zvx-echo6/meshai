"""Tests for the satpass persisted-timer `due_at` column (schema only).

Pending satellite-pass consolidations used to be scheduled only as in-memory
asyncio TimerHandles, so a restart orphaned any satpass_pending rows: the row
survived but its timer did not, and it was never consolidated/broadcast. The
fix persisted a durable `due_at` on each pending row and added a startup
sweep (`CentralConsumer._sweep_pending_satpass`) that reconstructed a timer
for every pending consolidated_id off its persisted due_at.

The Central NATS consumer (and `_sweep_pending_satpass` with it) was retired
2026-07 -- the sweep's tests are gone with it. The native satpass path
(env/satpass.py) never used the satpass_pending buffer or this sweep in the
first place (it consolidates in-memory within a single tick), so nothing
live is affected.

The ingest path that wrote `due_at` (`central.satpass_handler.handle_satpass`)
was itself dead -- its only caller was the already-retired Central consumer
-- and was deleted in the same pass that relocated the still-live satellite
code to `env.satellite` (2026-07). Its due_at-persistence test is gone with
it; the `satpass_pending` table (and its due_at/peak_compass columns) are
now unused by any live code path, but the migrations that created them are
left in place (schema changes are out of scope for that pass). What remains
here:
  - SCHEMA_VERSION == 26 and the v22 migration (which added the due_at
    column) still applies cleanly on a fresh DB
"""
from __future__ import annotations

import pytest

from meshai.persistence import get_db, init_db, SCHEMA_VERSION


# ── schema / migration ───────────────────────────────────────────────

def test_schema_version_is_current():
    # SCHEMA_VERSION is derived from the highest vN.sql in migrations/, so
    # this just guards against the derivation returning something bogus
    # (e.g. 0, which would mean the migrations dir wasn't found). The v22
    # migration (this file's focus) must always be <= the current version.
    assert SCHEMA_VERSION >= 22


def test_v22_migration_applies_and_adds_due_at_column(tmp_path, monkeypatch):
    """Fresh DB migrates cleanly and satpass_pending has due_at (v22 column)."""
    from meshai.persistence import close_thread_connection
    from meshai.persistence import db as persistence_db
    db = str(tmp_path / "fresh-v22.sqlite")
    monkeypatch.setenv("MESHAI_DB_PATH", db)
    persistence_db._initialised.clear()
    close_thread_connection()
    conn = init_db()
    row = conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
    assert int(row["value"]) == SCHEMA_VERSION
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(satpass_pending)")}
    assert "due_at" in cols
    close_thread_connection()
    persistence_db._initialised.discard(db)
