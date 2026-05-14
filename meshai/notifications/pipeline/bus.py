"""Event bus for the notification pipeline.

The bus is the entry point for all events flowing through the pipeline.
Adapters call bus.emit(event) to push Events into the system.

Usage:
    from meshai.notifications.pipeline import get_bus
    from meshai.notifications.events import make_event

    bus = get_bus()
    event = make_event(source="nws", category="weather_warning", severity="immediate", ...)
    bus.emit(event)
"""

import logging
from typing import Callable, Iterable

from meshai.notifications.events import Event


class EventBus:
    """Central event bus for the notification pipeline.

    Subscribers register handlers that receive every emitted event.
    Errors in one subscriber do not prevent other subscribers from
    receiving the event.
    """

    def __init__(self):
        self._subscribers: list[Callable[[Event], None]] = []
        self._logger = logging.getLogger("meshai.pipeline.bus")

    def subscribe(self, handler: Callable[[Event], None]) -> None:
        """Register a handler that receives every emitted event.

        Args:
            handler: Callable that takes an Event and returns None
        """
        self._subscribers.append(handler)
        self._logger.debug(f"Subscribed handler: {handler}")

    def emit(self, event: Event) -> None:
        """Push an event to all subscribers.

        Errors in one subscriber do not stop others from receiving
        the event. Exceptions are logged but not re-raised.

        Args:
            event: The Event to deliver to all subscribers
        """
        for handler in self._subscribers:
            try:
                handler(event)
            except Exception:
                self._logger.exception(
                    f"Subscriber {handler} failed on event {event.id}"
                )

    def emit_many(self, events: Iterable[Event]) -> None:
        """Emit multiple events in sequence.

        Args:
            events: Iterable of Events to emit
        """
        for event in events:
            self.emit(event)


# Module-level singleton for application-wide use
_bus: EventBus | None = None


def get_bus() -> EventBus:
    """Get the global EventBus singleton.

    This is the primary way adapters access the bus. Tests should
    construct a fresh EventBus() directly to avoid shared state.

    Returns:
        The global EventBus instance
    """
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus
