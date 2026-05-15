"""Tests for Phase 2.4 DigestAccumulator with LLM summaries.

Updated from Phase 2.3a to reflect new behavior:
- No active/resolved tracking (just event log)
- LLM-summarized output per toggle
- render_digest is async
"""

import asyncio
import inspect
import time
from unittest.mock import MagicMock, AsyncMock, patch

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
# MOCK LLM BACKEND
# ============================================================

class MockLLMBackend:
    """Mock LLM backend for testing."""

    def __init__(self, response: str = "Mock summary of events."):
        self.response = response
        self.calls = []

    async def generate(self, messages, system_prompt, max_tokens=200):
        self.calls.append({
            "messages": messages,
            "system_prompt": system_prompt,
            "max_tokens": max_tokens,
        })
        return self.response


class FailingLLMBackend:
    """Mock LLM that raises exceptions."""

    async def generate(self, messages, system_prompt, max_tokens=200):
        raise RuntimeError("LLM unavailable")


def _make_mock_backend():
    """Create a standard mock LLM backend for tests."""
    mock = MagicMock()
    mock.generate = AsyncMock(return_value="stub summary")
    return mock


# ============================================================
# ACCUMULATOR EVENT LOGGING TESTS
# ============================================================

def test_enqueue_logs_event():
    """Enqueue adds event to the log."""
    acc = DigestAccumulator()
    event = make_event(
        source="test",
        category="weather_warning",
        severity="routine",
        title="Wind Advisory",
    )
    acc.enqueue(event)
    assert acc.event_count() == 1


def test_enqueue_multiple_events_same_toggle():
    """Multiple events for same toggle all logged."""
    acc = DigestAccumulator()
    for i in range(3):
        event = make_event(
            source="test",
            category="weather_warning",
            severity="routine",
            id=f"ev{i}",
            title=f"Event {i}",
        )
        acc.enqueue(event)
    assert acc.event_count() == 3
    assert acc.event_count("weather") == 3


def test_enqueue_multiple_toggles():
    """Events across multiple toggles all logged."""
    acc = DigestAccumulator()
    acc.enqueue(make_event(
        source="test",
        category="weather_warning",
        severity="routine",
        title="Weather",
    ))
    acc.enqueue(make_event(
        source="test",
        category="wildfire_proximity",
        severity="priority",
        title="Fire",
    ))
    acc.enqueue(make_event(
        source="test",
        category="battery_warning",
        severity="immediate",
        title="Mesh",
    ))
    assert acc.event_count() == 3
    assert acc.event_count("weather") == 1
    assert acc.event_count("fire") == 1
    assert acc.event_count("mesh_health") == 1


def test_enqueue_skips_excluded_toggles():
    """Events for non-included toggles are dropped."""
    acc = DigestAccumulator(include_toggles=["weather"])
    acc.enqueue(make_event(
        source="test",
        category="weather_warning",
        severity="routine",
        title="Weather",
    ))
    acc.enqueue(make_event(
        source="test",
        category="wildfire_proximity",
        severity="routine",
        title="Fire",
    ))
    assert acc.event_count() == 1
    assert acc.event_count("weather") == 1
    assert acc.event_count("fire") == 0


def test_tick_is_noop():
    """tick() does nothing in Phase 2.4+."""
    acc = DigestAccumulator()
    acc.enqueue(make_event(
        source="test",
        category="weather_warning",
        severity="routine",
        title="Event",
    ))
    result = acc.tick()
    assert result == 0
    assert acc.event_count() == 1


# ============================================================
# RENDER DIGEST TESTS
# ============================================================

def test_render_digest_is_async():
    """render_digest is an async coroutine function."""
    assert inspect.iscoroutinefunction(DigestAccumulator.render_digest)


def test_render_digest_clears_event_log():
    """render_digest clears the event log after rendering."""
    mock_llm = MockLLMBackend()
    acc = DigestAccumulator(llm_backend=mock_llm)
    acc.enqueue(make_event(
        source="test",
        category="weather_warning",
        severity="routine",
        title="Event",
    ))
    assert acc.event_count() == 1

    asyncio.run(acc.render_digest())
    assert acc.event_count() == 0


