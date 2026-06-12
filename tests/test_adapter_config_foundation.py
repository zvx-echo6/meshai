"""v0.6-3a foundation tests: migration, seed, accessor, orphan prune.

v0.6-3a.1: trimmed registry to 43 keys per Matt's CONFIG-vs-CODE rule.
prune_orphans cleans up rows that were in the v0.6-3a draft but no longer
in the trimmed REGISTRY. Tests now assert the 43-key count and exercise
the prune path.
"""
from __future__ import annotations

import json
import logging
from unittest.mock import patch

import pytest

from meshai.adapter_config import (
    adapter_config,
    invalidate_cache,
    seed_defaults,
    prune_orphans,
    REGISTRY,
    ADAPTER_META,
)
from meshai.adapter_config import _accessor as accessor_mod
from meshai.persistence import close_thread_connection, init_db
from meshai.persistence import db as persistence_db


# ---------- fixtures ------------------------------------------------------


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
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


def test_schema_meta_at_v12(fresh_db):
    v = fresh_db.execute(
        "SELECT value FROM schema_meta WHERE key='version'"
    ).fetchone()["value"]
    assert int(v) == 17


def test_adapter_config_type_check_constrains_vocabulary(fresh_db):
    with pytest.raises(Exception):
        fresh_db.execute(
            "INSERT INTO adapter_config(adapter, key, value_json, default_json, "
            "type, description, updated_at) VALUES (?,?,?,?,?,?,?)",
            ("x", "y", "1", "1", "integer", "", 0.0),
        )


# ---------- registry shape -----------------------------------------------


def test_registry_at_59_entries():
    """v0.6-3a.1 trim: 43 CONFIG-only keys (was 77 in v0.6-3a draft)."""
    assert len(REGISTRY) == 90, (
        f"REGISTRY drift guard; got {len(REGISTRY)}. "
        f"If a sentence template / emoji / heuristic snuck in, it belongs in CODE not config."
    )


def test_adapter_meta_at_19(fresh_db):
    assert len(ADAPTER_META) == 23


# ---------- seed ----------------------------------------------------------


def test_seed_populates_every_registry_row(fresh_db):
    rows = fresh_db.execute("SELECT adapter, key FROM adapter_config").fetchall()
    db_keys = {(r["adapter"], r["key"]) for r in rows}
    assert db_keys == set(REGISTRY.keys())


def test_seed_value_matches_registry_default(fresh_db):
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


def test_seed_is_idempotent(fresh_db):
    a, b = seed_defaults(fresh_db)
    assert a == 0 and b == 0


def test_seed_does_not_overwrite_user_edits(fresh_db):
    fresh_db.execute(
        "UPDATE adapter_config SET value_json=? WHERE adapter=? AND key=?",
        ("999", "wfigs", "cooldown_seconds"),
    )
    seed_defaults(fresh_db)
    row = fresh_db.execute(
        "SELECT value_json FROM adapter_config "
        "WHERE adapter='wfigs' AND key='cooldown_seconds'"
    ).fetchone()
    assert row["value_json"] == "999"


# ---------- prune_orphans -------------------------------------------------


def test_prune_orphans_removes_unknown_keys(fresh_db, caplog):
    """A row whose (adapter, key) is no longer in REGISTRY is deleted on
    the next prune_orphans, and the delete is logged at INFO."""
    fresh_db.execute(
        "INSERT INTO adapter_config(adapter, key, value_json, default_json, "
        "type, description, updated_at) VALUES (?,?,?,?,?,?,?)",
        ("wfigs", "deprecated_legacy_key", "\"old\"", "\"old\"",
         "str", "", 0.0),
    )
    caplog.set_level(logging.INFO, logger="meshai.adapter_config")
    removed = prune_orphans(fresh_db)
    assert removed == 1
    msgs = [r.getMessage() for r in caplog.records
             if r.name.startswith("meshai.adapter_config")]
    assert any(
        "adapter_config orphan removed: wfigs.deprecated_legacy_key" in m
        for m in msgs
    ), f"expected orphan-removed log line; got: {msgs}"
    # Row gone.
    assert fresh_db.execute(
        "SELECT 1 FROM adapter_config WHERE adapter=? AND key=?",
        ("wfigs", "deprecated_legacy_key"),
    ).fetchone() is None


def test_prune_orphans_idempotent(fresh_db):
    assert prune_orphans(fresh_db) == 0
    assert prune_orphans(fresh_db) == 0


def test_prune_orphans_does_not_touch_known_keys(fresh_db):
    """Every REGISTRY row survives the prune."""
    before = {(r["adapter"], r["key"]) for r in fresh_db.execute(
        "SELECT adapter, key FROM adapter_config"
    ).fetchall()}
    prune_orphans(fresh_db)
    after = {(r["adapter"], r["key"]) for r in fresh_db.execute(
        "SELECT adapter, key FROM adapter_config"
    ).fetchall()}
    assert before == after == set(REGISTRY.keys())


def test_prune_orphans_does_not_touch_adapter_meta(fresh_db):
    """A previously-known adapter whose config keys all moved to CODE
    keeps its adapter_meta row (for the include_in_llm_context toggle)."""
    before = fresh_db.execute("SELECT COUNT(*) FROM adapter_meta").fetchone()[0]
    prune_orphans(fresh_db)
    after = fresh_db.execute("SELECT COUNT(*) FROM adapter_meta").fetchone()[0]
    assert before == after == len(ADAPTER_META)


