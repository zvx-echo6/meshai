"""Immediate event dispatcher.

The dispatcher routes immediate-severity events to configured delivery
channels based on the event's toggle category.

Phase 2.1 provides a stub that logs dispatch attempts. Phase 2.2 will
add real channel backends (Discord webhooks, Meshtastic broadcast, etc.).

Usage:
    dispatcher = Dispatcher(channel_config)
    dispatcher.dispatch(event)  # Called by SeverityRouter for immediate events
"""

import logging
from typing import Callable, Optional

from meshai.notifications.events import Event
from meshai.notifications.categories import get_toggle


class Dispatcher:
    """Dispatches immediate events to configured channels.

    Each toggle category can have multiple delivery channels configured.
    The dispatcher looks up the toggle for an event's category and sends
    to all channels registered for that toggle.

    Phase 2.1: Stub implementation that logs but doesn't actually deliver.
    Phase 2.2: Will add real channel backends.
    """

    def __init__(
        self,
        channel_config: Optional[dict[str, list[str]]] = None,
    ):
        """Initialize the dispatcher.

        Args:
            channel_config: Mapping of toggle -> list of channel names.
                            Example: {"mesh_health": ["discord", "meshtastic"]}
                            If None, defaults to empty (no channels configured).
        """
        self._channels = channel_config or {}
        self._logger = logging.getLogger("meshai.pipeline.dispatcher")
        self._backends: dict[str, Callable[[Event], None]] = {}

    def register_backend(
        self,
        channel_name: str,
        handler: Callable[[Event], None],
    ) -> None:
        """Register a delivery backend for a channel.

        Args:
            channel_name: Name of the channel (e.g., "discord", "meshtastic")
            handler: Callable that delivers the event to the channel
        """
        self._backends[channel_name] = handler
        self._logger.debug(f"Registered backend: {channel_name}")

    def dispatch(self, event: Event) -> None:
        """Dispatch an immediate event to configured channels.

        Looks up the toggle for the event's category, then sends to
        all channels configured for that toggle.

        Args:
            event: The immediate-severity Event to dispatch
        """
        toggle = get_toggle(event.category)
        if toggle is None:
            self._logger.warning(
                f"Unknown category {event.category!r} for event {event.id}, "
                "defaulting to mesh_health"
            )
            toggle = "mesh_health"

        channels = self._channels.get(toggle, [])
        if not channels:
            self._logger.info(
                f"No channels configured for toggle {toggle!r}, "
                f"event {event.id} not dispatched"
            )
            return

        for channel in channels:
            self._deliver_to_channel(event, channel, toggle)

    def _deliver_to_channel(
        self,
        event: Event,
        channel: str,
        toggle: str,
    ) -> None:
        """Deliver event to a specific channel.

        Args:
            event: The Event to deliver
            channel: Channel name
            toggle: Toggle category (for logging)
        """
        backend = self._backends.get(channel)
        if backend is None:
            # Phase 2.1: Log stub - no real backend yet
            self._logger.info(
                f"DISPATCH STUB [{toggle}] -> {channel}: {event.title}"
            )
            return

        try:
            backend(event)
            self._logger.info(
                f"DISPATCHED [{toggle}] -> {channel}: {event.title}"
            )
        except Exception:
            self._logger.exception(
                f"Failed to dispatch event {event.id} to {channel}"
            )


class StubChannelBackend:
    """Stub channel backend for testing.

    Collects all events "sent" to it for verification in tests.
    """

    def __init__(self, name: str):
        self.name = name
        self.events: list[Event] = []
        self._logger = logging.getLogger(f"meshai.pipeline.stub.{name}")

    def send(self, event: Event) -> None:
        """Record an event as sent.

        Args:
            event: The Event to record
        """
        self.events.append(event)
        self._logger.info(f"STUB {self.name}: {event.title}")

    def clear(self) -> None:
        """Clear recorded events."""
        self.events = []
