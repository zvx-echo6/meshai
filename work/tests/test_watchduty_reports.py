"""Group C: Watch Duty report-message alerts.

Covers the report helpers (is_automated_report, is_filtered_report,
first_sentences) and fetch_reports() in env/watchduty.py, the lazy
sentinel-based seeding + per-poll picking logic (_poll_reports and friends),
the gating decider (notifications/gating/watchduty.py::decide_report), and
the formatter (notifications/formatters/watchduty.py::format_report).

No real network calls anywhere -- every HTTP path patches
``meshai.env.watchduty.urlopen`` (where the module imports it from), the
same convention tests/test_watchduty_evac.py and tests/test_watchduty_adapter.py
use.
"""
from __future__ import annotations

import json
import re
import time

import pytest
from urllib.error import URLError

from meshai.config import EnvironmentalConfig, WatchDutyConfig
from meshai.env.store import EnvironmentalStore
from meshai.env.watchduty import (
    WatchDutyAdapter,
    first_sentences,
    incident_url,
    is_automated_report,
    is_filtered_report,
    strip_html,
)
from meshai.notifications.categories import ALERT_CATEGORIES
from meshai.notifications.cutover import NATIVE_ALWAYS_DECIDE
from meshai.notifications.formatters import FORMATTERS
from meshai.notifications.formatters.watchduty import format_report
from meshai.notifications.gating import DECIDERS
from meshai.notifications.gating.watchduty import decide_report
from meshai.notifications.pipeline.bus import EventBus
from meshai.persistence import get_db

_NOW = 1_800_000_000.0

_DEFAULT_PATTERNS = [
    "national interagency fire center",
    r"\bnifc\b",
    "perimeter (?:has been |was )?uploaded",
]


class _FakeResponse:
    def __init__(self, data: bytes):
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _wd_urlopen(*, geo_events=None, reports=None, calls, raise_for_reports=frozenset()):
    """Fake urlopen dispatching by endpoint (geo_events vs reports), keyed
    by geo_event_id for the reports endpoint. ``reports`` maps
    wd_event_id -> response payload (list, or dict with "results") and is
    read FRESH on every call, so a test can mutate it between ticks to
    simulate a new report arriving. ``raise_for_reports`` is a set of
    wd_event_ids whose reports fetch raises a URLError (network failure).
    """
    reports = reports if reports is not None else {}

    def _urlopen(req, timeout=30):
        calls.append(req)
        url = req.full_url
        if "/reports/" in url:
            m = re.search(r"geo_event_id=([^&]+)", url)
            wd_id = m.group(1) if m else None
            if wd_id in raise_for_reports:
                raise URLError("simulated report fetch failure")
            payload = reports.get(wd_id, [])
            return _FakeResponse(json.dumps(payload).encode("utf-8"))
        return _FakeResponse(json.dumps(geo_events or []).encode("utf-8"))

    return _urlopen


def _wd_geo_event(event_id, *, name="Test Fire WD", lat=44.0, lng=-114.0,
                   is_active=True):
    return {
        "id": event_id,
        "name": name,
        "is_active": is_active,
        "lat": lat,
        "lng": lng,
        "date_modified": None,
        "data": {
            "is_prescribed": False,
            "evacuation_orders": None,
            "evacuation_warnings": None,
            "evacuation_advisories": None,
            "evacuation_shelter_in_place": None,
            "has_custom_evacuation_orders": False,
            "has_custom_evacuation_warnings": False,
            "has_custom_evacuation_advisories": False,
            "has_custom_evacuation_shelter_in_place": False,
        },
    }


def _report(rid, message, date_created, *, geo_event_id="wd1",
            display_name="Human Reporter", username="humanuser",
            is_default_reporter=False, is_staff=False, is_verified=True,
            user_created="__default__"):
    if user_created == "__default__":
        user_created = {
            "id": "u1", "username": username, "is_staff": is_staff,
            "display_name": display_name, "is_verified": is_verified,
            "headline": "", "is_default_reporter": is_default_reporter,
        }
    return {
        "id": rid,
        "geo_event_id": geo_event_id,
        "status": "approved",
        "message": message,
        "date_created": date_created,
        "date_modified": date_created,
        "user_created": user_created,
        "notification_type": "report",
    }


