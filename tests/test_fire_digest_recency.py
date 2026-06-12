"""Tests for the fire digest deterministic renderer recency ordering.

Validates that render_digest() lists the 2 most recent fires in
last_event_at descending order, excludes contained/tombstoned fires,
shows the correct "N additional" count, and stays within 200 bytes.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from meshai.persistence import get_db


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
    """Seed fires: 3 active, 1 contained, 1 tombstoned."""
    now = int(time.time())
    day = 86400

    # 3 active fires with distinct recency
    _seed_fire(conn, irwin_id="F-01", name="Alpha Fire",
               acres=500, contained=None,
               last_event_at=now - 3600,  # 1 hour ago (most recent)
               county="Ada", state="ID")
    _seed_fire(conn, irwin_id="F-02", name="Bravo Fire",
               acres=200, contained=25,
               last_event_at=now - 7200,  # 2 hours ago
               county="Boise", state="ID")
    _seed_fire(conn, irwin_id="F-03", name="Charlie Fire",
               acres=1000, contained=None,
               last_event_at=now - 2 * day,  # 2 days ago
               county="Elmore", state="ID")

    # 1 fire 100% contained (should be excluded)
    _seed_fire(conn, irwin_id="F-04", name="Contained Fire",
               acres=800, contained=100,
               last_event_at=now - 1800,  # 30 min ago — very recent!
               county="Gem", state="ID")

    # 1 tombstoned fire (should be excluded)
    _seed_fire(conn, irwin_id="F-05", name="Tombstoned Fire",
               acres=3000, contained=50,
               last_event_at=now - day,
               tombstoned_at=now - 3600,
               county="Owyhee", state="ID")


class TestFireDigestRecency:
    """Deterministic fire digest renderer tests."""

    def test_top_2_listed_in_recency_order(self):
        """The 2 most recent active fires are listed in order."""
        conn = get_db()
        now = int(time.time())
        _seed_scenario(conn)
        from meshai.notifications.scheduled.fire_digest import render_digest
        wire, source = asyncio.run(render_digest(now=now))
        assert source == "deterministic"
        assert "Alpha Fire" in wire
        assert "Bravo Fire" in wire
        pos_alpha = wire.index("Alpha Fire")
        pos_bravo = wire.index("Bravo Fire")
        assert pos_alpha < pos_bravo, "Alpha Fire (most recent) should appear before Bravo Fire"

    def test_contained_excluded(self):
        """100%-contained fires are excluded from the digest."""
        conn = get_db()
        now = int(time.time())
        _seed_scenario(conn)
        from meshai.notifications.scheduled.fire_digest import render_digest
        wire, source = asyncio.run(render_digest(now=now))
        assert "Contained Fire" not in wire

    def test_tombstoned_excluded(self):
        """Tombstoned fires are excluded from the digest."""
        conn = get_db()
        now = int(time.time())
        _seed_scenario(conn)
        from meshai.notifications.scheduled.fire_digest import render_digest
        wire, source = asyncio.run(render_digest(now=now))
        assert "Tombstoned Fire" not in wire

    def test_n_additional_count_correct(self):
        """The 'N additional' tail sentence has the right count."""
        conn = get_db()
        now = int(time.time())
        _seed_scenario(conn)
        from meshai.notifications.scheduled.fire_digest import render_digest
        wire, source = asyncio.run(render_digest(now=now))
        # 3 active fires total, 2 shown, 1 remaining
        assert "There are 1 additional wildfires. DM me for the full list." in wire

    def test_n_zero_omits_sentence(self):
        """When N == 0, the tail sentence is omitted entirely."""
        conn = get_db()
        now = int(time.time())
        # Only 2 active fires
        _seed_fire(conn, irwin_id="X-01", name="Fire One",
                   acres=100, contained=None,
                   last_event_at=now - 3600,
                   county="Ada", state="ID")
        _seed_fire(conn, irwin_id="X-02", name="Fire Two",
                   acres=50, contained=10,
                   last_event_at=now - 7200,
                   county="Boise", state="ID")
        from meshai.notifications.scheduled.fire_digest import render_digest
        wire, source = asyncio.run(render_digest(now=now))
        assert source == "deterministic"
        assert "additional" not in wire
        assert "Fire One" in wire
        assert "Fire Two" in wire

    def test_rendered_within_200_bytes(self):
        """Rendered output must be <= 200 bytes for LoRa budget."""
        conn = get_db()
        now = int(time.time())
        _seed_scenario(conn)
        from meshai.notifications.scheduled.fire_digest import render_digest
        wire, source = asyncio.run(render_digest(now=now))
        byte_len = len(wire.encode("utf-8"))
        assert byte_len <= 200, f"Digest is {byte_len} bytes, exceeds 200-byte budget"

    def test_no_fires_returns_empty(self):
        """No active fires -> empty wire, 'no_fires' source."""
        from meshai.notifications.scheduled.fire_digest import render_digest
        wire, source = asyncio.run(render_digest())
        assert wire == ""
        assert source == "no_fires"
