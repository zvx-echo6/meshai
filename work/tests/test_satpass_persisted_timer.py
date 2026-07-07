"""Tests for the satpass persisted-timer reboot-recovery fix.

Pending satellite-pass consolidations used to be scheduled only as in-memory
asyncio TimerHandles, so a restart orphaned any satpass_pending rows: the row
survived but its timer did not, and it was never consolidated/broadcast.

The fix persists a durable `due_at` on each pending row and adds a startup
sweep (`CentralConsumer._sweep_pending_satpass`) that reconstructs a timer for
every pending consolidated_id off its persisted due_at, reusing the existing
`_satpass_consolidation_fire` emit path.

These tests cover:
  - a PAST-due orphan is recovered (its timer fires -> consolidation invoked)
  - a FUTURE-due row is scheduled, NOT fired immediately
  - `due_at` is persisted on the normal ingest path
  - SCHEMA_VERSION == 22 and the v22 migration applies cleanly on a fresh DB
"""
from __future__ import annotations

import asyncio
import json
import time
import types

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


def _insert_pending(consolidated_id, *, due_at, observer="Boise",
                    norad_id=25544, received_at=None):
    """Write a single satpass_pending row with an explicit due_at."""
    conn = get_db()
    now = int(time.time()) if received_at is None else received_at
    aos = now + 600
    los = aos + 360
    conn.execute(
        "INSERT OR REPLACE INTO satpass_pending("
        "consolidated_id, observer, sat_name, norad_id, max_elevation, "
        "aos_at, los_at, aos_compass, los_compass, peak_compass, received_at, "
        "due_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (consolidated_id, observer, "ISS", norad_id, 72.5,
         aos, los, "SW", "NE", "S", now, due_at))


def _make_consumer(bus=None):
    """Construct a CentralConsumer with minimal fakes (no NATS needed)."""
    from meshai.central.consumer import CentralConsumer
    env = types.SimpleNamespace(central=None)
    return CentralConsumer(env, bus)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


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
    # Bumped to 26 by the generic-source migration (v26 generic_events); v24
    # avalanche_events, v25 ducting_events; was 23 at native-satpass (v23).
    assert SCHEMA_VERSION == 26


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
    assert int(row["value"]) == 26
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


# ── startup sweep: past-due orphan is recovered ──────────────────────

def test_sweep_recovers_past_due_orphan(monkeypatch):
    """A pending row with due_at in the PAST fires consolidation via the sweep."""
    _enable_satpass_db(norad_ids=[25544], dry_run=True)
    now = int(time.time())
    cid = "25544:ORPHAN"
    _insert_pending(cid, due_at=now - 100, received_at=now - 105)

    fired = []
    import meshai.central.satpass_handler as sh
    real = sh.consolidate_satpass_pending

    def _spy(consolidated_id):
        fired.append(consolidated_id)
        return real(consolidated_id)  # exercise the real path (dry-run -> None)

    monkeypatch.setattr(sh, "consolidate_satpass_pending", _spy)

    consumer = _make_consumer(bus=None)

    async def _main():
        consumer._sweep_pending_satpass(now=now)
        # overdue orphan is armed at ~0.5s; give the loop time to fire it.
        await asyncio.sleep(1.0)

    _run(_main())

    assert cid in fired, "sweep did not fire consolidation for the orphaned cid"
    # Orphan recovered: consolidation (dry-run) drained its pending rows.
    conn = get_db()
    remaining = conn.execute(
        "SELECT COUNT(*) AS n FROM satpass_pending WHERE consolidated_id=?",
        (cid,)).fetchone()["n"]
    assert remaining == 0


# ── startup sweep: future row scheduled, not fired now ───────────────

def test_sweep_schedules_future_row_without_firing(monkeypatch):
    """A pending row with due_at in the FUTURE is armed but does not fire yet."""
    _enable_satpass_db(norad_ids=[25544], dry_run=True)
    now = int(time.time())
    cid = "25544:FUTURE"
    _insert_pending(cid, due_at=now + 3600, received_at=now)

    fired = []
    import meshai.central.satpass_handler as sh
    monkeypatch.setattr(sh, "consolidate_satpass_pending",
                        lambda c: fired.append(c))

    consumer = _make_consumer(bus=None)

    async def _main():
        consumer._sweep_pending_satpass(now=now)
        await asyncio.sleep(0.3)

    _run(_main())

    assert cid not in fired, "future row fired immediately"
    assert cid in consumer._pending_satpass_timers, "future row was not armed"
    # Pending row untouched (still awaiting its future fire).
    conn = get_db()
    remaining = conn.execute(
        "SELECT COUNT(*) AS n FROM satpass_pending WHERE consolidated_id=?",
        (cid,)).fetchone()["n"]
    assert remaining == 1


# ── sweep does not double-schedule an already-armed cid ──────────────

def test_sweep_does_not_double_schedule(monkeypatch):
    """A cid already armed by the live path is skipped by the sweep."""
    _enable_satpass_db(norad_ids=[25544], dry_run=True)
    now = int(time.time())
    cid = "25544:ARMED"
    _insert_pending(cid, due_at=now - 10, received_at=now - 15)

    consumer = _make_consumer(bus=None)

    fired = []
    import meshai.central.satpass_handler as sh
    monkeypatch.setattr(sh, "consolidate_satpass_pending",
                        lambda c: fired.append(c))

    async def _main():
        sentinel = object()
        consumer._pending_satpass_timers[cid] = sentinel  # live path owns it
        consumer._sweep_pending_satpass(now=now)
        # The sweep must not have replaced the live handle.
        assert consumer._pending_satpass_timers[cid] is sentinel
        await asyncio.sleep(0.1)

    _run(_main())
    assert cid not in fired, "sweep double-scheduled an already-armed cid"