def _insert_fire(conn, irwin_id, *, lat=44.0, lon=-114.0,
                  watchduty_event_id=None, watchduty_name=None,
                  watchduty_is_active=None, tombstoned_at=None,
                  last_broadcast_at=None):
    now = int(time.time())
    conn.execute(
        "INSERT INTO fires(irwin_id, incident_name, lat, lon, county, state, "
        "last_event_at, last_broadcast_at, tombstoned_at, watchduty_event_id, "
        "watchduty_name, watchduty_is_active) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (irwin_id, f"Fire {irwin_id}", lat, lon, "Boise", "ID", now,
         last_broadcast_at, tombstoned_at, watchduty_event_id,
         watchduty_name, watchduty_is_active),
    )


def _insert_matched_fire(conn, irwin_id, wd_event_id, **kwargs):
    kwargs.setdefault("watchduty_is_active", 1)
    kwargs.setdefault("watchduty_name", f"WD {irwin_id}")
    _insert_fire(conn, irwin_id, watchduty_event_id=wd_event_id, **kwargs)


# =============================================================================
# 1. is_automated_report
# =============================================================================

_LIVE_NIFC_TEXT = "The fire is now 9555 acres, per the National Interagency Fire Center (NIFC)."
_LIVE_NIFC_HTML = f"<p>{_LIVE_NIFC_TEXT}</p>"
_LIVE_CREW_UPDATE = (
    "09/15/26 Evening Update: Crews have mopped up deep into the timber "
    "stringer on the north ridge and starting from the west..."
)
_WILDCAD_NEW_FIRE = "New fire reported by WildCAD at 9:15 PM Tue Sep 15th (MDT)."
_WILDCAD_ACREAGE = "Acreage updated from (not provided) to 0.1 acres."


def test_is_automated_report_default_reporter_flag():
    r = _report("r1", "Some text.", "2026-09-15T21:15:00Z",
                display_name="Jeff Basham", is_default_reporter=True)
    assert is_automated_report(r) is True


def test_is_automated_report_wildcad_display_name():
    r = _report("r2", _WILDCAD_ACREAGE, "2026-09-15T21:20:00Z",
                display_name="WildCAD", username="wildcad_bot")
    assert is_automated_report(r) is True


def test_is_automated_report_wildcad_username():
    r = _report("r3", _WILDCAD_ACREAGE, "2026-09-15T21:20:00Z",
                display_name="Dispatch Feed", username="WildCAD")
    assert is_automated_report(r) is True


def test_is_automated_report_wildcad_text_with_human_name():
    r = _report("r4", _WILDCAD_NEW_FIRE, "2026-09-15T21:15:00Z",
                display_name="Jeff Basham", username="jbasham")
    assert is_automated_report(r) is True


def test_is_automated_report_human_is_false():
    r = _report("r5", _LIVE_CREW_UPDATE, "2026-09-15T22:00:00Z",
                display_name="Baylee Wright", username="bwright")
    assert is_automated_report(r) is False


def test_is_automated_report_missing_user_created():
    r = _report("r6", "Just a plain human-sounding update.",
                "2026-09-15T22:00:00Z", user_created=None)
    assert is_automated_report(r) is False


def test_is_automated_report_no_user_created_key():
    r = {"id": "r7", "message": "Plain update, no user_created key at all."}
    assert is_automated_report(r) is False


# =============================================================================
# 2. is_filtered_report
# =============================================================================

def test_is_filtered_report_nifc_message():
    text = strip_html(_LIVE_NIFC_HTML)
    assert is_filtered_report(text, _DEFAULT_PATTERNS) is True


def test_is_filtered_report_perimeter_uploaded():
    assert is_filtered_report("A new perimeter has been uploaded for this fire.",
                              _DEFAULT_PATTERNS) is True


def test_is_filtered_report_crew_update_not_filtered():
    assert is_filtered_report(_LIVE_CREW_UPDATE, _DEFAULT_PATTERNS) is False


def test_is_filtered_report_invalid_pattern_skipped_not_raised():
    patterns = ["[unclosed(", "national interagency fire center"]
    text = strip_html(_LIVE_NIFC_HTML)
    # Must not raise despite the invalid regex, and must still match on the
    # valid pattern.
    assert is_filtered_report(text, patterns) is True


# =============================================================================
# 3. first_sentences
# =============================================================================

def test_first_sentences_normal_case():
    text = "First sentence here. Second sentence here. Third sentence here."
    assert first_sentences(text, 2) == "First sentence here. Second sentence here."


