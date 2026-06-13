"""Tests for recency-only fire ordering in build_fires_detail.

Validates that the env_reporter surfaces fires ordered strictly by
last_event_at descending, excludes 100%-contained and tombstoned fires,
and respects the block character cap.
"""
from __future__ import annotations

import time

import pytest

from meshai.notifications.env_reporter import EnvReporter
from meshai.persistence import get_db


@pytest.fixture
def reporter():
    return EnvReporter()


def _seed_fire(conn, *, irwin_id, name, acres, contained=None, lat=43.6, lon=-116.2,
               county="Ada", state="ID", declared_at=None, last_event_at=None,
               tombstoned_at=None):
    now = int(time.time())
    conn.execute(
        "INSERT OR REPLACE INTO fires(irwin_id, incident_name, incident_type, "
        "current_acres, current_contained_pct, lat, lon, county, state, "
        "declared_at, last_event_at, tombstoned_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (irwin_id, name, "WF", acres, contained, lat, lon, county, state,
         declared_at or now, last_event_at or now, tombstoned_at),
    )


def _seed_scenario(conn):
    """Seed 14 fires: 11 older large, 2 fresh tiny, 1 large tombstoned."""
    now = int(time.time())
    day = 86400

    # 11 older large fires (3-6 days old, 50-15000 acres)
    for i in range(11):
        _seed_fire(conn, irwin_id=f"OLD-{i:02d}", name=f"Old Fire {i}",
                   acres=50 + i * 1400,  # 50, 1450, 2850 ... 14050
                   contained=100 if i < 5 else None,
                   last_event_at=now - (3 + i % 4) * day,
                   county="Owyhee", state="ID")

    # 2 fresh tiny fires (< 1 day old, small acres)
    _seed_fire(conn, irwin_id="FRESH-01", name="Bingham Co. Assist 3",
               acres=0.3, contained=None,
               last_event_at=now - 3600,  # 1 hour ago
               county="Bingham", state="ID")
    _seed_fire(conn, irwin_id="FRESH-02", name="IA 1",
               acres=0.1, contained=None,
               last_event_at=now - 7200,  # 2 hours ago
               county="Twin Falls", state="ID")

    # 1 large fire that is tombstoned
    _seed_fire(conn, irwin_id="TOMB-01", name="Tombstoned Blaze",
               acres=9999, contained=100,
               last_event_at=now - 2 * day,
               tombstoned_at=now - day,
               county="Lincoln", state="ID")


class TestFireRecencyOnly:
    """T1-T5: recency-only ordering + contained exclusion."""

    def test_t1_output_strictly_recency_ordered(self, reporter):
        """All listed fires appear in last_event_at descending order."""
        conn = get_db()
        _seed_scenario(conn)
        text = reporter.build_fires_detail()
        # Fresh fires (1h, 2h ago) must appear before older fires (3-6d)
        pos_fresh1 = text.index("Bingham Co. Assist 3")
        pos_fresh2 = text.index("IA 1")
        assert pos_fresh1 < pos_fresh2, (
            "Most recent fire should appear before second most recent")
        # Any old fire that appears must come after both fresh fires
        for i in range(5, 11):  # only non-contained old fires
            name = f"Old Fire {i}"
            if name in text:
                pos_old = text.index(name)
                assert pos_fresh2 < pos_old, (
                    f"Fresh fires should appear before '{name}'")

    def test_t2_contained_100_excluded(self, reporter):
        """Fires with current_contained_pct == 100 are excluded."""
        conn = get_db()
        _seed_scenario(conn)
        text = reporter.build_fires_detail()
        # Old fires 0-4 have contained=100
        for i in range(5):
            assert f"Old Fire {i}:" not in text, (
                f"100%-contained 'Old Fire {i}' should be excluded")

    def test_t2b_null_containment_included(self, reporter):
        """Fires with NULL containment (uncontained) are included."""
        conn = get_db()
        _seed_scenario(conn)
        text = reporter.build_fires_detail()
        assert "Bingham Co. Assist 3" in text
        assert "IA 1" in text

    def test_t3_tombstoned_excluded(self, reporter):
        """Tombstoned fires do not appear."""
        conn = get_db()
        _seed_scenario(conn)
        text = reporter.build_fires_detail()
        assert "Tombstoned Blaze" not in text

    def test_t4_respects_block_cap(self, reporter):
        """Output respects _block_cap() even with long names."""
        conn = get_db()
        now = int(time.time())
        for i in range(12):
            long_name = f"Extremely Long Incident Name For Testing Purposes Number {i:02d} " + "X" * 100
            _seed_fire(conn, irwin_id=f"LONG-{i:02d}", name=long_name,
                       acres=100 + i * 500,
                       last_event_at=now - 3600 * (i + 1),
                       county="Test", state="ID")
        text = reporter.build_fires_detail()
        from meshai.notifications.env_reporter import _block_cap
        assert len(text) <= _block_cap(), (
            f"Output length {len(text)} exceeds cap {_block_cap()}")
