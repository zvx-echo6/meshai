"""LLM-persistence-gap tests: satpass reader, avalanche + ducting
durable tables (writer -> table -> env_reporter reader).

Uses the autouse conftest fixture which points MESHAI_DB_PATH at a fresh
tmp file and runs init_db (so all migrations, now through v25, apply).
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from meshai.env.avalanche import AvalancheAdapter
from meshai.env.ducting import DuctingAdapter
from meshai.notifications.env_reporter import EnvReporter
from meshai.persistence import get_db


@pytest.fixture
def reporter():
    return EnvReporter()


# ============================================================================
# migrations: the two new tables exist on a fresh boot
# ============================================================================


def test_new_tables_exist_after_init():
    conn = get_db()
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "avalanche_events" in names
    assert "ducting_events" in names


# ============================================================================
# 1. satpass reader (data already persists via the native satpass path)
# ============================================================================


def _seed_satpass(conn, *, event_id, sat_name, aos_at, norad_id=25544,
                   observer="Boise", max_elevation=45.0, los_at=None):
    now = int(time.time())
    conn.execute(
        "INSERT OR REPLACE INTO satpass_events(event_id, norad_id, sat_name, "
        "observer, max_elevation, aos_at, los_at, payload_json, first_seen_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (event_id, norad_id, sat_name, observer, max_elevation, aos_at,
         los_at or (aos_at + 600), "{}", now),
    )


def test_satpass_detail_empty_when_no_passes(reporter):
    assert reporter.build_satpass_detail() == ""


def test_satpass_detail_renders_upcoming(reporter):
    conn = get_db()
    now = int(time.time())
    _seed_satpass(conn, event_id="P1", sat_name="ISS (ZARYA)",
                  aos_at=now + 1800, max_elevation=52.0, observer="Boise")
    text = reporter.build_satpass_detail()
    assert "UPCOMING SATELLITE PASSES" in text
    assert "ISS (ZARYA)" in text
    assert "max el 52" in text
    assert "Boise" in text


def test_satpass_detail_ignores_past_passes(reporter):
    conn = get_db()
    now = int(time.time())
    # A pass whose AOS already happened must not appear.
    _seed_satpass(conn, event_id="P_old", sat_name="NOAA 19",
                  aos_at=now - 3600)
    assert reporter.build_satpass_detail() == ""


def test_satpass_detail_meta_off(reporter):
    conn = get_db()
    now = int(time.time())
    _seed_satpass(conn, event_id="P1", sat_name="ISS", aos_at=now + 600)
    conn.execute(
        "INSERT OR REPLACE INTO adapter_meta(adapter, include_in_llm_context, "
        "updated_at) VALUES ('satpass', 0, ?)", (time.time(),))
    assert reporter.build_satpass_detail() == ""


# ============================================================================
# 2. avalanche: adapter writer -> avalanche_events -> reader
# ============================================================================


def _avy_config():
    cfg = MagicMock()
    cfg.center_ids = ["SNFAC"]
    cfg.tick_seconds = 1800
    cfg.season_months = [12, 1, 2, 3, 4]
    return cfg


def test_avalanche_writer_persists_row_then_reader_reads_it(reporter):
    adapter = AvalancheAdapter(_avy_config())
    now = time.time()
    # Synthetic assessment mirroring _fetch()'s stored-event dict shape.
    adapter._events = [{
        "source": "avalanche",
        "event_id": "avy_SNFAC_banner_summit",
        "center_id": "SNFAC",
        "zone_name": "Banner Summit",
        "danger_level": 4,
        "danger_name": "High",
        "travel_advice": "Very dangerous avalanche conditions.",
        "lat": 44.3,
        "lon": -115.2,
        "expires": now + 6 * 3600,
        "fetched_at": now,
    }]
    adapter._persist_events()

    conn = get_db()
    row = conn.execute(
        "SELECT * FROM avalanche_events WHERE event_id=?",
        ("avy_SNFAC_banner_summit",)).fetchone()
    assert row is not None
    assert row["danger_level"] == 4
    assert row["zone_name"] == "Banner Summit"

    text = reporter.build_avalanche_detail()
    assert "AVALANCHE ADVISORIES" in text
    assert "Banner Summit" in text
    assert "High (4)" in text


def test_avalanche_writer_upserts_in_place(reporter):
    adapter = AvalancheAdapter(_avy_config())
    now = time.time()
    base = {
        "source": "avalanche",
        "event_id": "avy_SNFAC_z1",
        "center_id": "SNFAC",
        "zone_name": "Zone One",
        "danger_level": 2,
        "danger_name": "Moderate",
        "travel_advice": "",
        "lat": 44.0,
        "lon": -115.0,
        "expires": now + 6 * 3600,
        "fetched_at": now,
    }
    adapter._events = [dict(base)]
    adapter._persist_events()
    # danger rises -> re-persist should update the same row, not duplicate.
    adapter._events = [dict(base, danger_level=4, danger_name="High")]
    adapter._persist_events()

    conn = get_db()
    rows = conn.execute(
        "SELECT danger_level FROM avalanche_events WHERE event_id=?",
        ("avy_SNFAC_z1",)).fetchall()
    assert len(rows) == 1
    assert rows[0]["danger_level"] == 4


def test_avalanche_detail_excludes_expired(reporter):
    adapter = AvalancheAdapter(_avy_config())
    now = time.time()
    adapter._events = [{
        "source": "avalanche",
        "event_id": "avy_SNFAC_old",
        "center_id": "SNFAC",
        "zone_name": "Stale Zone",
        "danger_level": 3,
        "danger_name": "Considerable",
        "travel_advice": "",
        "lat": 44.0,
        "lon": -115.0,
        "expires": now - 3600,   # already expired
        "fetched_at": now,
    }]
    adapter._persist_events()
    assert reporter.build_avalanche_detail() == ""


def test_avalanche_detail_empty_when_no_rows(reporter):
    assert reporter.build_avalanche_detail() == ""


# ============================================================================
# 3. ducting: adapter writer -> ducting_events -> reader
# ============================================================================


def _ducting_config():
    cfg = MagicMock()
    cfg.latitude = 43.6
    cfg.longitude = -116.2
    cfg.tick_seconds = 10800
    return cfg


def test_ducting_writer_persists_row_then_reader_reads_it(reporter):
    adapter = DuctingAdapter(_ducting_config())
    adapter._status = {
        "condition": "surface_duct",
        "tier": "surface_duct",
        "min_gradient": -120.0,
        "duct_base_m": 110,
        "duct_thickness_m": 650,
        "assessment": "Ducting -- extended UHF range likely",
        "fetched_at": time.time(),
    }
    adapter._persist_status()

    conn = get_db()
    row = conn.execute(
        "SELECT * FROM ducting_events WHERE id=?",
        ("ducting_43.6_-116.2",)).fetchone()
    assert row is not None
    assert row["tier"] == "surface_duct"
    assert row["min_gradient"] == -120.0

    text = reporter.build_ducting_detail()
    assert "RF PROPAGATION" in text
    assert "surface_duct" in text
    assert "extended UHF range" in text


def test_ducting_writer_keeps_single_current_row(reporter):
    adapter = DuctingAdapter(_ducting_config())
    adapter._status = {
        "condition": "normal", "tier": "normal", "min_gradient": 118.0,
        "duct_base_m": None, "duct_thickness_m": None,
        "assessment": "Normal propagation", "fetched_at": time.time(),
    }
    adapter._persist_status()
    adapter._status = dict(adapter._status, condition="super_refraction",
                            tier="super_refraction", min_gradient=40.0,
                            assessment="Enhanced range possible",
                            fetched_at=time.time() + 1)
    adapter._persist_status()

    conn = get_db()
    rows = conn.execute(
        "SELECT tier FROM ducting_events WHERE id=?",
        ("ducting_43.6_-116.2",)).fetchall()
    assert len(rows) == 1
    assert rows[0]["tier"] == "super_refraction"


def test_ducting_detail_empty_when_no_rows(reporter):
    assert reporter.build_ducting_detail() == ""


def test_ducting_detail_meta_off(reporter):
    adapter = DuctingAdapter(_ducting_config())
    adapter._status = {
        "condition": "surface_duct", "tier": "surface_duct",
        "min_gradient": -120.0, "duct_base_m": 110, "duct_thickness_m": 650,
        "assessment": "Ducting", "fetched_at": time.time(),
    }
    adapter._persist_status()
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO adapter_meta(adapter, include_in_llm_context, "
        "updated_at) VALUES ('ducting', 0, ?)", (time.time(),))
    assert reporter.build_ducting_detail() == ""


# ============================================================================
# build_all wires the three new blocks
# ============================================================================


def test_build_all_includes_new_blocks(reporter):
    conn = get_db()
    now = int(time.time())
    _seed_satpass(conn, event_id="P1", sat_name="ISS", aos_at=now + 1200)

    avy = AvalancheAdapter(_avy_config())
    avy._events = [{
        "source": "avalanche", "event_id": "avy_SNFAC_z",
        "center_id": "SNFAC", "zone_name": "Banner", "danger_level": 4,
        "danger_name": "High", "travel_advice": "", "lat": 44.3, "lon": -115.2,
        "expires": time.time() + 6 * 3600, "fetched_at": time.time(),
    }]
    avy._persist_events()

    duct = DuctingAdapter(_ducting_config())
    duct._status = {
        "condition": "surface_duct", "tier": "surface_duct",
        "min_gradient": -120.0, "duct_base_m": 110, "duct_thickness_m": 650,
        "assessment": "Ducting", "fetched_at": time.time(),
    }
    duct._persist_status()

    text = reporter.build_all()
    assert "UPCOMING SATELLITE PASSES" in text
    assert "AVALANCHE ADVISORIES" in text
    assert "RF PROPAGATION" in text
