"""Test cases for Phase 2.1 notification pipeline skeleton.

These tests verify the core routing and dispatch behavior of the
notification pipeline without requiring real channel backends.

Updated in Phase 2.4: Events now go to BOTH dispatcher and accumulator
(no severity-based fork). SeverityRouter class kept for backward
compatibility but not used in production wiring.
"""

import pytest
from unittest.mock import Mock, patch
from dataclasses import dataclass, field

from meshai.notifications.events import Event, make_event
from meshai.notifications.pipeline import build_pipeline_components
from meshai.notifications.pipeline.bus import EventBus
from meshai.notifications.pipeline.dispatcher import Dispatcher
from meshai.notifications.pipeline.severity_router import SeverityRouter, StubDigestQueue


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
        mock_factory = Mock(return_value=mock_channel)
        bus = EventBus()
        dispatcher = Dispatcher(config, mock_factory)
        digest = StubDigestQueue()
        router = SeverityRouter(
            immediate_handler=dispatcher.dispatch,
            digest_handler=digest.enqueue,
        )
        bus.subscribe(router.handle)
        event = make_event(
            source="test",
            category="test_cat",
            severity="immediate",
            title="Test Alert",
            summary="Test summary message",
        )
        bus.emit(event)
        assert mock_channel.deliver.call_count == 1
        alert = mock_channel.deliver.call_args[0][0]
        assert alert["category"] == "test_cat"
        assert alert["severity"] == "immediate"
        assert alert["message"]


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
        mock_factory = Mock(return_value=mock_channel)

        # Create dispatcher and track calls
        dispatcher = Dispatcher(config, mock_factory)
        dispatch_calls = []
        original_dispatch = dispatcher.dispatch
        def tracking_dispatch(event):
            dispatch_calls.append(event)
            original_dispatch(event)
        dispatcher.dispatch = tracking_dispatch

        # Create accumulator mock
        accumulator_calls = []
        def mock_enqueue(event):
            accumulator_calls.append(event)

        # Tee closure (Phase 2.4 pattern)
        def tee(event):
            dispatcher.dispatch(event)
            mock_enqueue(event)

        bus = EventBus()
        bus.subscribe(tee)

        event = make_event(
            source="test",
            category="test_cat",
            severity="routine",
            title="Routine Alert",
        )
        bus.emit(event)

        # Both paths received the event
        assert len(dispatch_calls) == 1
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
        mock_factory = Mock(return_value=mock_channel)

        dispatcher = Dispatcher(config, mock_factory)
        dispatch_calls = []
        original_dispatch = dispatcher.dispatch
        def tracking_dispatch(event):
            dispatch_calls.append(event)
            original_dispatch(event)
        dispatcher.dispatch = tracking_dispatch

        accumulator_calls = []
        def mock_enqueue(event):
            accumulator_calls.append(event)

        def tee(event):
            dispatcher.dispatch(event)
            mock_enqueue(event)

        bus = EventBus()
        bus.subscribe(tee)

        event = make_event(
            source="test",
            category="test_cat",
            severity="priority",
            title="Priority Alert",
        )
        bus.emit(event)

        assert len(dispatch_calls) == 1
        assert len(accumulator_calls) == 1
        assert mock_channel.deliver.call_count == 1


class TestNoMatchingRule:

    def test_immediate_event_with_no_matching_rule_skips_silently(self):
        """Events with no matching rules don't crash."""
        config = ConfigStub(
            notifications=NotificationsConfigStub(rules=[])
        )
        mock_factory = Mock()
        bus = EventBus()
        dispatcher = Dispatcher(config, mock_factory)
        digest = StubDigestQueue()
        router = SeverityRouter(
            immediate_handler=dispatcher.dispatch,
            digest_handler=digest.enqueue,
        )
        bus.subscribe(router.handle)
        event = make_event(
            source="test",
            category="test_cat",
            severity="immediate",
            title="No Rule Alert",
        )
        bus.emit(event)
        mock_factory.assert_not_called()


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


class TestUnknownSeverity:

    def test_unknown_severity_dropped_without_crash(self):
        """Events with unknown severity are dropped gracefully."""
        config = ConfigStub(
            notifications=NotificationsConfigStub(rules=[])
        )
        mock_factory = Mock()
        bus = EventBus()
        dispatcher = Dispatcher(config, mock_factory)
        digest = StubDigestQueue()
        mock_dispatch = Mock()
        mock_enqueue = Mock()
        router = SeverityRouter(
            immediate_handler=mock_dispatch,
            digest_handler=mock_enqueue,
        )
        bus.subscribe(router.handle)
        event = Event(
            id="test123",
            source="test",
            category="test_cat",
            severity="bogus",
            title="Bogus Severity",
        )
        bus.emit(event)
        mock_dispatch.assert_not_called()
        mock_enqueue.assert_not_called()
