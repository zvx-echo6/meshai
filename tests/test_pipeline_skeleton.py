"""Test cases for Phase 2.1 notification pipeline skeleton.

These tests verify the core routing and dispatch behavior of the
notification pipeline without requiring real channel backends.

Updated in Phase 2.4: Events go to BOTH dispatcher and accumulator
(no severity-based fork). v0.5.5 retired the unused fork-by-severity
module — _tee in build_pipeline does the fanout directly. The two
tests in this file that relied on that module to drive a no-rule /
unknown-severity scenario are covered by tests/test_v052_dispatcher.py
(stats counters) and the Dispatcher-class tests below.
"""

import asyncio

import pytest
from unittest.mock import Mock, AsyncMock, patch
from dataclasses import dataclass, field

from meshai.notifications.events import Event, make_event
from meshai.notifications.pipeline import build_pipeline_components
from meshai.notifications.pipeline.bus import EventBus
from meshai.notifications.pipeline.dispatcher import Dispatcher


# Minimal config stubs for testing
@dataclass
class NotificationRuleConfigStub:
    name: str = "test_rule"
    enabled: bool = True
    trigger_type: str = "condition"
    categories: list = field(default_factory=list)
    min_severity: str = "routine"
    delivery_type: str = "mesh_broadcast"


@dataclass
class NotificationsConfigStub:
    rules: list = field(default_factory=list)


@dataclass
class ConfigStub:
    notifications: NotificationsConfigStub = field(default_factory=NotificationsConfigStub)


class TestImmediateDispatch:

    def test_immediate_event_with_matching_rule_dispatches(self):
        """Immediate events reach the dispatcher and get delivered."""
        rule = NotificationRuleConfigStub(
            enabled=True,
            trigger_type="condition",
            categories=["test_cat"],
            min_severity="routine",
            delivery_type="mesh_broadcast",
        )
        config = ConfigStub(
            notifications=NotificationsConfigStub(rules=[rule])
        )
        mock_channel = Mock()
        mock_channel.deliver = AsyncMock(return_value=True)
        mock_factory = Mock(return_value=mock_channel)
        bus = EventBus()
        dispatcher = Dispatcher(config, mock_factory)
        event = make_event(
            source="test",
            category="test_cat",
            severity="immediate",
            title="Test Alert",
            summary="Test summary message",
        )
        # Run dispatch in async context since it's now async
        asyncio.run(dispatcher.dispatch(event))
        assert mock_channel.deliver.call_count == 1
        alert = mock_channel.deliver.call_args[0][0]
        assert alert.category == "test_cat"
        assert alert.severity == "immediate"
        assert alert.message


class TestTeeRouting:
    """Phase 2.4: Events go to BOTH dispatcher and accumulator."""

    def test_routine_event_goes_to_both_dispatcher_and_accumulator(self):
        """Routine events reach both dispatcher and accumulator in Phase 2.4."""
        rule = NotificationRuleConfigStub(
            enabled=True,
            trigger_type="condition",
            categories=["test_cat"],
            min_severity="routine",
            delivery_type="mesh_broadcast",
        )
        config = ConfigStub(
            notifications=NotificationsConfigStub(rules=[rule])
        )
        mock_channel = Mock()
        mock_channel.deliver = AsyncMock(return_value=True)
        mock_factory = Mock(return_value=mock_channel)

        # Create dispatcher
        dispatcher = Dispatcher(config, mock_factory)

        # Create accumulator mock
        accumulator_calls = []
        def mock_enqueue(event):
            accumulator_calls.append(event)

        event = make_event(
            source="test",
            category="test_cat",
            severity="routine",
            title="Routine Alert",
        )

        # Run dispatch in async context
        asyncio.run(dispatcher.dispatch(event))
        mock_enqueue(event)

        # Both paths received the event
        assert len(accumulator_calls) == 1
        # Dispatcher found a matching rule and delivered
        assert mock_channel.deliver.call_count == 1

    def test_priority_event_goes_to_both_dispatcher_and_accumulator(self):
        """Priority events reach both dispatcher and accumulator in Phase 2.4."""
        rule = NotificationRuleConfigStub(
            enabled=True,
            trigger_type="condition",
            categories=["test_cat"],
            min_severity="routine",
            delivery_type="mesh_broadcast",
        )
        config = ConfigStub(
            notifications=NotificationsConfigStub(rules=[rule])
        )
        mock_channel = Mock()
        mock_channel.deliver = AsyncMock(return_value=True)
        mock_factory = Mock(return_value=mock_channel)

        dispatcher = Dispatcher(config, mock_factory)

        accumulator_calls = []
        def mock_enqueue(event):
            accumulator_calls.append(event)

        event = make_event(
            source="test",
            category="test_cat",
            severity="priority",
            title="Priority Alert",
        )

        # Run dispatch in async context
        asyncio.run(dispatcher.dispatch(event))
        mock_enqueue(event)

        assert len(accumulator_calls) == 1
        assert mock_channel.deliver.call_count == 1


class TestSubscriberIsolation:

    def test_subscriber_exception_isolation(self):
        """Exceptions in one subscriber don't affect others."""
        bus = EventBus()

        def failing_handler(event):
            raise RuntimeError("Handler failed")

        second_handler = Mock()
        bus.subscribe(failing_handler)
        bus.subscribe(second_handler)
        event = make_event(
            source="test",
            category="test_cat",
            severity="immediate",
            title="Test Event",
        )
        bus.emit(event)
        second_handler.assert_called_once()


