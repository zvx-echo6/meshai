"""v0.6-2 dispatcher state persistence tests (audit doc finding #1).

Covers:
  - state row restored on dispatcher __init__
  - counter increments survive restart
  - cold-start anchor survives restart
  - cooldown map rebuilt from dispatcher_cooldowns on init
  - dedup LRU rebuilt from dispatcher_dedup (most-recent 10k) on init
  - 7-day dedup cleanup runs on insert
  - 2*cooldown_s cooldown prune runs on insert
  - v5 migration is idempotent
  - synthetic storm + restart + replay probe (commit spec step 5)
"""
from __future__ import annotations

import asyncio
import time as _time
from unittest.mock import MagicMock, AsyncMock

import pytest

from meshai.config import Config, NotificationToggle
from meshai.notifications.events import make_event
from meshai.notifications.pipeline.dispatcher import (
    Dispatcher, _DEDUP_LRU_MAX, _DEDUP_DB_RETENTION_S,
)
from meshai.persistence import close_thread_connection, init_db
from meshai.persistence import db as persistence_db


# ---------- fixtures --------------------------------------------------------


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    p = str(tmp_path / "disp-test.sqlite")
    monkeypatch.setenv("MESHAI_DB_PATH", p)
    persistence_db._initialised.clear()
    close_thread_connection()
    init_db()
    yield p
    close_thread_connection()
    persistence_db._initialised.discard(p)


def _build_config(*, cold_start_grace=60,
                    fire_enabled=True, fire_cooldown=300,
                    fire_freshness=600,
                    fire_severity_channels=None):
    """Minimal Config with the `fire` family toggle enabled. The toggle uses
    a fake `broadcast_channel=1` + mesh_broadcast severity-channel mapping so
    dispatch() reaches the channel.deliver() path."""
    cfg = Config()
    cfg.notifications.cold_start_grace_seconds = cold_start_grace
    cfg.notifications.toggles = {
        "fire": NotificationToggle(
            name="fire", enabled=fire_enabled,
            freshness_seconds=fire_freshness,
            cooldown_seconds=fire_cooldown,
            severity_channels=(fire_severity_channels or
                                {"routine": ["mesh_broadcast"],
                                 "priority": ["mesh_broadcast"],
                                 "immediate": ["mesh_broadcast"]}),
            broadcast_channel=1,
        ),
    }
    return cfg


def _mk_channel_factory(deliver_outcome=True):
    """Returns (factory, channel_mock_list). Each call appends one mock and
    returns it so tests can assert call counts."""
    created = []

    def _factory(rule, connector):
        ch = MagicMock()
        ch.deliver = AsyncMock(return_value=deliver_outcome)
        created.append(ch)
        return ch
    return _factory, created


def _fire_event(*, source="fires", category="wildfire_incident",
                event_id=None, severity="priority", region="US-ID",
                timestamp=None):
    """Build a wildfire Event whose category maps to the `fire` toggle."""
    e = make_event(
        source=source, category=category, severity=severity,
        region=region,
        title="🔥 Test fire", lat=43.6, lon=-116.2,
    )
    if event_id is not None:
        e.id = event_id
    if timestamp is not None:
        e.timestamp = timestamp
    return e


# ============================================================================
# Schema + table baseline
# ============================================================================


def test_v5_tables_present_after_migration(db_path):
    """v5.sql ran during init_db. All three tables + the singleton row."""
    conn = persistence_db.get_db(db_path)
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "dispatcher_state" in tables
    assert "dispatcher_cooldowns" in tables
    assert "dispatcher_dedup" in tables

    row = conn.execute("SELECT * FROM dispatcher_state").fetchone()
    assert row is not None
    assert row["id"] == 1
    assert row["cold_start_anchor"] is None
    assert row["stale_dropped"] == 0
    assert row["cooldown_dropped"] == 0
    assert row["dedup_dropped"] == 0
    assert row["cold_start_dropped"] == 0


