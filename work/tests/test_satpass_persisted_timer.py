"""Tests for the satpass persisted-timer `due_at` column.

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
live is affected. What remains here:
  - `due_at` is persisted on the normal ingest path (satpass_handler.py,
    still live -- shared by both paths historically, now native-only)
  - SCHEMA_VERSION == 26 and the v22 migration (which added the due_at
    column) still applies cleanly on a fresh DB
"""
from __future__ import annotations

import json
import time

import pytest

from meshai.persistence import get_db, init_db, SCHEMA_VERSION
from meshai.adapter_config import invalidate_cache


# ── helpers ───────────────────────────────────────────────────────────

def _enable_satpass_db(norad_ids=(25544,), dry_run=True):
    """Enable satpass and set opt-in norad_ids in the test DB."""
    conn = get_db()
    conn.execute("UPDATE adapter_config SET value_json='true' "
                 "WHERE adapter='satpass' AND key='enabled'")
    conn.execute("UPDATE adapter_config SET value_json=? "
                 "WHERE adapter='satpass' AND key='dry_run'",
                 (json.dumps(bool(dry_run)),))
    conn.execute("UPDATE adapter_config SET value_json=? "
                 "WHERE adapter='satpass' AND key='norad_ids'",
                 (json.dumps(list(norad_ids)),))
    invalidate_cache()


def _ingest_envelope(norad_id=25544, observer="Boise", max_el=72.5,
                     aos="2026-06-12T03:32:00Z", los="2026-06-12T03:38:00Z"):
    return {
        "specversion": "1.0",
        "type": "central.sat.pass",
        "source": "central",
        "id": f"pass-{norad_id}-{aos}",
        "data": {
            "adapter": "n2yo_visualpasses",
            "category": "pass.n2yo_visualpasses",
            "data": {
                "norad_id": norad_id,
                "satellite_name": "ISS",
                "observer_name": observer,
                "max_elevation_deg": max_el,
                "aos_time": aos,
                "los_time": los,
                "azimuth_at_peak_compass": "S",
                "azimuth_at_aos_compass": "SW",
                "azimuth_at_los_compass": "NE",
            },
        },
    }


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


# ── due_at persisted on normal ingest ────────────────────────────────

def test_due_at_persisted_on_normal_ingest():
    """handle_satpass writes due_at = received_at + CONSOLIDATION_DELAY."""
    from meshai.central.satpass_handler import (
        handle_satpass, CONSOLIDATION_DELAY, _parse_iso_epoch)
    _enable_satpass_db(norad_ids=[25544], dry_run=True)
    for attr in ("_disabled_logged", "_no_norad_ids_logged"):
        if hasattr(handle_satpass, attr):
            delattr(handle_satpass, attr)

    env = _ingest_envelope()
    aos_epoch = _parse_iso_epoch("2026-06-12T03:32:00Z")
    now = aos_epoch - 300  # inside horizon, before los
    assert handle_satpass(env, "central.sat.pass.iss", now=now) is None

    conn = get_db()
    row = conn.execute(
        "SELECT received_at, due_at FROM satpass_pending "
        "WHERE norad_id=25544").fetchone()
    assert row is not None, "ingest did not write a pending row"
    assert row["due_at"] is not None
    assert row["due_at"] == row["received_at"] + CONSOLIDATION_DELAY
    assert row["due_at"] == now + CONSOLIDATION_DELAY