def test_first_sentences_no_punctuation_falls_back_to_whole_text():
    text = "just some text with no terminal punctuation at all"
    assert first_sentences(text, 2) == text


def test_first_sentences_abbreviations_do_not_crash():
    text = "Crews responded near U.S. Forest Service land. Containment improved overnight."
    # Must not raise; some reasonable non-empty string comes back.
    out = first_sentences(text, 2)
    assert isinstance(out, str)
    assert out


# =============================================================================
# 4. fetch_reports
# =============================================================================

def test_fetch_reports_dict_with_results():
    adapter = WatchDutyAdapter(WatchDutyConfig(enabled=True))
    r1 = _report("r1", "Human update.", "2026-09-15T21:15:00Z")
    calls = []
    import meshai.env.watchduty as wd_mod
    orig = wd_mod.urlopen
    wd_mod.urlopen = _wd_urlopen(
        reports={"wd1": {"count": 1, "results": [r1], "next": None, "previous": None}},
        calls=calls)
    try:
        out = adapter.fetch_reports("wd1", 20)
    finally:
        wd_mod.urlopen = orig
    assert out == [r1]
    assert len(calls) == 1


def test_fetch_reports_bare_list():
    adapter = WatchDutyAdapter(WatchDutyConfig(enabled=True))
    r1 = _report("r1", "Human update.", "2026-09-15T21:15:00Z")
    calls = []
    import meshai.env.watchduty as wd_mod
    orig = wd_mod.urlopen
    wd_mod.urlopen = _wd_urlopen(reports={"wd1": [r1]}, calls=calls)
    try:
        out = adapter.fetch_reports("wd1", 20)
    finally:
        wd_mod.urlopen = orig
    assert out == [r1]


def test_fetch_reports_garbage_html_raises():
    adapter = WatchDutyAdapter(WatchDutyConfig(enabled=True))

    def _bad_urlopen(req, timeout=30):
        return _FakeResponse(b"<html>not json, watchduty is down</html>")

    import meshai.env.watchduty as wd_mod
    orig = wd_mod.urlopen
    wd_mod.urlopen = _bad_urlopen
    try:
        with pytest.raises(Exception):
            adapter.fetch_reports("wd1", 20)
    finally:
        wd_mod.urlopen = orig


def test_fetch_reports_dict_without_results_raises():
    adapter = WatchDutyAdapter(WatchDutyConfig(enabled=True))
    calls = []
    import meshai.env.watchduty as wd_mod
    orig = wd_mod.urlopen
    wd_mod.urlopen = _wd_urlopen(reports={"wd1": {"detail": "nope"}}, calls=calls)
    try:
        with pytest.raises(ValueError):
            adapter.fetch_reports("wd1", 20)
    finally:
        wd_mod.urlopen = orig


# =============================================================================
# 5. Seeding
# =============================================================================

def test_first_poll_seeds_all_ids_and_sentinel_no_readings():
    conn = get_db()
    _insert_matched_fire(conn, "i1", "wd1")
    adapter = WatchDutyAdapter(WatchDutyConfig(enabled=True))
    r1 = _report("r1", "Old backlog one.", "2026-09-10T10:00:00Z")
    r2 = _report("r2", "Old backlog two.", "2026-09-10T11:00:00Z")
    calls = []
    import meshai.env.watchduty as wd_mod
    orig = wd_mod.urlopen
    wd_mod.urlopen = _wd_urlopen(reports={"wd1": [r1, r2]}, calls=calls)
    try:
        adapter._poll_reports()
    finally:
        wd_mod.urlopen = orig

    assert adapter.get_events() == []

    rows = {r["report_id"]: r for r in conn.execute(
        "SELECT report_id, seeded, sent_at FROM watchduty_reports_sent "
        "WHERE irwin_id='i1'").fetchall()}
    assert set(rows) == {"r1", "r2", "seed:i1"}
    for rid, row in rows.items():
        assert row["seeded"] == 1
        assert row["sent_at"] is None


