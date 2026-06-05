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
import logging

from meshai.notifications.channels import create_channel
from meshai.notifications.pipeline.bus import EventBus, get_bus
from meshai.notifications.pipeline.dispatcher import Dispatcher
try:
    from meshai.notifications.scheduled.band_conditions import (
        BandConditionsScheduler,
    )
except ImportError:
    BandConditionsScheduler = None
from meshai.notifications.pipeline.inhibitor import Inhibitor
from meshai.notifications.pipeline.grouper import Grouper
from meshai.notifications.pipeline.toggle_filter import ToggleFilter
from meshai.notifications.pipeline.digest import DigestAccumulator, Digest
from meshai.notifications.pipeline.scheduler import DigestScheduler


_logger = logging.getLogger("meshai.pipeline")


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

    # v0.5.13 toggle-enable read: iterate the family->NotificationToggle
    # dict and collect family names whose .enabled is True. The old
    # code did getattr(dict, "enabled", None) which is always None ->
    # ToggleFilter passed everything through, allowing the v0.5.7-regression
    # leak. The PRIMARY broadcast gate is now consumer._normalize()'s
    # default-deny rule; this ToggleFilter is a secondary user-pref filter.
    toggles_cfg = getattr(config.notifications, "toggles", None) or {}
    enabled_toggles = set()
    if isinstance(toggles_cfg, dict):
        for fam_name, tog in toggles_cfg.items():
            if getattr(tog, "enabled", False):
                enabled_toggles.add(str(fam_name))

    if not enabled_toggles:
        _logger.warning(
            "v0.5.13: zero toggle families are enabled -- ToggleFilter"
            " will drop everything (user disabled all families)."
        )
    else:
        _logger.info(
            "v0.5.13: ToggleFilter enabled families: %s",
            sorted(enabled_toggles),
        )

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
    # v0.5.11 band-conditions scheduler -- spawn alongside the
    # digest scheduler. Best-effort: failures in the scheduled
    # broadcaster must NOT break notifications pipeline startup.
    if BandConditionsScheduler is not None:
        try:
            comps = getattr(bus, "_pipeline_components", {}) or {}
            disp = comps.get("dispatcher")
            if disp is not None:
                bc_sched = BandConditionsScheduler(config, disp)
                await bc_sched.start()
                comps["band_conditions_scheduler"] = bc_sched
                bus._pipeline_components = comps
        except Exception:
            import logging as _lg
            _lg.getLogger("meshai.pipeline").exception(
                "band_conditions scheduler failed to start")


    # Phase 2.16.1: periodically flush the grouper so coalesced (routine/
    # priority) events are delivered within the window even when poll cadence
    # is sparse. Immediate events bypass the grouper and don't need this.
    grouper = components["grouper"]
    flush_interval = getattr(config.notifications, "grouper_flush_seconds", 5.0) or 5.0
    flush_stop = asyncio.Event()

    async def _grouper_flush_loop():
        while not flush_stop.is_set():
            try:
                await asyncio.wait_for(flush_stop.wait(), timeout=flush_interval)
                return
            except asyncio.TimeoutError:
                pass
            try:
                grouper.tick()
            except Exception:
                _logger.exception("Grouper flush tick failed")

    flush_task = asyncio.create_task(_grouper_flush_loop(), name="grouper-flush")
    scheduler._grouper_flush_task = flush_task
    scheduler._grouper_flush_stop = flush_stop
    _logger.info(f"Grouper flush task started (every {flush_interval:.0f}s)")

    # Stash scheduler for stop_pipeline
    bus._pipeline_scheduler = scheduler

    return scheduler


async def stop_pipeline(scheduler: DigestScheduler) -> None:
    """Stop the pipeline's async components.

    Args:
        scheduler: DigestScheduler returned by start_pipeline()
    """
    if scheduler is not None:
        flush_stop = getattr(scheduler, "_grouper_flush_stop", None)
        flush_task = getattr(scheduler, "_grouper_flush_task", None)
        if flush_stop is not None:
            flush_stop.set()
        if flush_task is not None:
            flush_task.cancel()
            try:
                await flush_task
            except (asyncio.CancelledError, Exception):
                pass
        await scheduler.stop()


__all__ = [
    "EventBus",
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
