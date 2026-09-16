"""Tests for env/watchduty.py's WatchDutyAdapter: match_and_store,
tick()'s interval/backoff/HTTP gating, and watchduty_info's failure modes.

No real network calls -- every HTTP path patches
``meshai.env.watchduty.urlopen`` (where the module imports it from).
"""
from __future__ import annotations

import json
import time

import pytest

from meshai.config import WatchDutyConfig
from meshai.env.watchduty import WatchDutyAdapter
from meshai.notifications.formatters.fire import watchduty_info, format as fire_format
from meshai.persistence import get_db

_LAT0, _LON0 = 44.0, -114.0


class _FakeResponse:
    def __init__(self, data: bytes):
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_urlopen(payload_obj, calls: list):
    body = json.dumps(payload_obj).encode("utf-8")

    def _urlopen(req, timeout=30):
        calls.append(req)
        return _FakeResponse(body)

    return _urlopen


def _insert_fire(conn, irwin_id, *, lat=_LAT0, lon=_LON0,
                  last_broadcast_at=None, tombstoned_at=None,
                  watchduty_event_id=None, watchduty_name=None,
                  watchduty_is_active=None):
    now = int(time.time())
    conn.execute(
        "INSERT INTO fires(irwin_id, incident_name, current_acres, "
        "current_contained_pct, lat, lon, county, state, declared_at, "
        "last_event_at, last_broadcast_at, tombstoned_at, watchduty_event_id, "
        "watchduty_name, watchduty_is_active) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (irwin_id, f"Fire {irwin_id}", 100.0, 0, lat, lon, "Boise", "ID",
         now, now, last_broadcast_at, tombstoned_at, watchduty_event_id,
         watchduty_name, watchduty_is_active),
    )


@pytest.fixture
def adapter():
    return WatchDutyAdapter(WatchDutyConfig(enabled=True))


# ---------- match_and_store --------------------------------------------------


def test_match_and_store_writes_and_refreshes(adapter, monkeypatch):
    conn = get_db()
    now = time.time()
    _insert_fire(conn, "irwin_new", last_broadcast_at=now - 10)
    _insert_fire(conn, "irwin_old", last_broadcast_at=now - 10,
                 watchduty_event_id="wd_old", watchduty_name="Old Name",
                 watchduty_is_active=1)

    wd_events = [
        {"id": "wd_new", "name": "New Fire WD", "is_active": True,
         "lat": _LAT0, "lng": _LON0, "data": {"is_prescribed": False}},
        {"id": "wd_old", "name": "Refreshed Name", "is_active": False,
         "lat": _LAT0, "lng": _LON0, "data": {"is_prescribed": False}},
    ]
    monkeypatch.setattr(adapter, "fetch_geo_events", lambda: wd_events)

    new_count = adapter.match_and_store()
    assert new_count == 1

    row_new = conn.execute(
        "SELECT watchduty_event_id, watchduty_name, watchduty_matched_at, "
        "watchduty_is_active FROM fires WHERE irwin_id=?", ("irwin_new",)
    ).fetchone()
    assert row_new["watchduty_event_id"] == "wd_new"
    assert row_new["watchduty_name"] == "New Fire WD"
    assert row_new["watchduty_matched_at"] is not None
    assert row_new["watchduty_is_active"] == 1

    row_old = conn.execute(
        "SELECT watchduty_event_id, watchduty_name, watchduty_is_active "
        "FROM fires WHERE irwin_id=?", ("irwin_old",)
    ).fetchone()
    assert row_old["watchduty_event_id"] == "wd_old"   # unchanged
    assert row_old["watchduty_name"] == "Refreshed Name"  # refreshed
    assert row_old["watchduty_is_active"] == 0            # refreshed


def test_match_and_store_scoped_to_single_irwin(adapter, monkeypatch):
    conn = get_db()
    now = time.time()
    _insert_fire(conn, "irwin_a", last_broadcast_at=now - 10)
    _insert_fire(conn, "irwin_b", last_broadcast_at=now - 10)
    wd_events = [
        {"id": "wd_a", "name": "Fire A WD", "is_active": True,
         "lat": _LAT0, "lng": _LON0, "data": {"is_prescribed": False}},
    ]
    monkeypatch.setattr(adapter, "fetch_geo_events", lambda: wd_events)
    new_count = adapter.match_and_store(irwin_id="irwin_a")
    assert new_count == 1
    assert conn.execute(
        "SELECT watchduty_event_id FROM fires WHERE irwin_id='irwin_b'"
    ).fetchone()["watchduty_event_id"] is None