def test_v5_migration_idempotent(db_path):
    """Re-running migrations is a no-op (only one singleton row, no errors)."""
    # Force a second migration pass via the public path.
    persistence_db._initialised.discard(db_path)
    persistence_db._apply_migrations(persistence_db.get_db(db_path))
    conn = persistence_db.get_db(db_path)
    n = conn.execute("SELECT COUNT(*) FROM dispatcher_state").fetchone()[0]
    assert n == 1


# ============================================================================
# Construction: restore from empty DB = clean defaults
# ============================================================================


def test_init_restores_empty_state(db_path):
    cfg = _build_config()
    factory, _ = _mk_channel_factory()
    d = Dispatcher(cfg, factory)
    assert d._stale_dropped == 0
    assert d._cooldown_dropped == 0
    assert d._dedup_dropped == 0
    assert d._cold_start_dropped == 0
    assert d._first_event_at is None
    assert d._toggle_cooldown == {}
    assert len(d._dedup_lru) == 0


# ============================================================================
# Counters survive restart
# ============================================================================


def test_counter_increments_persist_across_restart(db_path):
    cfg = _build_config()
    factory, _ = _mk_channel_factory()
    d1 = Dispatcher(cfg, factory)

    # Forcibly bump each counter via the persistence helper. Mirrors what
    # _dispatch_toggles does on a drop.
    d1._stale_dropped = 5
    d1._cooldown_dropped = 3
    d1._dedup_dropped = 7
    d1._cold_start_dropped = 2
    d1._first_event_at = 12345.678
    d1._persist_state()

    # Fresh dispatcher, same DB.
    d2 = Dispatcher(cfg, factory)
    assert d2._stale_dropped == 5
    assert d2._cooldown_dropped == 3
    assert d2._dedup_dropped == 7
    assert d2._cold_start_dropped == 2
    assert d2._first_event_at == pytest.approx(12345.678)


def test_dispatch_stats_reflects_persisted_state(db_path):
    cfg = _build_config()
    factory, _ = _mk_channel_factory()
    d1 = Dispatcher(cfg, factory)
    d1._stale_dropped = 99
    d1._persist_state()

    d2 = Dispatcher(cfg, factory)
    stats = d2.dispatch_stats()
    assert stats["stale_dropped"] == 99


# ============================================================================
# Cooldown survives restart, re-arms on boot
# ============================================================================


def test_cooldown_persists_across_restart(db_path):
    cfg = _build_config(fire_cooldown=300)
    factory, _ = _mk_channel_factory()
    d1 = Dispatcher(cfg, factory)

    key = ("fire", "wildfire_incident", "US-ID")
    now = _time.time()
    d1._toggle_cooldown[key] = now
    d1._persist_cooldown(key, now, 300)

    d2 = Dispatcher(cfg, factory)
    assert key in d2._toggle_cooldown
    assert d2._toggle_cooldown[key] == pytest.approx(now)


def test_cooldown_rearms_on_boot_within_window(db_path):
    """Cooldown was set 100s before restart; check 100s after restart
    (so 200s after the fire) with cooldown_s=300 => still in cooldown
    (200 < 300)."""
    cfg = _build_config(fire_cooldown=300)
    factory, _ = _mk_channel_factory()
    d1 = Dispatcher(cfg, factory)

    fire_t = 10_000.0
    key = ("fire", "wildfire_incident", "US-ID")
    d1._toggle_cooldown[key] = fire_t
    d1._persist_cooldown(key, fire_t, 300)

    # Restart -- fresh dispatcher reads from DB.
    d2 = Dispatcher(cfg, factory)
    assert key in d2._toggle_cooldown
    last = d2._toggle_cooldown[key]
    now = fire_t + 200    # 200s elapsed
    assert (now - last) < 300, "must still be in cooldown window"


