"""Abstract base class for mesh transport backends."""

import abc
import asyncio
from typing import Callable, Optional


class MeshTransport(abc.ABC):
    """Abstract interface every mesh transport must satisfy.

    The surface matches exactly what downstream code (main.py, router.py,
    responder.py, notification pipeline) calls on the connector today.
    Transport-specific internals (watchdog hooks, socket probing, etc.) live
    in the concrete subclass and are NOT part of this interface.
    """

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def connect(self) -> None:
        """Establish the transport connection."""

    @abc.abstractmethod
    def disconnect(self) -> None:
        """Close the transport connection."""

    # ------------------------------------------------------------------
    # Message I/O
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def send_message(
        self,
        text: str,
        destination: Optional[str] = None,
        channel: int = 0,
    ) -> bool:
        """Send a text message.

        Args:
            text: Message text to send.
            destination: Node ID for a DM, or None for broadcast.
            channel: Channel index to send on.

        Returns:
            True if send was initiated successfully.
        """

    @abc.abstractmethod
    def set_message_callback(
        self,
        callback: Callable,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        """Register a callback for incoming messages.

        Args:
            callback: Async callable invoked with a MeshMessage on each
                      received text message.
            loop: The running asyncio event loop used to schedule the
                  callback from a background thread.
        """

    # ------------------------------------------------------------------
    # Node identity / topology
    # ------------------------------------------------------------------

    @property
    @abc.abstractmethod
    def my_node_id(self) -> Optional[str]:
        """Our own node ID (hex string e.g. '!abcd1234'), or None."""

    @property
    @abc.abstractmethod
    def connected(self) -> bool:
        """True when the transport has an active connection."""

    @abc.abstractmethod
    def get_node_name(self, node_id: str) -> str:
        """Return the cached display name for *node_id*, or *node_id* itself."""

    @abc.abstractmethod
    def get_node_position(self, node_id: str) -> Optional[tuple]:
        """Return cached (latitude, longitude) for *node_id*, or None."""
