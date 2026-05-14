"""Tests for Phase 2.3a DigestAccumulator.

13 tests covering:
- Accumulator active/since_last behavior (6 tests)
- Renderer output (6 tests)
- Pipeline integration (1 test already covered, plus 2 more)
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from meshai.notifications.events import make_event
from meshai.notifications.pipeline import (
    build_pipeline_components,
    DigestAccumulator,
    Digest,
)
from meshai.config import Config


# ============================================================
# ACCUMULATOR ACTIVE/SINCE_LAST TESTS
# ============================================================

def test_enqueue_adds_to_active():
    """Enqueue one routine Event with no expires → active_count == 1."""
    acc = DigestAccumulator()
    event = make_event(
        source="test",
        category="weather_warning",
        severity="routine",
        title="Wind Advisory",
    )
    acc.enqueue(event)
    assert acc.active_count() == 1
    assert acc.since_last_count() == 0


def test_enqueue_same_id_updates_in_place():
    """Enqueue same id twice → still 1 active, title updated."""
    acc = DigestAccumulator()
    event1 = make_event(
        source="test",
        category="weather_warning",
        severity="routine",
        id="abc",
        title="initial",
    )
    event2 = make_event(
        source="test",
        category="weather_warning",
        severity="routine",
        id="abc",
        title="updated",
    )
    acc.enqueue(event1)
    acc.enqueue(event2)
    assert acc.active_count() == 1
    # Check the held event's title
    toggle = "weather"
    events = acc._active.get(toggle, [])
    assert len(events) == 1
    assert events[0].title == "updated"


def test_two_different_ids_both_active():
    """Two different routine events → both active."""
    acc = DigestAccumulator()
    event1 = make_event(
        source="test",
        category="weather_warning",
        severity="routine",
        id="ev1",
        title="Event 1",
    )
    event2 = make_event(
        source="test",
        category="weather_warning",
        severity="routine",
        id="ev2",
        title="Event 2",
    )
    acc.enqueue(event1)
    acc.enqueue(event2)
    assert acc.active_count() == 2


def test_resolution_marker_in_title_moves_active_to_since_last():
    """Resolution marker in title moves matching active to since_last."""
    acc = DigestAccumulator()
    event1 = make_event(
        source="test",
        category="wildfire_proximity",
        severity="priority",
        group_key="fire:42",
        title="Snake River Fire",
    )
    acc.enqueue(event1)
    assert acc.active_count() == 1
    assert acc.since_last_count() == 0

    event2 = make_event(
        source="test",
        category="wildfire_proximity",
        severity="priority",
        group_key="fire:42",
        title="Snake River Fire ended",
    )
    acc.enqueue(event2)
    assert acc.active_count() == 0
    assert acc.since_last_count() == 1


def test_expired_event_via_tick_moves_to_since_last():
    """tick() moves expired events from active to since_last."""
    acc = DigestAccumulator()
    base_time = 1000000.0

    # Monkeypatch _now to control time
    acc._now = lambda: base_time

    event = make_event(
        source="test",
        category="weather_warning",
        severity="routine",
        title="Temporary Warning",
        expires=base_time + 60,  # expires in 60 seconds
    )
    acc.enqueue(event)
    assert acc.active_count() == 1
    assert acc.since_last_count() == 0

    # Tick at base_time + 30 → still active
    moved = acc.tick(now=base_time + 30)
    assert moved == 0
    assert acc.active_count() == 1

    # Tick at base_time + 120 → expired, moved to since_last
    moved = acc.tick(now=base_time + 120)
    assert moved == 1
    assert acc.active_count() == 0
    assert acc.since_last_count() == 1


def test_render_digest_clears_since_last_but_keeps_active():
    """render_digest() clears since_last but preserves active."""
    acc = DigestAccumulator()
    base_time = 1000000.0
    acc._now = lambda: base_time

    # Add an active event
    active_event = make_event(
        source="test",
        category="weather_warning",
        severity="routine",
        title="Ongoing Storm",
    )
    acc.enqueue(active_event)

    # Add an event that becomes since_last via resolution marker
    resolved_event = make_event(
        source="test",
        category="road_closure",
        severity="routine",
        group_key="roads:99",
        title="US-93 reopened at MP 47",
    )
    acc.enqueue(resolved_event)

    # Now we should have 1 active, 1 since_last
    assert acc.active_count() == 1
    assert acc.since_last_count() == 1

    # Render digest
    digest = acc.render_digest(now=base_time)
    assert len(digest.active) > 0
    assert len(digest.since_last) > 0

    # After render: active preserved, since_last cleared
    assert acc.active_count() == 1
    assert acc.since_last_count() == 0

    # Second render has only active
    digest2 = acc.render_digest(now=base_time + 10)
    assert len(digest2.active) > 0
    assert len(digest2.since_last) == 0


# ============================================================
# RENDERER TESTS
# ============================================================

def test_render_full_lists_active_and_since_last_with_labels():
    """Full render includes ACTIVE NOW, SINCE LAST DIGEST, toggle labels."""
    acc = DigestAccumulator()
    base_time = 1000000.0
    acc._now = lambda: base_time

    # Weather event (active)
    weather_event = make_event(
        source="test",
        category="weather_warning",
        severity="routine",
        title="Wind Advisory until 21:00",
    )
    acc.enqueue(weather_event)

    # Roads event with resolution marker → since_last
    roads_event = make_event(
        source="test",
        category="road_closure",
        severity="routine",
        title="US-93 reopened at MP 47",
    )
    acc.enqueue(roads_event)

    digest = acc.render_digest(now=base_time)

    assert "ACTIVE NOW:" in digest.full
    assert "[Weather]" in digest.full
    assert "Wind Advisory" in digest.full
    assert "SINCE LAST DIGEST:" in digest.full
    assert "[Roads]" in digest.full
    assert "US-93" in digest.full


def test_render_mesh_compact_under_char_limit():
    """mesh_compact is <= 200 chars with proper format."""
    acc = DigestAccumulator()
    base_time = 1000000.0
    acc._now = lambda: base_time

    # Add 10 events across 4 toggles
    categories = [
        ("weather_warning", "Weather Event"),
        ("weather_warning", "Weather Event 2"),
        ("weather_warning", "Weather Event 3"),
        ("wildfire_proximity", "Fire Event"),
        ("wildfire_proximity", "Fire Event 2"),
        ("battery_warning", "Mesh Event"),
        ("battery_warning", "Mesh Event 2"),
        ("battery_warning", "Mesh Event 3"),
        ("road_closure", "Road Event"),
        ("road_closure", "Road Event 2"),
    ]
    for i, (cat, title) in enumerate(categories):
        event = make_event(
            source="test",
            category=cat,
            severity="routine",
            id=f"ev{i}",
            title=title,
        )
        acc.enqueue(event)

    digest = acc.render_digest(now=base_time)
    assert len(digest.mesh_compact) <= 200
    assert digest.mesh_compact.startswith("DIGEST ")
    assert "ACTIVE:" in digest.mesh_compact


def test_render_mesh_compact_all_quiet_when_empty():
    """Empty accumulator renders 'All quiet.' in mesh_compact."""
    acc = DigestAccumulator()
    base_time = 1000000.0
    acc._now = lambda: base_time

    digest = acc.render_digest(now=base_time)
    assert "All quiet" in digest.mesh_compact


def test_render_full_handles_empty_accumulator():
    """Empty accumulator → is_empty() True, 'ACTIVE NOW: nothing'."""
    acc = DigestAccumulator()
    base_time = 1000000.0
    acc._now = lambda: base_time

    digest = acc.render_digest(now=base_time)
    assert digest.is_empty() is True
    assert "ACTIVE NOW: nothing" in digest.full


def test_render_orders_toggles_by_priority():
    """Toggles appear in TOGGLE_ORDER sequence in full output."""
    acc = DigestAccumulator()
    base_time = 1000000.0
    acc._now = lambda: base_time

    # Add one event each for weather, mesh_health, and fire
    # (intentionally out of order)
    mesh_event = make_event(
        source="test",
        category="battery_warning",  # maps to mesh_health toggle
        severity="routine",
        title="Mesh battery low",
    )
    fire_event = make_event(
        source="test",
        category="wildfire_proximity",
        severity="routine",
        title="Fire nearby",
    )
    weather_event = make_event(
        source="test",
        category="weather_warning",
        severity="routine",
        title="Storm coming",
    )
    acc.enqueue(mesh_event)
    acc.enqueue(fire_event)
    acc.enqueue(weather_event)

    digest = acc.render_digest(now=base_time)

    # In TOGGLE_ORDER: weather, fire, ..., mesh_health
    weather_pos = digest.full.find("[Weather]")
    fire_pos = digest.full.find("[Fire]")
    mesh_pos = digest.full.find("[Mesh]")

    assert weather_pos < fire_pos, "Weather should appear before Fire"
    assert fire_pos < mesh_pos, "Fire should appear before Mesh"


def test_format_event_line_appends_expires_hint():
    """_format_event_line() appends '(until HH:MM)' for future expires."""
    acc = DigestAccumulator()
    base_time = 1000000.0
    acc._now = lambda: base_time

    event = make_event(
        source="test",
        category="weather_warning",
        severity="routine",
        title="Severe Thunderstorm Warning",
        expires=base_time + 3600,  # 1 hour in future
    )

    line = acc._format_event_line(event)
    assert "(until " in line
    # Should have time in HH:MM format
    assert ":" in line.split("(until ")[-1]


# ============================================================
# PIPELINE INTEGRATION TESTS
# ============================================================

def test_pipeline_routes_routine_event_to_accumulator():
    """Routine event via bus.emit ends up in DigestAccumulator."""
    config = Config()
    bus, inhibitor, grouper, severity_router, dispatcher, digest = \
        build_pipeline_components(config)

    event = make_event(
        source="test",
        category="weather_warning",
        severity="routine",
        title="Test routine event",
    )

    # Flush through grouper
    grouper.flush_all()
    bus.emit(event)
    grouper.flush_all()

    assert digest.active_count() == 1


def test_pipeline_routes_immediate_event_to_dispatcher_not_accumulator():
    """Immediate event goes to dispatcher, not accumulator."""
    config = Config()
    bus, inhibitor, grouper, severity_router, dispatcher, digest = \
        build_pipeline_components(config)

    # Mock the severity_router's immediate handler (already bound to dispatcher.dispatch)
    mock_immediate = MagicMock()
    severity_router._immediate = mock_immediate

    event = make_event(
        source="test",
        category="weather_warning",
        severity="immediate",
        title="Test immediate event",
    )

    grouper.flush_all()
    bus.emit(event)
    grouper.flush_all()

    # Immediate handler should have been called
    assert mock_immediate.called
    # Accumulator should have nothing
    assert digest.active_count() == 0
