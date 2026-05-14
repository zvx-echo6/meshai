"""Notification pipeline package.

Phase 2.1 provides the bare skeleton:
- EventBus: Central pub/sub for all events
- SeverityRouter: Routes immediate vs digest events
- Dispatcher: Delivers immediate events to channels
- StubDigestQueue: Placeholder for Phase 2.3 aggregator

Usage:
    from meshai.notifications.pipeline import build_pipeline

    pipeline = build_pipeline(channel_config={
        "mesh_health": ["discord"],
        "weather": ["discord", "meshtastic"],
    })

    # Emit events through the bus
    pipeline["bus"].emit(event)
"""

from meshai.notifications.pipeline.bus import EventBus, get_bus
from meshai.notifications.pipeline.severity_router import (
    SeverityRouter,
    StubDigestQueue,
)
from meshai.notifications.pipeline.dispatcher import (
    Dispatcher,
    StubChannelBackend,
)


def build_pipeline(
    channel_config: dict[str, list[str]] | None = None,
) -> dict:
    """Build and wire up the notification pipeline.

    Creates all pipeline components and connects them:
    - EventBus receives all events
    - SeverityRouter subscribes to bus, routes by severity
    - Dispatcher handles immediate events
    - StubDigestQueue collects priority/routine events

    Args:
        channel_config: Mapping of toggle -> channel names for dispatch.
                        Example: {"mesh_health": ["discord"]}

    Returns:
        Dict with all pipeline components:
        - bus: EventBus instance
        - router: SeverityRouter instance
        - dispatcher: Dispatcher instance
        - digest_queue: StubDigestQueue instance
    """
    # Create components
    bus = EventBus()
    dispatcher = Dispatcher(channel_config)
    digest_queue = StubDigestQueue()

    # Wire up the router
    router = SeverityRouter(
        immediate_handler=dispatcher.dispatch,
        digest_handler=digest_queue.enqueue,
    )

    # Subscribe router to bus
    bus.subscribe(router.handle)

    return {
        "bus": bus,
        "router": router,
        "dispatcher": dispatcher,
        "digest_queue": digest_queue,
    }


__all__ = [
    # Core classes
    "EventBus",
    "SeverityRouter",
    "Dispatcher",
    # Stubs for testing/Phase 2.x
    "StubDigestQueue",
    "StubChannelBackend",
    # Factory
    "build_pipeline",
    # Singleton accessor
    "get_bus",
]
