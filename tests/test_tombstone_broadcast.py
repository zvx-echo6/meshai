"""Tests for tombstone broadcast path fix.

Validates:
  T1: tombstone yields _severity_override="immediate" + commit handles
  T2: closure wire dispatches when New was broadcast >=10min earlier
  T3: build_env_summary excludes tombstoned and 100%-contained fires
"""
from __future__ import annotations

import time

import pytest

from meshai.central.wfigs_handler import handle_wfigs
from meshai.notifications.env_reporter import EnvReporter
from meshai.persistence import get_db


@pytest.fixture
def reporter():
    return EnvReporter()


def _seed_fire(conn, *, irwin_id, name, acres, contained=None,
               last_broadcast_at=None, last_event_at=None,
               tombstoned_at=None, county="Ada", state="ID"):
    now = int(time.time())
    conn.execute(
        "INSERT OR REPLACE INTO fires(irwin_id, incident_name, incident_type, "
        "current_acres, current_contained_pct, lat, lon, county, state, "
        "declared_at, last_event_at, last_broadcast_at, tombstoned_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (irwin_id, name, "WF", acres, contained, 43.6, -116.2,
         county, state, now, last_event_at or now,
         last_broadcast_at, tombstoned_at),
    )


class TestTombstoneSeverityAndCommitHandles:
    """T1: tombstone branch sets immediate severity and attaches commit handles."""

    def test_severity_is_immediate(self):
        conn = get_db()
        now = int(time.time())
        _seed_fire(conn, irwin_id="FIRE-001", name="Test Fire",
                   acres=500, contained=80,
                   last_broadcast_at=now - 3600)

        data = {}
        wire = handle_wfigs(
            normalized={"_kind": "wfigs_tombstone", "irwin_id": "FIRE-001"},
            envelope={"data": {"category": "wildfire", "severity": "immediate"}},
            subject="wfigs.tombstone",
            data=data,
            now=now,
        )
        assert wire is not None, "tombstone should produce wire for previously-broadcast fire"
        assert data.get("_severity_override") == "immediate", (
            f"expected immediate, got {data.get('_severity_override')}")

    def test_commit_handles_attached(self):
        conn = get_db()
        now = int(time.time())
        _seed_fire(conn, irwin_id="FIRE-002", name="Handled Fire",
                   acres=1000, contained=95,
                   last_broadcast_at=now - 7200)

        data = {}
        wire = handle_wfigs(
            normalized={"_kind": "wfigs_tombstone", "irwin_id": "FIRE-002"},
            envelope={"data": {"category": "wildfire", "severity": "immediate"}},
            subject="wfigs.tombstone",
            data=data,
            now=now,
        )
        assert wire is not None
        assert "_on_broadcast_committed" in data, "commit callback missing"
        assert "_broadcast_audit" in data, "broadcast audit descriptor missing"
        assert "_cooldown_suffix" in data, "cooldown suffix missing"
        assert data["_cooldown_suffix"] == "FIRE-002"

    def test_dedup_suffix_is_closed(self):
        conn = get_db()
        now = int(time.time())
        _seed_fire(conn, irwin_id="FIRE-003", name="Dedup Fire",
                   acres=200, contained=100,
                   last_broadcast_at=now - 600)

        data = {}
        wire = handle_wfigs(
            normalized={"_kind": "wfigs_tombstone", "irwin_id": "FIRE-003"},
            envelope={"data": {"category": "wildfire", "severity": "immediate"}},
            subject="wfigs.tombstone",
            data=data,
            now=now,
        )
        assert wire is not None
        assert data.get("_dedup_suffix") == "closed", (
            f"expected 'closed', got {data.get('_dedup_suffix')}")

    def test_commit_callback_flips_handled(self):
        """The commit callback should flip event_log.handled to 1."""
        conn = get_db()
        now = int(time.time())
        _seed_fire(conn, irwin_id="FIRE-004", name="Callback Fire",
                   acres=300, contained=50,
                   last_broadcast_at=now - 1800)

        data = {}
        wire = handle_wfigs(
            normalized={"_kind": "wfigs_tombstone", "irwin_id": "FIRE-004"},
            envelope={"data": {"category": "wildfire", "severity": "immediate"}},
            subject="wfigs.tombstone",
            data=data,
            now=now,
        )
        assert wire is not None
        assert "_on_broadcast_committed" in data

        # Before commit: event_log row should have handled=0
        row_before = conn.execute(
            "SELECT id, handled FROM event_log WHERE event_id_external='FIRE-004' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row_before is not None, "event_log row should exist"
        assert row_before["handled"] == 0, "should be unhandled before commit"

        # Fire the commit callback
        data["_on_broadcast_committed"](float(now))

        # After commit: handled should be 1
        row_after = conn.execute(
            "SELECT handled FROM event_log WHERE id=?",
            (row_before["id"],)
        ).fetchone()
        assert row_after["handled"] == 1, "should be handled=1 after commit callback"


class TestTombstoneAfterNewBroadcast:
    """T2: closure dispatches when a New broadcast went out earlier."""

    def test_closure_wire_after_prior_broadcast(self):
        """Fire that was broadcast 10 min ago gets a closure wire on tombstone."""
        conn = get_db()
        now = int(time.time())
        _seed_fire(conn, irwin_id="IA-1", name="IA 1",
                   acres=0.1, contained=None,
                   last_broadcast_at=now - 600,  # 10 min ago
                   last_event_at=now - 600)

        data = {}
        wire = handle_wfigs(
            normalized={"_kind": "wfigs_tombstone", "irwin_id": "IA-1"},
            envelope={"data": {"category": "wildfire", "severity": "immediate"}},
            subject="wfigs.tombstone",
            data=data,
            now=now,
        )
        assert wire is not None, "tombstone should produce wire"
        assert "✅" in wire, "closure wire should contain checkmark"
        assert "IA 1" in wire, "closure wire should name the fire"
        assert data["category"] == "wildfire_closed"
        assert data["_severity_override"] == "immediate"
        assert callable(data.get("_on_broadcast_committed"))

    def test_no_wire_when_never_broadcast(self):
        """Fire that was never broadcast should NOT get a closure wire."""
        conn = get_db()
        now = int(time.time())
        _seed_fire(conn, irwin_id="SILENT-1", name="Silent Fire",
                   acres=5, contained=None,
                   last_broadcast_at=None,  # never broadcast
                   last_event_at=now - 600)

        data = {}
        wire = handle_wfigs(
            normalized={"_kind": "wfigs_tombstone", "irwin_id": "SILENT-1"},
            envelope={"data": {"category": "wildfire", "severity": "immediate"}},
            subject="wfigs.tombstone",
            data=data,
            now=now,
        )
        assert wire is None, "no closure wire for never-broadcast fire"

    def test_tombstoned_at_stamped(self):
        """Tombstone should stamp tombstoned_at on the fires row."""
        conn = get_db()
        now = int(time.time())
        _seed_fire(conn, irwin_id="STAMP-1", name="Stamp Fire",
                   acres=10, contained=50,
                   last_broadcast_at=now - 3600)

        handle_wfigs(
            normalized={"_kind": "wfigs_tombstone", "irwin_id": "STAMP-1"},
            envelope={"data": {"category": "wildfire", "severity": "immediate"}},
            subject="wfigs.tombstone",
            data={},
            now=now,
        )
        row = conn.execute(
            "SELECT tombstoned_at FROM fires WHERE irwin_id='STAMP-1'"
        ).fetchone()
        assert row is not None
        assert row["tombstoned_at"] == now


class TestEnvSummaryExcludesContainedTombstoned:
    """T3: build_env_summary fire count excludes tombstoned and 100%-contained."""

    def test_summary_excludes_tombstoned(self, reporter):
        conn = get_db()
        now = int(time.time())
        # Active fire
        _seed_fire(conn, irwin_id="ACTIVE-1", name="Active Fire",
                   acres=500, contained=20,
                   last_event_at=now - 3600)
        # Tombstoned fire
        _seed_fire(conn, irwin_id="DEAD-1", name="Dead Fire",
                   acres=1000, contained=100,
                   last_event_at=now - 7200,
                   tombstoned_at=now - 3600)
        text = reporter.build_env_summary(now=now)
        assert "Active fires (WFIGS, last 7d): 1" in text, (
            f"should count 1 active fire, got: {text}")

    def test_summary_excludes_100_contained(self, reporter):
        conn = get_db()
        now = int(time.time())
        # Active fire, 50% contained
        _seed_fire(conn, irwin_id="HALF-1", name="Half Contained",
                   acres=300, contained=50,
                   last_event_at=now - 3600)
        # 100% contained, NOT tombstoned
        _seed_fire(conn, irwin_id="FULL-1", name="Fully Contained",
                   acres=800, contained=100,
                   last_event_at=now - 7200)
        text = reporter.build_env_summary(now=now)
        assert "Active fires (WFIGS, last 7d): 1" in text, (
            f"should count 1 active fire, got: {text}")

    def test_summary_includes_null_containment(self, reporter):
        conn = get_db()
        now = int(time.time())
        # Two fires with NULL containment
        _seed_fire(conn, irwin_id="NULL-1", name="No Containment 1",
                   acres=10, contained=None,
                   last_event_at=now - 3600)
        _seed_fire(conn, irwin_id="NULL-2", name="No Containment 2",
                   acres=20, contained=None,
                   last_event_at=now - 7200)
        text = reporter.build_env_summary(now=now)
        assert "Active fires (WFIGS, last 7d): 2" in text, (
            f"should count 2 active fires, got: {text}")

    def test_summary_empty_when_all_excluded(self, reporter):
        conn = get_db()
        now = int(time.time())
        # Only tombstoned and 100%-contained fires
        _seed_fire(conn, irwin_id="EX-1", name="Ex 1",
                   acres=100, contained=100,
                   last_event_at=now - 3600,
                   tombstoned_at=now - 1800)
        _seed_fire(conn, irwin_id="EX-2", name="Ex 2",
                   acres=200, contained=100,
                   last_event_at=now - 7200)
        text = reporter.build_env_summary(now=now)
        assert "Active fires" not in text, (
            f"should not mention fires when all excluded, got: {text}")
