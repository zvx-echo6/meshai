"""Tests for the fire digest deterministic renderer.

Validates recency ordering, contained/tombstoned exclusion, the
"Name in County Co, ST" line format, correct tail count after budget
trimming, singular/plural grammar, and the 140-byte universal mesh budget.
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

    _seed_fire(conn, irwin_id="F-01", name="Alpha Fire",
               acres=500, contained=None,
               last_event_at=now - 3600,
               county="Ada", state="ID")
    _seed_fire(conn, irwin_id="F-02", name="Bravo Fire",
               acres=200, contained=25,
               last_event_at=now - 7200,
               county="Boise", state="ID")
    _seed_fire(conn, irwin_id="F-03", name="Charlie Fire",
               acres=1000, contained=None,
               last_event_at=now - 2 * day,
               county="Elmore", state="ID")

    _seed_fire(conn, irwin_id="F-04", name="Contained Fire",
               acres=800, contained=100,
               last_event_at=now - 1800,
               county="Gem", state="ID")

    _seed_fire(conn, irwin_id="F-05", name="Tombstoned Fire",
               acres=3000, contained=50,
               last_event_at=now - day,
               tombstoned_at=now - 3600,
               county="Owyhee", state="ID")


class TestFireDigestRecency:
    """Deterministic fire digest renderer tests."""

    def test_top_2_listed_in_recency_order(self):
        """The most recent active fire is listed first.

        With the universal 140-byte budget, the header + 1 fire line + tail
        fits; the second fire line does not.  Alpha (most recent) must appear;
        Bravo belongs to the tail count.
        """
        conn = get_db()
        now = int(time.time())
        _seed_scenario(conn)
        from meshai.notifications.scheduled.fire_digest import render_digest
        wire, source = asyncio.run(render_digest(now=now))
        assert source == "deterministic"
        # Most recent fire must appear in the wire body.
        assert "Alpha Fire" in wire
        # Bravo does not fit within 140 bytes alongside the header + tail.
        assert "Bravo Fire" not in wire

    def test_line_format_name_in_county_co_state(self):
        """Fire lines render as 'Name in County Co, ST'.

        Only the most recent fire fits within the 140-byte budget.
        """
        conn = get_db()
        now = int(time.time())
        _seed_scenario(conn)
        from meshai.notifications.scheduled.fire_digest import render_digest
        wire, _ = asyncio.run(render_digest(now=now))
        assert "Alpha Fire in Ada Co, ID" in wire

    def test_missing_county_renders_name_in_state(self):
        """Fire with no county renders as 'Name in ST'."""
        conn = get_db()
        now = int(time.time())
        _seed_fire(conn, irwin_id="NC-01", name="No County Blaze",
                   acres=100, contained=None,
                   last_event_at=now - 3600,
                   county=None, state="MT")
        _seed_fire(conn, irwin_id="NC-02", name="Second Fire",
                   acres=50, contained=None,
                   last_event_at=now - 7200,
                   county="Ada", state="ID")
        from meshai.notifications.scheduled.fire_digest import render_digest
        wire, _ = asyncio.run(render_digest(now=now))
        assert "No County Blaze in MT" in wire

    def test_contained_excluded(self):
        """100%-contained fires are excluded from the digest."""
        conn = get_db()
        now = int(time.time())
        _seed_scenario(conn)
        from meshai.notifications.scheduled.fire_digest import render_digest
        wire, _ = asyncio.run(render_digest(now=now))
        assert "Contained Fire" not in wire

    def test_tombstoned_excluded(self):
        """Tombstoned fires are excluded from the digest."""
        conn = get_db()
        now = int(time.time())
        _seed_scenario(conn)
        from meshai.notifications.scheduled.fire_digest import render_digest
        wire, _ = asyncio.run(render_digest(now=now))
        assert "Tombstoned Fire" not in wire

    def test_n1_grammar_singular(self):
        """N == 1 renders 'There is 1 additional wildfire.'."""
        conn = get_db()
        now = int(time.time())
        # 3 fires; short names + no county → lines are short enough that
        # 2 fit within the 140-byte budget, leaving 1 in the tail.
        for irwin, name, offset in [
            ("SG-01", "A", 3600),
            ("SG-02", "B", 7200),
            ("SG-03", "C", 10800),
        ]:
            _seed_fire(conn, irwin_id=irwin, name=name, acres=100,
                       contained=None, last_event_at=now - offset,
                       county=None, state="ID")
        from meshai.notifications.scheduled.fire_digest import render_digest
        wire, _ = asyncio.run(render_digest(now=now))
        # 3 active total, 2 shown (budget), 1 remaining
        assert "There is 1 additional wildfire. DM me for the full list." in wire

    def test_n_plural_grammar(self):
        """N > 1 renders 'There are N additional wildfires.'."""
        conn = get_db()
        now = int(time.time())
        for i in range(4):
            _seed_fire(conn, irwin_id=f"PL-{i:02d}", name=f"Fire {i}",
                       acres=100 + i, contained=None,
                       last_event_at=now - 3600 * (i + 1),
                       county="Ada", state="ID")
        from meshai.notifications.scheduled.fire_digest import render_digest
        wire, _ = asyncio.run(render_digest(now=now))
        # 4 active; "Fire N in Ada Co, ID" lines total ~142 bytes for 2 lines +
        # header + tail → only 1 line fits in the 140-byte budget → 3 remaining.
        assert "There are 3 additional wildfires. DM me for the full list." in wire

    def test_n_zero_omits_sentence(self):
        """When N == 0, the tail sentence is omitted entirely."""
        conn = get_db()
        now = int(time.time())
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

    def test_tail_count_correct_after_budget_trim(self):
        """When budget trims a line, N reflects actual shown count."""
        conn = get_db()
        now = int(time.time())
        # Long names force the second line to be trimmed
        long_name_1 = "A" * 60
        long_name_2 = "B" * 60
        _seed_fire(conn, irwin_id="LN-01", name=long_name_1,
                   acres=500, contained=None,
                   last_event_at=now - 3600,
                   county="Bonneville", state="ID")
        _seed_fire(conn, irwin_id="LN-02", name=long_name_2,
                   acres=200, contained=None,
                   last_event_at=now - 7200,
                   county="Bannock", state="ID")
        _seed_fire(conn, irwin_id="LN-03", name="Short Fire",
                   acres=100, contained=None,
                   last_event_at=now - 3 * 86400,
                   county="Ada", state="ID")
        from meshai.notifications.scheduled.fire_digest import render_digest
        wire, source = asyncio.run(render_digest(now=now))
        assert source == "deterministic"
        byte_len = len(wire.encode("utf-8"))
        assert byte_len <= 140, f"Wire is {byte_len} bytes, exceeds 140"
        # If both long lines fit, remaining = 1; if only one fits, remaining = 2
        # Either way the tail count must match (total - shown)
        lines = wire.split("\n")
        shown_fires = [l for l in lines if long_name_1 in l or long_name_2 in l]
        remaining = 3 - len(shown_fires)
        if remaining == 1:
            assert "There is 1 additional wildfire." in wire
        elif remaining > 1:
            assert f"There are {remaining} additional wildfires." in wire

    def test_rendered_within_200_bytes(self):
        """Rendered output must be <= 200 bytes for LoRa budget."""
        conn = get_db()
        now = int(time.time())
        _seed_scenario(conn)
        from meshai.notifications.scheduled.fire_digest import render_digest
        wire, _ = asyncio.run(render_digest(now=now))
        byte_len = len(wire.encode("utf-8"))
        assert byte_len <= 140, f"Digest is {byte_len} bytes, exceeds 140-byte budget"

    def test_no_fires_returns_empty(self):
        """No active fires -> empty wire, 'no_fires' source."""
        from meshai.notifications.scheduled.fire_digest import render_digest
        wire, source = asyncio.run(render_digest())
        assert wire == ""
        assert source == "no_fires"
