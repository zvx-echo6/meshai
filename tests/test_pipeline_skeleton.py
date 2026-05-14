"""Test cases for Phase 2.1 notification pipeline skeleton.

These tests verify the core routing and dispatch behavior of the
notification pipeline without requiring real channel backends.
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


class TestDigestRouting:

    def test_routine_event_goes_to_digest_not_dispatcher(self):
        rule = NotificationRuleConfigStub(
            enabled=True,
            trigger_type="condition",
            categories=["test_cat"],
            min_severity="routine",
        )
        config = ConfigStub(
            notifications=NotificationsConfigStub(rules=[rule])
        )
        mock_factory = Mock()
        bus = EventBus()
        dispatcher = Dispatcher(config, mock_factory)
        digest = StubDigestQueue()
        with patch.object(dispatcher, "dispatch", wraps=dispatcher.dispatch) as mock_dispatch:
            router = SeverityRouter(
                immediate_handler=mock_dispatch,
                digest_handler=digest.enqueue,
            )
            bus.subscribe(router.handle)
            event = make_event(
                source="test",
                category="test_cat",
                severity="routine",
                title="Routine Alert",
            )
            bus.emit(event)
            assert len(digest) == 1
            mock_dispatch.assert_not_called()

    def test_priority_event_goes_to_digest_not_dispatcher(self):
        rule = NotificationRuleConfigStub(
            enabled=True,
            trigger_type="condition",
            categories=["test_cat"],
            min_severity="routine",
        )
        config = ConfigStub(
            notifications=NotificationsConfigStub(rules=[rule])
        )
        mock_factory = Mock()
        bus = EventBus()
        dispatcher = Dispatcher(config, mock_factory)
        digest = StubDigestQueue()
        with patch.object(dispatcher, "dispatch", wraps=dispatcher.dispatch) as mock_dispatch:
            router = SeverityRouter(
                immediate_handler=mock_dispatch,
                digest_handler=digest.enqueue,
            )
            bus.subscribe(router.handle)
            event = make_event(
                source="test",
                category="test_cat",
                severity="priority",
                title="Priority Alert",
            )
            bus.emit(event)
            assert len(digest) == 1
            mock_dispatch.assert_not_called()


class TestNoMatchingRule:

    def test_immediate_event_with_no_matching_rule_skips_silently(self):
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
