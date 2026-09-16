"""Watch Duty rendering integration tests: notifications/formatters/fire.py,
env/fire_render.py (FIRMS wildfire_growth path), and
notifications/formatters/firms.py (wildfire_spotting/halted).

Verifies: a matched fire gets Watch Duty's name + a trailing incident-link
line (never truncated) on every shape; an unmatched fire is unaffected.
``geocoder_city`` is always supplied so anchor resolution short-circuits
before it could ever reach the Photon reverse-geocoder network fallback.
"""
from __future__ import annotations

import time

import pytest

from meshai.env.fire_render import _render as _wfigs_render
from meshai.env.watchduty import incident_url
from meshai.notifications.formatters import firms as firms_fmt
from meshai.notifications.formatters.fire import format as fire_format
from meshai.persistence import get_db

_LAT0, _LON0 = 44.0, -114.0


class _FakeEvent:
    def __init__(self, data, category=None):
        self.data = data
        self.category = category


def _insert_matched_fire(conn, irwin_id, *, wd_id="wd123",
                          wd_name="Watch Duty Fire Name"):
    now = int(time.time())
    conn.execute(
        "INSERT INTO fires(irwin_id, incident_name, current_acres, "
        "current_contained_pct, lat, lon, county, state, declared_at, "
        "last_event_at, watchduty_event_id, watchduty_name, "
        "watchduty_is_active) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (irwin_id, f"WFIGS Name {irwin_id}", 500, 10, _LAT0, _LON0,
         "Boise", "ID", now, now, wd_id, wd_name, 1),
    )


# ---------- formatters/fire.py: incident New/Update -------------------------


def test_matched_fire_incident_new_has_wd_name_and_link():
    conn = get_db()
    _insert_matched_fire(conn, "irwin_new")
    d = {
        "irwin_id": "irwin_new", "incident_name": "WFIGS Fallback Name",
        "acres": 1200, "contained_pct": 10, "geocoder_city": "Near Boise, ID",
        "is_update": False,
    }
    wire = fire_format(_FakeEvent(d, category="wildfire_declared"), now=time.time(), budget=140)
    assert "Watch Duty Fire Name" in wire
    assert "WFIGS Fallback Name" not in wire
    assert wire.splitlines()[-1] == incident_url("wd123")
    assert len(wire) <= 140


def test_matched_fire_incident_update_has_wd_name_and_link():
    conn = get_db()
    _insert_matched_fire(conn, "irwin_upd")
    d = {
        "irwin_id": "irwin_upd", "incident_name": "WFIGS Fallback Name",
        "acres": 1500, "contained_pct": 15, "geocoder_city": "Near Boise, ID",
        "is_update": True, "last_bcast_acres": 1200,
    }
    wire = fire_format(_FakeEvent(d, category="wildfire_incident"), now=time.time(), budget=140)
    assert "Watch Duty Fire Name" in wire
    assert "Update" in wire
    assert wire.splitlines()[-1] == incident_url("wd123")


def test_matched_fire_allclear_has_wd_name_and_link():
    conn = get_db()
    _insert_matched_fire(conn, "irwin_clear")
    d = {
        "irwin_id": "irwin_clear", "incident_name": "WFIGS Fallback Name",
        "acres": 1500, "contained_pct": 100,
    }
    wire = fire_format(_FakeEvent(d, category="wildfire_closed"), now=time.time(), budget=140)
    assert "Watch Duty Fire Name" in wire
    assert "contained & closed" in wire
    assert wire.splitlines()[-1] == incident_url("wd123")


def test_unmatched_fire_incident_unchanged_no_link():
    d = {
        "irwin_id": "irwin_unmatched_render", "incident_name": "Plain WFIGS Fire",
        "acres": 1200, "contained_pct": 10, "geocoder_city": "Near Boise, ID",
        "is_update": False,
    }
    wire = fire_format(_FakeEvent(d, category="wildfire_declared"), now=time.time(), budget=140)
    assert "Plain WFIGS Fire" in wire
    assert "watchduty.org" not in wire
    assert "Cause:" not in wire and "Discovered" not in wire


def test_unmatched_fire_allclear_unchanged_no_link():
    d = {
        "irwin_id": "irwin_unmatched_clear", "incident_name": "Plain WFIGS Fire",
        "acres": 1500, "contained_pct": 100,
    }
    wire = fire_format(_FakeEvent(d, category="wildfire_closed"), now=time.time(), budget=140)
    assert "Plain WFIGS Fire" in wire
    assert "watchduty.org" not in wire


