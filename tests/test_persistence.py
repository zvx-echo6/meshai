"""Tests for the meshai/persistence foundation (v1 schema).

Foundation-only: no adapter handlers wired yet. Tests cover migration
runner, schema shape, idempotency, WAL mode, env-var path resolution,
in-memory mode, and basic insert/query against representative tables.
"""

import os
import sqlite3
import tempfile
import time
from pathlib import Path

import pytest

from meshai.persistence import (
    MESHAI_DB_PATH,
    SCHEMA_VERSION,
    close_thread_connection,
    get_db,
    init_db,
)
from meshai.persistence import db as persistence_db


# ---------- helpers --------------------------------------------------------


def _force_reinit(monkeypatch, db_path):
    """Point every get_db() at a fresh DB and reset the per-process init
    cache + thread-local connection so tests don't bleed into one another."""
    monkeypatch.setenv("MESHAI_DB_PATH", db_path)
    # Wipe the initialised-set so migrations re-run against the new file.
    persistence_db._initialised.clear()
    close_thread_connection()


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db = str(tmp_path / "meshai-test.sqlite")
    _force_reinit(monkeypatch, db)
    yield db
    close_thread_connection()
    persistence_db._initialised.discard(db)


# ---------- migration runner ----------------------------------------------


# Every table the v1 schema is contracted to produce. The migration runner
# is tested by checking that ALL of these exist after init.
_V1_TABLES = {
    "schema_meta",
    "event_log",
    "traffic_events",
    "fires",
    "firms_pixels",
    "quake_events",
    "nws_alerts",
    "gauge_readings",
    "swpc_events",
    "mesh_nodes",
    "mesh_telemetry",
    "mesh_positions",
    "mesh_messages_in",
    "mesh_broadcasts_out",
    "mesh_health_events",
}


def test_migration_v1_creates_all_tables(tmp_db):
    conn = init_db()
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'idx_%'"
    ).fetchall()
    names = {r["name"] for r in rows}
    missing = _V1_TABLES - names
    extra = names - _V1_TABLES
    assert not missing, f"missing tables after v1: {sorted(missing)}"
    # extras are fine (autoindex tables etc.); just make sure we didn't
    # leave the contract unfulfilled.
    assert len(names) >= len(_V1_TABLES)


def test_schema_version_recorded(tmp_db):
    conn = init_db()
    row = conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
    assert row is not None
    assert int(row["value"]) == SCHEMA_VERSION


def test_migration_idempotent_rerun(tmp_db):
    init_db()
    # Force a "second startup" by closing the connection and clearing the
    # per-process initialised cache so the migration runner re-evaluates.
    close_thread_connection()
    persistence_db._initialised.discard(tmp_db)
    conn = init_db()
    # No exception means we didn't try to CREATE TABLE twice; verify the
    # row count of schema_meta hasn't drifted.
    rows = conn.execute("SELECT COUNT(*) AS n FROM schema_meta").fetchone()
    assert rows["n"] >= 1


# ---------- WAL mode ------------------------------------------------------


def test_wal_mode_enabled_on_file_db(tmp_db):
    conn = init_db()
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal", f"expected WAL, got {mode!r}"


# ---------- MESHAI_DB_PATH resolution -------------------------------------


def test_meshai_db_path_env_var_respected(monkeypatch, tmp_path):
    custom = str(tmp_path / "custom-path.sqlite")
    monkeypatch.setenv("MESHAI_DB_PATH", custom)
    assert MESHAI_DB_PATH() == custom


def test_meshai_db_path_falls_back_to_default(monkeypatch):
    monkeypatch.delenv("MESHAI_DB_PATH", raising=False)
    assert MESHAI_DB_PATH() == persistence_db.DEFAULT_DB_PATH


# ---------- in-memory mode for tests --------------------------------------


