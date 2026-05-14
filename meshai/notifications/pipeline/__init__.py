"""Notification pipeline package.

Phase 2.1 + 2.2 + 2.3a:
  - EventBus: pub/sub ingress
  - Inhibitor: suppresses redundant events by inhibit_keys
  - Grouper: coalesces events sharing group_key within a window
  - SeverityRouter: forks immediate vs digest
  - Dispatcher: routes immediate via channels (existing rules schema)
  - DigestAccumulator: tracks priority/routine events for periodic digest

Usage:
    from meshai.notifications.pipeline import build_pipeline
    bus = build_pipeline(config)
    bus.emit(event)
"""

from meshai.notifications.channels import create_channel
from meshai.notifications.pipeline.bus import EventBus, get_bus
from meshai.notifications.pipeline.severity_router import (
    SeverityRouter,
    StubDigestQueue,  # kept for Phase 2.1 backward-compat tests
)
from meshai.notifications.pipeline.dispatcher import Dispatcher
from meshai.notifications.pipeline.inhibitor import Inhibitor
from meshai.notifications.pipeline.grouper import Grouper
from meshai.notifications.pipeline.digest import DigestAccumulator, Digest


def build_pipeline(config) -> EventBus:
    """Build the pipeline and return the EventBus."""
    bus = EventBus()
    dispatcher = Dispatcher(config, create_channel)
    digest = DigestAccumulator()
    severity_router = SeverityRouter(
        immediate_handler=dispatcher.dispatch,
        digest_handler=digest.enqueue,
    )
    grouper = Grouper(next_handler=severity_router.handle)
    inhibitor = Inhibitor(next_handler=grouper.handle)
    bus.subscribe(inhibitor.handle)
    return bus


def build_pipeline_components(config) -> tuple:
    """Like build_pipeline, but returns all components for tests.

    Returns (bus, inhibitor, grouper, severity_router, dispatcher, digest).
    """
    bus = EventBus()
    dispatcher = Dispatcher(config, create_channel)
    digest = DigestAccumulator()
    severity_router = SeverityRouter(
        immediate_handler=dispatcher.dispatch,
        digest_handler=digest.enqueue,
    )
    grouper = Grouper(next_handler=severity_router.handle)
    inhibitor = Inhibitor(next_handler=grouper.handle)
    bus.subscribe(inhibitor.handle)
    return bus, inhibitor, grouper, severity_router, dispatcher, digest


__all__ = [
    "EventBus",
    "SeverityRouter",
    "StubDigestQueue",
    "Dispatcher",
    "Inhibitor",
    "Grouper",
    "DigestAccumulator",
    "Digest",
    "build_pipeline",
    "build_pipeline_components",
    "get_bus",
]