def test_cooldown_prune_2x_window(db_path):
    """Inserting a cooldown deletes rows whose last_fired_at < (now - 2*cooldown_s)."""
    cfg = _build_config(fire_cooldown=300)
    factory, _ = _mk_channel_factory()
    d = Dispatcher(cfg, factory)

    now = _time.time()
    very_old_t = now - 1000.0   # > 2*300=600s old
    fresh_t = now - 100.0

    d._persist_cooldown(("oldfam", "cat", "*"), very_old_t, 300)
    d._persist_cooldown(("freshfam", "cat", "*"), fresh_t, 300)
    # The 2nd call's prune deletes the old row.
    # Issue one more recent insert to make sure prune runs again with `now`.
    d._persist_cooldown(("nowfam", "cat", "*"), now, 300)

    conn = persistence_db.get_db(db_path)
    rows = conn.execute(
        "SELECT toggle FROM dispatcher_cooldowns ORDER BY toggle"
    ).fetchall()
    toggles = [r["toggle"] for r in rows]
    assert "oldfam" not in toggles, "stale cooldown should be pruned"
    assert "freshfam" in toggles
    assert "nowfam" in toggles


# ============================================================================
# Dedup survives restart
# ============================================================================


def test_dedup_lru_rebuilt_on_restart(db_path):
    cfg = _build_config()
    factory, _ = _mk_channel_factory()
    d1 = Dispatcher(cfg, factory)

    now = _time.time()
    for i in range(5):
        key = ("firms", f"event_{i}")
        d1._dedup_lru[key] = True
        d1._persist_dedup(key, now + i)

    d2 = Dispatcher(cfg, factory)
    assert len(d2._dedup_lru) == 5
    for i in range(5):
        assert ("firms", f"event_{i}") in d2._dedup_lru


def test_dedup_lru_rebuild_caps_at_10k(db_path):
    """If on-disk has > 10k rows, in-memory restores only the most recent 10k."""
    cfg = _build_config()
    factory, _ = _mk_channel_factory()
    d = Dispatcher(cfg, factory)

    conn = persistence_db.get_db(db_path)
    base = _time.time() - 10000
    rows = [("firms", f"e{i}", base + i)
             for i in range(_DEDUP_LRU_MAX + 50)]
    conn.executemany(
        "INSERT INTO dispatcher_dedup(source, event_id, seen_at) VALUES (?,?,?)",
        rows,
    )

    d2 = Dispatcher(cfg, factory)
    assert len(d2._dedup_lru) == _DEDUP_LRU_MAX
    # Most recent IDs survived (e.g. e_(_DEDUP_LRU_MAX+49)) but oldest didn't (e_0).
    assert ("firms", "e0") not in d2._dedup_lru
    assert ("firms", f"e{_DEDUP_LRU_MAX + 49}") in d2._dedup_lru


def test_dedup_7_day_cleanup(db_path):
    cfg = _build_config()
    factory, _ = _mk_channel_factory()
    d = Dispatcher(cfg, factory)

    now = _time.time()
    # Need ancient < cutoff strictly. cutoff = now - retention; ancient =
    # now - retention - 1000 leaves 1000s of headroom.
    too_old = now - (_DEDUP_DB_RETENTION_S + 1000)

    # Seed an ancient row directly to bypass the prune-on-insert guard.
    conn = persistence_db.get_db(db_path)
    conn.execute(
        "INSERT INTO dispatcher_dedup(source, event_id, seen_at) VALUES (?,?,?)",
        ("firms", "ancient", too_old),
    )
    # Now an insert via the helper triggers the cleanup: cutoff = now - retention.
    d._persist_dedup(("firms", "fresh"), now)

    rows = conn.execute(
        "SELECT event_id FROM dispatcher_dedup ORDER BY event_id"
    ).fetchall()
    ids = [r["event_id"] for r in rows]
    assert "ancient" not in ids
    assert "fresh" in ids


def test_dedup_same_key_updates_seen_at(db_path):
    """Re-seeing the same (source, event_id) refreshes its seen_at without
    creating a second row -- the INSERT OR REPLACE on the composite PK."""
    cfg = _build_config()
    factory, _ = _mk_channel_factory()
    d = Dispatcher(cfg, factory)

    t1 = _time.time()
    t2 = t1 + 50

    d._persist_dedup(("firms", "abc"), t1)
    d._persist_dedup(("firms", "abc"), t2)

    conn = persistence_db.get_db(db_path)
    rows = conn.execute("SELECT seen_at FROM dispatcher_dedup WHERE event_id='abc'").fetchall()
    assert len(rows) == 1
    assert rows[0]["seen_at"] == pytest.approx(t2)


