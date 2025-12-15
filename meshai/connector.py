"""Meshtastic connection management for MeshAI."""

import asyncio
import logging
from dataclasses import dataclass
from typing import Callable, Optional

import meshtastic
import meshtastic.serial_interface
import meshtastic.tcp_interface
from meshtastic import BROADCAST_NUM
from pubsub import pub

from .config import ConnectionConfig

logger = logging.getLogger(__name__)


@dataclass
class MeshMessage:
    """Represents an incoming mesh message."""

    sender_id: str  # Node ID (hex string like "!abcd1234")
    sender_name: str  # Short name or long name
    text: str  # Message content
    channel: int  # Channel index
    is_dm: bool  # True if direct message to us
    packet: dict  # Raw packet for additional data

    @property
    def sender_position(self) -> Optional[tuple[float, float]]:
        """Get sender's GPS position if available (lat, lon)."""
        # Position comes from node info, not the message itself
        # This will be populated by the connector if available
        return self._position if hasattr(self, "_position") else None


class MeshConnector:
    """Manages connection to Meshtastic node."""

    def __init__(self, config: ConnectionConfig):
        self.config = config
        self._interface: Optional[meshtastic.MeshInterface] = None
        self._my_node_id: Optional[str] = None
        self._message_callback: Optional[Callable[[MeshMessage], None]] = None
        self._node_positions: dict[str, tuple[float, float]] = {}
        self._node_names: dict[str, str] = {}
        self._connected = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    @property
    def connected(self) -> bool:
        """Check if connected to node."""
        return self._connected and self._interface is not None

    @property
    def my_node_id(self) -> Optional[str]:
        """Get our node's ID."""
        return self._my_node_id

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

            self._connected = True

        except Exception as e:
            logger.error(f"Failed to connect: {e}")
            self._connected = False
            raise

    def disconnect(self) -> None:
        """Close connection to Meshtastic node."""
        if self._interface:
            try:
                pub.unsubscribe(self._on_receive, "meshtastic.receive.text")
                pub.unsubscribe(self._on_node_update, "meshtastic.node.updated")
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
        """Set callback for incoming messages.

        Args:
            callback: Async function to call with MeshMessage
            loop: Event loop to schedule callback on
        """
        self._message_callback = callback
        self._loop = loop

    def _cache_node_info(self) -> None:
        """Cache node names and positions from node database."""
        if not self._interface:
            return

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
        node_id = f"!{node['num']:08x}"

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

            # Get sender name
            sender_name = self._node_names.get(sender_num, sender_num)

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
            if sender_num in self._node_positions:
                msg._position = self._node_positions[sender_num]

            # Schedule callback on event loop
            self._loop.call_soon_threadsafe(
                lambda m=msg: asyncio.create_task(self._message_callback(m))
            )

        except Exception as e:
            logger.error(f"Error processing received message: {e}")

    def send_message(
        self,
        text: str,
        destination: Optional[str] = None,
        channel: int = 0,
    ) -> bool:
        """Send a text message.

        Args:
            text: Message text to send
            destination: Node ID for DM, or None for broadcast
            channel: Channel index to send on

        Returns:
            True if send was initiated successfully
        """
        if not self._interface:
            logger.error("Cannot send: not connected")
            return False

        try:
            if destination:
                # DM to specific node
                # Convert hex string to int if needed
                if destination.startswith("!"):
                    dest_num = int(destination[1:], 16)
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
        return self._node_positions.get(node_id)

    def get_node_name(self, node_id: str) -> str:
        """Get cached name for a node.

        Args:
            node_id: Node ID (hex string like "!abcd1234")

        Returns:
            Node name or the node ID if name not available
        """
        return self._node_names.get(node_id, node_id)