def test_seeding_fetch_error_leaves_no_sentinel_next_poll_seeds():
    conn = get_db()
    _insert_matched_fire(conn, "i1", "wd1")
    adapter = WatchDutyAdapter(WatchDutyConfig(enabled=True))
    calls = []
    import meshai.env.watchduty as wd_mod
    orig = wd_mod.urlopen
    wd_mod.urlopen = _wd_urlopen(reports={}, calls=calls, raise_for_reports={"wd1"})
    try:
        adapter._poll_reports()  # logs a warning, swallows the error
    finally:
        wd_mod.urlopen = orig

    assert adapter.get_events() == []
    row = conn.execute(
        "SELECT 1 FROM watchduty_reports_sent WHERE report_id='seed:i1'").fetchone()
    assert row is None, "a fetch failure must not leave a sentinel behind"

    # Next poll succeeds -> seeds normally.
    r1 = _report("r1", "Old backlog one.", "2026-09-10T10:00:00Z")
    wd_mod.urlopen = _wd_urlopen(reports={"wd1": [r1]}, calls=calls)
    try:
        adapter._poll_reports()
    finally:
        wd_mod.urlopen = orig
    assert adapter.get_events() == []
    row = conn.execute(
        "SELECT 1 FROM watchduty_reports_sent WHERE report_id='seed:i1'").fetchone()
    assert row is not None


# =============================================================================
# 6. One new report -> one reading; persists until commit.
# =============================================================================

def test_new_report_reading_persists_until_commit():
    conn = get_db()
    _insert_matched_fire(conn, "i1", "wd1")
    adapter = WatchDutyAdapter(WatchDutyConfig(enabled=True))
    calls = []
    import meshai.env.watchduty as wd_mod
    orig = wd_mod.urlopen

    # Seed poll: empty backlog.
    wd_mod.urlopen = _wd_urlopen(reports={"wd1": []}, calls=calls)
    try:
        adapter._poll_reports()
    finally:
        wd_mod.urlopen = orig
    assert adapter.get_events() == []

    new_report = _report("new1", "<p>Fresh human update on the fire.</p>",
                         "2026-09-15T21:15:00Z", display_name="Baylee Wright",
                         username="bwright")
    wd_mod.urlopen = _wd_urlopen(reports={"wd1": [new_report]}, calls=calls)
    try:
        adapter._poll_reports()
        first_readings = adapter.get_events()
        assert len(first_readings) == 1
        assert first_readings[0]["report_id"] == "new1"
        assert first_readings[0]["type"] == "report"

        # Poll again BEFORE any commit -- report_id still absent from the
        # table, so it must still qualify and yield the same reading.
        adapter._poll_reports()
        second_readings = adapter.get_events()
        assert len(second_readings) == 1
        assert second_readings[0]["report_id"] == "new1"

        # Simulate the decider's deferred commit landing (delivery
        # confirmed).
        conn.execute(
            "INSERT OR IGNORE INTO watchduty_reports_sent"
            "(report_id, irwin_id, geo_event_id, sent_at, seeded, created_at) "
            "VALUES (?,?,?,?,0,?)",
            ("new1", "i1", "wd1", time.time(), time.time()),
        )

        adapter._poll_reports()
        third_readings = adapter.get_events()
        assert third_readings == []
    finally:
        wd_mod.urlopen = orig


# =============================================================================
# 7. Three new reports -> exactly one reading (the newest human one).
# =============================================================================

def test_three_new_reports_yields_only_newest_human_reading():
    conn = get_db()
    _insert_matched_fire(conn, "i1", "wd1")
    adapter = WatchDutyAdapter(WatchDutyConfig(enabled=True))
    calls = []
    import meshai.env.watchduty as wd_mod
    orig = wd_mod.urlopen

    # Seed poll: empty backlog.
    wd_mod.urlopen = _wd_urlopen(reports={"wd1": []}, calls=calls)
    try:
        adapter._poll_reports()
    finally:
        wd_mod.urlopen = orig
    assert adapter.get_events() == []

    wildcad = _report("wc1", _WILDCAD_NEW_FIRE, "2026-09-15T20:00:00Z",
                      display_name="WildCAD", username="wildcad")
    nifc = _report("nifc1", _LIVE_NIFC_HTML, "2026-09-15T20:30:00Z",
                   display_name="Jeff Basham", username="jbasham")
    human_older = _report("h1", "<p>Earlier human update.</p>",
                          "2026-09-15T20:45:00Z", display_name="Baylee Wright",
                          username="bwright")
    human_newest = _report("h2", "<p>Latest human update from the line.</p>",
                           "2026-09-15T21:15:00Z", display_name="Baylee Wright",
                           username="bwright")

    wd_mod.urlopen = _wd_urlopen(
        reports={"wd1": [wildcad, nifc, human_older, human_newest]}, calls=calls)
    try:
        adapter._poll_reports()
    finally:
        wd_mod.urlopen = orig

    readings = adapter.get_events()
    assert len(readings) == 1
    assert readings[0]["report_id"] == "h2"

    rows = {r["report_id"]: r["seeded"] for r in conn.execute(
        "SELECT report_id, seeded FROM watchduty_reports_sent "
        "WHERE irwin_id='i1'").fetchall()}
    # wildcad + nifc + the older human report are all marked seeded=1
    # (deliberately skipped); the newest human report is NOT in the table
    # yet -- the decider's deferred commit does that.
    assert rows.get("wc1") == 1
    assert rows.get("nifc1") == 1
    assert rows.get("h1") == 1
    assert "h2" not in rows