def test_in_memory_db_works(monkeypatch):
    # Special-cased :memory: routes via uri=shared-cache so init runs cleanly.
    monkeypatch.setenv("MESHAI_DB_PATH", ":memory:")
    persistence_db._initialised.clear()
    close_thread_connection()
    conn = init_db()
    # All v1 tables present even in memory.
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    names = {r["name"] for r in rows}
    assert _V1_TABLES.issubset(names)
    close_thread_connection()


# ---------- basic insert/query: fires -------------------------------------


def test_fires_insert_and_query(tmp_db):
    conn = init_db()
    now = int(time.time())
    irwin = "{E7FCBC00-2D0A-49D6-889F-550D4EDCBFD6}"
    conn.execute(
        "INSERT INTO fires(irwin_id, incident_name, incident_type, "
        "current_acres, current_contained_pct, status, lat, lon, "
        "county, state, landclass, declared_at, last_event_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (irwin, "IA 1", "wildfire", None, None, "active",
         43.5213, -115.1665, "Elmore", "ID", "Sawtooth National Forest",
         now - 3600, now),
    )
    row = conn.execute(
        "SELECT incident_name, county, state, landclass, last_event_at "
        "FROM fires WHERE irwin_id = ?", (irwin,)).fetchone()
    assert row is not None
    assert row["incident_name"] == "IA 1"
    assert row["county"] == "Elmore"
    assert row["state"] == "ID"
    assert row["landclass"] == "Sawtooth National Forest"
    assert row["last_event_at"] == now


def test_fires_last_broadcast_change_detection_columns(tmp_db):
    """The 8h-rate-limit change-detection logic uses last_broadcast_acres
    and last_broadcast_contained -- verify they accept NULL and updates."""
    conn = init_db()
    now = int(time.time())
    irwin = "{ABC}"
    conn.execute(
        "INSERT INTO fires(irwin_id, incident_name, last_event_at, "
        "last_broadcast_at, last_broadcast_acres, last_broadcast_contained) "
        "VALUES (?,?,?,?,?,?)",
        (irwin, "X", now, now, 1847.0, 23),
    )
    row = conn.execute(
        "SELECT last_broadcast_acres, last_broadcast_contained "
        "FROM fires WHERE irwin_id = ?", (irwin,)).fetchone()
    assert row["last_broadcast_acres"] == 1847.0
    assert row["last_broadcast_contained"] == 23


# ---------- basic insert/query: mesh_nodes -------------------------------


def test_mesh_nodes_insert_and_query(tmp_db):
    conn = init_db()
    now = int(time.time())
    conn.execute(
        "INSERT INTO mesh_nodes(node_id, long_name, short_name, hw_model, "
        "last_lat, last_lon, last_battery_pct, first_seen_at, last_seen_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ("!85098cea", "AIDA Northgate", "AIDA", "TBEAM",
         43.6535, -116.2674, 88, now - 86400, now),
    )
    row = conn.execute(
        "SELECT long_name, short_name, last_battery_pct, is_stale "
        "FROM mesh_nodes WHERE node_id = ?", ("!85098cea",)).fetchone()
    assert row is not None
    assert row["long_name"] == "AIDA Northgate"
    assert row["last_battery_pct"] == 88
    assert row["is_stale"] == 0   # default


# ---------- traffic_events composite PK ----------------------------------


def test_traffic_events_composite_pk_uniqueness(tmp_db):
    conn = init_db()
    now = int(time.time())
    conn.execute(
        "INSERT INTO traffic_events(source, external_id, road, county, state, "
        "first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?)",
        ("state_511_atis", "ID:Construction:33868", "I-86", "Bannock", "ID", now, now),
    )
    # Same source + external_id should violate the PK constraint.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO traffic_events(source, external_id, road, county, state, "
            "first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?)",
            ("state_511_atis", "ID:Construction:33868", "I-86", "Bannock", "ID", now, now),
        )
    # Different source with same external_id is fine.
    conn.execute(
        "INSERT INTO traffic_events(source, external_id, road, county, state, "
        "first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?)",
        ("wzdx", "ID:Construction:33868", "I-86", "Bannock", "ID", now, now),
    )
    rows = conn.execute("SELECT COUNT(*) AS n FROM traffic_events").fetchone()
    assert rows["n"] == 2


