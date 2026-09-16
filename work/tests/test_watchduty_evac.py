"""Group B: Watch Duty evacuation alerts.

Covers env/watchduty.py's evac_level() + evac-reading pipeline
(match_and_store -> get_events), the gating decider
(notifications/gating/watchduty.py::decide_evac), and the formatter
(notifications/formatters/watchduty.py::format_evac).

No real network calls anywhere -- every HTTP path patches
``meshai.env.watchduty.urlopen`` (where the module imports it from), the
same convention tests/test_watchduty_adapter.py uses.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import pytest

from meshai.config import EnvironmentalConfig, WatchDutyConfig
from meshai.env.store import EnvironmentalStore
from meshai.env.watchduty import WatchDutyAdapter, evac_level, incident_url
from meshai.notifications.categories import ALERT_CATEGORIES
from meshai.notifications.cutover import NATIVE_ALWAYS_DECIDE
from meshai.notifications.formatters import FORMATTERS
from meshai.notifications.formatters.watchduty import format_evac
from meshai.notifications.gating import DECIDERS
from meshai.notifications.gating.watchduty import decide_evac
from meshai.notifications.pipeline.bus import EventBus
from meshai.notifications.pipeline.dispatcher import Dispatcher
from meshai.persistence import get_db

_NOW = 1_800_000_000.0


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


def _wd_event(event_id, *, name="Test Fire WD", lat=44.0, lng=-114.0,
              is_active=True, evacuation_orders=None, evacuation_warnings=None,
              evacuation_advisories=None, evacuation_shelter_in_place=None,
              has_custom_evacuation_orders=False, has_custom_evacuation_warnings=False,
              has_custom_evacuation_advisories=False,
              has_custom_evacuation_shelter_in_place=False,
              date_modified=None) -> dict:
    return {
        "id": event_id,
        "name": name,
        "is_active": is_active,
        "lat": lat,
        "lng": lng,
        "date_modified": date_modified,
        "data": {
            "is_prescribed": False,
            "evacuation_orders": evacuation_orders,
            "evacuation_warnings": evacuation_warnings,
            "evacuation_advisories": evacuation_advisories,
            "evacuation_shelter_in_place": evacuation_shelter_in_place,
            "has_custom_evacuation_orders": has_custom_evacuation_orders,
            "has_custom_evacuation_warnings": has_custom_evacuation_warnings,
            "has_custom_evacuation_advisories": has_custom_evacuation_advisories,
            "has_custom_evacuation_shelter_in_place": has_custom_evacuation_shelter_in_place,
        },
    }


def _insert_fire(conn, irwin_id, *, lat=44.0, lon=-114.0,
                  watchduty_event_id=None, watchduty_name=None,
                  tombstoned_at=None, evac_state=None, evac_zone_text=None,
                  evac_broadcast_at=None, evac_updated_at=None):
    now = int(time.time())
    conn.execute(
        "INSERT INTO fires(irwin_id, incident_name, lat, lon, county, state, "
        "last_event_at, tombstoned_at, watchduty_event_id, watchduty_name, "
        "watchduty_evac_state, watchduty_evac_zone_text, "
        "watchduty_evac_broadcast_at, watchduty_evac_updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (irwin_id, f"Fire {irwin_id}", lat, lon, "Boise", "ID", now,
         tombstoned_at, watchduty_event_id, watchduty_name,
         evac_state, evac_zone_text, evac_broadcast_at, evac_updated_at),
    )


# =============================================================================
# 1. evac_level
# =============================================================================

_LIVE_ORDER_HTML = (
    '<p>A Level 3 (Go Now) Evacuation is in effect for the area depicted in '
    'red on the <a href="https://example.com">Kittitas County Evacuation Map'
    '</a></p>'
)


def test_evac_level_live_order_html():
    level, zone = evac_level({"data": {"evacuation_orders": _LIVE_ORDER_HTML}})
    assert level == "order"
    assert "Level 3 (Go Now)" in zone
    assert "<" not in zone and ">" not in zone


def test_evac_level_warnings_only():
    level, zone = evac_level({"data": {"evacuation_warnings": "<p>Warning text</p>"}})
    assert level == "warning"
    assert zone == "Warning text"


def test_evac_level_has_custom_order_flag_empty_text():
    level, zone = evac_level({"data": {"has_custom_evacuation_orders": True}})
    assert level == "order"
    assert zone == ""


def test_evac_level_has_custom_warning_flag_empty_text():
    level, zone = evac_level({"data": {"has_custom_evacuation_warnings": True}})
    assert level == "warning"
    assert zone == ""


def test_evac_level_shelter_only_prefixed_and_ranks_as_order():
    level, zone = evac_level({"data": {"evacuation_shelter_in_place": "Stay indoors"}})
    assert level == "order"
    assert zone == "Shelter in place: Stay indoors"


def test_evac_level_order_and_shelter_joined():
    level, zone = evac_level({"data": {
        "evacuation_orders": "Evacuate zone A",
        "evacuation_shelter_in_place": "Stay indoors zone B",
    }})
    assert level == "order"
    assert zone == "Evacuate zone A; Shelter in place: Stay indoors zone B"


def test_evac_level_advisories_only_is_none():
    level, zone = evac_level({"data": {"evacuation_advisories": "<p>Advisory text</p>"}})
    assert level == "none"
    assert zone == ""


def test_evac_level_everything_empty_is_none():
    assert evac_level({"data": {}}) == ("none", "")
    assert evac_level({}) == ("none", "")


def test_evac_level_newline_collapsing():
    html = "<p>Line one.</p><p>Line two.</p>"
    level, zone = evac_level({"data": {"evacuation_orders": html}})
    assert level == "order"
    assert "\n" not in zone
    assert zone == "Line one.; Line two."


# =============================================================================
# 2. Readings: built only for matched/non-tombstoned fires present in the
#    response; none when disabled; get_events clears them.
# =============================================================================

def test_readings_built_only_for_matched_nontombstoned_in_response():
    conn = get_db()
    _insert_fire(conn, "irwin_a", watchduty_event_id="wd_a")
    _insert_fire(conn, "irwin_b", watchduty_event_id="wd_b",
                 tombstoned_at=time.time())
    _insert_fire(conn, "irwin_c")  # not matched -- no watchduty_event_id

    wd_events = [
        _wd_event("wd_a", evacuation_orders="Evacuate now"),
        _wd_event("wd_b", evacuation_orders="Ignored, tombstoned"),
        _wd_event("wd_missing_fire", evacuation_orders="No matching fire row"),
    ]
    adapter = WatchDutyAdapter(WatchDutyConfig(enabled=True))
    import meshai.env.watchduty as wd_mod
    orig = wd_mod.urlopen
    wd_mod.urlopen = _fake_urlopen(wd_events, [])
    try:
        adapter.match_and_store()
    finally:
        wd_mod.urlopen = orig

    readings = adapter.get_events()
    assert {r["irwin_id"] for r in readings} == {"irwin_a"}
    assert readings[0]["level"] == "order"
    assert readings[0]["zone_text"] == "Evacuate now"
    assert readings[0]["wd_event_id"] == "wd_a"


def test_readings_none_when_evac_alerts_disabled():
    from meshai.adapter_config._accessor import set_runtime_override, _overrides
    conn = get_db()
    _insert_fire(conn, "irwin_a", watchduty_event_id="wd_a")
    wd_events = [_wd_event("wd_a", evacuation_orders="Evacuate now")]
    adapter = WatchDutyAdapter(WatchDutyConfig(enabled=True))

    set_runtime_override("watchduty", "evac_alerts_enabled", False)
    try:
        import meshai.env.watchduty as wd_mod
        orig = wd_mod.urlopen
        wd_mod.urlopen = _fake_urlopen(wd_events, [])
        try:
            adapter.match_and_store()
        finally:
            wd_mod.urlopen = orig
        assert adapter.get_events() == []
    finally:
        _overrides.pop(("watchduty", "evac_alerts_enabled"), None)


def test_get_events_drains_and_clears_readings():
    conn = get_db()
    _insert_fire(conn, "irwin_a", watchduty_event_id="wd_a")
    wd_events = [_wd_event("wd_a", evacuation_warnings="Be ready")]
    adapter = WatchDutyAdapter(WatchDutyConfig(enabled=True))
    import meshai.env.watchduty as wd_mod
    orig = wd_mod.urlopen
    wd_mod.urlopen = _fake_urlopen(wd_events, [])
    try:
        adapter.match_and_store()
    finally:
        wd_mod.urlopen = orig

    first = adapter.get_events()
    assert len(first) == 1
    assert adapter.get_events() == [], "a second drain must come back empty"


# =============================================================================
# 3. Single-fire match resets the tick interval; other cases leave it alone.
# =============================================================================

def test_single_fire_new_match_resets_tick_interval():
    conn = get_db()
    _insert_fire(conn, "irwin_new")  # not yet matched
    adapter = WatchDutyAdapter(WatchDutyConfig(enabled=True))
    adapter._last_tick = time.time()  # pretend a batch tick just ran

    wd_events = [_wd_event("wd_new")]
    import meshai.env.watchduty as wd_mod
    orig = wd_mod.urlopen
    wd_mod.urlopen = _fake_urlopen(wd_events, [])
    try:
        new_count = adapter.match_and_store(irwin_id="irwin_new")
    finally:
        wd_mod.urlopen = orig

    assert new_count == 1
    assert adapter._last_tick == 0.0


def test_single_fire_no_new_match_leaves_tick_interval_alone():
    conn = get_db()
    _insert_fire(conn, "irwin_already", watchduty_event_id="wd_already")
    adapter = WatchDutyAdapter(WatchDutyConfig(enabled=True))
    recent = time.time()
    adapter._last_tick = recent

    wd_events = [_wd_event("wd_already")]
    import meshai.env.watchduty as wd_mod
    orig = wd_mod.urlopen
    wd_mod.urlopen = _fake_urlopen(wd_events, [])
    try:
        new_count = adapter.match_and_store(irwin_id="irwin_already")
    finally:
        wd_mod.urlopen = orig

    assert new_count == 0
    assert adapter._last_tick == recent


def test_batch_new_match_does_not_reset_tick_interval():
    conn = get_db()
    # Batch-mode candidates additionally require a recent last_broadcast_at
    # (see WatchDutyAdapter._candidate_fires).
    conn.execute(
        "INSERT INTO fires(irwin_id, incident_name, lat, lon, "
        "last_event_at, last_broadcast_at) VALUES (?,?,?,?,?,?)",
        ("irwin_batch", "Batch Fire", 44.0, -114.0, int(time.time()),
         int(time.time())),
    )
    adapter = WatchDutyAdapter(WatchDutyConfig(enabled=True))
    recent = time.time()
    adapter._last_tick = recent

    wd_events = [_wd_event("wd_batch")]
    import meshai.env.watchduty as wd_mod
    orig = wd_mod.urlopen
    wd_mod.urlopen = _fake_urlopen(wd_events, [])
    try:
        new_count = adapter.match_and_store()  # batch mode -- irwin_id=None
    finally:
        wd_mod.urlopen = orig

    assert new_count == 1
    assert adapter._last_tick == recent, "only the single-fire path resets _last_tick"


# =============================================================================
# 4. decide_evac transitions
# =============================================================================

def _data(irwin_id, level, zone_text="", wd_modified=None):
    return {"irwin_id": irwin_id, "level": level, "zone_text": zone_text,
            "wd_modified": wd_modified}


def test_decide_null_order_broadcasts():
    conn = get_db()
    _insert_fire(conn, "i1")
    gate = decide_evac(_data("i1", "order", "Evacuate now"), source="watchduty", now=_NOW)
    assert gate.broadcast is True
    assert gate.data_patch["kind"] == "order"
    assert callable(gate.commit)


def test_decide_null_warning_silent_seed():
    conn = get_db()
    _insert_fire(conn, "i1")
    gate = decide_evac(_data("i1", "warning", "Be ready"), source="watchduty", now=_NOW)
    assert gate.broadcast is False
    row = conn.execute(
        "SELECT watchduty_evac_state, watchduty_evac_zone_text, "
        "watchduty_evac_broadcast_at FROM fires WHERE irwin_id='i1'").fetchone()
    assert row["watchduty_evac_state"] == "warning"
    assert row["watchduty_evac_zone_text"] == "Be ready"
    assert row["watchduty_evac_broadcast_at"] is None, "seed never touches broadcast_at"


def test_decide_null_none_silent_seed():
    conn = get_db()
    _insert_fire(conn, "i1")
    gate = decide_evac(_data("i1", "none", ""), source="watchduty", now=_NOW)
    assert gate.broadcast is False
    row = conn.execute(
        "SELECT watchduty_evac_state FROM fires WHERE irwin_id='i1'").fetchone()
    assert row["watchduty_evac_state"] == "none"


def test_decide_none_to_warning_broadcasts():
    conn = get_db()
    _insert_fire(conn, "i1", evac_state="none", evac_zone_text="")
    gate = decide_evac(_data("i1", "warning", "Be ready"), source="watchduty", now=_NOW)
    assert gate.broadcast is True
    assert gate.data_patch["kind"] == "warning"


def test_decide_warning_to_order_broadcasts():
    conn = get_db()
    _insert_fire(conn, "i1", evac_state="warning", evac_zone_text="Be ready")
    gate = decide_evac(_data("i1", "order", "Evacuate now"), source="watchduty", now=_NOW)
    assert gate.broadcast is True
    assert gate.data_patch["kind"] == "order"


def test_decide_none_to_order_broadcasts():
    conn = get_db()
    _insert_fire(conn, "i1", evac_state="none", evac_zone_text="")
    gate = decide_evac(_data("i1", "order", "Evacuate now"), source="watchduty", now=_NOW)
    assert gate.broadcast is True
    assert gate.data_patch["kind"] == "order"


def test_decide_order_to_warning_downgraded():
    conn = get_db()
    _insert_fire(conn, "i1", evac_state="order", evac_zone_text="Evacuate now")
    gate = decide_evac(_data("i1", "warning", "Be ready"), source="watchduty", now=_NOW)
    assert gate.broadcast is True
    assert gate.data_patch["kind"] == "downgraded"


def test_decide_order_to_none_lifted():
    conn = get_db()
    _insert_fire(conn, "i1", evac_state="order", evac_zone_text="Evacuate now")
    gate = decide_evac(_data("i1", "none", ""), source="watchduty", now=_NOW)
    assert gate.broadcast is True
    assert gate.data_patch["kind"] == "lifted"


def test_decide_warning_to_none_lifted():
    conn = get_db()
    _insert_fire(conn, "i1", evac_state="warning", evac_zone_text="Be ready")
    gate = decide_evac(_data("i1", "none", ""), source="watchduty", now=_NOW)
    assert gate.broadcast is True
    assert gate.data_patch["kind"] == "lifted"


def test_decide_same_level_changed_text_no_prior_broadcast_updates():
    conn = get_db()
    _insert_fire(conn, "i1", evac_state="order", evac_zone_text="Old text",
                 evac_broadcast_at=None)
    gate = decide_evac(_data("i1", "order", "New text"), source="watchduty", now=_NOW)
    assert gate.broadcast is True
    assert gate.data_patch["kind"] == "updated"


def test_decide_same_level_changed_text_inside_cooldown_suppressed():
    conn = get_db()
    _insert_fire(conn, "i1", evac_state="order", evac_zone_text="Old text",
                 evac_broadcast_at=_NOW - 100)  # well inside 3600s default cooldown
    gate = decide_evac(_data("i1", "order", "New text"), source="watchduty", now=_NOW)
    assert gate.broadcast is False
    row = conn.execute(
        "SELECT watchduty_evac_zone_text FROM fires WHERE irwin_id='i1'").fetchone()
    assert row["watchduty_evac_zone_text"] == "Old text", "stored text must be untouched"


def test_decide_same_level_changed_text_after_cooldown_updates():
    conn = get_db()
    _insert_fire(conn, "i1", evac_state="order", evac_zone_text="Old text",
                 evac_broadcast_at=_NOW - 3700)  # past 3600s default cooldown
    gate = decide_evac(_data("i1", "order", "New text"), source="watchduty", now=_NOW)
    assert gate.broadcast is True
    assert gate.data_patch["kind"] == "updated"


def test_decide_same_text_suppressed():
    conn = get_db()
    _insert_fire(conn, "i1", evac_state="order", evac_zone_text="Same text")
    gate = decide_evac(_data("i1", "order", "Same text"), source="watchduty", now=_NOW)
    assert gate.broadcast is False


def test_decide_tombstoned_suppressed():
    conn = get_db()
    _insert_fire(conn, "i1", tombstoned_at=_NOW - 10)
    gate = decide_evac(_data("i1", "order", "Evacuate now"), source="watchduty", now=_NOW)
    assert gate.broadcast is False
    assert gate.lifecycle == "suppress"


def test_decide_disabled_suppressed():
    from meshai.adapter_config._accessor import set_runtime_override, _overrides
    conn = get_db()
    _insert_fire(conn, "i1")
    set_runtime_override("watchduty", "evac_alerts_enabled", False)
    try:
        gate = decide_evac(_data("i1", "order", "Evacuate now"),
                           source="watchduty", now=_NOW)
    finally:
        _overrides.pop(("watchduty", "evac_alerts_enabled"), None)
    assert gate.broadcast is False


def test_decide_none_prev_none_suppressed_no_seed_write_change():
    """level == prev == 'none' -- suppress; a bare no-op, not a re-seed."""
    conn = get_db()
    _insert_fire(conn, "i1", evac_state="none", evac_zone_text="")
    gate = decide_evac(_data("i1", "none", ""), source="watchduty", now=_NOW)
    assert gate.broadcast is False


# =============================================================================
# 5. Deferred commit: writes all four columns; nothing changes before it runs.
# =============================================================================

def test_deferred_commit_writes_all_four_columns():
    conn = get_db()
    _insert_fire(conn, "i1")
    wd_modified = "2026-09-16T12:00:00Z"
    gate = decide_evac(_data("i1", "order", "Evacuate now", wd_modified),
                       source="watchduty", now=_NOW)
    assert gate.broadcast is True

    row = conn.execute(
        "SELECT watchduty_evac_state, watchduty_evac_broadcast_at FROM fires "
        "WHERE irwin_id='i1'").fetchone()
    assert row["watchduty_evac_state"] is None, "nothing writes before commit runs"
    assert row["watchduty_evac_broadcast_at"] is None

    gate.commit(_NOW)
    row = conn.execute(
        "SELECT watchduty_evac_state, watchduty_evac_zone_text, "
        "watchduty_evac_broadcast_at, watchduty_evac_updated_at FROM fires "
        "WHERE irwin_id='i1'").fetchone()
    assert row["watchduty_evac_state"] == "order"
    assert row["watchduty_evac_zone_text"] == "Evacuate now"
    assert row["watchduty_evac_broadcast_at"] == _NOW
    expected_epoch = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc).timestamp()
    assert row["watchduty_evac_updated_at"] == pytest.approx(expected_epoch)


def test_deferred_commit_never_runs_on_suppress():
    conn = get_db()
    _insert_fire(conn, "i1", evac_state="order", evac_zone_text="Same text")
    gate = decide_evac(_data("i1", "order", "Same text"), source="watchduty", now=_NOW)
    assert gate.broadcast is False
    assert gate.commit is None


# =============================================================================
# 6. format_evac
# =============================================================================

class _FakeEvent:
    def __init__(self, data):
        self.data = data


def _evt(kind, level, name="Test Fire", zone_text="", wd_event_id="wd123"):
    return _FakeEvent({"kind": kind, "level": level, "name": name,
                       "zone_text": zone_text, "wd_event_id": wd_event_id})


def test_format_order():
    out = format_evac(_evt("order", "order", zone_text="Level 3 evac"),
                      now=_NOW, budget=140)
    assert out == f"EVACUATION ORDER: Test Fire\nLevel 3 evac\n{incident_url('wd123')}"


def test_format_warning():
    out = format_evac(_evt("warning", "warning", zone_text="Be ready"),
                      now=_NOW, budget=140)
    assert out == f"EVACUATION WARNING: Test Fire\nBe ready\n{incident_url('wd123')}"


def test_format_downgraded():
    out = format_evac(_evt("downgraded", "warning", zone_text="Be ready"),
                      now=_NOW, budget=140)
    assert out.startswith("Evacuation order downgraded to warning: Test Fire\n")


def test_format_lifted_omits_zone_text():
    out = format_evac(_evt("lifted", "none", zone_text="stale text that must not appear"),
                      now=_NOW, budget=140)
    assert out == f"Evacuations lifted: Test Fire\n{incident_url('wd123')}"
    assert "stale text" not in out


def test_format_updated_order():
    out = format_evac(_evt("updated", "order", zone_text="New order text"),
                      now=_NOW, budget=140)
    assert out.startswith("EVACUATION ORDER UPDATED: Test Fire\n")


def test_format_updated_warning():
    out = format_evac(_evt("updated", "warning", zone_text="New warning text"),
                      now=_NOW, budget=140)
    assert out.startswith("EVACUATION WARNING UPDATED: Test Fire\n")


def test_format_long_zone_text_trimmed_link_intact_within_budget():
    long_text = "Evacuate the area immediately. " * 20
    ev = _evt("order", "order", zone_text=long_text)
    out = format_evac(ev, now=_NOW, budget=140)
    assert len(out) <= 140
    assert out.endswith(incident_url("wd123"))


# =============================================================================
# 8. End-to-end via the store: match -> tick -> exactly one wildfire_evac
#    event; a second identical tick after commit produces nothing.
# =============================================================================

def test_end_to_end_order_via_store_then_second_tick_produces_nothing():
    conn = get_db()
    irwin_id = "IRWIN-E2E-1"
    conn.execute(
        "INSERT INTO fires(irwin_id, incident_name, lat, lon, county, state, "
        "last_event_at, watchduty_event_id, watchduty_name, watchduty_is_active) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (irwin_id, "E2E Fire", 44.0, -114.0, "Boise", "ID", int(time.time()),
         "wd_e2e", "E2E Fire WD", 1),
    )

    bus = EventBus()
    captured: list = []
    bus.subscribe(lambda e: captured.append(e))
    store = EnvironmentalStore(EnvironmentalConfig(), event_bus=bus)
    adapter = WatchDutyAdapter(WatchDutyConfig(enabled=True))
    store._adapters["watchduty"] = adapter

    wd_events = [_wd_event("wd_e2e", name="E2E Fire WD",
                           evacuation_orders="Level 3 (Go Now) evacuation.")]

    import meshai.env.watchduty as wd_mod
    orig = wd_mod.urlopen
    wd_mod.urlopen = _fake_urlopen(wd_events, [])
    try:
        adapter.match_and_store()
        store._ingest("watchduty", adapter)
    finally:
        wd_mod.urlopen = orig

    assert len(captured) == 1
    ev = captured[0]
    assert ev.category == "wildfire_evac"
    assert ev.data["kind"] == "order"

    commit = ev.data.get("_on_broadcast_committed")
    assert callable(commit), "an order broadcast must arm the deferred commit"
    commit(time.time())

    # Second, identical poll: state+text unchanged -> decider suppresses.
    wd_mod.urlopen = _fake_urlopen(wd_events, [])
    try:
        adapter.match_and_store()
        store._ingest("watchduty", adapter)
    finally:
        wd_mod.urlopen = orig

    assert len(captured) == 1, "an unchanged repeat poll must broadcast nothing new"


# =============================================================================
# 9. Registration: category / cutover / _SOURCE_TO_TABLE / decider+formatter.
# =============================================================================

def test_category_registered_under_fire_toggle():
    assert "wildfire_evac" in ALERT_CATEGORIES
    assert ALERT_CATEGORIES["wildfire_evac"]["toggle"] == "fire"
    assert ALERT_CATEGORIES["wildfire_evac"]["default_severity"] == "priority"


def test_native_always_decide_includes_wildfire_evac():
    assert "wildfire_evac" in NATIVE_ALWAYS_DECIDE


def test_source_to_table_watchduty_is_fires():
    assert Dispatcher._SOURCE_TO_TABLE["watchduty"] == "fires"


def test_decider_and_formatter_registered():
    assert DECIDERS["wildfire_evac"] is decide_evac
    assert FORMATTERS["wildfire_evac"] is format_evac
