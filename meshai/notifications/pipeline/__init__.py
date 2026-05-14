"""Notification pipeline package.

Phase 2.1 skeleton:
  - EventBus: pub/sub for adapter ingress
  - SeverityRouter: forks immediate vs digest paths
  - Dispatcher: routes immediate events to channels via existing rules
  - StubDigestQueue: placeholder for Phase 2.3 aggregator

Usage:
    from meshai.notifications.pipeline import build_pipeline
    bus = build_pipeline(config)
    bus.emit(event)
"""

from meshai.notifications.channels import create_channel
from meshai.notifications.pipeline.bus import EventBus, get_bus
from meshai.notifications.pipeline.severity_router import (
    SeverityRouter,
    StubDigestQueue,
)
from meshai.notifications.pipeline.dispatcher import Dispatcher


def build_pipeline(config) -> EventBus:
    """Build the pipeline and return the EventBus.

    Adapters emit events to this bus and they flow through the
    severity router to either the dispatcher (immediate) or the
    digest stub (priority/routine).
    """
    bus = EventBus()
    dispatcher = Dispatcher(config, create_channel)
    digest = StubDigestQueue()
    severity_router = SeverityRouter(
        immediate_handler=dispatcher.dispatch,
        digest_handler=digest.enqueue,
    )
    bus.subscribe(severity_router.handle)
    return bus


def build_pipeline_components(config) -> tuple:
    """Like build_pipeline, but returns all components for test inspection.

    Returns (bus, dispatcher, digest, severity_router).
    """
    bus = EventBus()
    dispatcher = Dispatcher(config, create_channel)
    digest = StubDigestQueue()
    severity_router = SeverityRouter(
        immediate_handler=dispatcher.dispatch,
        digest_handler=digest.enqueue,
    )
    bus.subscribe(severity_router.handle)
    return bus, dispatcher, digest, severity_router


__all__ = [
    "EventBus",
    "SeverityRouter",
    "StubDigestQueue",
    "Dispatcher",
    "build_pipeline",
    "build_pipeline_components",
    "get_bus",
]