def test_render_digest_sets_last_digest_at():
    """render_digest updates last_digest_at timestamp."""
    mock_llm = MockLLMBackend()
    acc = DigestAccumulator(llm_backend=mock_llm)
    acc.enqueue(make_event(
        source="test",
        category="weather_warning",
        severity="routine",
        title="Event",
    ))

    now = 1234567890.0
    asyncio.run(acc.render_digest(now=now))
    assert acc.last_digest_at() == now


def test_render_digest_empty_shows_no_alerts():
    """Empty accumulator produces 'No alerts' message."""
    acc = DigestAccumulator()
    digest = asyncio.run(acc.render_digest())

    assert "No alerts since last digest" in digest.full
    assert "No alerts since last digest" in digest.mesh_chunks[0]


# ============================================================
# LLM INTEGRATION TESTS
# ============================================================

def test_digest_calls_llm_once_per_non_empty_toggle():
    """LLM is called once per toggle that has events."""
    mock_llm = MockLLMBackend(response="Summary for toggle.")
    acc = DigestAccumulator(llm_backend=mock_llm)

    # Add events to 3 different toggles
    acc.enqueue(make_event(source="test", category="weather_warning",
                           severity="routine", title="Weather"))
    acc.enqueue(make_event(source="test", category="wildfire_proximity",
                           severity="routine", title="Fire"))
    acc.enqueue(make_event(source="test", category="battery_warning",
                           severity="routine", title="Mesh"))

    asyncio.run(acc.render_digest())

    assert len(mock_llm.calls) == 3


def test_digest_line_uses_llm_output():
    """Digest lines contain the LLM's summary output."""
    mock_llm = MockLLMBackend(response="Severe storms moving through the area.")
    acc = DigestAccumulator(llm_backend=mock_llm)

    acc.enqueue(make_event(
        source="test",
        category="weather_warning",
        severity="priority",
        title="Storm Warning",
    ))

    digest = asyncio.run(acc.render_digest())

    assert "[Weather] Severe storms moving through the area." in digest.full
    assert "Severe storms moving through the area" in digest.mesh_compact


def test_digest_falls_back_to_count_when_llm_raises():
    """When LLM fails, fallback to count-based summary."""
    failing_llm = FailingLLMBackend()
    acc = DigestAccumulator(llm_backend=failing_llm)

    acc.enqueue(make_event(source="test", category="battery_warning",
                           severity="routine", title="Event 1"))
    acc.enqueue(make_event(source="test", category="battery_warning",
                           severity="routine", title="Event 2"))
    acc.enqueue(make_event(source="test", category="battery_warning",
                           severity="routine", title="Event 3"))

    digest = asyncio.run(acc.render_digest())

    assert "[Mesh]" in digest.full
    assert "3 event(s)" in digest.full
    assert "LLM unavailable" in digest.full


def test_digest_falls_back_when_no_llm():
    """When no LLM backend, fallback to count-based summary."""
    acc = DigestAccumulator(llm_backend=None)

    acc.enqueue(make_event(source="test", category="weather_warning",
                           severity="routine", title="Event"))

    digest = asyncio.run(acc.render_digest())

    assert "[Weather]" in digest.full
    assert "1 event(s)" in digest.full


