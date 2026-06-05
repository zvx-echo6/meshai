"""v0.6-3a foundation tests: migration, seed, accessor.

No handler is wired to adapter_config in 3a (that lands in 3b). These
tests verify the foundation in isolation:
  - v6 migration creates adapter_config + adapter_meta
  - seed_defaults seeds every REGISTRY + ADAPTER_META row
  - seed_defaults is idempotent (re-run is a no-op)
  - accessor returns typed values for int / float / str / bool / json
  - cache hit: second read does not re-hit the DB
  - invalidate_cache forces reload
  - registry fallback when the DB row is missing (defensive)
  - unknown key raises AttributeError
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from meshai.adapter_config import (
    adapter_config,
    invalidate_cache,
    seed_defaults,
    REGISTRY,
    ADAPTER_META,
)
from meshai.adapter_config import _accessor as accessor_mod
from meshai.persistence import close_thread_connection, init_db
from meshai.persistence import db as persistence_db


# ---------- fixtures ------------------------------------------------------


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Forces a fresh DB AND clears the in-memory accessor cache.

    The conftest autouse fixture also redirects MESHAI_DB_PATH, but
    explicit cleanup here makes the tests robust to ordering.
    """
    p = str(tmp_path / "ac-test.sqlite")
    monkeypatch.setenv("MESHAI_DB_PATH", p)
    persistence_db._initialised.clear()
    close_thread_connection()
    invalidate_cache()
    conn = init_db()
    yield conn
    close_thread_connection()
    persistence_db._initialised.discard(p)
    invalidate_cache()


# ---------- schema --------------------------------------------------------