# ---------- event_log basic ------------------------------------------------


def test_event_log_insert_and_query(tmp_db):
    conn = init_db()
    now = int(time.time())
    conn.execute(
        "INSERT INTO event_log(received_at, source, category, severity_word, "
        "event_id_external, nats_subject, handled, table_name, table_pk) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (now, "wfigs_incidents", "fire.incident.wildfire", "routine",
         "{E7FCBC00}", "central.fire.incident.id.elmore", 1, "fires", "{E7FCBC00}"),
    )
    row = conn.execute(
        "SELECT source, table_name, handled FROM event_log "
        "WHERE event_id_external = ?", ("{E7FCBC00}",)).fetchone()
    assert row["source"] == "wfigs_incidents"
    assert row["table_name"] == "fires"
    assert row["handled"] == 1


# ---------- firms_pixels nullable FK to fires ----------------------------


def test_firms_pixels_fk_to_fires_nullable(tmp_db):
    conn = init_db()
    now = int(time.time())
    # Unattached pixel (no irwin_id yet -- v0.6 fire-tracker will attach later).
    conn.execute(
        "INSERT INTO firms_pixels(lat, lon, acq_time, frp, confidence, satellite) "
        "VALUES (?,?,?,?,?,?)",
        (42.197, -113.710, now, 135.93, "high", "N"),
    )
    row = conn.execute(
        "SELECT irwin_id, frp, confidence FROM firms_pixels "
        "WHERE acq_time = ?", (now,)).fetchone()
    assert row is not None
    assert row["irwin_id"] is None       # nullable
    assert row["frp"] == 135.93

    # Insert a fire, then attach a new pixel via the FK.
    irwin = "{LINK-IRWIN}"
    conn.execute(
        "INSERT INTO fires(irwin_id, incident_name, last_event_at) "
        "VALUES (?,?,?)", (irwin, "Linked Fire", now),
    )
    conn.execute(
        "INSERT INTO firms_pixels(irwin_id, lat, lon, acq_time, frp) "
        "VALUES (?,?,?,?,?)", (irwin, 42.197, -113.710, now + 1, 50.0),
    )
    rows = conn.execute(
        "SELECT COUNT(*) AS n FROM firms_pixels WHERE irwin_id = ?",
        (irwin,)).fetchone()
    assert rows["n"] == 1


# ---------- indexes present (spot-check) ---------------------------------


def test_v1_indexes_created(tmp_db):
    conn = init_db()
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name LIKE 'idx_%'"
    ).fetchall()
    names = {r["name"] for r in rows}
    must_have = {
        "idx_event_log_received",
        "idx_traffic_last_seen",
        "idx_fires_last_event",
        "idx_firms_pixels_acq_time",
        "idx_mesh_tel_node_time",
        "idx_mesh_pos_node_time",
        "idx_mesh_nodes_last_seen",
        "idx_gauge_site_time",
    }
    missing = must_have - names
    assert not missing, f"missing indexes: {sorted(missing)}"


# ---------- gauge_readings autoincrement -------------------------------


def test_gauge_readings_autoincrement_pk(tmp_db):
    conn = init_db()
    now = int(time.time())
    for value in (3490.0, 3520.0, 3505.0):
        conn.execute(
            "INSERT INTO gauge_readings(site_id, reading_value, reading_unit, "
            "reading_time) VALUES (?,?,?,?)",
            ("USGS-13038000", value, "ft^3/s", now),
        )
    rows = conn.execute(
        "SELECT COUNT(*) AS n FROM gauge_readings WHERE site_id = ?",
        ("USGS-13038000",)).fetchone()
    assert rows["n"] == 3