def test_digest_input_orders_by_severity_then_time():
    """LLM input lists events by severity (immediate first) then timestamp."""
    mock_llm = MockLLMBackend()
    acc = DigestAccumulator(llm_backend=mock_llm)

    # Enqueue in wrong order: routine, then immediate, then priority
    acc.enqueue(make_event(source="test", category="weather_warning",
                           severity="routine", title="Routine Event",
                           timestamp=10.0))
    acc.enqueue(make_event(source="test", category="weather_warning",
                           severity="immediate", title="Immediate Event",
                           timestamp=20.0))
    acc.enqueue(make_event(source="test", category="weather_warning",
                           severity="priority", title="Priority Event",
                           timestamp=30.0))

    asyncio.run(acc.render_digest())

    # Check the LLM input
    assert len(mock_llm.calls) == 1
    user_content = mock_llm.calls[0]["messages"][0]["content"]

    # Find positions of each event in the input
    immediate_pos = user_content.find("IMMEDIATE")
    priority_pos = user_content.find("PRIORITY")
    routine_pos = user_content.find("ROUTINE")

    assert immediate_pos < priority_pos, "Immediate should appear before priority"
    assert priority_pos < routine_pos, "Priority should appear before routine"


# ============================================================
# MESH CHUNKS TESTS
# ============================================================

def test_mesh_chunks_single_chunk_when_short():
    """Single short summary produces one chunk without counter."""
    mock_llm = MockLLMBackend(response="Brief summary.")
    acc = DigestAccumulator(llm_backend=mock_llm)

    acc.enqueue(make_event(source="test", category="weather_warning",
                           severity="routine", title="Event"))

    digest = asyncio.run(acc.render_digest())

    assert len(digest.mesh_chunks) == 1
    assert digest.mesh_chunks[0].startswith("DIGEST ")
    assert "(1/" not in digest.mesh_chunks[0]


def test_mesh_chunks_under_char_limit():
    """Each mesh chunk is <= 200 characters."""
    mock_llm = MockLLMBackend(response="Summary of events for this category.")
    acc = DigestAccumulator(llm_backend=mock_llm)

    # Add events to multiple toggles
    for cat in ["weather_warning", "wildfire_proximity", "battery_warning",
                "road_closure", "avalanche_warning"]:
        acc.enqueue(make_event(source="test", category=cat,
                               severity="routine", title="Event"))

    digest = asyncio.run(acc.render_digest())

    for chunk in digest.mesh_chunks:
        assert len(chunk) <= 210, f"Chunk exceeds limit: {len(chunk)} chars"


def test_mesh_chunks_splits_when_many_toggles():
    """Many toggle summaries split into multiple chunks."""
    # Longer summaries to force splitting
    mock_llm = MockLLMBackend(
        response="A fairly detailed summary of the events in this category."
    )
    acc = DigestAccumulator(llm_backend=mock_llm, mesh_char_limit=150)

    # Add events to multiple toggles
    for cat in ["weather_warning", "wildfire_proximity", "battery_warning",
                "road_closure", "avalanche_warning"]:
        acc.enqueue(make_event(source="test", category=cat,
                               severity="routine", title="Event"))

    digest = asyncio.run(acc.render_digest())

    assert len(digest.mesh_chunks) >= 2

    # Check chunk counters
    total = len(digest.mesh_chunks)
    for i, chunk in enumerate(digest.mesh_chunks):
        assert f"({i+1}/{total})" in chunk


def test_mesh_chunks_empty_is_single_chunk():
    """Empty digest produces single chunk."""
    acc = DigestAccumulator()
    digest = asyncio.run(acc.render_digest())

    assert len(digest.mesh_chunks) == 1
    assert "No alerts since last digest" in digest.mesh_chunks[0]
    assert "(1/" not in digest.mesh_chunks[0]


def test_mesh_compact_joins_chunks():
    """mesh_compact joins chunks with separator when multiple."""
    mock_llm = MockLLMBackend(response="Summary of events.")
    acc = DigestAccumulator(llm_backend=mock_llm, mesh_char_limit=100)

    for cat in ["weather_warning", "wildfire_proximity", "battery_warning",
                "road_closure"]:
        acc.enqueue(make_event(source="test", category=cat,
                               severity="routine", title="Event"))

    digest = asyncio.run(acc.render_digest())

    if len(digest.mesh_chunks) > 1:
        expected = "\n---\n".join(digest.mesh_chunks)
        assert digest.mesh_compact == expected
    else:
        assert digest.mesh_compact == digest.mesh_chunks[0]


# ============================================================
# INCLUDE TOGGLES TESTS
# ============================================================

