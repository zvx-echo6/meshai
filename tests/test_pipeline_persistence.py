"""v0.6-6 pipeline persistence tests.

Inhibitor + Grouper state now survives across instances (write-through to
inhibit_state + grouper_held tables). ToggleFilter has a refresh() method
that re-reads the live config.
"""
from __future__ import annotations

import json
import time

import pytest

from meshai.config import Config, NotificationToggle
from meshai.notifications.events import Event, make_event
from meshai.notifications.pipeline.inhibitor import Inhibitor
from meshai.notifications.pipeline.grouper import Grouper
from meshai.notifications.pipeline.toggle_filter import ToggleFilter
from meshai.persistence import get_db


# ============================================================================
# v10 schema
# ============================================================================


def test_v10_tables_present():
    conn = get_db()
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "inhibit_state" in tables
    assert "grouper_held" in tables


# ============================================================================
# Inhibitor persistence
# ============================================================================


def test_inhibit_persists_across_restart():
    sink = []
    inh = Inhibitor(next_handler=sink.append, ttl_seconds=1800)
    ev = make_event(source="nws", category="weather_warning", severity="immediate",
                     inhibit_keys=["k1"])
    inh.handle(ev)
    # Sanity: key now in instance.
    assert "k1" in inh._active

    # Simulated restart -- new Inhibitor instance, same DB.
    inh2 = Inhibitor(next_handler=sink.append, ttl_seconds=1800)
    assert "k1" in inh2._active


def test_inhibit_expired_rows_dropped_on_restore():
    """Rows with expires_at in the past are not restored."""
    conn = get_db()
    conn.execute(
        "INSERT INTO inhibit_state(key, rank, expires_at, updated_at) VALUES (?,?,?,?)",
        ("stale", 2, time.time() - 60, time.time()),
    )
    inh = Inhibitor(next_handler=lambda x: None, ttl_seconds=1800)
    assert "stale" not in inh._active


def test_inhibit_prune_removes_from_disk():
    sink = []
    inh = Inhibitor(next_handler=sink.append, ttl_seconds=0.001)
    ev = make_event(source="nws", category="weather_warning", severity="immediate",
                     inhibit_keys=["short"])
    inh.handle(ev)
    time.sleep(0.02)
    # Trigger prune via next handle()
    ev2 = make_event(source="nws", category="weather_warning", severity="immediate",
                      inhibit_keys=["other"])
    inh.handle(ev2)
    # On-disk should not contain 'short'.
    conn = get_db()
    r = conn.execute("SELECT * FROM inhibit_state WHERE key=?", ("short",)).fetchone()
    assert r is None


def test_inhibit_clear_removes_from_disk():
    inh = Inhibitor(next_handler=lambda x: None, ttl_seconds=1800)
    ev = make_event(source="nws", category="weather_warning", severity="immediate",
                     inhibit_keys=["k1"])
    inh.handle(ev)
    inh.clear()
    conn = get_db()
    n = conn.execute("SELECT COUNT(*) FROM inhibit_state").fetchone()[0]
    assert n == 0


# ============================================================================
# Grouper persistence
# ============================================================================


def test_grouper_persists_across_restart():
    g = Grouper(next_handler=lambda x: None, window_seconds=60)
    ev = make_event(source="nws", category="weather_warning", severity="routine",
                     group_key="storm:1", title="x")
    g.handle(ev)
    assert "storm:1" in g._held

    g2 = Grouper(next_handler=lambda x: None, window_seconds=60)
    assert "storm:1" in g2._held


def test_grouper_tick_clears_disk():
    """A successful tick() flush deletes the row from grouper_held."""
    sink = []
    g = Grouper(next_handler=sink.append, window_seconds=0.001)
    ev = make_event(source="nws", category="weather_warning", severity="routine",
                     group_key="storm:fast", title="x")
    g.handle(ev)
    time.sleep(0.05)
    g.tick()
    conn = get_db()
    r = conn.execute("SELECT * FROM grouper_held WHERE group_key=?", ("storm:fast",)).fetchone()
    assert r is None


def test_grouper_expired_row_not_restored():
    conn = get_db()
    ev = make_event(source="x", category="weather_warning", severity="routine",
                     group_key="stale", title="x")
    conn.execute(
        "INSERT INTO grouper_held(group_key, event_json, hold_until_at, updated_at) "
        "VALUES (?,?,?,?)",
        ("stale", json.dumps(ev.to_dict()), time.time() - 100, time.time()),
    )
    g = Grouper(next_handler=lambda x: None, window_seconds=60)
    assert "stale" not in g._held


# ============================================================================
# ToggleFilter.refresh()
# ============================================================================


def test_toggle_filter_refresh_picks_up_new_enabled_set():
    cfg = Config()
    cfg.notifications.toggles["fire"].enabled = False
    tf = ToggleFilter(next_handler=lambda x: None, enabled_toggles=set())
    # No toggles enabled -> drop everything.
    assert tf._enabled == set()

    # Mutate config and refresh.
    cfg.notifications.toggles["fire"].enabled = True
    tf.refresh(cfg)
    assert tf._enabled is not None
    assert "fire" in tf._enabled


def test_toggle_filter_refresh_with_none_config_is_noop():
    """Defensive: refresh(None) keeps current state."""
    tf = ToggleFilter(next_handler=lambda x: None, enabled_toggles={"weather"})
    tf.refresh(None)
    assert tf._enabled == {"weather"}


def test_toggle_filter_refresh_drops_disabled_families():
    """Disabling a family in config and refreshing removes it from enabled."""
    cfg = Config()
    cfg.notifications.toggles["fire"].enabled = True
    cfg.notifications.toggles["weather"].enabled = True
    tf = ToggleFilter(next_handler=lambda x: None, enabled_toggles={"fire", "weather"})
    cfg.notifications.toggles["fire"].enabled = False
    tf.refresh(cfg)
    assert "fire" not in (tf._enabled or set())
    assert "weather" in (tf._enabled or set())
