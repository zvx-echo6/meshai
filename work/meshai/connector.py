"""Meshtastic connection management for MeshAI."""

import asyncio
import logging
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import meshtastic
import meshtastic.serial_interface
import meshtastic.tcp_interface
from meshtastic import BROADCAST_NUM
from pubsub import pub

from .config import ConnectionConfig
from .transport.base import MeshTransport
from .transport.send_queue import RadioSendQueue

logger = logging.getLogger(__name__)


@dataclass
class MeshMessage:
    """Represents an incoming mesh message."""

    sender_id: str  # Node ID (hex string like "!abcd1234")
    sender_name: str  # Short name or long name
    text: str  # Message content
    channel: int  # Channel index
    is_dm: bool  # True if direct message to us
    # Raw packet for additional data.  Optional so non-Meshtastic transports
    # (MeshCore, etc.) can leave it None while sharing the same dataclass.
    packet: Optional[dict] = None
    # Transport tag so consumers can branch on origin if needed.
    transport: str = "meshtastic"
    _position: Optional[tuple[float, float]] = field(default=None, repr=False, init=False)

    @property
    def sender_position(self) -> Optional[tuple[float, float]]:
        """Get sender's GPS position if available (lat, lon)."""
        return self._position


class MeshtasticTransport(MeshTransport):
    """Manages connection to a Meshtastic node (Meshtastic transport backend)."""

    # Name tag used by CompositeTransport for routing hints.
    transport_name: str = "meshtastic"

    def __init__(self, config: ConnectionConfig):
        self.config = config
        self._interface: Optional[meshtastic.MeshInterface] = None
        self._my_node_id: Optional[str] = None
        self._message_callback: Optional[Callable[[MeshMessage], None]] = None
        self._node_positions: dict[str, tuple[float, float]] = {}
        self._node_names: dict[str, str] = {}
        self._connected = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock = threading.Lock()
        # --- watchdog link-state (watchdog is the SINGLE source of truth) ---
        self._last_rx: float = time.monotonic()
        self._link_suspect: bool = False
        self._wake: Optional[asyncio.Event] = None  # created by main once loop exists
        self._link_path = "/tmp/meshai.link"
        self._reconnect_lock = threading.Lock()
        # --- per-radio send queue (serialized + paced) ---
        self._mt_queue: Optional[RadioSendQueue] = None

    @property
    def connected(self) -> bool:
        """Check if connected to node."""
        return self._connected and self._interface is not None

    @property
    def my_node_id(self) -> Optional[str]:
        """Get our node's ID."""
        return self._my_node_id

    @property
    def max_chars(self) -> int:
        return self.config.mesh_max_chars

    def connect(self) -> None:
        """Establish connection to Meshtastic node."""
        logger.info(f"Connecting to Meshtastic node via {self.config.type}...")

        try:
            if self.config.type == "serial":
                self._interface = meshtastic.serial_interface.SerialInterface(
                    devPath=self.config.serial_port
                )
            elif self.config.type == "tcp":
                self._interface = meshtastic.tcp_interface.TCPInterface(
                    hostname=self.config.tcp_host, portNumber=self.config.tcp_port
                )
            else:
                raise ValueError(f"Unknown connection type: {self.config.type}")

            # Get our node info
            my_info = self._interface.getMyNodeInfo()
            self._my_node_id = f"!{my_info['num']:08x}"
            logger.info(f"Connected as node {self._my_node_id}")

            # Cache node info
            self._cache_node_info()

            # Subscribe to messages
            pub.subscribe(self._on_receive, "meshtastic.receive.text")
            pub.subscribe(self._on_node_update, "meshtastic.node.updated")
            pub.subscribe(self._on_any_rx, "meshtastic.receive")
            pub.subscribe(self._on_connection_lost, "meshtastic.connection.lost")
            self._last_rx = time.monotonic()

            self._connected = True

        except Exception as e:
            logger.error(f"Failed to connect: {e}")
            self._connected = False
            raise

    def disconnect(self) -> None:
        """Close connection to Meshtastic node."""
        # Stop the send queue drain and wait for it to finish so all pending
        # futures are resolved before the interface is torn down.
        if self._mt_queue is not None and self._mt_queue.running and self._loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(
                    self._mt_queue.stop(), self._loop
                ).result(timeout=5.0)
            except Exception:
                pass
        if self._interface:
            try:
                pub.unsubscribe(self._on_receive, "meshtastic.receive.text")
                pub.unsubscribe(self._on_node_update, "meshtastic.node.updated")
                pub.unsubscribe(self._on_any_rx, "meshtastic.receive")
                pub.unsubscribe(self._on_connection_lost, "meshtastic.connection.lost")
            except Exception:
                pass

            try:
                self._interface.close()
            except Exception as e:
                logger.warning(f"Error closing interface: {e}")

            self._interface = None
            self._connected = False
            logger.info("Disconnected from Meshtastic node")

    def set_message_callback(
        self, callback: Callable[[MeshMessage], None], loop: asyncio.AbstractEventLoop
    ) -> None:
        """Set callback for incoming messages and start the send queue drain.

        Args:
            callback: Async function to call with MeshMessage
            loop: Event loop to schedule callback on
        """
        self._message_callback = callback
        self._loop = loop
        # Start the per-radio send queue on the main event loop.
        pacing_fn = lambda: max(0.25, getattr(self.config, "meshtastic_send_pacing_seconds", 2.0))
        self._mt_queue = RadioSendQueue(pacing_fn=pacing_fn)

        def _start_drain() -> None:
            self._mt_queue.start(loop)

        loop.call_soon_threadsafe(_start_drain)

    def _cache_node_info(self) -> None:
        """Cache node names and positions from node database."""
        if not self._interface:
            return

        with self._lock:
            for node_id, node in self._interface.nodes.items():
                # Cache name
                if user := node.get("user"):
                    name = user.get("shortName") or user.get("longName") or node_id
                    self._node_names[node_id] = name

                # Cache position
                if position := node.get("position"):
                    lat = position.get("latitude")
                    lon = position.get("longitude")
                    if lat is not None and lon is not None:
                        self._node_positions[node_id] = (lat, lon)

    def _on_node_update(self, node, interface) -> None:
        """Handle node info updates."""
        self._last_rx = time.monotonic()
        node_id = f"!{node['num']:08x}"

        with self._lock:
            # Update name cache
            if user := node.get("user"):
                name = user.get("shortName") or user.get("longName") or node_id
                self._node_names[node_id] = name

            # Update position cache
            if position := node.get("position"):
                lat = position.get("latitude")
                lon = position.get("longitude")
                if lat is not None and lon is not None:
                    self._node_positions[node_id] = (lat, lon)

    def _on_receive(self, packet, interface) -> None:
        """Handle incoming text message."""
        self._last_rx = time.monotonic()
        if not self._message_callback or not self._loop:
            return

        try:
            # Extract message details
            sender_num = packet.get("fromId") or f"!{packet['from']:08x}"
            to_num = packet.get("toId") or f"!{packet['to']:08x}"
            decoded = packet.get("decoded", {})
            text = decoded.get("text", "")
            channel = packet.get("channel", 0)

            if not text:
                return

            # Determine if DM (sent directly to us, not broadcast)
            is_dm = to_num == self._my_node_id

            with self._lock:
                # Get sender name
                sender_name = self._node_names.get(sender_num, sender_num)
                # Get position if available
                position = self._node_positions.get(sender_num)

            # Create message object
            msg = MeshMessage(
                sender_id=sender_num,
                sender_name=sender_name,
                text=text,
                channel=channel,
                is_dm=is_dm,
                packet=packet,
            )

            # Attach position if available
            if position:
                msg._position = position

            # Schedule callback on event loop
            self._loop.call_soon_threadsafe(
                lambda m=msg: asyncio.create_task(self._message_callback(m))
            )

        except Exception as e:
            logger.error(f"Error processing received message: {e}")

    # --- watchdog: receive ANY packet bumps last_rx (liveness) ---
    def _on_any_rx(self, packet=None, interface=None, *args, **kwargs):
        self._last_rx = time.monotonic()

    # --- watchdog: connection.lost ONLY flags suspect + wakes watchdog.
    # Strict pubsub arg-spec passes interface= -> signature MUST accept it.
    # Does NOT write link state, NOT toggle _connected, NOT reconnect.
    def _on_connection_lost(self, interface=None, *args, **kwargs):
        self._link_suspect = True
        logger.warning("connection.lost fired -> link suspect (waking watchdog)")
        if self._wake is not None and self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._wake.set)
            except Exception:
                pass

    @property
    def last_rx(self) -> float:
        return self._last_rx

    @property
    def link_suspect(self) -> bool:
        return self._link_suspect

    def clear_suspect(self) -> None:
        self._link_suspect = False

    def write_link_status(self, status: str) -> None:
        """SINGLE writer of the link-state file. Called ONLY by the watchdog."""
        try:
            with open(self._link_path, "w") as f:
                f.write(status)
        except Exception as e:
            logger.warning(f"Failed to write link status: {e}")

    # --- watchdog: socket-based liveness (getMyNodeInfo is NOT trusted; it
    # returns cached data on a dead link -> false-alive). Verified meshtastic 2.7.9.
    def socket_state(self) -> str:
        """'open', or 'dead' from isConnected + non-blocking MSG_PEEK."""
        iface = self._interface
        if iface is None:
            return "dead"
        ev = getattr(iface, "isConnected", None)
        if ev is not None and hasattr(ev, "is_set") and not ev.is_set():
            return "dead"
        sock = getattr(iface, "socket", None)
        if sock is None:
            return "dead"
        try:
            data = sock.recv(1, socket.MSG_DONTWAIT | socket.MSG_PEEK)
            return "open" if data else "dead"
        except BlockingIOError:
            return "open"
        except (OSError, AttributeError):
            return "dead"

    def interface_open(self) -> bool:
        return self.socket_state() == "open"

    def active_probe(self, probe_wait: float = 5.0) -> bool:
        """sendHeartbeat then watch for genuine life. BLOCKING -> call via to_thread.
        ALIVE = last_rx advances OR socket stays verifiably open. Never trusts getMyNodeInfo."""
        if self.socket_state() == "dead":
            logger.info("probe: DEAD (socket closed / isConnected cleared)")
            return False
        rx_before = self._last_rx
        try:
            self._interface.sendHeartbeat()
        except Exception as e:
            logger.info(f"probe: sendHeartbeat raised {e!r} -> link dead")
            return False
        deadline = time.monotonic() + probe_wait
        while time.monotonic() < deadline:
            if self._last_rx > rx_before:
                logger.info("probe: ALIVE (last_rx advanced)")
                return True
            if self.socket_state() == "dead":
                logger.info("probe: DEAD (socket went dead during probe)")
                return False
            time.sleep(0.25)
        if self.socket_state() == "open":
            logger.info("probe: ALIVE (socket verifiably open)")
            return True
        logger.info("probe: DEAD (no rx advance, socket not open)")
        return False

    def reconnect(self) -> None:
        """Tear down old interface + build a fresh one under the reconnect lock,
        exponential backoff forever. BLOCKING -> call via to_thread. Does NOT
        write link state (watchdog owns that)."""
        initial = getattr(self.config, "reconnect_initial_delay", 2.0)
        maxd = getattr(self.config, "reconnect_max_delay", 60.0)
        with self._reconnect_lock:
            backoff = initial
            attempt = 0
            while True:
                attempt += 1
                logger.warning(f"reconnect attempt {attempt}...")
                # tear down old interface (also stops the lib's internal threads)
                try:
                    self.disconnect()
                except Exception as e:
                    logger.warning(f"  teardown warn: {e!r}")
                # build fresh
                try:
                    self.connect()
                    self._last_rx = time.monotonic()
                    self._link_suspect = False
                    logger.info(f"reconnected as node {self._my_node_id} (attempt {attempt})")
                    return
                except Exception as e:
                    logger.warning(f"  reconnect attempt {attempt} failed: {e!r}; sleeping {backoff:.0f}s")
                    time.sleep(backoff)
                    backoff = min(backoff * 2, maxd)

    async def send_message_async(
        self,
        text: str,
        destination: Optional[str] = None,
        channel: int = 0,
        transport: Optional[str] = None,
        meshcore_channel: Optional[str] = None,
    ) -> bool:
        """Async send through the Meshtastic per-radio queue.

        Enqueues the send job and awaits the ACTUAL radio-acceptance result so
        the caller (e.g. channel.deliver) gets the real success bool.  Falls
        back to a direct executor call if the queue has not been started yet
        (e.g. during tests or before set_message_callback is called).
        """
        if self._mt_queue is None or not self._mt_queue.running:
            # Queue not started: run in executor to avoid blocking the loop.
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None,
                lambda: self.send_message(text, destination, channel, transport, meshcore_channel),
            )

        def _job() -> bool:
            return self._blocking_mt_send(text, destination, channel)

        async def _async_job() -> bool:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, _job)

        return await self._mt_queue.enqueue_async(_async_job)

    def _blocking_mt_send(
        self,
        text: str,
        destination: Optional[str],
        channel: int,
    ) -> bool:
        """Synchronous Meshtastic send — called from the thread executor by the drain."""
        if not self._interface:
            logger.error("Cannot send: not connected")
            return False
        try:
            if destination:
                if isinstance(destination, int):
                    dest_num = destination
                elif destination.startswith("!"):
                    dest_num = int(destination[1:], 16)
                elif destination.isdigit():
                    dest_num = int(destination)
                else:
                    dest_num = int(destination, 16)
                self._interface.sendText(text=text, destinationId=dest_num, channelIndex=channel)
            else:
                from meshtastic import BROADCAST_NUM
                self._interface.sendText(text=text, destinationId=BROADCAST_NUM, channelIndex=channel)
            logger.debug("MT send to %s: %s…", destination or "broadcast", text[:50])
            return True
        except Exception as exc:
            logger.error("MT send failed: %s", exc)
            return False

    def send_message(
        self,
        text: str,
        destination: Optional[str] = None,
        channel: int = 0,
        transport: Optional[str] = None,  # routing hint — accepted and IGNORED by single-transport impl
        meshcore_channel: Optional[str] = None,  # per-family MeshCore channel — accepted and IGNORED here
    ) -> bool:
        """Send a text message.

        Args:
            text: Message text to send
            destination: Node ID for DM, or None for broadcast
            channel: Channel index to send on
            transport: Optional routing hint (for CompositeTransport); ignored here.
            meshcore_channel: Per-family MeshCore channel name; ignored by Meshtastic.

        Returns:
            True if send was initiated successfully
        """
        if not self._interface:
            logger.error("Cannot send: not connected")
            return False

        try:
            if destination:
                # DM to specific node - handle int or string
                if isinstance(destination, int):
                    dest_num = destination
                elif destination.startswith("!"):
                    dest_num = int(destination[1:], 16)
                elif destination.isdigit():
                    dest_num = int(destination)
                else:
                    dest_num = int(destination, 16)

                self._interface.sendText(
                    text=text,
                    destinationId=dest_num,
                    channelIndex=channel,
                )
            else:
                # Broadcast
                self._interface.sendText(
                    text=text,
                    destinationId=BROADCAST_NUM,
                    channelIndex=channel,
                )

            logger.debug(f"Sent message to {destination or 'broadcast'}: {text[:50]}...")
            return True

        except Exception as e:
            logger.error(f"Failed to send message: {e}")
            return False

    def get_node_position(self, node_id: str) -> Optional[tuple[float, float]]:
        """Get cached position for a node.

        Args:
            node_id: Node ID (hex string like "!abcd1234")

        Returns:
            Tuple of (latitude, longitude) or None if not available
        """
        with self._lock:
            return self._node_positions.get(node_id)

    def get_node_name(self, node_id: str) -> str:
        """Get cached name for a node.

        Args:
            node_id: Node ID (hex string like "!abcd1234")

        Returns:
            Node name or the node ID if name not available
        """
        with self._lock:
            return self._node_names.get(node_id, node_id)
    def send_and_wait_ack(
        self,
        text: str,
        destination: Optional[str] = None,
        channel: int = 0,
        timeout: float = 30.0,
    ) -> bool:
        """Send a text message and wait for ACK.

        Args:
            text: Message text
            destination: Node ID for DM
            channel: Channel index
            timeout: Seconds to wait for ACK

        Returns:
            True if ACK received, False if timeout
        """
        if not self._interface:
            logger.error("Cannot send: not connected")
            return False

        ack_event = threading.Event()
        ack_success = [False]

        def onAckNak(packet):
            # Check if this is an ACK (not a NACK or error)
            routing = packet.get("decoded", {}).get("routing", {})
            error_reason = routing.get("errorReason")
            if error_reason is None or error_reason == "NONE":
                ack_success[0] = True
            else:
                logger.warning(f"Message NACK: {error_reason}")
            ack_event.set()

        try:
            if destination:
                if destination.startswith("!"):
                    dest_num = int(destination[1:], 16)
                else:
                    dest_num = int(destination, 16)

                self._interface.sendText(
                    text=text,
                    destinationId=dest_num,
                    channelIndex=channel,
                    wantAck=True,
                    onResponse=onAckNak,
                )
            else:
                self._interface.sendText(
                    text=text,
                    destinationId=BROADCAST_NUM,
                    channelIndex=channel,
                    wantAck=True,
                    onResponse=onAckNak,
                )

            # Wait for ACK or timeout
            received = ack_event.wait(timeout=timeout)

            if received and ack_success[0]:
                logger.debug(f"ACK received for message to {destination or 'broadcast'}")
                return True
            elif received:
                logger.warning(f"NACK received for message to {destination or 'broadcast'}")
                return False
            else:
                logger.warning(f"ACK timeout ({timeout}s) for message to {destination or 'broadcast'}")
                return False

        except Exception as e:
            logger.error(f"Failed to send message: {e}")
            return False


# ---------------------------------------------------------------------------
# Backward-compatibility alias.  All existing imports of ``MeshConnector``
# continue to work without any changes.  New code should prefer
# ``MeshtasticTransport`` or use ``build_transport()`` from the factory.
# ---------------------------------------------------------------------------
MeshConnector = MeshtasticTransport
