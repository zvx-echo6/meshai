"""Abstract base class for mesh transport backends."""

import abc
import asyncio
from typing import Callable, Optional
import concurrent.futures


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
        transport: Optional[str] = None,
        meshcore_channel: Optional[str] = None,
    ) -> bool:
        """Send a text message.

        Args:
            text: Message text to send.
            destination: Node ID for a DM, or None for broadcast.
            channel: Channel index to send on (Meshtastic semantics).
            transport: Optional routing hint (used by CompositeTransport to
                       select the child mesh that originated an inbound DM).
                       Single-transport implementations accept and IGNORE this
                       parameter; it is always None in non-composite callers.
            meshcore_channel: Per-family MeshCore channel NAME for broadcasts.
                       MeshtasticTransport ignores this; MeshCoreTransport
                       resolves the name to a companion slot at send time.
                       CompositeTransport uses it to route each child correctly.
                       None = do not broadcast on MeshCore for this family.

        Returns:
            True if send was initiated successfully.
        """

    async def send_message_async(
        self,
        text: str,
        destination: Optional[str] = None,
        channel: int = 0,
        transport: Optional[str] = None,
        meshcore_channel: Optional[str] = None,
    ) -> bool:
        """Async send through the per-radio serialized queue.

        Default implementation runs ``send_message()`` in the default thread
        executor so it never blocks the event loop.  Concrete subclasses with
        a send queue override this to enqueue the job and await the actual
        radio result.

        Returns:
            True if the send was accepted by the radio.
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            lambda: self.send_message(text, destination, channel, transport, meshcore_channel),
        )

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

    @property
    @abc.abstractmethod
    def max_chars(self) -> int:
        """Maximum characters per mesh packet for the active transport."""
        ...

    @abc.abstractmethod
    def get_node_name(self, node_id: str) -> str:
        """Return the cached display name for *node_id*, or *node_id* itself."""

    @abc.abstractmethod
    def get_node_position(self, node_id: str) -> Optional[tuple]:
        """Return cached (latitude, longitude) for *node_id*, or None."""
