"""Tests for observer_locations (v23): migration, accessors, seed-from-config."""
from __future__ import annotations

import pytest

from meshai.config import SatpassConfig
from meshai.persistence import SCHEMA_VERSION, get_db
from meshai.persistence.observer_locations import (
    get_observers,
    seed_observers_from_config,
    upsert_observer,
)


# -- schema / migration -------------------------------------------------------

def test_schema_version_is_23():
    assert SCHEMA_VERSION == 23


def test_observer_locations_table_exists():
    conn = get_db()
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "observer_locations" in tables


def test_schema_meta_at_23():
    conn = get_db()
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key='version'").fetchone()
    assert int(row["value"]) == 23


# -- accessors ----------------------------------------------------------------

def test_upsert_and_get_roundtrip():
    upsert_observer("boise", "Boise", 43.615, -116.202, alt_m=824.0)
    obs = get_observers()
    assert len(obs) == 1
    r = obs[0]
    assert r["slug"] == "boise"
    assert r["name"] == "Boise"
    assert r["lat"] == pytest.approx(43.615)
    assert r["lon"] == pytest.approx(-116.202)
    assert r["alt_m"] == pytest.approx(824.0)


def test_upsert_updates_existing():
    upsert_observer("site1", "Old Name", 40.0, -110.0)
    upsert_observer("site1", "New Name", 41.0, -111.0, alt_m=500.0)
    obs = {o["slug"]: o for o in get_observers()}
    assert obs["site1"]["name"] == "New Name"
    assert obs["site1"]["lat"] == pytest.approx(41.0)
    assert obs["site1"]["alt_m"] == pytest.approx(500.0)


def test_disabled_observers_excluded():
    upsert_observer("on", "Enabled", 40.0, -110.0, enabled=True)
    upsert_observer("off", "Disabled", 41.0, -111.0, enabled=False)
    slugs = {o["slug"] for o in get_observers()}
    assert "on" in slugs
    assert "off" not in slugs


def test_alt_m_defaults_to_zero():
    upsert_observer("noalt", "No Altitude", 40.0, -110.0)
    r = get_observers()[0]
    assert r["alt_m"] == pytest.approx(0.0)


# -- seed from config ---------------------------------------------------------

def test_seed_observers_from_config():
    cfg = SatpassConfig(observers=[
        {"slug": "boise", "name": "Boise", "lat": 43.615, "lon": -116.202, "alt_m": 824.0},
        {"slug": "twin", "name": "Twin Falls", "lat": 42.563, "lon": -114.461},
    ])
    n = seed_observers_from_config(cfg)
    assert n == 2
    slugs = {o["slug"] for o in get_observers()}
    assert slugs == {"boise", "twin"}


def test_seed_respects_disabled_flag():
    cfg = SatpassConfig(observers=[
        {"slug": "a", "name": "A", "lat": 40.0, "lon": -110.0},
        {"slug": "b", "name": "B", "lat": 41.0, "lon": -111.0, "enabled": False},
    ])
    seed_observers_from_config(cfg)
    slugs = {o["slug"] for o in get_observers()}
    assert "a" in slugs
    assert "b" not in slugs


def test_seed_is_idempotent_and_updates():
    cfg = SatpassConfig(observers=[
        {"slug": "x", "name": "X", "lat": 40.0, "lon": -110.0},
    ])
    assert seed_observers_from_config(cfg) == 1
    # Re-seed with an edited name — upsert, not duplicate.
    cfg2 = SatpassConfig(observers=[
        {"slug": "x", "name": "X Renamed", "lat": 40.0, "lon": -110.0},
    ])
    seed_observers_from_config(cfg2)
    obs = get_observers()
    assert len(obs) == 1
    assert obs[0]["name"] == "X Renamed"


def test_seed_skips_malformed_entries():
    cfg = SatpassConfig(observers=[
        {"slug": "good", "name": "Good", "lat": 40.0, "lon": -110.0},
        {"name": "MissingSlug", "lat": 41.0, "lon": -111.0},  # no slug
        "not-a-dict",
    ])
    n = seed_observers_from_config(cfg)
    assert n == 1
    assert {o["slug"] for o in get_observers()} == {"good"}


def test_seed_empty_config_noop():
    cfg = SatpassConfig(observers=[])
    assert seed_observers_from_config(cfg) == 0
    assert get_observers() == []