# ============================================================================
# End-to-end: counters via real _dispatch_toggles
# ============================================================================


def test_real_dispatch_arms_cold_start_anchor(db_path):
    """First event to reach the toggle anchors the grace window and the
    anchor lands in dispatcher_state."""
    cfg = _build_config(cold_start_grace=60)
    factory, _ = _mk_channel_factory()
    d = Dispatcher(cfg, factory)

    ev = _fire_event(event_id="fire_anchor_001", timestamp=_time.time())
    asyncio.run(d._dispatch_toggles(ev))

    assert d._first_event_at is not None
    # And in SQLite.
    conn = persistence_db.get_db(db_path)
    row = conn.execute("SELECT cold_start_anchor FROM dispatcher_state").fetchone()
    assert row["cold_start_anchor"] is not None


def test_real_dispatch_stale_drop_persists(db_path):
    """A stale event increments stale_dropped on disk."""
    # The dispatcher reads fire freshness from adapter_config.wfigs.freshness_seconds
    # (default 0 = disabled). Set it to 60 so the stale gate triggers.
    from meshai.persistence import get_db as _gdb
    _gdb().execute(
        "UPDATE adapter_config SET default_json='60', value_json='60' "
        "WHERE adapter='wfigs' AND key='freshness_seconds'"
    )
    from meshai.adapter_config import adapter_config as _ac
    _ac.invalidate()
    cfg = _build_config(cold_start_grace=0, fire_freshness=60)
    factory, _ = _mk_channel_factory()
    d = Dispatcher(cfg, factory)

    ev = _fire_event(event_id="fire_stale_001",
                      timestamp=_time.time() - 600)  # 10 min old vs 60s window
    asyncio.run(d._dispatch_toggles(ev))

    assert d._stale_dropped == 1
    conn = persistence_db.get_db(db_path)
    row = conn.execute("SELECT stale_dropped FROM dispatcher_state").fetchone()
    assert row["stale_dropped"] == 1


def test_real_dispatch_dedup_writes_through(db_path):
    """A successful first-sighting writes a dedup row; a second sighting
    drops at dedup AND refreshes seen_at."""
    cfg = _build_config(cold_start_grace=0, fire_freshness=86400, fire_cooldown=0)
    factory, _ = _mk_channel_factory()
    d = Dispatcher(cfg, factory)

    ev1 = _fire_event(event_id="fire_dup_001", timestamp=_time.time())
    asyncio.run(d._dispatch_toggles(ev1))

    conn = persistence_db.get_db(db_path)
    rows = conn.execute("SELECT source, event_id FROM dispatcher_dedup").fetchall()
    assert ("fires", "fire_dup_001") in [(r["source"], r["event_id"]) for r in rows]

    # second sighting
    ev2 = _fire_event(event_id="fire_dup_001", timestamp=_time.time())
    asyncio.run(d._dispatch_toggles(ev2))

    assert d._dedup_dropped == 1
    row = conn.execute("SELECT dedup_dropped FROM dispatcher_state").fetchone()
    assert row["dedup_dropped"] == 1


# ============================================================================
# Synthetic storm -> restart -> replay (commit spec step 5)
# ============================================================================


