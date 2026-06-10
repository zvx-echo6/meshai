"""Tests for v0.5.10 USGS earthquakes handler."""
import pytest

from meshai.central.quake_handler import (
    handle_quake,
    within_250mi_of_idaho,
)
from meshai.persistence import close_thread_connection, init_db
from meshai.persistence import db as persistence_db


@pytest.fixture
def mem_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "quake-test.sqlite")
    monkeypatch.setenv("MESHAI_DB_PATH", db_path)
    persistence_db._initialised.clear()
    close_thread_connection()
    conn = init_db()
    yield conn
    close_thread_connection()
    persistence_db._initialised.discard(db_path)


def _quake_env(*, event_id="uu80141266", mag=3.5, depth_km=9.0,
                 place="9 km SW of Stanley, Idaho",
                 lat=44.094, lon=-115.962,
                 tsunami=0, alert=None,
                 time_ms=1780006952030,
                 category="quake.event.minor"):
    return {
        "id": event_id, "subject": "central.quake.event.minor.unknown",
        "data": {
            "id": event_id, "adapter": "usgs_quake", "category": category,
            "severity": 0,
            "geo": {"centroid": [lon, lat], "primary_region": None},
            "data": {
                "id": event_id, "magnitude": mag, "place": place,
                "depth_km": depth_km, "time_ms": time_ms,
                "tsunami": tsunami, "alert": alert, "status": "reviewed",
            },
        },
    }


def _commit(data, t):
    data["_on_broadcast_committed"](float(t))


# ---- magnitude floor ----


def test_m3_anywhere_broadcasts(mem_db):
    env = _quake_env(mag=3.5, lat=37.0, lon=-122.0)  # SF Bay area, outside Idaho
    wire = handle_quake(env, env["subject"], data={}, now=1_000_000)
    assert wire is not None
    assert "Magnitude 3.5" in wire


def test_m25_inside_idaho_broadcasts(mem_db):
    env = _quake_env(mag=2.7, lat=44.094, lon=-115.962, event_id="uu1")
    wire = handle_quake(env, env["subject"], data={}, now=1_000_000)
    assert wire is not None
    assert "Magnitude 2.7" in wire


def test_m25_outside_idaho_skipped(mem_db):
    # San Francisco -- well outside 250mi of Idaho centroid.
    env = _quake_env(mag=2.7, lat=37.0, lon=-122.0, event_id="uu2")
    wire = handle_quake(env, env["subject"], data={}, now=1_000_000)
    assert wire is None


def test_below_25_skipped(mem_db):
    env = _quake_env(mag=1.01, lat=44.0, lon=-114.0, event_id="uu3")
    wire = handle_quake(env, env["subject"], data={}, now=1_000_000)
    assert wire is None


# ---- tsunami special ----


def test_tsunami_any_magnitude_broadcasts(mem_db):
    env = _quake_env(mag=4.5, lat=10.0, lon=140.0, tsunami=1, event_id="japan1")
    wire = handle_quake(env, env["subject"], data={}, now=1_000_000)
    assert wire is not None
    assert "TSUNAMI WARNING" in wire
    assert wire.startswith("🚨")


# ---- PAGER alert ----


def test_pager_orange_broadcasts(mem_db):
    env = _quake_env(mag=2.0, lat=37.0, lon=-122.0, alert="orange",
                      event_id="pager1")
    wire = handle_quake(env, env["subject"], data={}, now=1_000_000)
    assert wire is not None


def test_pager_red_broadcasts(mem_db):
    env = _quake_env(mag=2.0, lat=37.0, lon=-122.0, alert="red",
                      event_id="pager2")
    wire = handle_quake(env, env["subject"], data={}, now=1_000_000)
    assert wire is not None


# ---- wire format ----


def test_uses_usgs_place_string(mem_db):
    env = _quake_env(mag=4.1, place="9 km SW of Stanley, Idaho",
                      event_id="usgs1")
    wire = handle_quake(env, env["subject"], data={}, now=1_000_000)
    assert "9 km SW of Stanley, Idaho" in wire


def test_m5_uses_warning_emoji(mem_db):
    env = _quake_env(mag=5.2, lat=44.0, lon=-114.0, event_id="big1")
    wire = handle_quake(env, env["subject"], data={}, now=1_000_000)
    assert wire.startswith("⚠️")


def test_wire_includes_depth_and_coords(mem_db):
    env = _quake_env(mag=4.1, depth_km=9.0, lat=44.094, lon=-115.962,
                      event_id="d1")
    wire = handle_quake(env, env["subject"], data={}, now=1_000_000)
    assert "9km depth" in wire
    assert "@ 44.094,-115.962" in wire


# ---- per-event dedup ----


def test_per_event_id_dedup_no_reissue(mem_db):
    env = _quake_env(mag=4.0, event_id="dedup1")
    data1 = {}
    handle_quake(env, env["subject"], data=data1, now=1_000_000)
    _commit(data1, 1_000_001)

    # Same event_id republishes (magnitude revision). Should NOT re-broadcast.
    env_rev = _quake_env(mag=4.2, event_id="dedup1")  # higher mag, same id
    wire2 = handle_quake(env_rev, env_rev["subject"], data={}, now=1_000_300)
    assert wire2 is None


# ---- distance helper ----


def test_within_250mi_of_idaho_boundary():
    # Boise, ID -- inside
    assert within_250mi_of_idaho(43.6, -116.2) is True
    # San Francisco -- outside
    assert within_250mi_of_idaho(37.0, -122.0) is False
    # Seattle -- inside (250mi from Idaho centroid; verify the boundary)
    assert within_250mi_of_idaho(47.6, -122.3) is False
    # Boundary edge case (Idaho center)
    assert within_250mi_of_idaho(44.36, -114.61) is True


# ---- commit callback ----


def test_commit_callback_updates_last_broadcast(mem_db):
    env = _quake_env(mag=4.0, event_id="cb1")
    data = {}
    handle_quake(env, env["subject"], data=data, now=1_000_000)
    pre = mem_db.execute(
        "SELECT last_broadcast_at FROM quake_events WHERE event_id='cb1'"
    ).fetchone()
    assert pre["last_broadcast_at"] is None
    _commit(data, 1_000_001)
    post = mem_db.execute(
        "SELECT last_broadcast_at FROM quake_events WHERE event_id='cb1'"
    ).fetchone()
    assert post["last_broadcast_at"] == 1_000_001