def test_match_and_store_marks_absent_matched_fire_inactive(adapter, monkeypatch):
    """WD's response is always the full current list (never filtered by
    id) -- a matched fire's id missing from it means WD no longer lists
    the incident as active, so watchduty_is_active must flip to 0."""
    conn = get_db()
    now = time.time()
    _insert_fire(conn, "irwin_vanished", last_broadcast_at=now - 10,
                 watchduty_event_id="wd_vanished", watchduty_name="Vanished Fire",
                 watchduty_is_active=1)
    # The response no longer contains "wd_vanished" at all.
    monkeypatch.setattr(adapter, "fetch_geo_events", lambda: [])

    new_count = adapter.match_and_store()
    assert new_count == 0
    row = conn.execute(
        "SELECT watchduty_event_id, watchduty_name, watchduty_is_active "
        "FROM fires WHERE irwin_id='irwin_vanished'"
    ).fetchone()
    assert row["watchduty_event_id"] == "wd_vanished"   # match itself untouched
    assert row["watchduty_name"] == "Vanished Fire"      # name untouched
    assert row["watchduty_is_active"] == 0               # flipped inactive


def test_match_and_store_marks_absent_matched_fire_inactive_irwin_id_mode(adapter, monkeypatch):
    conn = get_db()
    now = time.time()
    _insert_fire(conn, "irwin_vanished2", last_broadcast_at=now - 10,
                 watchduty_event_id="wd_vanished2", watchduty_is_active=1)
    monkeypatch.setattr(adapter, "fetch_geo_events", lambda: [])

    adapter.match_and_store(irwin_id="irwin_vanished2")
    row = conn.execute(
        "SELECT watchduty_is_active FROM fires WHERE irwin_id='irwin_vanished2'"
    ).fetchone()
    assert row["watchduty_is_active"] == 0


def test_should_poll_false_after_matched_fire_goes_inactive(adapter, monkeypatch):
    conn = get_db()
    now = time.time()
    _insert_fire(conn, "irwin_vanished3", last_broadcast_at=now - 10,
                 watchduty_event_id="wd_vanished3", watchduty_is_active=1)
    assert adapter._should_poll() is True  # matched + active -> poll-worthy

    monkeypatch.setattr(adapter, "fetch_geo_events", lambda: [])
    adapter.match_and_store()

    assert adapter._should_poll() is False


# ---------- tick(): interval / HTTP gating ------------------------------------


def test_tick_disabled_makes_zero_http_calls():
    a = WatchDutyAdapter(WatchDutyConfig(enabled=False))
    calls = []

    def _urlopen(req, timeout=30):
        calls.append(req)
        raise AssertionError("must not be called when disabled")

    import meshai.env.watchduty as wd_mod
    orig = wd_mod.urlopen
    wd_mod.urlopen = _urlopen
    try:
        result = a.tick()
    finally:
        wd_mod.urlopen = orig
    assert result is False
    assert calls == []


def test_tick_enabled_no_candidates_makes_zero_http_calls(adapter):
    calls = []
    import meshai.env.watchduty as wd_mod
    orig = wd_mod.urlopen
    wd_mod.urlopen = _fake_urlopen([], calls)
    try:
        result = adapter.tick()
    finally:
        wd_mod.urlopen = orig
    assert result is False
    assert calls == []


def test_tick_enabled_with_candidate_makes_exactly_one_http_call(adapter):
    conn = get_db()
    now = time.time()
    _insert_fire(conn, "irwin_cand", last_broadcast_at=now - 10)

    calls = []
    import meshai.env.watchduty as wd_mod
    orig = wd_mod.urlopen
    wd_mod.urlopen = _fake_urlopen([], calls)
    try:
        result = adapter.tick()
    finally:
        wd_mod.urlopen = orig
    assert result is True
    assert len(calls) == 1


