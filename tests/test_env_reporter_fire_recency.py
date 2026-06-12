"""Tests for the hybrid recent+largest fire context in build_fires_detail.

Validates that the env_reporter now surfaces small fresh fires alongside
large historic ones, excludes tombstoned fires, deduplicates, and respects
the block character cap.
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


class TestFireRecencyHybrid:
    """T1-T5: hybrid recent+largest query tests."""

    def test_t1_fresh_tiny_fires_appear(self, reporter):
        """T1: Both fresh tiny fires appear in build_fires_detail output."""
        conn = get_db()
        _seed_scenario(conn)
        text = reporter.build_fires_detail()
        assert "Bingham Co. Assist 3" in text, (
            "Fresh tiny fire 'Bingham Co. Assist 3' missing from output")
        assert "IA 1" in text, (
            "Fresh tiny fire 'IA 1' missing from output")

    def test_t2_largest_non_tombstoned_appears(self, reporter):
        """T2: The largest non-tombstoned fire appears (hybrid keeps big fires)."""
        conn = get_db()
        _seed_scenario(conn)
        text = reporter.build_fires_detail()
        # Old Fire 10 has acres=14050, the largest non-tombstoned
        assert "Old Fire 10" in text, (
            "Largest non-tombstoned fire 'Old Fire 10' missing from output")

    def test_t3_tombstoned_excluded(self, reporter):
        """T3: The tombstoned fire does NOT appear."""
        conn = get_db()
        _seed_scenario(conn)
        text = reporter.build_fires_detail()
        assert "Tombstoned Blaze" not in text, (
            "Tombstoned fire should be excluded")

    def test_t4_no_duplicates(self, reporter):
        """T4: No incident is listed twice (dedup by irwin_id)."""
        conn = get_db()
        _seed_scenario(conn)
        text = reporter.build_fires_detail()
        # Each fire name should appear at most once in the output.
        for name in ("Bingham Co. Assist 3", "IA 1", "Old Fire 10"):
            count = text.count(name)
            assert count <= 1, f"'{name}' appears {count} times (expected <=1)"

    def test_t5_respects_block_cap(self, reporter):
        """T5: Output respects _block_cap() even with long names."""
        conn = get_db()
        now = int(time.time())
        # Seed fires with very long names to exceed cap
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
