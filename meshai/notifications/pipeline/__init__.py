"""Notification pipeline package.

Phase 2.1 + 2.2 + 2.3a + 2.3b:
  - EventBus: pub/sub ingress
  - Inhibitor: suppresses redundant events by inhibit_keys
  - Grouper: coalesces events sharing group_key within a window
  - SeverityRouter: forks immediate vs digest
  - Dispatcher: routes immediate via channels (existing rules schema)
  - DigestAccumulator: tracks priority/routine events for periodic digest
  - DigestScheduler: fires digest at configured time (Phase 2.3b)

Usage:
    from meshai.notifications.pipeline import build_pipeline, start_pipeline, stop_pipeline
    bus = build_pipeline(config)
    bus.emit(event)

    # Async lifecycle
    scheduler = await start_pipeline(bus, config)
    ...
    await stop_pipeline(scheduler)
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
from meshai.notifications.pipeline.scheduler import DigestScheduler


def build_pipeline(config) -> EventBus:
    """Build the pipeline and return the EventBus.

    Components are stashed on bus._pipeline_components for lifecycle use.
    """
    bus = EventBus()
    dispatcher = Dispatcher(config, create_channel)

    # Build include_toggles from config
    digest_cfg = getattr(config.notifications, "digest", None)
    include_toggles = None
    if digest_cfg is not None:
        include_list = getattr(digest_cfg, "include", None)
        if include_list:
            include_toggles = list(include_list)

    digest = DigestAccumulator(include_toggles=include_toggles)
    severity_router = SeverityRouter(
        immediate_handler=dispatcher.dispatch,
        digest_handler=digest.enqueue,
    )
    grouper = Grouper(next_handler=severity_router.handle)
    inhibitor = Inhibitor(next_handler=grouper.handle)
    bus.subscribe(inhibitor.handle)

    # Stash components for lifecycle management
    bus._pipeline_components = {
        "inhibitor": inhibitor,
        "grouper": grouper,
        "severity_router": severity_router,
        "dispatcher": dispatcher,
        "digest": digest,
    }

    return bus


def build_pipeline_components(config) -> tuple:
    """Like build_pipeline, but returns all components for tests.

    Returns (bus, inhibitor, grouper, severity_router, dispatcher, digest).
    """
    bus = EventBus()
    dispatcher = Dispatcher(config, create_channel)

    # Build include_toggles from config
    digest_cfg = getattr(config.notifications, "digest", None)
    include_toggles = None
    if digest_cfg is not None:
        include_list = getattr(digest_cfg, "include", None)
        if include_list:
            include_toggles = list(include_list)

    digest = DigestAccumulator(include_toggles=include_toggles)
    severity_router = SeverityRouter(
        immediate_handler=dispatcher.dispatch,
        digest_handler=digest.enqueue,
    )
    grouper = Grouper(next_handler=severity_router.handle)
    inhibitor = Inhibitor(next_handler=grouper.handle)
    bus.subscribe(inhibitor.handle)
    return bus, inhibitor, grouper, severity_router, dispatcher, digest


async def start_pipeline(bus: EventBus, config) -> DigestScheduler:
    """Start the pipeline's async components (scheduler).

    Args:
        bus: EventBus returned by build_pipeline()
        config: Config object with notifications.digest settings

    Returns:
        DigestScheduler instance (running). Call stop_pipeline() to stop.
    """
    components = getattr(bus, "_pipeline_components", None)
    if components is None:
        raise RuntimeError("bus missing _pipeline_components; use build_pipeline()")

    digest = components["digest"]

    scheduler = DigestScheduler(
        accumulator=digest,
        config=config,
        channel_factory=create_channel,
    )
    await scheduler.start()

    # Stash scheduler for stop_pipeline
    bus._pipeline_scheduler = scheduler

    return scheduler


async def stop_pipeline(scheduler: DigestScheduler) -> None:
    """Stop the pipeline's async components.

    Args:
        scheduler: DigestScheduler returned by start_pipeline()
    """
    if scheduler is not None:
        await scheduler.stop()


__all__ = [
    "EventBus",
    "SeverityRouter",
    "StubDigestQueue",
    "Dispatcher",
    "Inhibitor",
    "Grouper",
    "DigestAccumulator",
    "Digest",
    "DigestScheduler",
    "build_pipeline",
    "build_pipeline_components",
    "start_pipeline",
    "stop_pipeline",
    "get_bus",
]