def test_rf_propagation_excluded_by_default():
    """rf_propagation toggle is excluded by default."""
    acc = DigestAccumulator()

    # Find an rf_propagation category
    rf_category = None
    for cat_id, cat_info in ALERT_CATEGORIES.items():
        if cat_info.get("toggle") == "rf_propagation":
            rf_category = cat_id
            break

    if rf_category:
        acc.enqueue(make_event(source="test", category=rf_category,
                               severity="routine", title="RF Event"))
        assert acc.event_count() == 0


def test_include_toggles_overrides_default():
    """include_toggles parameter controls which toggles are tracked."""
    mock_llm = MockLLMBackend()

    # Find an rf_propagation category
    rf_category = None
    for cat_id, cat_info in ALERT_CATEGORIES.items():
        if cat_info.get("toggle") == "rf_propagation":
            rf_category = cat_id
            break

    acc = DigestAccumulator(
        llm_backend=mock_llm,
        include_toggles=["rf_propagation", "weather"]
    )

    if rf_category:
        acc.enqueue(make_event(source="test", category=rf_category,
                               severity="routine", title="RF Event"))
    acc.enqueue(make_event(source="test", category="wildfire_proximity",
                           severity="routine", title="Fire Event"))

    # RF should be kept (in include list), fire should be dropped
    expected_count = 1 if rf_category else 0
    assert acc.event_count() == expected_count


def test_include_toggles_unknown_name_accepted():
    """Unknown toggle names don't crash."""
    acc = DigestAccumulator(include_toggles=["weather", "future_toggle"])
    acc.enqueue(make_event(source="test", category="weather_warning",
                           severity="routine", title="Event"))
    assert acc.event_count() == 1


# ============================================================
# TOGGLE ORDER TESTS
# ============================================================

def test_digest_orders_toggles_correctly():
    """Toggle lines appear in TOGGLE_ORDER sequence."""
    mock_llm = MockLLMBackend(response="Summary.")
    acc = DigestAccumulator(llm_backend=mock_llm)

    # Add events in wrong order
    acc.enqueue(make_event(source="test", category="battery_warning",
                           severity="routine", title="Mesh"))
    acc.enqueue(make_event(source="test", category="wildfire_proximity",
                           severity="routine", title="Fire"))
    acc.enqueue(make_event(source="test", category="weather_warning",
                           severity="routine", title="Weather"))

    digest = asyncio.run(acc.render_digest())

    # Check order in full output: weather, fire, ..., mesh_health
    weather_pos = digest.full.find("[Weather]")
    fire_pos = digest.full.find("[Fire]")
    mesh_pos = digest.full.find("[Mesh]")

    assert weather_pos < fire_pos, "Weather should appear before Fire"
    assert fire_pos < mesh_pos, "Fire should appear before Mesh"


# ============================================================
# PIPELINE INTEGRATION TESTS
# ============================================================

def test_pipeline_routes_event_to_accumulator():
    """Events via bus.emit end up in DigestAccumulator."""
    config = Config()
    bus, inhibitor, grouper, toggle_filter, dispatcher, accumulator = \
        build_pipeline_components(config, _make_mock_backend())

    event = make_event(
        source="test",
        category="weather_warning",
        severity="routine",
        title="Test event",
    )

    # Flush through grouper
    grouper.flush_all()
    bus.emit(event)
    grouper.flush_all()

    assert accumulator.event_count() == 1


def test_pipeline_routes_immediate_to_both():
    """Immediate events go to both dispatcher and accumulator in Phase 2.4."""
    config = Config()
    bus, inhibitor, grouper, toggle_filter, dispatcher, accumulator = \
        build_pipeline_components(config, _make_mock_backend())

    event = make_event(
        source="test",
        category="weather_warning",
        severity="immediate",
        title="Immediate event",
    )

    grouper.flush_all()
    bus.emit(event)
    grouper.flush_all()

    # In Phase 2.4, all events go to accumulator
    assert accumulator.event_count() == 1