def test_synthetic_storm_restart_replay(db_path):
    """Phase 1: 50 distinct events. Phase 2: new Dispatcher, same DB. Phase 3:
    5 more events including one duplicate from phase 1. Assertions:
      - Phase 3 duplicate is dropped by dedup
      - Counters in phase 2/3 inherit phase 1 values (not reset)
      - Dedup LRU + cooldown carries across
    """
    cfg = _build_config(cold_start_grace=0, fire_freshness=86400, fire_cooldown=0)
    factory_a, _ = _mk_channel_factory()
    d1 = Dispatcher(cfg, factory_a)

    # ---- Phase 1: 50 distinct events ----
    base_ts = _time.time()
    for i in range(50):
        ev = _fire_event(event_id=f"storm_{i}", timestamp=base_ts)
        asyncio.run(d1._dispatch_toggles(ev))

    p1_dedup_rows = persistence_db.get_db(db_path).execute(
        "SELECT COUNT(*) FROM dispatcher_dedup"
    ).fetchone()[0]
    p1_dedup_dropped = d1._dedup_dropped
    p1_stale_dropped = d1._stale_dropped

    assert p1_dedup_rows == 50
    # All 50 were first-sightings -> no dedup_dropped increments.
    assert p1_dedup_dropped == 0

    # ---- Phase 2: simulated restart, fresh Dispatcher, same DB ----
    factory_b, _ = _mk_channel_factory()
    d2 = Dispatcher(cfg, factory_b)
    # Carry-over from phase 1:
    assert d2._dedup_dropped == p1_dedup_dropped
    assert d2._stale_dropped == p1_stale_dropped
    assert len(d2._dedup_lru) == 50

    # ---- Phase 3: 5 new events incl one phase-1 duplicate ----
    for i in range(50, 54):
        asyncio.run(d2._dispatch_toggles(
            _fire_event(event_id=f"storm_{i}", timestamp=base_ts)
        ))
    # The duplicate:
    dup_ev = _fire_event(event_id="storm_3", timestamp=base_ts)
    asyncio.run(d2._dispatch_toggles(dup_ev))

    # Now: dedup_dropped must have incremented by exactly 1 (the duplicate).
    assert d2._dedup_dropped == p1_dedup_dropped + 1, (
        f"expected +1 dedup drop, got {d2._dedup_dropped - p1_dedup_dropped}"
    )

    # 50 phase-1 + 4 phase-3-new = 54 distinct rows on disk.
    final_rows = persistence_db.get_db(db_path).execute(
        "SELECT COUNT(*) FROM dispatcher_dedup"
    ).fetchone()[0]
    assert final_rows == 54


# ============================================================================
# Graceful degrade: dispatcher works even when persistence is broken
# ============================================================================


def test_dispatcher_construct_when_db_unavailable(monkeypatch, tmp_path):
    """If get_db() raises on construct, the dispatcher must still init
    with fresh in-memory state (no crash, no abort)."""
    cfg = _build_config()
    factory, _ = _mk_channel_factory()

    def _broken_get_db(*a, **kw):
        raise RuntimeError("persistence layer down")

    monkeypatch.setattr(
        "meshai.notifications.pipeline.dispatcher.__name__", "<patched>"
    )
    # Patch the import target: persistence.get_db.
    import meshai.persistence as p
    monkeypatch.setattr(p, "get_db", _broken_get_db)

    d = Dispatcher(cfg, factory)   # must not raise
    assert d._stale_dropped == 0
    assert d._first_event_at is None


# ============================================================================
# _SOURCE_TO_TABLE fallback — native adapter events without _broadcast_audit
# ============================================================================