def test_tick_interval_gate_blocks_second_call_within_window(adapter):
    conn = get_db()
    now = time.time()
    _insert_fire(conn, "irwin_cand2", last_broadcast_at=now - 10)

    calls = []
    import meshai.env.watchduty as wd_mod
    orig = wd_mod.urlopen
    wd_mod.urlopen = _fake_urlopen([], calls)
    try:
        adapter.tick()
        adapter.tick()  # immediately again -- still inside tick_seconds
    finally:
        wd_mod.urlopen = orig
    assert len(calls) == 1


# ---------- tick(): error / backoff -------------------------------------------


def test_tick_error_backs_off_and_records_health(adapter):
    conn = get_db()
    now = time.time()
    _insert_fire(conn, "irwin_err", last_broadcast_at=now - 10)

    def _bad_urlopen(req, timeout=30):
        return _FakeResponse(b"<html>not json, watchduty is down</html>")

    import meshai.env.watchduty as wd_mod
    orig = wd_mod.urlopen
    wd_mod.urlopen = _bad_urlopen
    try:
        result = adapter.tick()
    finally:
        wd_mod.urlopen = orig

    assert result is False
    assert adapter._consecutive_errors == 1
    assert adapter._last_error is not None
    first_backoff = adapter._backoff_seconds
    assert first_backoff > 0
    assert adapter._backoff_until > time.time()

    hs = adapter.health_status
    assert hs["consecutive_errors"] == 1
    assert hs["last_error"] is not None

    # Simulate enough wall-clock time passing for the interval + backoff
    # gates to clear, and force a second consecutive failure.
    adapter._backoff_until = 0.0
    adapter._last_tick = 0.0
    wd_mod.urlopen = _bad_urlopen
    try:
        result2 = adapter.tick()
    finally:
        wd_mod.urlopen = orig

    assert result2 is False
    assert adapter._consecutive_errors == 2
    assert adapter._backoff_seconds == first_backoff * 2


def test_tick_success_resets_backoff_after_error(adapter):
    conn = get_db()
    now = time.time()
    _insert_fire(conn, "irwin_recov", last_broadcast_at=now - 10)

    import meshai.env.watchduty as wd_mod
    orig = wd_mod.urlopen
    wd_mod.urlopen = lambda req, timeout=30: _FakeResponse(b"not json")
    try:
        adapter.tick()
    finally:
        wd_mod.urlopen = orig
    assert adapter._consecutive_errors == 1

    adapter._backoff_until = 0.0
    adapter._last_tick = 0.0
    wd_mod.urlopen = _fake_urlopen([], [])
    try:
        result = adapter.tick()
    finally:
        wd_mod.urlopen = orig

    assert result is True
    assert adapter._consecutive_errors == 0
    assert adapter._last_error is None
    assert adapter._backoff_seconds == 0.0


# ---------- watchduty_info failure modes --------------------------------------


def test_watchduty_info_none_irwin_id():
    assert watchduty_info(None) is None
    assert watchduty_info("") is None


def test_watchduty_info_unknown_irwin_id():
    assert watchduty_info("does-not-exist") is None


def test_watchduty_info_unmatched_fire_returns_none():
    conn = get_db()
    _insert_fire(conn, "irwin_unmatched")
    assert watchduty_info("irwin_unmatched") is None


def test_watchduty_info_matched_fire_returns_id_and_name():
    conn = get_db()
    _insert_fire(conn, "irwin_matched", watchduty_event_id="wd9",
                 watchduty_name="Matched Fire WD")
    info = watchduty_info("irwin_matched")
    assert info == {"id": "wd9", "name": "Matched Fire WD"}


def test_watchduty_info_db_error_returns_none_and_rendering_unaffected(monkeypatch):
    def _raise_get_db(*a, **kw):
        raise RuntimeError("no such column: watchduty_event_id")

    import meshai.persistence as persistence_mod
    monkeypatch.setattr(persistence_mod, "get_db", _raise_get_db)

    assert watchduty_info("anything") is None

    class _FakeEvent:
        def __init__(self, data):
            self.data = data
            self.category = "wildfire_incident"

    d = {"irwin_id": "anything", "incident_name": "Some Fire", "acres": 10,
         "contained_pct": 5, "lat": _LAT0, "lon": _LON0}
    wire = fire_format(_FakeEvent(d), now=time.time(), budget=140)
    assert "Some Fire" in wire
    assert "app.watchduty.org" not in wire
