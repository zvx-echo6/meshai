"""Tests for tombstone broadcast path fix.

Validates:
  T1: tombstone yields _severity_override="priority" + commit handles
  T2: closure wire dispatches when New was broadcast >=10min earlier
  T3: build_env_summary excludes tombstoned and 100%-contained fires

Severity note: fire broadcasts (new/update/tombstone-closure alike) were
downgraded from "immediate" to "priority" by commit 2f677e85
("fix(fire): drain-mode pacer to prevent post-reconnect broadcast spam").
After a NATS consumer outage, LAST_PER_SUBJECT delivery could flood
thousands of backlogged events at once; "immediate" severity bypassed the
Grouper and zeroed dispatcher cooldowns, so a backlog replay produced
duplicate "New" broadcasts for the same fire. "priority" routes fire
broadcasts back through the normal pipeline guards (Grouper, cooldown).
This file's expectations were written before that downgrade and never
updated -- "immediate" here would be reverting a deliberate, documented
incident fix.

chore/ripout-2dii: `handle_wfigs` (the dead Central NATS-envelope entrypoint
T1/T2 used to drive) has been REMOVED from `meshai.env.fire_render` -- zero
live production callers. T1/T2 now drive `gating.fire.decide` directly (the
LIVE, shared decider -- `_kind="wfigs_tombstone"` is its all-clear branch,
reused by the shared fire formatter). `test_commit_callback_flips_handled`
(which asserted handle_wfigs's OWN event_log-row flip on commit -- a
Central-only concept the native path never used) was deleted with it.
"""
from __future__ import annotations

import time

import pytest

from meshai.notifications.gating.fire import decide as fire_decide
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
    """T1: tombstone branch sets priority severity and attaches commit handles.

    Drives gating.fire.decide() directly (chore/ripout-2dii: handle_wfigs, the
    dead entrypoint this used to wrap, is gone). decide()'s data_patch carries
    the same stamps the dispatcher reads off event.data.
    """

    def test_severity_is_priority(self):
        conn = get_db()
        now = int(time.time())
        _seed_fire(conn, irwin_id="FIRE-001", name="Test Fire",
                   acres=500, contained=80,
                   last_broadcast_at=now - 3600)

        gate = fire_decide({"_kind": "wfigs_tombstone", "irwin_id": "FIRE-001"},
                           source="wfigs", now=float(now))
        assert gate.broadcast is True, "tombstone should broadcast for previously-broadcast fire"
        # See module docstring: fire severity was deliberately downgraded
        # from "immediate" to "priority" (commit 2f677e85) so fire
        # broadcasts flow through the normal Grouper/cooldown guards.
        assert gate.data_patch.get("_severity_override") == "priority", (
            f"expected priority, got {gate.data_patch.get('_severity_override')}")

    def test_commit_handles_attached(self):
        conn = get_db()
        now = int(time.time())
        _seed_fire(conn, irwin_id="FIRE-002", name="Handled Fire",
                   acres=1000, contained=95,
                   last_broadcast_at=now - 7200)

        gate = fire_decide({"_kind": "wfigs_tombstone", "irwin_id": "FIRE-002"},
                           source="wfigs", now=float(now))
        assert gate.broadcast is True
        assert callable(gate.commit), "commit callback missing"
        assert gate.data_patch.get("_cooldown_suffix") == "FIRE-002"

    def test_dedup_suffix_is_closed(self):
        conn = get_db()
        now = int(time.time())
        _seed_fire(conn, irwin_id="FIRE-003", name="Dedup Fire",
                   acres=200, contained=100,
                   last_broadcast_at=now - 600)

        gate = fire_decide({"_kind": "wfigs_tombstone", "irwin_id": "FIRE-003"},
                           source="wfigs", now=float(now))
        assert gate.broadcast is True
        assert gate.data_patch.get("_dedup_suffix") == "closed", (
            f"expected 'closed', got {gate.data_patch.get('_dedup_suffix')}")


class TestTombstoneAfterNewBroadcast:
    """T2: closure dispatches when a New broadcast went out earlier.

    Drives gating.fire.decide() directly (chore/ripout-2dii: handle_wfigs is
    gone). The wire itself is rendered by the shared, LIVE fire formatter
    (notifications/formatters/fire.py::format) from decide()'s data_patch --
    the same contract the native WFIGS path uses.
    """

    def test_closure_wire_after_prior_broadcast(self):
        """Fire that was broadcast 10 min ago gets a closure wire on tombstone."""
        from meshai.notifications.formatters.fire import format as fire_format
        from meshai.notifications.formatters._budget import budget_for

        class _FakeEvent:
            def __init__(self, data, category=None):
                self.data = data
                self.category = category

        conn = get_db()
        now = int(time.time())
        _seed_fire(conn, irwin_id="IA-1", name="IA 1",
                   acres=0.1, contained=None,
                   last_broadcast_at=now - 600,  # 10 min ago
                   last_event_at=now - 600)

        gate = fire_decide({"_kind": "wfigs_tombstone", "irwin_id": "IA-1"},
                           source="wfigs", now=float(now))
        assert gate.broadcast is True, "tombstone should broadcast"
        assert gate.data_patch["category"] == "wildfire_closed"
        # See module docstring: downgraded from "immediate" to "priority"
        # by commit 2f677e85 to prevent Grouper/cooldown bypass.
        assert gate.data_patch["_severity_override"] == "priority"
        assert callable(gate.commit)

        wire = fire_format(_FakeEvent(gate.data_patch, category="wildfire_closed"),
                           now=0.0, budget=budget_for("wfigs"))
        assert "✅" in wire, "closure wire should contain checkmark"
        assert "IA 1" in wire, "closure wire should name the fire"

    def test_no_wire_when_never_broadcast(self):
        """Fire that was never broadcast should NOT get a closure wire."""
        conn = get_db()
        now = int(time.time())
        _seed_fire(conn, irwin_id="SILENT-1", name="Silent Fire",
                   acres=5, contained=None,
                   last_broadcast_at=None,  # never broadcast
                   last_event_at=now - 600)

        gate = fire_decide({"_kind": "wfigs_tombstone", "irwin_id": "SILENT-1"},
                           source="wfigs", now=float(now))
        assert gate.broadcast is False, "no closure wire for never-broadcast fire"


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
