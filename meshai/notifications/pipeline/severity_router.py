"""Severity-based event routing.

The severity router subscribes to the bus and forks each event into
one of two paths based on severity:

- immediate → immediate_handler (dispatcher for live delivery)
- priority/routine → digest_handler (queue for batched summaries)

Usage:
    router = SeverityRouter(
        immediate_handler=dispatcher.dispatch,
        digest_handler=digest_queue.enqueue,
    )
    bus.subscribe(router.handle)
"""

import logging
from typing import Callable

from meshai.notifications.events import Event
from meshai.notifications.categories import get_toggle


class SeverityRouter:
    """Routes events to immediate or digest handlers based on severity.

    Immediate-severity events go directly to live delivery channels.
    Priority and routine events are queued for periodic digest summaries.
    """

    def __init__(
        self,
        immediate_handler: Callable[[Event], None],
        digest_handler: Callable[[Event], None],
    ):
        """Initialize the severity router.

        Args:
            immediate_handler: Called for severity="immediate" events
            digest_handler: Called for severity in ("priority", "routine")
        """
        self._immediate = immediate_handler
        self._digest = digest_handler
        self._logger = logging.getLogger("meshai.pipeline.severity_router")

    def handle(self, event: Event) -> None:
        """Route an event based on its severity.

        Args:
            event: The Event to route
        """
        if event.severity == "immediate":
            self._logger.info(
                f"IMMEDIATE: {event.source}/{event.category} {event.title}"
            )
            self._immediate(event)
        elif event.severity in ("priority", "routine"):
            self._logger.info(
                f"DIGEST QUEUED [{event.severity}]: {event.title}"
            )
            self._digest(event)
        else:
            self._logger.warning(
                f"Unknown severity {event.severity!r} on event {event.id}, dropping"
            )


class StubDigestQueue:
    """Placeholder digest queue for Phase 2.1.

    This is a stub that simply collects events in memory. Phase 2.3
    will replace this with the real aggregator that renders and
    delivers periodic digest summaries.
    """

    def __init__(self):
        self._queue: list[Event] = []
        self._logger = logging.getLogger("meshai.pipeline.digest_stub")

    def enqueue(self, event: Event) -> None:
        """Add an event to the digest queue.

        Args:
            event: The Event to queue for digest delivery
        """
        self._queue.append(event)
        toggle = get_toggle(event.category) or "unknown"
        self._logger.info(f"DIGEST QUEUED [{toggle}]: {event.title}")

    def drain(self) -> list[Event]:
        """Return and clear all queued events.

        For tests and the future aggregator. Returns the current
        queue contents and resets the queue to empty.

        Returns:
            List of all queued Events
        """
        events, self._queue = self._queue, []
        return events

    def __len__(self) -> int:
        """Return the number of queued events."""
        return len(self._queue)
