"""Notification pipeline package.

Phase 2.4:
  - EventBus: pub/sub ingress
  - Inhibitor: suppresses redundant events by inhibit_keys
  - Grouper: coalesces events sharing group_key within a window
  - ToggleFilter: drops events whose toggle isn't enabled
  - Tee: sends events to both dispatcher and accumulator
  - Dispatcher: routes to channels based on rules
  - DigestAccumulator: logs events for LLM-summarized periodic digest
  - DigestScheduler: fires digest at configured time

Usage:
    from meshai.notifications.pipeline import build_pipeline, start_pipeline, stop_pipeline
    bus = build_pipeline(config, llm_backend)  # llm_backend from main.py
    bus.emit(event)

    # Async lifecycle
    scheduler = await start_pipeline(bus, config)
    ...
    await stop_pipeline(scheduler)
"""

import asyncio

from meshai.notifications.channels import create_channel
from meshai.notifications.pipeline.bus import EventBus, get_bus
from meshai.notifications.pipeline.severity_router import (
    SeverityRouter,
    StubDigestQueue,  # kept for Phase 2.1 backward-compat tests
)
from meshai.notifications.pipeline.dispatcher import Dispatcher
from meshai.notifications.pipeline.inhibitor import Inhibitor
from meshai.notifications.pipeline.grouper import Grouper
from meshai.notifications.pipeline.toggle_filter import ToggleFilter
from meshai.notifications.pipeline.digest import DigestAccumulator, Digest
from meshai.notifications.pipeline.scheduler import DigestScheduler


def build_pipeline(config, llm_backend, connector=None) -> EventBus:
    """Build the pipeline and return the EventBus.

    Args:
        config: Full Config object.
        llm_backend: An already-constructed LLMBackend instance
            (from main.py or a test). Pipeline components share
            this single instance. May be None for fallback behavior.
        connector: Optional MeshtasticConnector for mesh channels.

    Components are stashed on bus._pipeline_components for lifecycle use.
    """
    bus = EventBus()
    dispatcher = Dispatcher(config, create_channel, connector=connector)

    # Build include_toggles from config
    digest_cfg = getattr(config.notifications, "digest", None)
    include_toggles = None
    if digest_cfg is not None:
        include_list = getattr(digest_cfg, "include", None)
        if include_list:
            include_toggles = list(include_list)

    accumulator = DigestAccumulator(
        llm_backend=llm_backend,
        include_toggles=include_toggles,
    )

    # Tee closure: events go to BOTH dispatcher and accumulator
    # dispatcher.dispatch() is async, so fire-and-forget with create_task
    def _tee(event):
        try:
            asyncio.create_task(dispatcher.dispatch(event))
        except RuntimeError:
            # No running event loop (e.g. sync tests) - skip async dispatch
            pass
        accumulator.enqueue(event)

    # Build enabled toggles set from config
    toggles_cfg = getattr(config.notifications, "toggles", None)
    enabled_toggles = None
    if toggles_cfg is not None:
        enabled_list = getattr(toggles_cfg, "enabled", None)
        if enabled_list:
            enabled_toggles = set(enabled_list)

    toggle_filter = ToggleFilter(
        next_handler=_tee,
        enabled_toggles=enabled_toggles,
    )

    grouper = Grouper(next_handler=toggle_filter.handle)
    inhibitor = Inhibitor(next_handler=grouper.handle)
    bus.subscribe(inhibitor.handle)

    # Stash components for lifecycle management
    bus._pipeline_components = {
        "inhibitor": inhibitor,
        "grouper": grouper,
        "toggle_filter": toggle_filter,
        "dispatcher": dispatcher,
        "accumulator": accumulator,
        "connector": connector,
    }

    return bus


def build_pipeline_components(config, llm_backend, connector=None) -> tuple:
    """Like build_pipeline, but returns all components for tests.

    Args:
        config: Full Config object.
        llm_backend: An already-constructed LLMBackend instance
            (from main.py or a test). Pipeline components share
            this single instance. May be None for fallback behavior.
        connector: Optional MeshtasticConnector for mesh channels.

    Returns:
        (bus, inhibitor, grouper, toggle_filter, dispatcher, accumulator).
    """
    bus = EventBus()
    dispatcher = Dispatcher(config, create_channel, connector=connector)

    # Build include_toggles from config
    digest_cfg = getattr(config.notifications, "digest", None)
    include_toggles = None
    if digest_cfg is not None:
        include_list = getattr(digest_cfg, "include", None)
        if include_list:
            include_toggles = list(include_list)

    accumulator = DigestAccumulator(
        llm_backend=llm_backend,
        include_toggles=include_toggles,
    )

    # Tee closure: events go to BOTH dispatcher and accumulator
    # dispatcher.dispatch() is async, so fire-and-forget with create_task
    def _tee(event):
        try:
            asyncio.create_task(dispatcher.dispatch(event))
        except RuntimeError:
            # No running event loop (e.g. sync tests) - skip async dispatch
            pass
        accumulator.enqueue(event)

    # Build enabled toggles set from config
    toggles_cfg = getattr(config.notifications, "toggles", None)
    enabled_toggles = None
    if toggles_cfg is not None:
        enabled_list = getattr(toggles_cfg, "enabled", None)
        if enabled_list:
            enabled_toggles = set(enabled_list)

    toggle_filter = ToggleFilter(
        next_handler=_tee,
        enabled_toggles=enabled_toggles,
    )

    grouper = Grouper(next_handler=toggle_filter.handle)
    inhibitor = Inhibitor(next_handler=grouper.handle)
    bus.subscribe(inhibitor.handle)

    return bus, inhibitor, grouper, toggle_filter, dispatcher, accumulator


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

    accumulator = components["accumulator"]

    connector = components.get("connector")
    scheduler = DigestScheduler(
        accumulator=accumulator,
        config=config,
        channel_factory=create_channel,
        connector=connector,
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
    "ToggleFilter",
    "DigestAccumulator",
    "Digest",
    "DigestScheduler",
    "build_pipeline",
    "build_pipeline_components",
    "start_pipeline",
    "stop_pipeline",
    "get_bus",
]