# =============================================================================
# 8. One fire's fetch error doesn't stop the others; tick returns True.
# =============================================================================

def test_one_fire_report_error_does_not_stop_others_or_tick():
    conn = get_db()
    _insert_matched_fire(conn, "i_bad", "wd_bad")
    _insert_matched_fire(conn, "i_good", "wd_good")
    adapter = WatchDutyAdapter(WatchDutyConfig(enabled=True))

    geo_events = [_wd_geo_event("wd_bad"), _wd_geo_event("wd_good")]
    r_good = _report("g1", "<p>Good fire human update.</p>",
                     "2026-09-15T21:15:00Z", display_name="Baylee Wright",
                     username="bwright", geo_event_id="wd_good")
    calls = []
    import meshai.env.watchduty as wd_mod
    orig = wd_mod.urlopen
    wd_mod.urlopen = _wd_urlopen(
        geo_events=geo_events,
        reports={"wd_good": [r_good]},
        calls=calls,
        raise_for_reports={"wd_bad"},
    )
    try:
        result = adapter.tick()
    finally:
        wd_mod.urlopen = orig

    assert result is True
    assert adapter._consecutive_errors == 0
    assert adapter._backoff_seconds == 0.0
    assert adapter._backoff_until == 0.0

    # wd_bad's seed never happened (fetch raised); wd_good got a fresh seed.
    bad_sentinel = conn.execute(
        "SELECT 1 FROM watchduty_reports_sent WHERE report_id='seed:i_bad'").fetchone()
    assert bad_sentinel is None
    good_sentinel = conn.execute(
        "SELECT 1 FROM watchduty_reports_sent WHERE report_id='seed:i_good'").fetchone()
    assert good_sentinel is not None
    # wd_good's first poll is a seed -- no REPORT readings yet (match_and_store
    # still rebuilds the unrelated evac snapshot for both fires; that's Group
    # B's own concern, not this test's).
    report_readings = [r for r in adapter.get_events() if r.get("type") == "report"]
    assert report_readings == []


# =============================================================================
# 9 & 10. Config gating: disabled -> zero report requests; ineligible fires
#         (unmatched / inactive / tombstoned) -> zero report requests.
# =============================================================================

def test_report_alerts_disabled_makes_zero_report_requests():
    from meshai.adapter_config._accessor import set_runtime_override, _overrides
    conn = get_db()
    _insert_matched_fire(conn, "i1", "wd1")
    adapter = WatchDutyAdapter(WatchDutyConfig(enabled=True))
    geo_events = [_wd_geo_event("wd1")]
    calls = []
    import meshai.env.watchduty as wd_mod
    orig = wd_mod.urlopen
    wd_mod.urlopen = _wd_urlopen(geo_events=geo_events, calls=calls)

    set_runtime_override("watchduty", "report_alerts_enabled", False)
    try:
        result = adapter.tick()
    finally:
        wd_mod.urlopen = orig
        _overrides.pop(("watchduty", "report_alerts_enabled"), None)

    assert result is True
    assert len(calls) == 1, "only the geo_events list fetch, no report fetches"


def test_unmatched_fire_no_report_requests():
    conn = get_db()
    _insert_fire(conn, "i1")  # watchduty_event_id is NULL -- never matched
    adapter = WatchDutyAdapter(WatchDutyConfig(enabled=True))
    calls = []
    import meshai.env.watchduty as wd_mod
    orig = wd_mod.urlopen
    wd_mod.urlopen = _wd_urlopen(calls=calls)
    try:
        adapter._poll_reports()
    finally:
        wd_mod.urlopen = orig
    assert calls == []


