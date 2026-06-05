"""v0.6-phase3 NWS dedup-window relaxation tests.

The same CAP id is now re-broadcast (with Active: prefix) when more than
`nws.duplicate_allowed_after_seconds` (default 10800 = 3h) have elapsed
since the last broadcast.
"""
from __future__ import annotations

import time

import pytest

from meshai.central.nws_handler import handle_nws
from meshai.persistence import get_db


def _env(*, cap_id="urn:oid:dedup.001", event="Severe Thunderstorm Warning",
          severity="Severe", area="Ada County", county="Ada", state="ID",
          expires="2026-06-05T03:00:00Z", lat=43.6, lon=-116.2,
          category="wx.alert.severe_thunderstorm_warning"):
    return {
        "id": cap_id, "subject": "central.wx.alert.us.id",
        "data": {
            "id": cap_id, "adapter": "nws", "category": category,
            "severity": 2,
            "geo": {"centroid": [lon, lat], "primary_region": "US-ID"},
            "data": {
                "id": cap_id, "event": event, "severity": severity,
                "areaDesc": area, "msgType": "Alert",
                "headline": f"{event} for {area}",
                "description": "X", "expires": expires,
                "_enriched": {"geocoder": {"city": None,
                                              "county": county, "state": state}},
            },
        },
    }


def _commit(data, ts):
    cb = data["_on_broadcast_committed"]
    cb(float(ts))


def test_first_broadcast_no_active_prefix():
    """A first sighting renders without Active: prefix."""
    env = _env()
    data = {}
    wire = handle_nws(env, env["subject"], data=data, now=1_000_000)
    assert wire is not None
    assert "Active:" not in wire


def test_repeat_within_3h_suppressed():
    env = _env(cap_id="urn:oid:rep1")
    data = {}
    # First broadcast at t=0.
    wire1 = handle_nws(env, env["subject"], data=data, now=0)
    assert wire1 is not None
    _commit(data, 0)

    # Same CAP id again 2h later -- inside 3h window -> suppressed.
    env2 = _env(cap_id="urn:oid:rep1")
    data2 = {}
    wire2 = handle_nws(env2, env2["subject"], data=data2, now=2 * 3600)
    assert wire2 is None


def test_repeat_after_3h_allowed_with_active_prefix():
    env = _env(cap_id="urn:oid:rep2")
    data = {}
    wire1 = handle_nws(env, env["subject"], data=data, now=0)
    assert wire1 is not None
    _commit(data, 0)

    # Same CAP id 4h later -> allowed, Active: prefix.
    env2 = _env(cap_id="urn:oid:rep2")
    data2 = {}
    wire2 = handle_nws(env2, env2["subject"], data=data2, now=4 * 3600)
    assert wire2 is not None
    assert "Active:" in wire2


def test_dedup_window_respects_config_override():
    """Changing nws.duplicate_allowed_after_seconds via adapter_config takes effect."""
    from meshai.adapter_config import invalidate_cache
    conn = get_db()
    conn.execute(
        "UPDATE adapter_config SET value_json='3600' "
        "WHERE adapter='nws' AND key='duplicate_allowed_after_seconds'"
    )
    invalidate_cache()

    env = _env(cap_id="urn:oid:tunable")
    data = {}
    handle_nws(env, env["subject"], data=data, now=0)
    _commit(data, 0)

    # 90 min later -> still suppressed (under new 1h window).
    # Wait, 90 min > 60 min so it would BE allowed. Use 30 min instead.
    env2 = _env(cap_id="urn:oid:tunable")
    data2 = {}
    wire = handle_nws(env2, env2["subject"], data=data2, now=30 * 60)
    assert wire is None  # 30 min < 60 min window

    # 2h later -> allowed (over 1h window now).
    env3 = _env(cap_id="urn:oid:tunable")
    data3 = {}
    wire3 = handle_nws(env3, env3["subject"], data=data3, now=2 * 3600)
    assert wire3 is not None
    assert "Active:" in wire3


def test_handler_stamps_first_broadcast_at():
    """The commit callback writes first_broadcast_at via COALESCE -- only on
    the first commit, never overwriting it."""
    env = _env(cap_id="urn:oid:stamp")
    data = {}
    handle_nws(env, env["subject"], data=data, now=0)
    _commit(data, 100.0)

    conn = get_db()
    row = conn.execute(
        "SELECT first_broadcast_at, last_broadcast_at FROM nws_alerts "
        "WHERE event_id='urn:oid:stamp'"
    ).fetchone()
    assert row["first_broadcast_at"] == 100.0
    assert row["last_broadcast_at"] == 100.0

    # Second broadcast 4h later -> last_broadcast_at updates, first_broadcast_at preserved.
    env2 = _env(cap_id="urn:oid:stamp")
    data2 = {}
    wire2 = handle_nws(env2, env2["subject"], data=data2, now=4 * 3600)
    assert wire2 is not None
    _commit(data2, 4 * 3600.0)
    row2 = conn.execute(
        "SELECT first_broadcast_at, last_broadcast_at FROM nws_alerts "
        "WHERE event_id='urn:oid:stamp'"
    ).fetchone()
    assert row2["first_broadcast_at"] == 100.0  # unchanged
    assert row2["last_broadcast_at"] == 4 * 3600.0
