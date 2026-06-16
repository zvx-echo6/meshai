"""MQTT source adapter for Meshtastic broker subscriptions.

Push-based source that subscribes to MQTT topics and decodes
ServiceEnvelope-wrapped MeshPackets. Provides live node/packet
data without polling.
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# Port number to name mapping (from portnums_pb2)
PORTNUM_NAMES = {
    0: "UNKNOWN_APP",
    1: "TEXT_MESSAGE_APP",
    2: "REMOTE_HARDWARE_APP",
    3: "POSITION_APP",
    4: "NODEINFO_APP",
    5: "ROUTING_APP",
    6: "ADMIN_APP",
    7: "TEXT_MESSAGE_COMPRESSED_APP",
    8: "WAYPOINT_APP",
    9: "AUDIO_APP",
    10: "DETECTION_SENSOR_APP",
    11: "ALERT_APP",
    32: "REPLY_APP",
    33: "IP_TUNNEL_APP",
    34: "PAXCOUNTER_APP",
    64: "SERIAL_APP",
    65: "STORE_FORWARD_APP",
    66: "RANGE_TEST_APP",
    67: "TELEMETRY_APP",
    68: "ZPS_APP",
    69: "SIMULATOR_APP",
    70: "TRACEROUTE_APP",
    71: "NEIGHBORINFO_APP",
    72: "ATAK_PLUGIN",
    73: "MAP_REPORT_APP",
    74: "POWERSTRESS_APP",
    256: "PRIVATE_APP",
    257: "ATAK_FORWARDER",
}


@dataclass
class MQTTNodeInfo:
    """Cached node info from MQTT."""

    node_num: int
    node_id_hex: str = ""
    short_name: str = ""
    long_name: str = ""
    hw_model: str = ""
    role: int = 0
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    altitude: Optional[float] = None
    last_heard: float = 0.0
    battery_percent: Optional[float] = None
    voltage: Optional[float] = None
    channel_utilization: Optional[float] = None
    air_util_tx: Optional[float] = None
    snr: Optional[float] = None
    rssi: Optional[int] = None
    via_mqtt: bool = True


@dataclass
class MQTTPacketInfo:
    """Packet received from MQTT."""

    packet_id: int
    from_node: int
    to_node: int
    portnum: int
    portnum_name: str
    channel: int
    timestamp: float
    snr: Optional[float] = None
    rssi: Optional[int] = None
    hop_limit: Optional[int] = None
    hop_start: Optional[int] = None
    payload_size: int = 0
    gateway_id: str = ""


class MQTTSource:
    """MQTT source adapter subscribing to Meshtastic broker topics.

    Maintains a subscription loop that processes ServiceEnvelope messages
    and updates node/packet caches. Unlike poll-based sources, this is
    push-based and receives data as it arrives.
    """

    def __init__(
        self,
        host: str,
        port: int = 1883,
        username: str = "",
        password: str = "",
        topic_root: str = "msh/US",
        use_tls: bool = False,
        name: str = "mqtt",
    ):
        """Initialize MQTT source.

        Args:
            host: MQTT broker hostname
            port: MQTT broker port (1883 for plain, 8883 for TLS)
            username: MQTT username (optional)
            password: MQTT password (optional, supports ${ENV_VAR})
            topic_root: Topic root to subscribe to (default: msh/US)
            use_tls: Enable TLS for connection
            name: Source name for logging/attribution
        """
        self._host = host
        self._port = port
        self._username = username
        self._password = self._resolve_env(password)
        self._topic_root = topic_root.rstrip("/")
        self._use_tls = use_tls
        self._name = name

        # State
        self._nodes: dict[int, MQTTNodeInfo] = {}
        self._packets: list[MQTTPacketInfo] = []
        self._max_packets = 1000  # Ring buffer
        self._is_connected: bool = False
        self._is_loaded: bool = False
        self._last_message: float = 0.0
        self._last_error: str = ""
        self._message_count: int = 0
        self._data_changed: bool = False

        # Subscription task
        self._task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None

        # Retry settings
        self._retry_delay = 5  # Initial retry delay
        self._max_retry_delay = 300  # Max 5 minutes between retries

    def _resolve_env(self, value: str) -> str:
        """Resolve ${ENV_VAR} references in value."""
        if value and value.startswith("${") and value.endswith("}"):
            env_var = value[2:-1]
            return os.environ.get(env_var, "")
        return value

    @property
    def nodes(self) -> dict[int, MQTTNodeInfo]:
        """Return cached nodes."""
        return self._nodes

    @property
    def packets(self) -> list[dict]:
        """Return packets as dicts for compatibility."""
        return [
            {
                "packet_id": p.packet_id,
                "from_node": p.from_node,
                "to_node": p.to_node,
                "portnum": p.portnum,
                "portnum_name": p.portnum_name,
                "channel": p.channel,
                "timestamp": p.timestamp,
                "snr": p.snr,
                "rssi": p.rssi,
                "hop_limit": p.hop_limit,
                "hop_start": p.hop_start,
                "payload_size": p.payload_size,
                "gateway_id": p.gateway_id,
            }
            for p in self._packets
        ]

    @property
    def is_loaded(self) -> bool:
        """Return True if we have received any data."""
        return self._is_loaded

    @property
    def data_changed(self) -> bool:
        """Return True if data changed since last check, then reset."""
        changed = self._data_changed
        self._data_changed = False
        return changed

    @property
    def health_status(self) -> dict:
        """Return health status for dashboard."""
        return {
            "name": self._name,
            "type": "mqtt",
            "host": self._host,
            "port": self._port,
            "topic_root": self._topic_root,
            "is_connected": self._is_connected,
            "is_loaded": self._is_loaded,
            "last_message": self._last_message,
            "last_error": self._last_error,
            "message_count": self._message_count,
            "node_count": len(self._nodes),
            "packet_count": len(self._packets),
        }

    async def start(self) -> None:
        """Start the subscription loop."""
        if self._task is not None:
            logger.warning(f"MQTT source '{self._name}' already started")
            return

        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._subscription_loop())
        logger.info(f"Started MQTT source '{self._name}' -> {self._host}:{self._port}")

    async def stop(self) -> None:
        """Stop the subscription loop."""
        if self._stop_event:
            self._stop_event.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._is_connected = False
        logger.info(f"Stopped MQTT source '{self._name}'")

    async def _subscription_loop(self) -> None:
        """Main subscription loop with reconnection logic."""
        try:
            import aiomqtt
        except ImportError:
            logger.error("aiomqtt not installed. Run: pip install aiomqtt")
            self._last_error = "aiomqtt not installed"
            return

        retry_delay = self._retry_delay

        while not self._stop_event.is_set():
            try:
                # Build connection kwargs
                kwargs = {
                    "hostname": self._host,
                    "port": self._port,
                }
                if self._username:
                    kwargs["username"] = self._username
                if self._password:
                    kwargs["password"] = self._password

                # TLS setup
                if self._use_tls:
                    import ssl
                    tls_context = ssl.create_default_context()
                    kwargs["tls_context"] = tls_context

                async with aiomqtt.Client(**kwargs) as client:
                    self._is_connected = True
                    self._last_error = ""
                    retry_delay = self._retry_delay  # Reset on successful connect
                    logger.info(f"MQTT '{self._name}' connected to {self._host}:{self._port}")

                    # Subscribe to all topics under root
                    # Meshtastic uses: msh/{region}/{channel}/json/{node_id}
                    # and: msh/{region}/{channel}/!{node_id}
                    topic = f"{self._topic_root}/#"
                    await client.subscribe(topic)
                    logger.info(f"MQTT '{self._name}' subscribed to {topic}")

                    async for message in client.messages:
                        if self._stop_event.is_set():
                            break
                        await self._process_message(message)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._is_connected = False
                self._last_error = str(e)
                logger.warning(f"MQTT '{self._name}' error: {e}. Retrying in {retry_delay}s")

                # Exponential backoff
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, self._max_retry_delay)

    async def _process_message(self, message) -> None:
        """Process an incoming MQTT message."""
        try:
            topic = str(message.topic)
            payload = message.payload

            # Skip JSON topics (we want binary ServiceEnvelope)
            if "/json/" in topic:
                return

            # Skip map reports (stat/ or map/ topics)
            if "/stat/" in topic or "/map/" in topic:
                return

            # Parse ServiceEnvelope
            from meshtastic.protobuf import mqtt_pb2

            envelope = mqtt_pb2.ServiceEnvelope()
            envelope.ParseFromString(payload)

            if not envelope.packet:
                return

            packet = envelope.packet
            gateway_id = envelope.gateway_id or ""
            channel_id = envelope.channel_id or ""

            # Update stats
            self._last_message = time.time()
            self._message_count += 1
            self._is_loaded = True
            self._data_changed = True

            # Extract packet info
            pkt_info = MQTTPacketInfo(
                packet_id=packet.id,
                from_node=packet.from_,
                to_node=packet.to,
                portnum=packet.decoded.portnum if packet.HasField("decoded") else 0,
                portnum_name=PORTNUM_NAMES.get(
                    packet.decoded.portnum if packet.HasField("decoded") else 0,
                    "UNKNOWN"
                ),
                channel=packet.channel,
                timestamp=time.time(),
                snr=packet.rx_snr if packet.rx_snr else None,
                rssi=packet.rx_rssi if packet.rx_rssi else None,
                hop_limit=packet.hop_limit if packet.hop_limit else None,
                hop_start=packet.hop_start if packet.hop_start else None,
                payload_size=len(packet.decoded.payload) if packet.HasField("decoded") else 0,
                gateway_id=gateway_id,
            )

            # Add to packet ring buffer
            self._packets.append(pkt_info)
            if len(self._packets) > self._max_packets:
                self._packets = self._packets[-self._max_packets:]

            # Process decoded payload by portnum
            if packet.HasField("decoded"):
                await self._process_decoded(packet, gateway_id)

        except Exception as e:
            logger.debug(f"MQTT message parse error: {e}")

    async def _process_decoded(self, packet, gateway_id: str) -> None:
        """Process decoded packet payload."""
        decoded = packet.decoded
        portnum = decoded.portnum
        from_node = packet.from_

        # Ensure node exists in cache
        if from_node not in self._nodes:
            self._nodes[from_node] = MQTTNodeInfo(
                node_num=from_node,
                node_id_hex=f"!{from_node:08x}",
            )

        node = self._nodes[from_node]
        node.last_heard = time.time()
        node.snr = packet.rx_snr if packet.rx_snr else node.snr
        node.rssi = packet.rx_rssi if packet.rx_rssi else node.rssi

        # NODEINFO_APP (4)
        if portnum == 4:
            from meshtastic.protobuf import mesh_pb2
            user = mesh_pb2.User()
            try:
                user.ParseFromString(decoded.payload)
                node.short_name = user.short_name or node.short_name
                node.long_name = user.long_name or node.long_name
                node.hw_model = mesh_pb2.HardwareModel.Name(user.hw_model) if user.hw_model else ""
                node.role = user.role
            except Exception:
                pass

        # POSITION_APP (3)
        elif portnum == 3:
            from meshtastic.protobuf import mesh_pb2
            pos = mesh_pb2.Position()
            try:
                pos.ParseFromString(decoded.payload)
                if pos.latitude_i:
                    node.latitude = pos.latitude_i * 1e-7
                if pos.longitude_i:
                    node.longitude = pos.longitude_i * 1e-7
                if pos.altitude:
                    node.altitude = pos.altitude
            except Exception:
                pass

        # TELEMETRY_APP (67)
        elif portnum == 67:
            from meshtastic.protobuf import telemetry_pb2
            telem = telemetry_pb2.Telemetry()
            try:
                telem.ParseFromString(decoded.payload)
                if telem.HasField("device_metrics"):
                    dm = telem.device_metrics
                    if dm.battery_level and dm.battery_level <= 100:
                        node.battery_percent = dm.battery_level
                    if dm.voltage:
                        node.voltage = dm.voltage
                    if dm.channel_utilization:
                        node.channel_utilization = dm.channel_utilization
                    if dm.air_util_tx:
                        node.air_util_tx = dm.air_util_tx
            except Exception:
                pass

    # Compatibility methods for MeshDataStore integration

    def tick(self) -> Optional[str]:
        """Tick method for compatibility. MQTT is push-based, not polled.

        Returns None since we do not poll endpoints.
        """
        return None

    def maybe_refresh(self) -> bool:
        """Check if data changed (for legacy compatibility)."""
        return self.data_changed