def test_inactive_fire_no_report_requests():
    conn = get_db()
    _insert_matched_fire(conn, "i1", "wd1", watchduty_is_active=0)
    adapter = WatchDutyAdapter(WatchDutyConfig(enabled=True))
    calls = []
    import meshai.env.watchduty as wd_mod
    orig = wd_mod.urlopen
    wd_mod.urlopen = _wd_urlopen(calls=calls)
    try:
        adapter._poll_reports()
    finally:
        wd_mod.urlopen = orig
    assert calls == []


def test_tombstoned_fire_no_report_requests():
    conn = get_db()
    _insert_matched_fire(conn, "i1", "wd1", tombstoned_at=_NOW - 10)
    adapter = WatchDutyAdapter(WatchDutyConfig(enabled=True))
    calls = []
    import meshai.env.watchduty as wd_mod
    orig = wd_mod.urlopen
    wd_mod.urlopen = _wd_urlopen(calls=calls)
    try:
        adapter._poll_reports()
    finally:
        wd_mod.urlopen = orig
    assert calls == []


# =============================================================================
# 11. decide_report
# =============================================================================

def _report_data(irwin_id="i1", report_id="rep1", wd_event_id="wd1", text="Update text."):
    return {"irwin_id": irwin_id, "wd_event_id": wd_event_id, "name": "Test Fire",
            "report_id": report_id, "text": text, "date_created": "2026-09-15T21:15:00Z",
            "lat": 44.0, "lon": -114.0, "county": "Boise", "state": "ID"}


def test_decide_report_missing_irwin_id_suppressed():
    gate = decide_report({**_report_data(), "irwin_id": None}, source="watchduty", now=_NOW)
    assert gate.broadcast is False


def test_decide_report_missing_report_id_suppressed():
    gate = decide_report({**_report_data(), "report_id": None}, source="watchduty", now=_NOW)
    assert gate.broadcast is False


def test_decide_report_alerts_disabled_suppressed():
    from meshai.adapter_config._accessor import set_runtime_override, _overrides
    conn = get_db()
    _insert_matched_fire(conn, "i1", "wd1")
    set_runtime_override("watchduty", "report_alerts_enabled", False)
    try:
        gate = decide_report(_report_data(), source="watchduty", now=_NOW)
    finally:
        _overrides.pop(("watchduty", "report_alerts_enabled"), None)
    assert gate.broadcast is False


def test_decide_report_no_fires_row_suppressed():
    gate = decide_report(_report_data(irwin_id="no-such-fire"),
                         source="watchduty", now=_NOW)
    assert gate.broadcast is False


def test_decide_report_tombstoned_suppressed():
    conn = get_db()
    _insert_matched_fire(conn, "i1", "wd1", tombstoned_at=_NOW - 10)
    gate = decide_report(_report_data(), source="watchduty", now=_NOW)
    assert gate.broadcast is False


def test_decide_report_already_sent_suppressed():
    conn = get_db()
    _insert_matched_fire(conn, "i1", "wd1")
    conn.execute(
        "INSERT INTO watchduty_reports_sent"
        "(report_id, irwin_id, geo_event_id, sent_at, seeded, created_at) "
        "VALUES (?,?,?,?,?,?)",
        ("rep1", "i1", "wd1", _NOW - 5, 0, _NOW - 5),
    )
    gate = decide_report(_report_data(), source="watchduty", now=_NOW)
    assert gate.broadcast is False


def test_decide_report_broadcasts_and_commits():
    conn = get_db()
    _insert_matched_fire(conn, "i1", "wd1")
    gate = decide_report(_report_data(), source="watchduty", now=_NOW)
    assert gate.broadcast is True
    assert gate.lifecycle == "report"
    assert callable(gate.commit)

    row = conn.execute(
        "SELECT 1 FROM watchduty_reports_sent WHERE report_id='rep1'").fetchone()
    assert row is None, "nothing writes before commit runs"

    gate.commit(_NOW)
    row = conn.execute(
        "SELECT irwin_id, geo_event_id, sent_at, seeded FROM watchduty_reports_sent "
        "WHERE report_id='rep1'").fetchone()
    assert row["irwin_id"] == "i1"
    assert row["geo_event_id"] == "wd1"
    assert row["sent_at"] == _NOW
    assert row["seeded"] == 0

    # Idempotent: calling commit again must not raise or duplicate.
    gate.commit(_NOW + 5)
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM watchduty_reports_sent WHERE report_id='rep1'"
    ).fetchone()["n"]
    assert count == 1