def test_v6_tables_exist(fresh_db):
    tables = {r["name"] for r in fresh_db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "adapter_config" in tables
    assert "adapter_meta" in tables


def test_schema_meta_at_v6(fresh_db):
    v = fresh_db.execute(
        "SELECT value FROM schema_meta WHERE key='version'"
    ).fetchone()["value"]
    assert int(v) == 6


def test_adapter_config_type_check_constrains_vocabulary(fresh_db):
    """The CHECK constraint on `type` rejects unknown labels."""
    with pytest.raises(Exception):
        fresh_db.execute(
            "INSERT INTO adapter_config(adapter, key, value_json, default_json, "
            "type, description, updated_at) VALUES (?,?,?,?,?,?,?)",
            ("x", "y", "1", "1", "integer", "", 0.0),   # 'integer' is not in vocab
        )


# ---------- seed ----------------------------------------------------------


def test_seed_populates_every_registry_row(fresh_db):
    """Every (adapter, key) in REGISTRY lands in adapter_config."""
    rows = fresh_db.execute("SELECT adapter, key FROM adapter_config").fetchall()
    db_keys = {(r["adapter"], r["key"]) for r in rows}
    assert db_keys == set(REGISTRY.keys()), (
        f"DB missing {set(REGISTRY) - db_keys}, "
        f"extra in DB {db_keys - set(REGISTRY)}"
    )


def test_seed_value_matches_registry_default(fresh_db):
    """value_json == default_json on first seed; both = json.dumps(default)."""
    for (adapter, key), spec in REGISTRY.items():
        row = fresh_db.execute(
            "SELECT value_json, default_json, type FROM adapter_config "
            "WHERE adapter=? AND key=?",
            (adapter, key),
        ).fetchone()
        expected = json.dumps(spec["default"])
        assert row["value_json"] == expected, f"{adapter}.{key} value drift"
        assert row["default_json"] == expected, f"{adapter}.{key} default drift"
        assert row["type"] == spec["type"]


def test_seed_populates_every_adapter_meta_row(fresh_db):
    rows = fresh_db.execute("SELECT adapter, include_in_llm_context FROM adapter_meta").fetchall()
    db_adapters = {r["adapter"] for r in rows}
    assert db_adapters == set(ADAPTER_META.keys())
    for r in rows:
        spec = ADAPTER_META[r["adapter"]]
        expected = 1 if spec.get("include_in_llm_context", True) else 0
        assert r["include_in_llm_context"] == expected


def test_seed_is_idempotent(fresh_db):
    """Re-running seed_defaults inserts zero new rows."""
    a, b = seed_defaults(fresh_db)
    assert a == 0 and b == 0
    # Row counts unchanged.
    cfg = fresh_db.execute("SELECT COUNT(*) FROM adapter_config").fetchone()[0]
    meta = fresh_db.execute("SELECT COUNT(*) FROM adapter_meta").fetchone()[0]
    assert cfg == len(REGISTRY)
    assert meta == len(ADAPTER_META)


def test_seed_does_not_overwrite_user_edits(fresh_db):
    """A user-edited value_json survives a re-seed (INSERT OR IGNORE)."""
    fresh_db.execute(
        "UPDATE adapter_config SET value_json=? WHERE adapter=? AND key=?",
        ("999", "wfigs", "cooldown_seconds"),
    )
    seed_defaults(fresh_db)
    row = fresh_db.execute(
        "SELECT value_json FROM adapter_config "
        "WHERE adapter='wfigs' AND key='cooldown_seconds'"
    ).fetchone()
    assert row["value_json"] == "999", "seed must not overwrite user edits"


# ---------- accessor -----------------------------------------------------


def test_accessor_returns_int(fresh_db):
    invalidate_cache()
    v = adapter_config.wfigs.cooldown_seconds
    assert isinstance(v, int)
    assert v == 28800


def test_accessor_returns_float(fresh_db):
    invalidate_cache()
    v = adapter_config.usgs_quake.global_mag_floor
    assert isinstance(v, float)
    assert v == 3.0


def test_accessor_returns_str(fresh_db):
    invalidate_cache()
    v = adapter_config.geocoder.photon_url
    assert isinstance(v, str)
    assert v == "http://100.64.0.24:2322"


def test_accessor_returns_bool(fresh_db):
    invalidate_cache()
    v = adapter_config.tomtom_incidents.drop_zero_magnitude
    assert isinstance(v, bool)
    assert v is True


def test_accessor_returns_json_list(fresh_db):
    invalidate_cache()
    v = adapter_config.nws.broadcast_severities
    assert v == ["Extreme", "Severe"]


def test_accessor_returns_json_dict(fresh_db):
    invalidate_cache()
    v = adapter_config.usgs_nwis.threshold_labels
    assert v["action"] == "action stage"


def test_accessor_returns_json_none(fresh_db):
    """A registry default of None survives the round-trip."""
    invalidate_cache()
    v = adapter_config.firms.bbox
    assert v is None


# ---------- cache --------------------------------------------------------


def test_cache_hits_second_read(fresh_db):
    """Second read of the same key bypasses the DB."""
    invalidate_cache()
    # Prime the cache.
    _ = adapter_config.wfigs.cooldown_seconds
    # Patch _load_from_db to fail; if the cache works we never call it.
    with patch.object(accessor_mod, "_load_from_db",
                      side_effect=AssertionError("cache miss -- went to DB")):
        v = adapter_config.wfigs.cooldown_seconds
    assert v == 28800


def test_invalidate_forces_reload(fresh_db):
    """After invalidate_cache the next read hits the DB and sees fresh values."""
    invalidate_cache()
    _ = adapter_config.wfigs.cooldown_seconds  # prime cache

    fresh_db.execute(
        "UPDATE adapter_config SET value_json=? WHERE adapter='wfigs' AND key='cooldown_seconds'",
        ("3600",),
    )
    # Without invalidation, the cached 28800 is returned.
    assert adapter_config.wfigs.cooldown_seconds == 28800
    invalidate_cache()
    assert adapter_config.wfigs.cooldown_seconds == 3600


# ---------- defensive fallback paths -------------------------------------


def test_registry_fallback_when_db_row_missing(fresh_db, caplog):
    """If the seed somehow missed a key, the accessor still returns the
    registry default (with a WARNING)."""
    invalidate_cache()
    # Delete one row to simulate the missing-seed case.
    fresh_db.execute(
        "DELETE FROM adapter_config WHERE adapter='wfigs' AND key='cooldown_seconds'"
    )
    import logging
    caplog.set_level(logging.WARNING, logger="meshai.adapter_config._accessor")
    v = adapter_config.wfigs.cooldown_seconds
    assert v == 28800   # from REGISTRY
    assert any("missing from DB" in r.message for r in caplog.records)


def test_unknown_key_raises(fresh_db):
    invalidate_cache()
    with pytest.raises(AttributeError):
        _ = adapter_config.wfigs.no_such_key


def test_unknown_adapter_section_still_works_until_key_lookup(fresh_db):
    """Accessing adapter_config.<unknown> returns a section object;
    the AttributeError comes on the key dereference."""
    invalidate_cache()
    section = adapter_config.nonexistent_adapter
    assert section is not None   # section object exists
    with pytest.raises(AttributeError):
        _ = section.any_key


def test_setattr_blocked(fresh_db):
    """Writes via the accessor are forbidden -- use the API."""
    invalidate_cache()
    with pytest.raises(AttributeError):
        adapter_config.wfigs.cooldown_seconds = 999


# ---------- registry sanity ----------------------------------------------


def test_every_registry_default_round_trips_through_json():
    """Every default must json.dumps + json.loads to an equivalent value."""
    for (adapter, key), spec in REGISTRY.items():
        encoded = json.dumps(spec["default"])
        decoded = json.loads(encoded)
        assert decoded == spec["default"], f"{adapter}.{key}: JSON round-trip drift"


def test_every_registry_type_is_in_vocabulary():
    valid = {"int", "float", "str", "bool", "json"}
    for (adapter, key), spec in REGISTRY.items():
        assert spec["type"] in valid, f"{adapter}.{key}: invalid type {spec['type']!r}"


def test_adapter_meta_includes_every_registry_adapter():
    """Every adapter referenced in REGISTRY should have a meta row so the
    GUI can render an include_in_llm_context toggle for it."""
    reg_adapters = {a for a, _ in REGISTRY}
    meta_adapters = set(ADAPTER_META)
    missing = reg_adapters - meta_adapters
    assert not missing, f"adapters in REGISTRY but missing ADAPTER_META: {missing}"
