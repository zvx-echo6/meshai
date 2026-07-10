"""Part 2 tests -- gating/incident.py decide() work-zone broadcast suppression.

A work zone (source='wzdx', OR any source with sub_type='road_works') must
be STORED in traffic_events but its decide() call must return
GateResult.broadcast=False. A 511 crash/closure event (different sub_type)
must still broadcast unchanged.
"""
from __future__ import annotations

import pytest

from meshai.notifications.gating.incident import decide
from meshai.persistence import get_db


_WZDX_DATA = {
    "external_id":    "wzdx_US-95|43.63|-116.915|lanes reduced, surface work",
    "source":         "wzdx",
    "sub_type":       "lanes reduced, surface work",
    "road":           "US-95",
    "direction":      "southbound",
    "from_loc":       None,
    "to_loc":         None,
    "mile_start":     93,
    "mile_end":       89,
    "county":         None,
    "state":          None,
    "lat":            43.63,
    "lon":            -116.915,
    "impact":         "partial",
    "start_at":       1_783_200_000,
    "end_at":         1_783_260_000,
    "magnitude":      None,
    "delay_seconds":  None,
    "icon_category":  "road_works",
}

_ITD511_WORKZONE_DATA = {
    "external_id":    "511_WZ-9001",
    "source":         "itd_511",
    "sub_type":       "road_works",
    "road":           "I-84",
    "direction":      "eastbound",
    "from_loc":       None,
    "to_loc":         None,
    "mile_start":     168,
    "mile_end":       173,
    "county":         "Ada",
    "state":          "ID",
    "lat":            43.5,
    "lon":            -116.2,
    "impact":         "partial",
    "start_at":       1_783_200_000,
    "end_at":         None,
    "magnitude":      None,
    "delay_seconds":  None,
    "icon_category":  "road_works",
}

_ITD511_CRASH_DATA = {
    "external_id":    "511_INC-4242",
    "source":         "itd_511",
    "sub_type":       "accident",
    "road":           "US-93",
    "direction":      "northbound",
    "from_loc":       None,
    "to_loc":         None,
    "mile_start":     47,
    "mile_end":       None,
    "county":         "Twin Falls",
    "state":          "ID",
    "lat":            42.5,
    "lon":            -114.5,
    "impact":         None,
    "start_at":       1_783_200_000,
    "end_at":         None,
    "magnitude":      3,
    "delay_seconds":  600,
    "icon_category":  "accident",
}

_ITD511_CLOSURE_DATA = {
    "external_id":    "511_CL-7777",
    "source":         "itd_511",
    "sub_type":       "road_closed",
    "road":           "I-15",
    "direction":      "both",
    "from_loc":       None,
    "to_loc":         None,
    "mile_start":     100,
    "mile_end":       102,
    "county":         "Bingham",
    "state":          "ID",
    "lat":            43.0,
    "lon":            -112.3,
    "impact":         "full_closure",
    "start_at":       1_783_200_000,
    "end_at":         None,
    "magnitude":      None,
    "delay_seconds":  None,
    "icon_category":  "road_closed",
}


def _row(source, external_id):
    conn = get_db()
    return conn.execute(
        "SELECT * FROM traffic_events WHERE source=? AND external_id=?",
        (source, external_id),
    ).fetchone()


class TestWorkZoneSuppressed:
    """A work zone is persisted but never broadcast per-event."""

    def test_native_wzdx_new_row_stored_but_not_broadcast(self):
        result = decide(dict(_WZDX_DATA), source="wzdx", now=1_783_200_000.0)
        assert result.broadcast is False
        assert result.lifecycle == "suppress"
        assert "work_zone" in result.reason

        row = _row("wzdx", _WZDX_DATA["external_id"])
        assert row is not None
        assert row["road"] == "US-95"
        assert row["last_broadcast_at"] is None

    def test_native_wzdx_existing_row_still_stored_not_broadcast(self):
        # First decide() inserts the row.
        decide(dict(_WZDX_DATA), source="wzdx", now=1_783_200_000.0)
        # Second decide() (existing row branch) must ALSO suppress + persist.
        updated = dict(_WZDX_DATA, impact="full_closure")
        result = decide(updated, source="wzdx", now=1_783_200_050.0)
        assert result.broadcast is False
        assert result.lifecycle == "suppress"

        row = _row("wzdx", _WZDX_DATA["external_id"])
        assert row is not None
        assert row["impact"] == "full_closure"   # refreshed by the UPDATE
        assert row["last_broadcast_at"] is None  # never armed

    def test_itd511_road_works_subtype_also_suppressed(self):
        """The sub_type='road_works' discriminator catches a work zone
        arriving through the itd_511 source too (not just native wzdx)."""
        result = decide(dict(_ITD511_WORKZONE_DATA), source="itd_511",
                          now=1_783_200_000.0)
        assert result.broadcast is False
        assert result.lifecycle == "suppress"

        row = _row("itd_511", _ITD511_WORKZONE_DATA["external_id"])
        assert row is not None
        assert row["sub_type"] == "road_works"
        assert row["last_broadcast_at"] is None


class Test511IncidentsStillBroadcast:
    """511 crash/closure (non-work-zone sub_types) are completely unaffected."""

    def test_itd511_crash_broadcasts(self):
        result = decide(dict(_ITD511_CRASH_DATA), source="itd_511",
                          now=1_783_200_000.0)
        assert result.broadcast is True
        assert result.lifecycle == "new"
        assert callable(result.commit)

        row = _row("itd_511", _ITD511_CRASH_DATA["external_id"])
        assert row is not None

    def test_itd511_closure_broadcasts(self):
        result = decide(dict(_ITD511_CLOSURE_DATA), source="itd_511",
                          now=1_783_200_000.0)
        assert result.broadcast is True
        assert result.lifecycle == "new"
        assert callable(result.commit)

        row = _row("itd_511", _ITD511_CLOSURE_DATA["external_id"])
        assert row is not None

    def test_itd511_crash_cold_start_still_broadcasts(self):
        """Row exists (from a prior INSERT) but never committed -> cold-start
        -> still broadcasts (unaffected by the work-zone suppression path)."""
        decide(dict(_ITD511_CRASH_DATA), source="itd_511", now=1_783_200_000.0)
        result = decide(dict(_ITD511_CRASH_DATA), source="itd_511",
                          now=1_783_200_010.0)
        assert result.broadcast is True
        assert result.lifecycle == "new"