# =============================================================================
# 12. format_report
# =============================================================================

class _FakeEvent:
    def __init__(self, data):
        self.data = data


def test_format_report_trims_long_text_keeps_link_within_budget():
    long_text = "Update sentence. " * 30
    evt = _FakeEvent({"name": "Buckhorn Fire", "text": long_text.strip(),
                      "wd_event_id": "wd123"})
    out = format_report(evt, now=_NOW, budget=140)
    assert len(out) <= 140
    assert out.endswith(incident_url("wd123"))
    assert out.startswith("Buckhorn Fire update:")


def test_format_report_short_text_intact():
    evt = _FakeEvent({"name": "Buckhorn Fire",
                      "text": "Crews have mopped up deep into the timber stringer.",
                      "wd_event_id": "wd123"})
    out = format_report(evt, now=_NOW, budget=140)
    assert out == (
        "Buckhorn Fire update:\nCrews have mopped up deep into the timber stringer.\n"
        f"{incident_url('wd123')}"
    )


# =============================================================================
# 13. to_event
# =============================================================================

def test_to_event_report_category_severity_id():
    adapter = WatchDutyAdapter(WatchDutyConfig(enabled=True))
    reading = {"type": "report", "irwin_id": "i1", "wd_event_id": "wd1",
               "name": "Test Fire", "report_id": "rep1", "text": "Update.",
               "date_created": "2026-09-15T21:15:00Z", "lat": 44.0, "lon": -114.0,
               "county": "Boise", "state": "ID"}
    ev = adapter.to_event(reading)
    assert ev is not None
    assert ev.source == "watchduty"
    assert ev.category == "wildfire_report"
    assert ev.severity == "routine"
    assert ev.id == "watchduty_report_rep1"
    assert ev.lat == 44.0
    assert ev.lon == -114.0
    assert ev.data["report_id"] == "rep1"


def test_to_event_evac_still_correct():
    adapter = WatchDutyAdapter(WatchDutyConfig(enabled=True))
    reading = {"type": "evac", "irwin_id": "i1", "wd_event_id": "wd1",
               "name": "Test Fire", "level": "order", "zone_text": "Evacuate now",
               "wd_modified": None, "lat": 44.0, "lon": -114.0, "county": "Boise",
               "state": "ID"}
    ev = adapter.to_event(reading)
    assert ev is not None
    assert ev.category == "wildfire_evac"
    assert ev.severity == "priority"


def test_to_event_report_missing_report_id_returns_none():
    adapter = WatchDutyAdapter(WatchDutyConfig(enabled=True))
    reading = {"type": "report", "irwin_id": "i1", "report_id": None,
               "lat": 44.0, "lon": -114.0}
    assert adapter.to_event(reading) is None


# =============================================================================
# 14. Routine reports are not paced on FirePacer.
# =============================================================================

class _FakePacer:
    def __init__(self):
        self.calls: list = []

    def enqueue(self, event) -> None:
        self.calls.append(event)


class _ReportStubAdapter:
    """to_event() always returns the same report Event, independent of the
    raw dict passed in -- mirrors tests/test_native_fire_pacer.py's
    _StubAdapter pattern."""

    def __init__(self, data: dict):
        self._data = data

    def to_event(self, raw_evt: dict):
        from meshai.notifications.events import make_event
        return make_event(
            source="watchduty", category="wildfire_report", severity="routine",
            title=self._data["name"], summary="update",
            lat=self._data["lat"], lon=self._data["lon"],
            group_key=f"watchduty_report_{self._data['report_id']}",
            id=f"watchduty_report_{self._data['report_id']}",
            data=dict(self._data),
        )