def test_long_name_and_long_anchor_keeps_link_whole():
    conn = get_db()
    long_name = "The Extremely Long Watch Duty Incident Name For This Wildfire " * 2
    _insert_matched_fire(conn, "irwin_long", wd_name=long_name.strip())
    d = {
        "irwin_id": "irwin_long", "incident_name": "short",
        "acres": 999999, "contained_pct": 3,
        "geocoder_city": "A Very Long Descriptive Nearby Location Name, Idaho",
        "is_update": False,
    }
    wire = fire_format(_FakeEvent(d, category="wildfire_declared"), now=time.time(), budget=140)
    link = incident_url("wd123")
    assert wire.endswith(link)
    assert wire.splitlines()[-1] == link, "link line must never be split/truncated"


# ---------- env/fire_render.py: FIRMS wildfire_growth path -------------------


def test_growth_path_matched_fire_has_wd_name_and_link():
    conn = get_db()
    _insert_matched_fire(conn, "irwin_growth")
    n = {
        "irwin_id": "irwin_growth", "incident_name": "WFIGS Fallback Name",
        "acres": 2000, "contained_pct": 20, "geocoder_city": "Near Boise, ID",
        "lat": _LAT0, "lon": _LON0,
    }
    movement = {"direction": "NE", "speed_mph": 2.5}
    wire = _wfigs_render(n, prefix="Update", movement=movement)
    assert "Watch Duty Fire Name" in wire
    assert wire.splitlines()[-1] == incident_url("wd123")


def test_growth_path_unmatched_fire_unchanged():
    n = {
        "irwin_id": "irwin_growth_unmatched", "incident_name": "Growth Fire Plain",
        "acres": 2000, "contained_pct": 20, "geocoder_city": "Near Boise, ID",
        "lat": _LAT0, "lon": _LON0,
    }
    wire = _wfigs_render(n, prefix="Update")
    assert "Growth Fire Plain" in wire
    assert "watchduty.org" not in wire
    assert "Cause:" not in wire and "Discovered" not in wire


def test_growth_path_matches_formatters_fire_byte_identical_when_matched():
    """env/fire_render._render must stay byte-identical to
    formatters/fire.py::format for the same matched-fire inputs
    (tests/test_fire_refactor.py asserts this for the unmatched case;
    this asserts the matched case too)."""
    conn = get_db()
    _insert_matched_fire(conn, "irwin_parity")
    shared = {
        "irwin_id": "irwin_parity", "incident_name": "WFIGS Fallback Name",
        "acres": 3000, "contained_pct": 30, "geocoder_city": "Near Boise, ID",
    }
    wire_a = _wfigs_render(dict(shared), prefix="Update")
    d = dict(shared)
    d["is_update"] = True
    wire_b = fire_format(_FakeEvent(d, category="wildfire_incident"), now=time.time(), budget=140)
    assert wire_a == wire_b


# ---------- notifications/formatters/firms.py: spotting / halted ------------


def test_firms_halted_matched_has_wd_name_and_link():
    conn = get_db()
    _insert_matched_fire(conn, "irwin_halt")
    d = {"irwin_id": "irwin_halt", "incident_name": "Plain Halt Fire", "hours": 18}
    wire = firms_fmt.format(_FakeEvent(d, category="wildfire_halted"), now=time.time(), budget=140)
    assert "Watch Duty Fire Name" in wire
    assert "Plain Halt Fire" not in wire
    assert wire.splitlines()[-1] == incident_url("wd123")


def test_firms_halted_unmatched_unchanged():
    d = {"irwin_id": "irwin_halt_unmatched", "incident_name": "Plain Halt Fire", "hours": 18}
    wire = firms_fmt.format(_FakeEvent(d, category="wildfire_halted"), now=time.time(), budget=140)
    assert wire == "🔥 Plain Halt Fire no growth in 18h"
    assert "watchduty.org" not in wire


def test_firms_spotting_matched_has_wd_name_and_link():
    conn = get_db()
    _insert_matched_fire(conn, "irwin_spot")
    d = {"irwin_id": "irwin_spot", "incident_name": "Plain Spot Fire",
         "dist_mi": 1.5, "direction": "NE"}
    wire = firms_fmt.format(_FakeEvent(d, category="wildfire_spotting"), now=time.time(), budget=140)
    assert "Watch Duty Fire Name" in wire
    assert "Plain Spot Fire" not in wire
    assert wire.splitlines()[-1] == incident_url("wd123")


def test_firms_spotting_unmatched_unchanged():
    d = {"irwin_id": "irwin_spot_unmatched", "incident_name": "Plain Spot Fire",
         "dist_mi": 1.5, "direction": "NE"}
    wire = firms_fmt.format(_FakeEvent(d, category="wildfire_spotting"), now=time.time(), budget=140)
    assert wire == "🔥 Possible spotting 1.5 mi NE of Plain Spot Fire perimeter"
    assert "watchduty.org" not in wire