def test_source_to_table_fallback_stamps_audit_row(db_path):
    """_post_broadcast_commit must write source_event_table via _SOURCE_TO_TABLE
    when the event has no _broadcast_audit.  Covers the core Fix 1 regression
    guard: event.source='nws' → source_event_table='nws_alerts'.
    """
    from unittest.mock import MagicMock
    from meshai.notifications.events import make_event
    from meshai.notifications.pipeline.dispatcher import Dispatcher
    from meshai.persistence import get_db

    cfg = _build_config(cold_start_grace=0)
    factory, _ = _mk_channel_factory()
    d = Dispatcher(cfg, factory)

    # Build a native NWS event — no _broadcast_audit, no data dict at all.
    ev = make_event(
        source="nws",
        category="weather_alert",
        severity="priority",
        region="US-ID",
        title="⚠️ Winter Weather Advisory",
        lat=43.6, lon=-116.2,
    )

    # Fake a minimal rule + payload for the internal commit call.
    rule = MagicMock()
    rule.broadcast_channel = 1
    rule.delivery_types = ["mesh_broadcast"]

    payload = MagicMock()
    payload.message = "⚠️ Winter Weather Advisory — test"

    # Call _post_broadcast_commit directly — does NOT transmit anything.
    d._post_broadcast_commit(ev, payload, rule, "mesh_broadcast", success=True)

    conn = get_db()
    row = conn.execute(
        "SELECT source_event_table, source_event_pk, transport "
        "FROM mesh_broadcasts_out ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None, "No audit row was written"
    assert row["source_event_table"] == "nws_alerts", (
        f"Expected 'nws_alerts', got {row['source_event_table']!r}"
    )
    assert row["source_event_pk"] is None, "pk should be NULL for fallback path"


def test_source_to_table_fallback_stamps_ipaws_audit_row(db_path):
    """Parity guard for the IPAWS civil-alert adapter: a native ipaws event
    with no _broadcast_audit must stamp source_event_table='ipaws_alerts' so
    emergency sends show labeled (not NULL/unlabeled) in the Activity Log.
    """
    from unittest.mock import MagicMock
    from meshai.notifications.events import make_event
    from meshai.notifications.pipeline.dispatcher import Dispatcher
    from meshai.persistence import get_db

    cfg = _build_config(cold_start_grace=0)
    factory, _ = _mk_channel_factory()
    d = Dispatcher(cfg, factory)

    ev = make_event(
        source="ipaws",
        category="emergency_evacuation",
        severity="immediate",
        region="US-ID",
        title="🚨 Evacuation Immediate",
        lat=43.6, lon=-116.2,
    )

    rule = MagicMock()
    rule.broadcast_channel = 1
    rule.delivery_types = ["mesh_broadcast"]

    payload = MagicMock()
    payload.message = "🚨 Evacuation Immediate — test"

    d._post_broadcast_commit(ev, payload, rule, "mesh_broadcast", success=True)

    conn = get_db()
    row = conn.execute(
        "SELECT source_event_table FROM mesh_broadcasts_out ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None, "No audit row was written"
    assert row["source_event_table"] == "ipaws_alerts", (
        f"Expected 'ipaws_alerts', got {row['source_event_table']!r}"
    )


def test_source_to_table_fallback_all_native_sources(db_path):
    """Verify _SOURCE_TO_TABLE covers all native adapter sources."""
    from meshai.notifications.pipeline.dispatcher import Dispatcher
    expected = {
        "nws":     "nws_alerts",
        "nifc":    "fires",
        "wzdx":    "traffic_events",
        "traffic": "traffic_events",
        "511":     "traffic_events",
        "ipaws":   "ipaws_alerts",
    }
    cfg = _build_config()
    factory, _ = _mk_channel_factory()
    d = Dispatcher(cfg, factory)
    assert d._SOURCE_TO_TABLE == expected, (
        f"Map mismatch: {d._SOURCE_TO_TABLE!r}"
    )


def test_broadcast_audit_present_not_overridden(db_path):
    """When _broadcast_audit IS stamped, _post_broadcast_commit must use its
    table/pk, NOT the _SOURCE_TO_TABLE fallback."""
    from unittest.mock import MagicMock
    from meshai.notifications.events import make_event
    from meshai.notifications.pipeline.dispatcher import Dispatcher
    from meshai.persistence import get_db

    cfg = _build_config(cold_start_grace=0)
    factory, _ = _mk_channel_factory()
    d = Dispatcher(cfg, factory)

    ev = make_event(
        source="nws",
        category="weather_alert",
        severity="priority",
        region="US-ID",
        title="⚠️ Test explicit audit",
        lat=43.6, lon=-116.2,
    )
    # Stamp explicit audit (as Central handlers do).
    ev.data["_broadcast_audit"] = {"table": "nws_alerts", "pk": "CAP-XYZ-001"}

    rule = MagicMock()
    rule.broadcast_channel = 1
    rule.delivery_types = ["mesh_broadcast"]
    payload = MagicMock()
    payload.message = "⚠️ Test explicit audit"

    d._post_broadcast_commit(ev, payload, rule, "mesh_broadcast", success=True)

    conn = get_db()
    row = conn.execute(
        "SELECT source_event_table, source_event_pk "
        "FROM mesh_broadcasts_out ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["source_event_table"] == "nws_alerts"
    assert row["source_event_pk"] == "CAP-XYZ-001", (
        f"Expected pk='CAP-XYZ-001', got {row['source_event_pk']!r}"
    )