def test_routine_report_not_queued_on_fire_pacer():
    conn = get_db()
    _insert_matched_fire(conn, "i1", "wd1")
    bus = EventBus()
    captured: list = []
    bus.subscribe(lambda e: captured.append(e))
    store = EnvironmentalStore(EnvironmentalConfig(), event_bus=bus)
    pacer = _FakePacer()
    store._fire_pacer = pacer

    data = {"irwin_id": "i1", "wd_event_id": "wd1", "name": "Pacer Fire",
            "report_id": "pacer-rep-1", "text": "Update.", "lat": 44.0, "lon": -114.0}
    adapter = _ReportStubAdapter(data)

    store._emit_event(adapter, {"event_id": "pacer-rep-1"})

    assert pacer.calls == [], "routine severity must never reach the pacer"
    assert len(captured) == 1
    assert captured[0].severity == "routine"
    assert captured[0].category == "wildfire_report"


# =============================================================================
# 15. End-to-end through the store: match, seed tick, new-report tick.
# =============================================================================

def test_end_to_end_match_seed_then_new_report_broadcasts_once():
    conn = get_db()
    conn.execute(
        "INSERT INTO fires(irwin_id, incident_name, lat, lon, county, state, "
        "last_event_at, last_broadcast_at) VALUES (?,?,?,?,?,?,?,?)",
        ("IRWIN-RPT-1", "Report E2E Fire", 44.0, -114.0, "Boise", "ID",
         int(time.time()), time.time()),
    )

    bus = EventBus()
    captured: list = []
    bus.subscribe(lambda e: captured.append(e))
    store = EnvironmentalStore(EnvironmentalConfig(), event_bus=bus)
    adapter = WatchDutyAdapter(WatchDutyConfig(enabled=True))
    store._adapters["watchduty"] = adapter

    geo_events = [_wd_geo_event("wd_rpt_1", name="Report E2E Fire")]
    backlog = [
        _report("bl1", "<p>Old backlog report one.</p>", "2026-09-14T10:00:00Z",
                geo_event_id="wd_rpt_1"),
        _report("bl2", "<p>Old backlog report two.</p>", "2026-09-14T11:00:00Z",
                geo_event_id="wd_rpt_1"),
    ]
    reports_state = {"wd_rpt_1": list(backlog)}

    calls = []
    import meshai.env.watchduty as wd_mod
    orig = wd_mod.urlopen
    wd_mod.urlopen = _wd_urlopen(geo_events=geo_events, reports=reports_state,
                                 calls=calls)
    try:
        # Phase 1: match the fire to the Watch Duty geo_event.
        new_count = adapter.match_and_store()
        assert new_count == 1

        # Phase 2: seed tick -- first report poll for this fire, backlog
        # seeded silently, no readings.
        result2 = adapter.tick()
        assert result2 is True

        # Phase 3: a new human report arrives; force past the tick-interval
        # gate (same convention tests/test_watchduty_evac.py uses).
        new_report = _report("new1", "<p>Fresh update from the field.</p>",
                             "2026-09-15T21:15:00Z", display_name="Baylee Wright",
                             username="bwright", geo_event_id="wd_rpt_1")
        reports_state["wd_rpt_1"] = backlog + [new_report]
        adapter._last_tick = 0.0
        result3 = adapter.tick()
        assert result3 is True
    finally:
        wd_mod.urlopen = orig

    store._ingest("watchduty", adapter)

    assert len(captured) == 1, "only the new report should broadcast (evac stays none/unchanged)"
    ev = captured[0]
    assert ev.category == "wildfire_report"
    assert ev.severity == "routine"
    assert ev.data["report_id"] == "new1"

    commit = ev.data.get("_on_broadcast_committed")
    assert callable(commit)
    commit(time.time())

    rendered = FORMATTERS["wildfire_report"](ev, now=time.time(), budget=140)
    assert rendered.startswith("Report E2E Fire update:")
    assert rendered.endswith(incident_url("wd_rpt_1"))


# =============================================================================
# 16. Registration: category / cutover / decider+formatter.
# =============================================================================

def test_category_registered_under_fire_toggle():
    assert "wildfire_report" in ALERT_CATEGORIES
    assert ALERT_CATEGORIES["wildfire_report"]["toggle"] == "fire"
    assert ALERT_CATEGORIES["wildfire_report"]["default_severity"] == "routine"


def test_native_always_decide_includes_wildfire_report():
    assert "wildfire_report" in NATIVE_ALWAYS_DECIDE


def test_decider_and_formatter_registered():
    assert DECIDERS["wildfire_report"] is decide_report
    assert FORMATTERS["wildfire_report"] is format_report
