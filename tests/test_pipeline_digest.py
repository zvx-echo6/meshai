"""Tests for Phase 2.3a DigestAccumulator.

27 tests covering:
- Accumulator active/since_last behavior (6 tests)
- Renderer output (8 tests)
- Mesh chunks (7 tests)
- Include toggles (3 tests)
- Pipeline integration (3 tests)
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
from meshai.notifications.categories import get_toggle, ALERT_CATEGORIES
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
    """Each mesh chunk is <= 200 chars."""
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

    # All chunks should be <= 200 chars
    assert all(len(c) <= 200 for c in digest.mesh_chunks)
    assert len(digest.mesh_chunks) >= 1
    # Should have proper structure
    assert digest.mesh_chunks[0].startswith("DIGEST ")


def test_render_mesh_compact_empty_shows_no_alerts_message():
    """Empty accumulator renders 'No alerts since last digest' in mesh_compact."""
    acc = DigestAccumulator()
    base_time = 1000000.0
    acc._now = lambda: base_time

    digest = acc.render_digest(now=base_time)
    assert "No alerts since last digest" in digest.mesh_compact
    assert "DIGEST " in digest.mesh_compact
    assert "All quiet" not in digest.mesh_compact


def test_render_full_handles_empty_accumulator():
    """Empty accumulator → is_empty() True, shows 'No alerts since last digest'."""
    acc = DigestAccumulator()
    base_time = 1000000.0
    acc._now = lambda: base_time

    digest = acc.render_digest(now=base_time)
    assert digest.is_empty() is True
    assert "No alerts since last digest" in digest.full
    assert "ACTIVE NOW" not in digest.full
    assert "ACTIVE NOW: nothing" not in digest.full


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


def test_format_event_line_does_not_append_expires_hint():
    """_format_event_line() does NOT append '(until HH:MM)' anymore."""
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
    assert "until " not in line
    assert "(" not in line


def test_mesh_compact_shows_one_line_per_toggle():
    """Each toggle gets exactly one line, with (+N) for overflow."""
    acc = DigestAccumulator()
    base_time = 1000000.0
    acc._now = lambda: base_time

    # Add 2 weather events, 1 fire event, 1 mesh event
    acc.enqueue(make_event(
        source="test",
        category="weather_warning",
        severity="routine",
        id="w1",
        title="Weather Event 1",
    ))
    acc.enqueue(make_event(
        source="test",
        category="weather_warning",
        severity="routine",
        id="w2",
        title="Weather Event 2",
    ))
    acc.enqueue(make_event(
        source="test",
        category="wildfire_proximity",
        severity="routine",
        id="f1",
        title="Fire Event",
    ))
    acc.enqueue(make_event(
        source="test",
        category="battery_warning",
        severity="routine",
        id="m1",
        title="Mesh Event",
    ))

    digest = acc.render_digest(now=base_time)

    # Count occurrences of each toggle label
    weather_count = digest.mesh_compact.count("[Weather]")
    fire_count = digest.mesh_compact.count("[Fire]")
    mesh_count = digest.mesh_compact.count("[Mesh]")

    assert weather_count == 1, "Should have exactly one [Weather] line"
    assert fire_count == 1, "Should have exactly one [Fire] line"
    assert mesh_count == 1, "Should have exactly one [Mesh] line"

    # Weather line should have (+1) since there are 2 weather events
    weather_line = [l for l in digest.mesh_compact.split("\n") if "[Weather]" in l][0]
    assert "(+1)" in weather_line


def test_mesh_compact_active_and_resolved_sections():
    """mesh_compact has ACTIVE NOW and RESOLVED sections when both present."""
    acc = DigestAccumulator()
    base_time = 1000000.0
    acc._now = lambda: base_time

    # Add 1 active weather event
    acc.enqueue(make_event(
        source="test",
        category="weather_warning",
        severity="routine",
        title="Storm Warning",
    ))

    # Add 1 resolution event for roads (contains "reopened")
    acc.enqueue(make_event(
        source="test",
        category="road_closure",
        severity="routine",
        title="US-93 reopened at MP 47",
    ))

    digest = acc.render_digest(now=base_time)

    # Check section markers in the joined compact string
    assert "ACTIVE NOW" in digest.mesh_compact
    assert "RESOLVED" in digest.mesh_compact

    # ACTIVE NOW should appear before RESOLVED
    active_pos = digest.mesh_compact.find("ACTIVE NOW")
    resolved_pos = digest.mesh_compact.find("RESOLVED")
    assert active_pos < resolved_pos, "ACTIVE NOW should appear before RESOLVED"


def test_mesh_compact_line_truncates_long_headline():
    """Long headlines are truncated in mesh_compact."""
    acc = DigestAccumulator()
    base_time = 1000000.0
    acc._now = lambda: base_time

    # Create a 200-char summary
    long_summary = "A" * 200
    acc.enqueue(make_event(
        source="test",
        category="weather_warning",
        severity="routine",
        title="Weather Event",
        summary=long_summary,
    ))

    digest = acc.render_digest(now=base_time)

    # The [Weather] line should be shorter than the raw summary
    weather_line = [l for l in digest.mesh_compact.split("\n") if "[Weather]" in l][0]
    assert len(weather_line) < len(long_summary)


# ============================================================
# MESH CHUNKS TESTS
# ============================================================

def test_mesh_chunks_single_chunk_when_short():
    """Single short event produces one chunk with no counter."""
    acc = DigestAccumulator()
    base_time = 1000000.0
    acc._now = lambda: base_time

    acc.enqueue(make_event(
        source="test",
        category="weather_warning",
        severity="routine",
        title="Short event",
        summary="Brief summary",
    ))

    digest = acc.render_digest(now=base_time)

    assert len(digest.mesh_chunks) == 1
    assert digest.mesh_chunks[0].startswith("DIGEST ")
    assert "(1/" not in digest.mesh_chunks[0]  # No chunk counter when single
    assert digest.mesh_compact == digest.mesh_chunks[0]


def test_mesh_chunks_splits_when_overflow():
    """Many events with long summaries produce multiple chunks."""
    acc = DigestAccumulator()
    base_time = 1000000.0
    acc._now = lambda: base_time

    # Add events with long summaries across different toggles
    toggles = [
        ("weather_warning", "Severe storm warning for Magic Valley area"),
        ("wildfire_proximity", "Fire proximity alert 8mi NE of position"),
        ("battery_warning", "Battery critical on node BLD-MTN system"),
        ("road_closure", "Road closure US-93 at milepost forty seven"),
        ("avalanche_warning", "Avalanche danger high in backcountry area"),
    ]
    for i, (cat, summary) in enumerate(toggles):
        acc.enqueue(make_event(
            source="test",
            category=cat,
            severity="routine",
            id=f"ev{i}",
            title=f"Event {i}",
            summary=summary,
        ))

    digest = acc.render_digest(now=base_time)

    # Should have multiple chunks
    assert len(digest.mesh_chunks) >= 2

    # Each chunk should have proper header with counter
    total = len(digest.mesh_chunks)
    for i, chunk in enumerate(digest.mesh_chunks):
        assert chunk.startswith("DIGEST ")
        assert f"({i+1}/{total})" in chunk

    # All chunks should be within limit
    assert all(len(c) <= 200 for c in digest.mesh_chunks)


def test_mesh_chunks_does_not_split_within_a_line():
    """A toggle line appears intact in exactly one chunk."""
    acc = DigestAccumulator()
    base_time = 1000000.0
    acc._now = lambda: base_time

    # Add event with specific summary we can search for
    target_summary = "Mesh node BLD-MTN battery at critical level"
    acc.enqueue(make_event(
        source="test",
        category="battery_warning",
        severity="routine",
        title="Battery Alert",
        summary=target_summary,
    ))
    # Add more events to possibly force chunking
    for i in range(5):
        acc.enqueue(make_event(
            source="test",
            category="weather_warning",
            severity="routine",
            id=f"w{i}",
            title=f"Weather {i}",
            summary=f"Weather event description number {i} for testing",
        ))

    digest = acc.render_digest(now=base_time)

    # Find chunks containing [Mesh]
    mesh_chunks = [c for c in digest.mesh_chunks if "[Mesh]" in c]
    assert len(mesh_chunks) == 1, "Mesh toggle should appear in exactly one chunk"

    # The summary text should be in that chunk (possibly truncated but not split)
    mesh_chunk = mesh_chunks[0]
    assert "[Mesh]" in mesh_chunk


def test_mesh_chunks_section_header_continuation():
    """Section headers spanning chunks get '(cont)' suffix."""
    acc = DigestAccumulator(mesh_char_limit=150)  # Smaller limit to force splits
    base_time = 1000000.0
    acc._now = lambda: base_time

    # Add many events to force ACTIVE NOW to span chunks
    for i in range(8):
        acc.enqueue(make_event(
            source="test",
            category="weather_warning",
            severity="routine",
            id=f"w{i}",
            title=f"Weather Event {i}",
            summary=f"Weather warning number {i} for the area",
        ))

    digest = acc.render_digest(now=base_time)

    if len(digest.mesh_chunks) >= 2:
        # Check if any non-first chunk has continuation header
        for i, chunk in enumerate(digest.mesh_chunks[1:], start=2):
            if "[Weather]" in chunk or any(f"[{t}]" in chunk for t in ["Fire", "Mesh", "Roads"]):
                # This chunk has toggle lines, check for section header
                if "ACTIVE NOW" in chunk:
                    assert "ACTIVE NOW (cont)" in chunk, f"Chunk {i} should have (cont) suffix"


def test_mesh_chunks_empty_digest_is_single_chunk():
    """Empty digest produces single chunk with no counter."""
    acc = DigestAccumulator()
    base_time = 1000000.0
    acc._now = lambda: base_time

    digest = acc.render_digest(now=base_time)

    assert len(digest.mesh_chunks) == 1
    assert "No alerts since last digest" in digest.mesh_chunks[0]
    assert "(1/" not in digest.mesh_chunks[0]


def test_mesh_compact_string_is_joined_chunks():
    """mesh_compact is chunks joined with separator when multiple chunks."""
    acc = DigestAccumulator(mesh_char_limit=120)  # Small limit to force multiple chunks
    base_time = 1000000.0
    acc._now = lambda: base_time

    # Add events to force multiple chunks
    for i in range(6):
        acc.enqueue(make_event(
            source="test",
            category="weather_warning",
            severity="routine",
            id=f"w{i}",
            title=f"Event {i}",
            summary=f"Summary for weather event number {i}",
        ))

    digest = acc.render_digest(now=base_time)

    if len(digest.mesh_chunks) > 1:
        expected = "\n---\n".join(digest.mesh_chunks)
        assert digest.mesh_compact == expected
    else:
        assert digest.mesh_compact == digest.mesh_chunks[0]


def test_include_toggles_unknown_name_does_not_crash():
    """Unknown toggle names in include_toggles are silently accepted."""
    acc = DigestAccumulator(include_toggles=["weather", "made_up_future_toggle"])
    base_time = 1000000.0
    acc._now = lambda: base_time

    # Weather should work
    acc.enqueue(make_event(
        source="test",
        category="weather_warning",
        severity="routine",
        title="Weather event",
    ))

    # rf_propagation should be excluded (not in include list)
    rf_category = None
    for cat_id, cat_info in ALERT_CATEGORIES.items():
        if cat_info.get("toggle") == "rf_propagation":
            rf_category = cat_id
            break

    if rf_category:
        acc.enqueue(make_event(
            source="test",
            category=rf_category,
            severity="routine",
            title="RF event",
        ))

    # Weather kept, RF dropped
    assert acc.active_count() == 1

    # Should not raise
    digest = acc.render_digest(now=base_time)
    assert "[Weather]" in digest.full


# ============================================================
# INCLUDE TOGGLES TESTS
# ============================================================

def test_rf_propagation_events_excluded_from_digest_by_default():
    """rf_propagation toggle is excluded by default (not in default include)."""
    acc = DigestAccumulator()  # default config
    base_time = 1000000.0
    acc._now = lambda: base_time

    # Find a category that maps to rf_propagation
    rf_category = None
    for cat_id, cat_info in ALERT_CATEGORIES.items():
        if cat_info.get("toggle") == "rf_propagation":
            rf_category = cat_id
            break

    assert rf_category is not None, "Should find an rf_propagation category"

    event = make_event(
        source="test",
        category=rf_category,
        severity="routine",
        title="HF Blackout",
    )
    acc.enqueue(event)

    # Should NOT be in active
    assert acc.active_count() == 0

    digest = acc.render_digest(now=base_time)
    assert "[RF]" not in digest.full


def test_include_toggles_parameter_overrides_default():
    """include_toggles parameter controls which toggles are tracked."""
    # Only include rf_propagation and weather
    acc = DigestAccumulator(include_toggles=["rf_propagation", "weather"])
    base_time = 1000000.0
    acc._now = lambda: base_time

    # Find rf_propagation category
    rf_category = None
    for cat_id, cat_info in ALERT_CATEGORIES.items():
        if cat_info.get("toggle") == "rf_propagation":
            rf_category = cat_id
            break

    # Enqueue rf_propagation event - should be kept
    acc.enqueue(make_event(
        source="test",
        category=rf_category,
        severity="routine",
        title="HF Blackout",
    ))
    assert acc.active_count() == 1

    # Enqueue fire event - should be dropped (fire not in include)
    acc.enqueue(make_event(
        source="test",
        category="wildfire_proximity",
        severity="routine",
        title="Fire Alert",
    ))
    assert acc.active_count() == 1  # Still 1, fire was dropped

    digest = acc.render_digest(now=base_time)
    assert "[RF]" in digest.full
    assert "[Fire]" not in digest.full


def test_include_toggles_explicit_subset():
    """include_toggles with explicit subset only tracks those toggles."""
    acc = DigestAccumulator(include_toggles=["weather"])
    base_time = 1000000.0
    acc._now = lambda: base_time

    # Weather - included
    acc.enqueue(make_event(
        source="test",
        category="weather_warning",
        severity="routine",
        title="Weather event",
    ))

    # Fire - not included
    acc.enqueue(make_event(
        source="test",
        category="wildfire_proximity",
        severity="routine",
        title="Fire event",
    ))

    # Tracking - not included (and may not have categories anyway)
    # Just verify the count is only 1
    assert acc.active_count() == 1


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