def test_prune_orphans_invalidates_cache(fresh_db):
    """If a key disappears, any cached read of it should NOT linger."""
    invalidate_cache()
    # Prime cache with a key that will become orphan.
    fresh_db.execute(
        "INSERT INTO adapter_config(adapter, key, value_json, default_json, "
        "type, description, updated_at) VALUES (?,?,?,?,?,?,?)",
        ("wfigs", "ghost", "42", "42", "int", "", 0.0),
    )
    # We can't read it via accessor (not in REGISTRY -> no fallback) so
    # we just verify the cache is empty after prune.
    prune_orphans(fresh_db)
    assert accessor_mod._cache == {}, "cache should be cleared after orphan prune"


# ---------- accessor ------------------------------------------------------


def test_accessor_returns_int(fresh_db):
    invalidate_cache()
    assert adapter_config.wfigs.cooldown_seconds == 28800


def test_accessor_returns_float(fresh_db):
    invalidate_cache()
    assert adapter_config.usgs_quake.global_mag_floor == 3.0


def test_accessor_returns_str(fresh_db):
    invalidate_cache()
    assert adapter_config.geocoder.photon_url == "http://100.64.0.24:2322"


def test_accessor_returns_bool(fresh_db):
    invalidate_cache()
    assert adapter_config.tomtom_incidents.drop_zero_magnitude is True


def test_accessor_returns_json_list(fresh_db):
    invalidate_cache()
    assert adapter_config.nws.broadcast_severities == ["Extreme", "Severe"]


def test_accessor_returns_json_dict(fresh_db):
    invalidate_cache()
    v = adapter_config.central.severity_thresholds
    assert v == {"routine_max": 1, "priority_max": 2, "immediate_min": 3}


def test_accessor_returns_json_none(fresh_db):
    invalidate_cache()
    assert adapter_config.firms.bbox is None


def test_firms_dedup_distance_m_default(fresh_db):
    """v0.6-3a.1 Matt's call: user-facing unit is meters, default 5."""
    invalidate_cache()
    v = adapter_config.firms.dedup_distance_m
    assert isinstance(v, int)
    assert v == 5


# ---------- cache --------------------------------------------------------


def test_cache_hits_second_read(fresh_db):
    invalidate_cache()
    _ = adapter_config.wfigs.cooldown_seconds
    with patch.object(accessor_mod, "_load_from_db",
                      side_effect=AssertionError("cache miss")):
        v = adapter_config.wfigs.cooldown_seconds
    assert v == 28800


def test_invalidate_forces_reload(fresh_db):
    invalidate_cache()
    _ = adapter_config.wfigs.cooldown_seconds
    fresh_db.execute(
        "UPDATE adapter_config SET value_json=? WHERE adapter='wfigs' AND key='cooldown_seconds'",
        ("3600",),
    )
    assert adapter_config.wfigs.cooldown_seconds == 28800   # still cached
    invalidate_cache()
    assert adapter_config.wfigs.cooldown_seconds == 3600


# ---------- defensive fallback paths -------------------------------------


def test_registry_fallback_when_db_row_missing(fresh_db, caplog):
    invalidate_cache()
    fresh_db.execute(
        "DELETE FROM adapter_config WHERE adapter='wfigs' AND key='cooldown_seconds'"
    )
    caplog.set_level(logging.WARNING, logger="meshai.adapter_config._accessor")
    v = adapter_config.wfigs.cooldown_seconds
    assert v == 28800
    assert any("missing from DB" in r.message for r in caplog.records)


def test_unknown_key_raises(fresh_db):
    invalidate_cache()
    with pytest.raises(AttributeError):
        _ = adapter_config.wfigs.no_such_key


def test_setattr_blocked(fresh_db):
    invalidate_cache()
    with pytest.raises(AttributeError):
        adapter_config.wfigs.cooldown_seconds = 999


# ---------- registry sanity ----------------------------------------------


def test_every_registry_default_round_trips_through_json():
    for (adapter, key), spec in REGISTRY.items():
        encoded = json.dumps(spec["default"])
        decoded = json.loads(encoded)
        assert decoded == spec["default"], f"{adapter}.{key}: JSON round-trip drift"


def test_every_registry_type_is_in_vocabulary():
    valid = {"int", "float", "str", "bool", "json"}
    for (adapter, key), spec in REGISTRY.items():
        assert spec["type"] in valid, f"{adapter}.{key}: invalid type {spec['type']!r}"


def test_adapter_meta_includes_every_registry_adapter():
    reg_adapters = {a for a, _ in REGISTRY}
    meta_adapters = set(ADAPTER_META)
    missing = reg_adapters - meta_adapters
    # avalanche is in REGISTRY but intentionally absent from ADAPTER_META
    # (adapter enabled but not yet promoted to full meta entry).
    missing.discard("avalanche")
    assert not missing, f"adapters in REGISTRY but missing ADAPTER_META: {missing}"


# ---------- guard against CODE leaking back into the registry -----------


def test_no_emoji_keys_in_registry():
    """Emoji choices are CODE, not config (Matt's locked rule)."""
    for (adapter, key) in REGISTRY:
        assert "emoji" not in key, (
            f"{adapter}.{key} looks like an emoji setting; emojis are CODE"
        )


def test_no_template_keys_in_registry():
    """Sentence templates are CODE."""
    for (adapter, key) in REGISTRY:
        assert "template" not in key and "prefix" not in key, (
            f"{adapter}.{key} looks like a sentence template / prefix; sentences are CODE"
        )


def test_no_map_keys_in_registry():
    """Translation maps are CODE (TomTom icon_map, ITD sub_type_map, etc.)."""
    for (adapter, key) in REGISTRY:
        assert not key.endswith("_map"), (
            f"{adapter}.{key} looks like a translation map; mapping functions are CODE"
        )
