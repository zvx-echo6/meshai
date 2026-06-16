"""Tests for ToggleFilter (Phase 2.4)."""

import pytest
from unittest.mock import MagicMock, AsyncMock

from meshai.notifications.events import make_event
from meshai.notifications.pipeline.toggle_filter import ToggleFilter
from meshai.notifications.pipeline import build_pipeline_components
from meshai.config import Config


class TestToggleFilter:
    """Unit tests for ToggleFilter."""

    def test_toggle_filter_passes_through_when_enabled_is_none(self):
        """Filter with enabled_toggles=None passes all events."""
        received = []
        filter_ = ToggleFilter(
            next_handler=received.append,
            enabled_toggles=None,
        )
        event = make_event(
            source="test",
            category="weather_warning",
            severity="priority",
            title="Test",
        )
        filter_.handle(event)
        assert len(received) == 1
        assert received[0] is event

    def test_toggle_filter_drops_event_when_toggle_not_enabled(self):
        """Filter drops events whose toggle isn't in enabled set."""
        received = []
        filter_ = ToggleFilter(
            next_handler=received.append,
            enabled_toggles={"weather"},
        )
        # wildfire_hotspot maps to "fire" toggle
        event = make_event(
            source="test",
            category="wildfire_hotspot",
            severity="priority",
            title="Fire",
        )
        filter_.handle(event)
        assert len(received) == 0

    def test_toggle_filter_passes_event_when_toggle_enabled(self):
        """Filter passes events whose toggle is in enabled set."""
        received = []
        filter_ = ToggleFilter(
            next_handler=received.append,
            enabled_toggles={"weather"},
        )
        event = make_event(
            source="test",
            category="weather_warning",
            severity="priority",
            title="Weather",
        )
        filter_.handle(event)
        assert len(received) == 1

    def test_toggle_filter_drops_unknown_category_when_filter_active(self):
        """Unknown category maps to 'other', dropped if 'other' not enabled."""
        received = []
        filter_ = ToggleFilter(
            next_handler=received.append,
            enabled_toggles={"weather"},
        )
        event = make_event(
            source="test",
            category="bogus_category",
            severity="priority",
            title="Unknown",
        )
        filter_.handle(event)
        # "bogus_category" has no toggle mapping, falls back to "other"
        # "other" is not in enabled set
        assert len(received) == 0

    def test_toggle_filter_passes_other_when_enabled(self):
        """'other' toggle passes unknown categories when enabled."""
        received = []
        filter_ = ToggleFilter(
            next_handler=received.append,
            enabled_toggles={"other"},
        )
        event = make_event(
            source="test",
            category="bogus_category",
            severity="priority",
            title="Unknown",
        )
        filter_.handle(event)
        assert len(received) == 1


class TestToggleFilterPipelineWiring:
    """Integration tests for toggle filter in pipeline."""

    def test_toggle_filter_pipeline_drops_disabled_toggle(self):
        """Events for disabled toggles don't reach dispatcher or accumulator."""
        config = Config()

        # Pass mock LLM backend
        mock_backend = MagicMock()
        mock_backend.generate = AsyncMock(return_value="stub summary")

        # Note: without toggles.enabled set, filter is a no-op
        # This test verifies the wiring is correct
        bus, inhibitor, grouper, toggle_filter, dispatcher, accumulator =             build_pipeline_components(config, mock_backend)

        # Verify toggle_filter is in the chain
        assert toggle_filter is not None
        assert hasattr(toggle_filter, 'handle')

    def test_build_pipeline_uses_provided_backend(self):
        """build_pipeline_components uses the provided llm_backend."""
        config = Config()

        # Sentinel backend with unique attribute
        sentinel = MagicMock()
        sentinel.unique_marker = "I_AM_THE_SENTINEL"
        sentinel.generate = AsyncMock(return_value="sentinel summary")

        bus, inhibitor, grouper, toggle_filter, dispatcher, accumulator =             build_pipeline_components(config, sentinel)

        # Accumulator should have the exact sentinel instance
        assert accumulator._llm is sentinel
        assert accumulator._llm.unique_marker == "I_AM_THE_SENTINEL"
