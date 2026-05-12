"""Mesh data store with ETL pipeline and SQLite persistence.

This module replaces mesh_sources.py with a clean three-layer architecture:
- Layer 1: Source adapters fetch raw data (unchanged)
- Layer 2: This module normalizes, merges, and persists to SQLite
- Layer 3: Consumers read unified model (no field guessing)
"""

import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .config import MeshSourceConfig
from .mesh_models import (
    DailyTraffic,
    MeshSnapshot,
    NodeSnapshot,
    UnifiedChannel,
    UnifiedEdge,
    UnifiedNode,
    UnifiedSolar,
    UnifiedTraceroute,
)
from .sources.meshmonitor_data import MeshMonitorDataSource
from .sources.meshview import MeshviewSource
from .sources.mqtt_source import MQTTSource

logger = logging.getLogger(__name__)

# Nodes not heard in this many days are excluded from live model
STALE_NODE_THRESHOLD_DAYS = 7

# Meshtastic role enum mapping (integer -> string)
MESHTASTIC_ROLE_MAP = {
    0: "CLIENT",
    1: "CLIENT_MUTE",
    2: "ROUTER",
    3: "ROUTER_CLIENT",
    4: "REPEATER",
    5: "TRACKER",
    6: "SENSOR",
    7: "TAK",
    8: "CLIENT_HIDDEN",
    9: "LOST_AND_FOUND",
    10: "TAK_TRACKER",
    11: "ROUTER_LATE",
    12: "CLIENT_BASE",
}

# Time window mapping
TIME_WINDOWS = {
    "24h": 86400,
    "48h": 172800,
    "72h": 259200,
    "7d": 604800,
    "14d": 1209600,
}

# Telemetry type mapping: raw MeshMonitor type -> UnifiedNode field
TELEMETRY_TYPE_MAP = {
    # Device metrics (already handled by node-level fields)
    "batteryLevel": "battery_percent",
    "voltage": "voltage",
    "channelUtilization": "channel_utilization",
    "airUtilTx": "air_util_tx",

    # Environment (BME280, BME680, SHT31, etc.)
    "temperature": "temperature",
    "humidity": "humidity",
    "relativeHumidity": "humidity",
    "pressure": "barometric_pressure",
    "barometricPressure": "barometric_pressure",
    "gasResistance": "gas_resistance",
    "iaq": "iaq",

    # Light (BH1750, TSL2591, etc.)
    "lux": "light_lux",
    "lightLevel": "light_lux",

    # Wind/Weather (DFROBOT_LARK stations)
    "windSpeed": "wind_speed",
    "wind_speed": "wind_speed",
    "windDirection": "wind_direction",
    "wind_direction": "wind_direction",
    "rainfall": "rainfall",
    "rain": "rainfall",

    # Air Quality (PMSA003I)
    "pm1_0": "pm1_0",
    "pm2_5": "pm2_5",
    "pm10": "pm10",
    "pm10_standard": "pm10",

    # Power monitoring (INA sensors)
    "ch3Voltage": "ext_voltage",
    "ch3Current": "ext_current",
    "current": "ext_current",
    "extVoltage": "ext_voltage",

    # Health (MAX30102, MLX)
    "heartRate": "heart_rate",
    "heart_rate": "heart_rate",
    "spo2": "spo2",
    "bodyTemperature": "body_temperature",

    # Radiation (RadSens)
    "radiationCpm": "radiation_cpm",

    # UV
    "uvIndex": "uv_index",
    "uv_index": "uv_index",
}

# SQLite schema
SCHEMA = """
-- Per-node snapshot taken on each refresh cycle
CREATE TABLE IF NOT EXISTS node_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    node_num INTEGER NOT NULL,
    short_name TEXT,
    long_name TEXT,
    role TEXT,
    hw_model TEXT,
    latitude REAL,
    longitude REAL,
    last_heard REAL,
    is_online INTEGER,
    battery_percent REAL,
    voltage REAL,
    packets_sent_24h INTEGER,
    packets_seen_24h INTEGER,
    channel_utilization REAL,
    air_util_tx REAL,
    uplink_enabled INTEGER,
    neighbor_count INTEGER,
    hops_away INTEGER,
    snr REAL,
    source TEXT
);
CREATE INDEX IF NOT EXISTS idx_node_snap_ts ON node_snapshots(timestamp);
CREATE INDEX IF NOT EXISTS idx_node_snap_node ON node_snapshots(node_num);
CREATE INDEX IF NOT EXISTS idx_node_snap_node_ts ON node_snapshots(node_num, timestamp);

-- Mesh-wide snapshot taken on each refresh
CREATE TABLE IF NOT EXISTS mesh_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    total_nodes INTEGER,
    active_nodes INTEGER,
    infra_online INTEGER,
    infra_total INTEGER,
    total_packets_24h INTEGER,
    avg_channel_utilization REAL,
    avg_battery_percent REAL,
    source_count INTEGER
);
CREATE INDEX IF NOT EXISTS idx_mesh_snap_ts ON mesh_snapshots(timestamp);

-- Individual packets (from MeshMonitor, capped at 1000 per refresh)
CREATE TABLE IF NOT EXISTS packet_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    packet_id TEXT UNIQUE,
    timestamp REAL NOT NULL,
    from_node INTEGER,
    to_node INTEGER,
    portnum INTEGER,
    portnum_name TEXT,
    channel INTEGER,
    snr REAL,
    rssi REAL,
    hop_limit INTEGER,
    hop_start INTEGER,
    payload_size INTEGER,
    source TEXT
);
CREATE INDEX IF NOT EXISTS idx_pkt_ts ON packet_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_pkt_from ON packet_log(from_node);
CREATE INDEX IF NOT EXISTS idx_pkt_from_ts ON packet_log(from_node, timestamp);
CREATE INDEX IF NOT EXISTS idx_pkt_portnum ON packet_log(portnum_name);

-- Telemetry readings (battery, voltage, environment over time)
CREATE TABLE IF NOT EXISTS telemetry_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    node_num INTEGER NOT NULL,
    telemetry_type TEXT NOT NULL,
    value REAL,
    unit TEXT,
    source TEXT,
    UNIQUE(node_num, telemetry_type, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_telem_node_ts ON telemetry_log(node_num, timestamp);
CREATE INDEX IF NOT EXISTS idx_telem_type ON telemetry_log(telemetry_type);

-- Daily traffic aggregates (from Meshview /api/stats?period_type=day)
CREATE TABLE IF NOT EXISTS traffic_daily (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    total_packets INTEGER,
    packets_by_type TEXT,
    source TEXT,
    UNIQUE(date, source)
);
CREATE INDEX IF NOT EXISTS idx_traffic_date ON traffic_daily(date);

-- Per-node daily packet counts
CREATE TABLE IF NOT EXISTS node_daily_traffic (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    node_num INTEGER NOT NULL,
    packets_sent INTEGER DEFAULT 0,
    packets_by_type TEXT,
    source TEXT,
    UNIQUE(date, node_num, source)
);
CREATE INDEX IF NOT EXISTS idx_node_daily_node ON node_daily_traffic(node_num);
CREATE INDEX IF NOT EXISTS idx_node_daily_date ON node_daily_traffic(date);
"""


class MeshDataStore:
    """Central data store with ETL pipeline and SQLite persistence."""

    def __init__(
        self,
        source_configs: list[MeshSourceConfig],
        db_path: str = "/data/mesh_history.db",
    ):
        """Initialize the data store.

        Args:
            source_configs: List of source configurations
            db_path: Path to SQLite database for historical data
        """
        self._sources: dict[str, MeshviewSource | MeshMonitorDataSource | MQTTSource] = {}
        self._db_path = db_path
        self._db: Optional[sqlite3.Connection] = None

        # Live state
        self._nodes: dict[int, UnifiedNode] = {}
        self._edges: list[UnifiedEdge] = []
        self._traceroutes: list[UnifiedTraceroute] = []
        self._channels: list[UnifiedChannel] = []
        self._solar: list[UnifiedSolar] = []
        self._daily_traffic: list[DailyTraffic] = []

        # Timing
        self._last_refresh: float = 0.0
        self._last_force_refresh: float = 0.0
        self._refresh_interval: int = 300  # Default 5 minutes
        self._is_loaded: bool = False

        # Deliverability metrics (from Meshview counts)
        self._deliverability: dict = {
            "avg_gateways": None,
            "total_packets": None,
            "total_seen": None,
            "gateway_count": 0,
        }

        # Initialize sources (cfg may be dict from YAML or MeshSourceConfig)
        for cfg in source_configs:
            enabled = cfg.get('enabled', True) if isinstance(cfg, dict) else cfg.enabled
            if not enabled:
                continue
            self._register_source(cfg)

        # Initialize database
        self._init_db()

    def _register_source(self, cfg) -> None:
        """Register a data source from config (dict or MeshSourceConfig)."""
        # Support both dict and dataclass configs
        if isinstance(cfg, dict):
            name = cfg.get('name', '')
            src_type = cfg.get('type', '')
            url = cfg.get('url', '')
            api_token = cfg.get('api_token', '')
            refresh_interval = cfg.get('refresh_interval', 300)
        else:
            name = cfg.name
            src_type = cfg.type
            url = cfg.url
            api_token = getattr(cfg, 'api_token', '')
            refresh_interval = cfg.refresh_interval

        if not name:
            logger.warning("Skipping source with empty name")
            return

        if name in self._sources:
            logger.warning(f"Duplicate source name '{name}', skipping")
            return

        try:
            if src_type == "meshview":
                polite = getattr(cfg, 'polite_mode', False) if not isinstance(cfg, dict) else cfg.get('polite_mode', False)
                self._sources[name] = MeshviewSource(
                    url=url,
                    refresh_interval=refresh_interval,
                    polite_mode=polite,
                )
                logger.info(f"Registered Meshview source '{name}' -> {url} (polite={polite})")

            elif src_type == "meshmonitor":
                polite = getattr(cfg, 'polite_mode', False) if not isinstance(cfg, dict) else cfg.get('polite_mode', False)
                self._sources[name] = MeshMonitorDataSource(
                    url=url,
                    api_token=api_token,
                    refresh_interval=refresh_interval,
                    polite_mode=polite,
                )
                logger.info(f"Registered MeshMonitor source '{name}' -> {url} (polite={polite})")

            elif src_type == "mqtt":
                # Extract MQTT-specific config
                if isinstance(cfg, dict):
                    host = cfg.get('host', '')
                    port = cfg.get('port', 1883)
                    username = cfg.get('username', '')
                    password = cfg.get('password', '')
                    topic_root = cfg.get('topic_root', 'msh/US')
                    use_tls = cfg.get('use_tls', False)
                else:
                    host = getattr(cfg, 'host', '')
                    port = getattr(cfg, 'port', 1883)
                    username = getattr(cfg, 'username', '')
                    password = getattr(cfg, 'password', '')
                    topic_root = getattr(cfg, 'topic_root', 'msh/US')
                    use_tls = getattr(cfg, 'use_tls', False)

                if not host:
                    logger.warning(f"MQTT source '{name}' missing host, skipping")
                    return

                self._sources[name] = MQTTSource(
                    host=host,
                    port=port,
                    username=username,
                    password=password,
                    topic_root=topic_root,
                    use_tls=use_tls,
                    name=name,
                )
                # Track MQTT sources separately for async start
                if not hasattr(self, '_mqtt_sources'):
                    self._mqtt_sources = []
                self._mqtt_sources.append(name)
                logger.info(f"Registered MQTT source '{name}' -> {host}:{port} topic={topic_root}")

            else:
                logger.warning(f"Unknown source type '{src_type}' for '{name}'")

            # Track minimum refresh interval
            if refresh_interval < self._refresh_interval:
                self._refresh_interval = refresh_interval

        except Exception as e:
            logger.error(f"Failed to register source '{name}': {e}")

    def _init_db(self) -> None:
        """Initialize SQLite database and create tables."""
        try:
            # Ensure directory exists
            db_dir = Path(self._db_path).parent
            db_dir.mkdir(parents=True, exist_ok=True)

            self._db = sqlite3.connect(self._db_path, check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            self._db.executescript(SCHEMA)

            # Schema migrations for deliverability columns
            for col, col_type in [
                ("avg_gateways_mesh", "REAL"),
                ("total_packets_global", "INTEGER"),
                ("total_seen_global", "INTEGER"),
            ]:
                try:
                    self._db.execute(f"ALTER TABLE mesh_snapshots ADD COLUMN {col} {col_type}")
                except Exception:
                    pass  # Column already exists

            self._db.commit()
            logger.info(f"Initialized SQLite database at {self._db_path}")
        except Exception as e:
            logger.error(f"Failed to initialize database: {e}")
            self._db = None

    # =========================================================================
    # Source Management
    # =========================================================================


    async def start_mqtt_sources(self) -> None:
        """Start all MQTT source subscription loops."""
        if not hasattr(self, '_mqtt_sources'):
            return
        for name in self._mqtt_sources:
            source = self._sources.get(name)
            if source and hasattr(source, 'start'):
                await source.start()

    async def stop_mqtt_sources(self) -> None:
        """Stop all MQTT source subscription loops."""
        if not hasattr(self, '_mqtt_sources'):
            return
        for name in self._mqtt_sources:
            source = self._sources.get(name)
            if source and hasattr(source, 'stop'):
                await source.stop()

    def _purge_stale_nodes(self):
        """Remove nodes not heard from in more than 7 days.

        These nodes are stale — they inflate counts, create false
        coverage gaps, and waste LLM tokens. They still exist in
        SQLite historical data, just not in the live model.
        """
        import time
        cutoff = time.time() - (STALE_NODE_THRESHOLD_DAYS * 86400)

        stale_nums = []
        for node_num, node in self._nodes.items():
            if node.last_heard and node.last_heard < cutoff:
                stale_nums.append(node_num)
            elif not node.last_heard or node.last_heard == 0:
                # No last_heard data at all — also consider stale
                stale_nums.append(node_num)

        for num in stale_nums:
            del self._nodes[num]

        if stale_nums:
            logger.info(f"Purged {len(stale_nums)} stale nodes (not heard in {STALE_NODE_THRESHOLD_DAYS} days)")

    def refresh(self) -> bool:
        """Tick-based refresh. Called every second from the main loop.

        Delegates to source tick() for sources that support it.
        Only does a full rebuild when nodes/edges/topology change.
        Only does a lightweight update when only packets change.

        Returns:
            True if any data changed
        """
        now = time.time()
        any_changed = False
        needs_rebuild = False
        needs_packet_update = False

        for name, source in self._sources.items():
            # Check if this source supports tick-based polling
            if hasattr(source, 'tick') and hasattr(source, '_tick_interval'):
                if now - source._last_tick >= source._tick_interval:
                    endpoint = source.tick()
                    if endpoint:
                        any_changed = True
                        # Major changes require full rebuild
                        if endpoint in ("nodes", "edges", "traceroutes", "topology", "telemetry"):
                            needs_rebuild = True
                        # Packet-only changes are lightweight
                        elif endpoint in ("packets",):
                            needs_packet_update = True
                        # stats, counts, channels, solar, network just update cached data
            else:
                # Legacy fallback for sources without tick support
                if source.maybe_refresh():
                    any_changed = True
                    needs_rebuild = True

        if needs_rebuild:
            self._rebuild()
            self._purge_stale_nodes()
            self._store_snapshot()
            self._purge_old_data()
            self._last_refresh = now
            self._is_loaded = True
        elif needs_packet_update:
            # Lightweight: just update packet-derived metrics without full rebuild
            self._update_packet_metrics()
            self._last_refresh = now

        return any_changed

    def force_refresh(self) -> bool:
        """Force an immediate refresh, bypassing the interval timer.

        Rate-limited: minimum 60 seconds between force refreshes.

        Returns:
            True if refresh was performed, False if rate-limited
        """
        now = time.time()
        if now - self._last_force_refresh < 60:
            logger.debug("Force refresh rate-limited")
            return False

        self._last_force_refresh = now
        return self._do_refresh(force=True)


    def _update_packet_metrics(self):
        """Lightweight update when only new packets arrived.

        Updates:
        - Per-node packet counts
        - Top senders
        - Deliverability (source overlap)
        Does NOT rebuild the full node model.
        """
        # Recount packets per node from all sources (deduped by packet ID)
        packet_counts: dict[int, int] = {}
        packets_by_type: dict[int, dict[str, int]] = {}
        seen_packet_ids: set = set()

        for name, source in self._sources.items():
            for pkt in (source.packets if hasattr(source, 'packets') else []):
                # Dedup: skip packets already seen from another source
                pkt_id = pkt.get("packet_id") or pkt.get("id")
                if pkt_id is not None:
                    if pkt_id in seen_packet_ids:
                        continue
                    seen_packet_ids.add(pkt_id)

                from_node = pkt.get("from_node") or pkt.get("from_node_id") or pkt.get("from")
                if from_node is None:
                    continue

                # Normalize to int
                if isinstance(from_node, str):
                    stripped = from_node.lstrip("!")
                    try:
                        from_node = int(stripped, 16) if not stripped.isdigit() else int(stripped)
                    except ValueError:
                        continue

                packet_counts[from_node] = packet_counts.get(from_node, 0) + 1

                portnum = str(pkt.get("portnum") or pkt.get("portnum_name") or pkt.get("type") or "UNKNOWN")
                if from_node not in packets_by_type:
                    packets_by_type[from_node] = {}
                packets_by_type[from_node][portnum] = packets_by_type[from_node].get(portnum, 0) + 1

        # Update nodes
        for node_num, count in packet_counts.items():
            if node_num in self._nodes:
                self._nodes[node_num].packets_sent_24h = count
                if node_num in packets_by_type:
                    self._nodes[node_num].packets_by_type = packets_by_type[node_num]

        logger.debug(f"Packet metrics updated: {sum(packet_counts.values())} packets across {len(packet_counts)} nodes")

    def _do_refresh(self, force: bool = False) -> bool:
        """Perform the actual refresh.

        Args:
            force: If True, bypass source refresh intervals

        Returns:
            True if any source was refreshed
        """
        refreshed = 0

        for name, source in self._sources.items():
            try:
                if force:
                    if source.fetch_all():
                        refreshed += 1
                else:
                    if source.maybe_refresh():
                        refreshed += 1
            except Exception as e:
                logger.error(f"Error refreshing source '{name}': {e}")

        if refreshed > 0:
            self._rebuild()
            self._purge_stale_nodes()  # Remove nodes not heard in 7+ days
            self._store_snapshot()
            self._purge_old_data()
            self._last_refresh = time.time()
            self._is_loaded = True
            return True

        return False

    # =========================================================================
    # Normalization
    # =========================================================================


    def _normalize_node_id(self, raw) -> Optional[int]:
        """Convert raw node ID (hex string, decimal string, int) to int node_num."""
        if isinstance(raw, int):
            return raw
        if isinstance(raw, str):
            stripped = raw.lstrip("!")
            try:
                # Check if hex
                if any(c in "abcdefABCDEF" for c in stripped):
                    return int(stripped, 16)
                return int(stripped)
            except ValueError:
                return None
        return None

    def _extract_node_num(self, raw: dict) -> Optional[int]:
        """Extract canonical node number from various formats."""
        # MeshMonitor: nodeNum is the canonical field
        if "nodeNum" in raw:
            val = raw["nodeNum"]
            if isinstance(val, int):
                return val
            if isinstance(val, str) and val.isdigit():
                return int(val)

        # Meshview: node_id is the numeric ID
        if "node_id" in raw:
            val = raw["node_id"]
            if isinstance(val, int):
                return val
            if isinstance(val, str):
                if val.isdigit():
                    return int(val)
                # Could be hex
                stripped = val.lstrip("!")
                try:
                    return int(stripped, 16)
                except ValueError:
                    pass

        # Try other numeric fields
        for field in ("num", "node_num"):
            if field in raw:
                val = raw[field]
                if isinstance(val, int):
                    return val
                if isinstance(val, str) and val.isdigit():
                    return int(val)

        # Try id field (but not small database IDs)
        if "id" in raw:
            val = raw["id"]
            if isinstance(val, int) and val > 100000:
                return val
            if isinstance(val, str):
                if val.startswith("!"):
                    try:
                        return int(val[1:], 16)
                    except ValueError:
                        pass
                elif len(val) == 8:
                    try:
                        return int(val, 16)
                    except ValueError:
                        pass

        return None

    def _node_num_to_hex(self, node_num: int) -> str:
        """Convert node number to hex ID format."""
        return f"!{node_num:08x}"

    def _normalize_node(self, raw: dict, source_name: str) -> Optional[UnifiedNode]:
        """Normalize a raw node dict to UnifiedNode.

        Uses actual field names discovered from API probing:
        - Meshview: id (hex), node_id (int), short_name, long_name, hw_model (str),
          role (str), last_lat/last_long (scaled int), last_seen_us
        - MeshMonitor: nodeNum, nodeId (hex), shortName, longName, hwModel (int),
          role (int), latitude/longitude (float), lastHeard, batteryLevel, voltage,
          channelUtilization, airUtilTx, hopsAway, snr, rssi, viaMqtt, uptimeSeconds
        """
        node_num = self._extract_node_num(raw)
        if node_num is None:
            return None

        node = UnifiedNode(node_num=node_num)
        node.node_id_hex = self._node_num_to_hex(node_num)
        node.sources = [source_name]

        # Short name: shortName (MM) or short_name (MV)
        node.short_name = raw.get("shortName") or raw.get("short_name") or ""

        # Long name: longName (MM) or long_name (MV)
        node.long_name = raw.get("longName") or raw.get("long_name") or ""

        # Role: int (MM) or string (MV)
        role = raw.get("role")
        if role is None:
            node.role = "UNKNOWN"
        elif isinstance(role, int):
            node.role = MESHTASTIC_ROLE_MAP.get(role, f"UNKNOWN_{role}")
        elif isinstance(role, str):
            node.role = role.upper()
        else:
            node.role = str(role).upper()

        # Hardware model: hw_model (MV, string) or hwModel (MM, int)
        hw = raw.get("hw_model")
        if hw and isinstance(hw, str):
            node.hw_model = hw
        else:
            hw = raw.get("hwModel")
            if hw is not None:
                node.hw_model = str(hw)  # Keep as string for display

        # Position: latitude/longitude (MM) or last_lat/last_long (MV, scaled)
        lat = raw.get("latitude")
        lon = raw.get("longitude")

        if lat is None:
            lat = raw.get("last_lat")
            if lat is not None and isinstance(lat, int) and abs(lat) > 1000:
                lat = lat / 1e7

        if lon is None:
            lon = raw.get("last_long")
            if lon is not None and isinstance(lon, int) and abs(lon) > 1000:
                lon = lon / 1e7

        # Validate coordinates (near 0,0 is likely invalid)
        if lat is not None and lon is not None:
            if abs(lat) < 0.001 and abs(lon) < 0.001:
                lat = None
                lon = None

        node.latitude = lat
        node.longitude = lon
        node.altitude = raw.get("altitude")

        # Last heard: lastHeard (MM, epoch seconds) or last_seen_us (MV, microseconds)
        ts = None
        if "lastHeard" in raw and raw["lastHeard"]:
            ts = raw["lastHeard"]
            # MM sometimes uses milliseconds
            if ts > 1e12:
                ts = ts / 1000
        elif "last_seen_us" in raw and raw["last_seen_us"]:
            ts = raw["last_seen_us"] / 1e6

        node.last_heard = ts or 0.0

        # Is online (computed from last_heard)
        now = time.time()
        node.is_online = (now - node.last_heard) < 86400 if node.last_heard else False

        # Hops, SNR, RSSI (MM)
        node.hops_away = raw.get("hopsAway")
        node.snr = raw.get("snr")
        node.rssi = raw.get("rssi")

        # === Power metrics - DIRECTLY from MeshMonitor node object ===
        bat = raw.get("batteryLevel")
        if bat is not None:
            try:
                node.battery_percent = float(bat)
            except (ValueError, TypeError):
                pass

        vol = raw.get("voltage")
        if vol is not None:
            try:
                node.voltage = float(vol)
            except (ValueError, TypeError):
                pass

        # === Device-reported metrics - DIRECTLY from MeshMonitor node ===
        ch_util = raw.get("channelUtilization")
        if ch_util is not None:
            try:
                node.channel_utilization = float(ch_util)
            except (ValueError, TypeError):
                pass

        air_tx = raw.get("airUtilTx")
        if air_tx is not None:
            try:
                node.air_util_tx = float(air_tx)
            except (ValueError, TypeError):
                pass

        uptime = raw.get("uptimeSeconds")
        if uptime is not None:
            try:
                node.uptime_seconds = int(uptime)
            except (ValueError, TypeError):
                pass

        # Connectivity
        node.via_mqtt = bool(raw.get("viaMqtt"))
        node.is_mqtt_gateway = bool(raw.get("is_mqtt_gateway"))

        # Firmware
        node.firmware_version = raw.get("firmwareVersion") or raw.get("firmware") or ""
        node.public_key = raw.get("publicKey") or ""

        return node

    def _merge_nodes(self, existing: UnifiedNode, new: UnifiedNode) -> None:
        """Merge new node data into existing node.

        Rules:
        - Non-null values win
        - Higher packet counts win
        - More recent timestamp wins
        - Sources list is extended
        """
        # Extend sources
        for src in new.sources:
            if src not in existing.sources:
                existing.sources.append(src)

        # Simple fields: non-null wins
        if new.short_name and not existing.short_name:
            existing.short_name = new.short_name
        if new.long_name and not existing.long_name:
            existing.long_name = new.long_name
        if new.hw_model and not existing.hw_model:
            existing.hw_model = new.hw_model
        if new.role != "UNKNOWN" and existing.role == "UNKNOWN":
            existing.role = new.role
        if new.firmware_version and not existing.firmware_version:
            existing.firmware_version = new.firmware_version

        # Position: prefer non-null
        if new.latitude is not None and existing.latitude is None:
            existing.latitude = new.latitude
            existing.longitude = new.longitude
        if new.altitude is not None and existing.altitude is None:
            existing.altitude = new.altitude

        # Timing: more recent wins
        if new.last_heard > existing.last_heard:
            existing.last_heard = new.last_heard
            existing.is_online = new.is_online

        # Metrics: prefer non-null, or higher values for counts
        if new.battery_percent is not None and existing.battery_percent is None:
            existing.battery_percent = new.battery_percent
        if new.voltage is not None and existing.voltage is None:
            existing.voltage = new.voltage
        if new.channel_utilization is not None and existing.channel_utilization is None:
            existing.channel_utilization = new.channel_utilization
        if new.air_util_tx is not None and existing.air_util_tx is None:
            existing.air_util_tx = new.air_util_tx
        if new.hops_away is not None and existing.hops_away is None:
            existing.hops_away = new.hops_away
        if new.snr is not None and existing.snr is None:
            existing.snr = new.snr
        if new.rssi is not None and existing.rssi is None:
            existing.rssi = new.rssi
        if new.uptime_seconds is not None and existing.uptime_seconds is None:
            existing.uptime_seconds = new.uptime_seconds

        # Connectivity flags
        if new.via_mqtt:
            existing.via_mqtt = True
        if new.is_mqtt_gateway:
            existing.is_mqtt_gateway = True
        if new.uplink_enabled:
            existing.uplink_enabled = True

    # =========================================================================
    # Rebuild Live State
    # =========================================================================

    def _rebuild(self) -> None:
        """Rebuild live state from all sources."""
        now = time.time()

        # Clear live state
        self._nodes = {}
        self._edges = []
        self._traceroutes = []
        self._channels = []
        self._solar = []
        self._daily_traffic = []

        # Collect and normalize nodes from all sources
        for name, source in self._sources.items():
            for raw in source.nodes:
                node = self._normalize_node(raw, name)
                if node is None:
                    continue

                if node.node_num in self._nodes:
                    self._merge_nodes(self._nodes[node.node_num], node)
                else:
                    self._nodes[node.node_num] = node

        # Collect edges (Meshview only)
        for name, source in self._sources.items():
            if isinstance(source, MeshviewSource):
                for raw in source.edges:
                    edge = self._normalize_edge(raw, name)
                    if edge:
                        self._edges.append(edge)

        # Collect traceroutes (MeshMonitor only)
        for name, source in self._sources.items():
            if isinstance(source, MeshMonitorDataSource):
                for raw in source.traceroutes:
                    tr = self._normalize_traceroute(raw, name)
                    if tr:
                        self._traceroutes.append(tr)

        # Collect channels (MeshMonitor only)
        for name, source in self._sources.items():
            if isinstance(source, MeshMonitorDataSource):
                for raw in source.channels:
                    ch = self._normalize_channel(raw, name)
                    if ch:
                        self._channels.append(ch)

        # Collect solar data (MeshMonitor only)
        for name, source in self._sources.items():
            if isinstance(source, MeshMonitorDataSource):
                if hasattr(source, "solar") and source.solar:
                    for raw in source.solar:
                        sol = self._normalize_solar(raw, name)
                        if sol:
                            self._solar.append(sol)

        # Collect daily traffic (Meshview only)
        for name, source in self._sources.items():
            if isinstance(source, MeshviewSource):
                if hasattr(source, "daily_stats") and source.daily_stats:
                    self._process_daily_stats(source.daily_stats, name)

        # Count packets per node from packet_log
        self._enrich_packet_counts()

        # Enrich with historical data
        self._enrich_historical()

        # Enrich with environmental telemetry
        self._enrich_environmental()

        # Enrich with deliverability metrics
        self._enrich_deliverability()

        # Enrich with feeder gateway data
        self._enrich_feeder_data()

        # Enrich edges with SNR from traceroutes and node data
        self._enrich_edges_from_traceroutes()
        self._enrich_edges_from_node_snr()

        # Update neighbor counts from edges
        self._compute_neighbors()

        logger.info(
            f"Rebuilt live state: {len(self._nodes)} nodes, "
            f"{len(self._edges)} edges, {len(self._traceroutes)} traceroutes"
        )

    def _normalize_edge(self, raw: dict, source_name: str) -> Optional[UnifiedEdge]:
        """Normalize an edge to UnifiedEdge."""
        from_num = raw.get("from_node") or raw.get("from") or raw.get("from_num")
        to_num = raw.get("to_node") or raw.get("to") or raw.get("to_num")

        if from_num is None or to_num is None:
            return None

        # Convert to int if string
        if isinstance(from_num, str):
            if from_num.isdigit():
                from_num = int(from_num)
            else:
                try:
                    from_num = int(from_num.lstrip("!"), 16)
                except ValueError:
                    return None

        if isinstance(to_num, str):
            if to_num.isdigit():
                to_num = int(to_num)
            else:
                try:
                    to_num = int(to_num.lstrip("!"), 16)
                except ValueError:
                    return None

        return UnifiedEdge(
            from_node=from_num,
            to_node=to_num,
            snr=raw.get("snr"),
            rssi=raw.get("rssi"),
            quality=raw.get("quality"),
            last_seen=raw.get("last_seen") or raw.get("timestamp") or 0.0,
            sources=[source_name],
        )

    def _normalize_traceroute(
        self, raw: dict, source_name: str
    ) -> Optional[UnifiedTraceroute]:
        """Normalize a traceroute to UnifiedTraceroute."""
        from_num = raw.get("fromNodeNum")
        to_num = raw.get("toNodeNum")

        if from_num is None or to_num is None:
            return None

        # Route is a list of node numbers
        route = raw.get("route") or []
        route_back = raw.get("routeBack") or []
        snr_towards = raw.get("snrTowards") or []
        snr_back = raw.get("snrBack") or []

        return UnifiedTraceroute(
            from_node=from_num,
            to_node=to_num,
            route=route if isinstance(route, list) else [],
            route_back=route_back if isinstance(route_back, list) else [],
            snr_towards=snr_towards if isinstance(snr_towards, list) else [],
            snr_back=snr_back if isinstance(snr_back, list) else [],
            timestamp=raw.get("timestamp") or raw.get("createdAt") or 0.0,
            request_id=raw.get("requestId") or 0,
            sources=[source_name],
        )

    def _normalize_channel(
        self, raw: dict, source_name: str
    ) -> Optional[UnifiedChannel]:
        """Normalize a channel to UnifiedChannel."""
        ch_id = raw.get("id")
        if ch_id is None:
            return None

        return UnifiedChannel(
            channel_id=ch_id,
            name=raw.get("name") or "",
            role=raw.get("role") or 0,
            role_name=raw.get("roleName") or "",
            uplink_enabled=bool(raw.get("uplinkEnabled")),
            downlink_enabled=bool(raw.get("downlinkEnabled")),
            position_precision=raw.get("positionPrecision") or 0,
            sources=[source_name],
        )

    def _normalize_solar(self, raw: dict, source_name: str) -> Optional[UnifiedSolar]:
        """Normalize solar data to UnifiedSolar."""
        node_num = self._extract_node_num(raw)
        if node_num is None:
            return None

        return UnifiedSolar(
            node_num=node_num,
            estimate_watts=raw.get("estimateWatts"),
            confidence=raw.get("confidence"),
            timestamp=raw.get("timestamp") or 0.0,
            sources=[source_name],
        )

    def _process_daily_stats(self, stats: list[dict], source_name: str) -> None:
        """Process daily stats from Meshview into DailyTraffic."""
        for item in stats:
            period = item.get("period") or item.get("date")
            count = item.get("count") or 0

            if not period:
                continue

            # Extract date portion (may be "2026-05-04 00:00" format)
            date_str = period.split()[0] if " " in period else period

            self._daily_traffic.append(
                DailyTraffic(
                    date=date_str,
                    total_packets=count,
                    packets_by_type={},  # Not available from Meshview hourly
                    source=source_name,
                )
            )


    def _enrich_edges_from_traceroutes(self) -> None:
        """Add SNR data to edges from traceroute snrTowards/snrBack arrays."""
        enriched = 0
        for tr in self._traceroutes:
            route = tr.route  # list of intermediate node_nums
            from_node = tr.from_node
            to_node = tr.to_node
            snr_towards = tr.snr_towards or []

            # Build the full path: [from_node] + route + [to_node]
            full_path = [from_node] + list(route) + [to_node]

            # Each snr_towards[i] is the SNR for the link full_path[i] -> full_path[i+1]
            for i, snr_val in enumerate(snr_towards):
                if i + 1 < len(full_path) and snr_val is not None:
                    link_from = full_path[i]
                    link_to = full_path[i + 1]
                    # Find matching edge and update SNR if better than existing
                    for edge in self._edges:
                        if (edge.from_node == link_from and edge.to_node == link_to) or                            (edge.from_node == link_to and edge.to_node == link_from):
                            if edge.snr is None or abs(snr_val) < abs(edge.snr):
                                edge.snr = snr_val
                                enriched += 1
                            break

            # Also process snr_back for the return path
            snr_back = tr.snr_back or []
            route_back = tr.route_back or list(reversed(route))
            back_path = [to_node] + list(route_back) + [from_node]

            for i, snr_val in enumerate(snr_back):
                if i + 1 < len(back_path) and snr_val is not None:
                    link_from = back_path[i]
                    link_to = back_path[i + 1]
                    for edge in self._edges:
                        if (edge.from_node == link_from and edge.to_node == link_to) or                            (edge.from_node == link_to and edge.to_node == link_from):
                            if edge.snr is None or abs(snr_val) < abs(edge.snr):
                                edge.snr = snr_val
                                enriched += 1
                            break

        if enriched > 0:
            logger.debug(f"Enriched {enriched} edges with SNR from traceroutes")

    def _enrich_edges_from_node_snr(self) -> None:
        """Fallback: use node-level SNR for edges that still have no signal data."""
        enriched = 0
        for edge in self._edges:
            if edge.snr is not None:
                continue
            # Check if either endpoint has node-level SNR
            from_node = self._nodes.get(edge.from_node)
            to_node = self._nodes.get(edge.to_node)
            if to_node and to_node.snr is not None:
                edge.snr = to_node.snr
                if to_node.rssi is not None:
                    edge.rssi = to_node.rssi
                enriched += 1
            elif from_node and from_node.snr is not None:
                edge.snr = from_node.snr
                if from_node.rssi is not None:
                    edge.rssi = from_node.rssi
                enriched += 1

        if enriched > 0:
            logger.debug(f"Enriched {enriched} edges with SNR from node data")

    def _compute_neighbors(self) -> None:
        """Compute neighbor counts and lists from edges."""
        neighbor_map: dict[int, set[int]] = {}

        for edge in self._edges:
            if edge.from_node not in neighbor_map:
                neighbor_map[edge.from_node] = set()
            if edge.to_node not in neighbor_map:
                neighbor_map[edge.to_node] = set()

            neighbor_map[edge.from_node].add(edge.to_node)
            neighbor_map[edge.to_node].add(edge.from_node)

        for node_num, neighbors in neighbor_map.items():
            if node_num in self._nodes:
                self._nodes[node_num].neighbors = list(neighbors)
                self._nodes[node_num].neighbor_count = len(neighbors)

    # =========================================================================
    # Packet Processing
    # =========================================================================

    def _enrich_packet_counts(self) -> None:
        """Enrich nodes with packet counts from packet_log."""
        if not self._db:
            return

        now = time.time()
        cutoff_24h = now - 86400

        try:
            cursor = self._db.execute(
                """
                SELECT from_node, portnum_name, COUNT(*) as cnt
                FROM packet_log
                WHERE timestamp > ?
                GROUP BY from_node, portnum_name
                """,
                (cutoff_24h,),
            )

            for row in cursor:
                node_num = row["from_node"]
                portnum = row["portnum_name"] or "UNKNOWN"
                count = row["cnt"]

                if node_num in self._nodes:
                    node = self._nodes[node_num]
                    node.packets_sent_24h += count
                    node.packets_by_type[portnum] = (
                        node.packets_by_type.get(portnum, 0) + count
                    )
                    if "TEXT" in portnum.upper():
                        node.text_messages_24h += count

        except Exception as e:
            logger.warning(f"Failed to enrich packet counts: {e}")

    # =========================================================================
    # Historical Enrichment
    # =========================================================================

    def _enrich_historical(self) -> None:
        """Enrich nodes with historical data from SQLite."""
        if not self._db:
            return

        now = time.time()

        for node_num, node in self._nodes.items():
            # Get packet counts by window
            node.packets_sent_48h = self._count_packets(node_num, now - 172800)
            node.packets_sent_7d = self._count_packets(node_num, now - 604800)
            node.packets_sent_14d = self._count_packets(node_num, now - 1209600)

            # Get daily packet counts
            node.daily_packet_counts = self._get_daily_counts(node_num)

            # Get battery trend
            trend = self._compute_battery_trend(node_num, node.battery_percent)
            node.battery_trend = trend["trend"]
            node.predicted_depletion_hours = trend.get("predicted_depletion_hours")

    def _count_packets(self, node_num: int, since: float) -> int:
        """Count packets from a node since a timestamp."""
        if not self._db:
            return 0

        try:
            cursor = self._db.execute(
                "SELECT COUNT(*) FROM packet_log WHERE from_node = ? AND timestamp > ?",
                (node_num, since),
            )
            row = cursor.fetchone()
            return row[0] if row else 0
        except Exception:
            return 0

    def _get_daily_counts(self, node_num: int) -> dict[str, int]:
        """Get daily packet counts for a node."""
        if not self._db:
            return {}

        try:
            cursor = self._db.execute(
                """
                SELECT date, packets_sent
                FROM node_daily_traffic
                WHERE node_num = ?
                ORDER BY date DESC
                LIMIT 14
                """,
                (node_num,),
            )
            return {row["date"]: row["packets_sent"] for row in cursor}
        except Exception:
            return {}

    def _compute_battery_trend(
        self, node_num: int, current_battery: Optional[float]
    ) -> dict:
        """Compute battery trend from historical telemetry."""
        result = {"trend": None, "predicted_depletion_hours": None}

        if current_battery is None or not self._db:
            return result

        try:
            # Get battery reading from ~24h ago
            cutoff = time.time() - 86400
            cursor = self._db.execute(
                """
                SELECT value FROM telemetry_log
                WHERE node_num = ? AND telemetry_type = 'batteryLevel'
                AND timestamp > ? AND timestamp < ?
                ORDER BY timestamp ASC
                LIMIT 1
                """,
                (node_num, cutoff - 3600, cutoff + 3600),
            )
            row = cursor.fetchone()

            if row:
                old_battery = row["value"]
                diff = current_battery - old_battery

                if diff > 2:
                    result["trend"] = "charging"
                elif diff < -5:
                    result["trend"] = "declining"
                    # Linear extrapolation
                    if current_battery > 0:
                        hours_elapsed = 24
                        rate_per_hour = abs(diff) / hours_elapsed
                        if rate_per_hour > 0:
                            result["predicted_depletion_hours"] = (
                                current_battery / rate_per_hour
                            )
                else:
                    result["trend"] = "stable"

        except Exception as e:
            logger.debug(f"Failed to compute battery trend for {node_num}: {e}")

        return result

    def _enrich_environmental(self) -> None:
        """Enrich nodes with environmental telemetry from SQLite.

        Queries the most recent reading for each environmental telemetry type
        and populates the corresponding UnifiedNode fields.
        """
        if not self._db:
            return

        # Environmental telemetry types to query (subset of TELEMETRY_TYPE_MAP)
        env_types = [
            "temperature", "humidity", "relativeHumidity", "pressure",
            "barometricPressure", "gasResistance", "iaq", "lux", "lightLevel",
            "windSpeed", "wind_speed", "windDirection", "wind_direction",
            "rainfall", "rain", "pm1_0", "pm2_5", "pm10", "pm10_standard",
            "ch3Voltage", "ch3Current", "extVoltage", "current",
            "heartRate", "heart_rate", "spo2", "bodyTemperature",
            "radiationCpm", "uvIndex", "uv_index",
        ]

        # Only look at recent telemetry (last 2 hours)
        cutoff = time.time() - 7200

        try:
            # Get most recent reading for each node/type combination
            placeholders = ",".join("?" * len(env_types))
            cursor = self._db.execute(
                f"""
                SELECT node_num, telemetry_type, value, MAX(timestamp) as ts
                FROM telemetry_log
                WHERE telemetry_type IN ({placeholders})
                AND timestamp > ?
                GROUP BY node_num, telemetry_type
                """,
                (*env_types, cutoff),
            )

            for row in cursor:
                node_num = row["node_num"]
                telem_type = row["telemetry_type"]
                value = row["value"]

                if node_num not in self._nodes:
                    continue

                node = self._nodes[node_num]

                # Map telemetry type to UnifiedNode field
                field_name = TELEMETRY_TYPE_MAP.get(telem_type)
                if field_name and hasattr(node, field_name):
                    try:
                        setattr(node, field_name, float(value))
                    except (ValueError, TypeError):
                        pass

            # Set sensor type flags based on populated fields
            for node in self._nodes.values():
                node.has_environment_sensor = any([
                    node.temperature is not None,
                    node.humidity is not None,
                    node.barometric_pressure is not None,
                    node.gas_resistance is not None,
                    node.iaq is not None,
                ])
                node.has_air_quality_sensor = any([
                    node.pm1_0 is not None,
                    node.pm2_5 is not None,
                    node.pm10 is not None,
                ])
                node.has_power_sensor = any([
                    node.ext_voltage is not None,
                    node.ext_current is not None,
                ])
                node.has_weather_station = any([
                    node.wind_speed is not None,
                    node.wind_direction is not None,
                    node.rainfall is not None,
                ])
                node.has_health_sensor = any([
                    node.heart_rate is not None,
                    node.spo2 is not None,
                    node.body_temperature is not None,
                ])

        except Exception as e:
            logger.warning(f"Failed to enrich environmental data: {e}")


    def _enrich_feeder_data(self):
        """Populate feeder gateway info on UnifiedNode from source sampling.

        Each Meshview source has ONE gateway feeding it. By aggregating
        packets_seen data from ALL sources for the same sender node,
        we discover which gateways hear that node.
        """
        # First, build a map of gateway node_id -> node info for name lookup
        gateway_names = {}
        for node in self._nodes.values():
            gateway_names[node.node_num] = node.short_name or node.long_name or ""

        # Aggregate feeder data from all sources
        # Key insight: each source's feeder_data tells us that source's gateway heard certain nodes
        for source_name, source in self._sources.items():
            if not hasattr(source, 'feeder_data'):
                continue

            feeder_data = source.feeder_data
            if not feeder_data:
                continue

            for from_node_raw, gateways in feeder_data.items():
                # Normalize from_node to int
                node_num = self._normalize_node_id(from_node_raw)
                if node_num is None or node_num not in self._nodes:
                    continue

                node = self._nodes[node_num]

                # Merge gateways from this source
                existing_gw_ids = {g["gateway_id"] for g in node.feeder_gateways}

                for gw in gateways:
                    gw_id = gw["gateway_id"]
                    if gw_id in existing_gw_ids:
                        # Merge signal data for existing gateway
                        for existing_gw in node.feeder_gateways:
                            if existing_gw["gateway_id"] == gw_id:
                                # Average the signal values
                                if gw.get("avg_rssi") is not None:
                                    if existing_gw.get("avg_rssi") is not None:
                                        existing_gw["avg_rssi"] = (existing_gw["avg_rssi"] + gw["avg_rssi"]) / 2
                                    else:
                                        existing_gw["avg_rssi"] = gw["avg_rssi"]
                                if gw.get("avg_snr") is not None:
                                    if existing_gw.get("avg_snr") is not None:
                                        existing_gw["avg_snr"] = (existing_gw["avg_snr"] + gw["avg_snr"]) / 2
                                    else:
                                        existing_gw["avg_snr"] = gw["avg_snr"]
                                existing_gw["packet_count"] = existing_gw.get("packet_count", 0) + gw.get("packet_count", 1)
                                break
                    else:
                        # Add new gateway entry
                        gw_node_id = self._normalize_node_id(gw_id)
                        gw_name = gw.get("gateway_name") or ""
                        if not gw_name and gw_node_id:
                            gw_name = gateway_names.get(gw_node_id, gw.get("gateway_hex", gw_id))

                        node.feeder_gateways.append({
                            "gateway_id": gw_id,
                            "gateway_name": gw_name,
                            "avg_rssi": gw.get("avg_rssi"),
                            "avg_snr": gw.get("avg_snr"),
                            "packet_count": gw.get("packet_count", 1),
                            "source": source_name,
                        })
                        existing_gw_ids.add(gw_id)

        # Update summary fields for all nodes with feeder data
        for node in self._nodes.values():
            if not node.feeder_gateways:
                continue

            node.feeder_count = len(node.feeder_gateways)

            # Best = strongest average RSSI (closest to 0)
            with_rssi = [g for g in node.feeder_gateways if g.get("avg_rssi") is not None]
            if with_rssi:
                best = max(with_rssi, key=lambda g: g["avg_rssi"])
                worst = min(with_rssi, key=lambda g: g["avg_rssi"])
                node.feeder_best = best.get("gateway_name") or best["gateway_id"]
                node.feeder_worst = worst.get("gateway_name") or worst["gateway_id"]

        # Log summary
        nodes_with_feeders = sum(1 for n in self._nodes.values() if n.feeder_count > 0)
        if nodes_with_feeders > 0:
            total_gw = set()
            for n in self._nodes.values():
                for gw in n.feeder_gateways:
                    total_gw.add(gw["gateway_id"])
            logger.info(f"Feeder enrichment: {nodes_with_feeders} nodes, {len(total_gw)} unique gateways")

    def _enrich_deliverability(self) -> None:
        """Compute deliverability metrics from node source overlap.

        Each Meshview/MeshMonitor source represents a gateway's view of the mesh.
        If a node is seen by N sources, its packets are reaching N gateways.
        This uses node visibility as a proxy for packet deliverability.
        """
        total_sources = len(self._sources)
        if total_sources == 0:
            return

        # Count Meshview sources specifically (these are the MQTT gateways)
        meshview_count = sum(
            1 for src in self._sources.values()
            if isinstance(src, MeshviewSource)
        )

        nodes_with_data = 0
        total_gateway_sum = 0
        max_gateways_seen = 0

        for node in self._nodes.values():
            if not node.sources:
                continue

            # Count how many sources see this node
            source_count = len(node.sources)

            # Set per-node metrics
            node.avg_gateways = float(source_count)
            node.max_gateways = source_count
            node.source_reach = float(source_count)

            # Deliverability score: % of max possible sources
            node.deliverability_score = (source_count / total_sources) * 100

            nodes_with_data += 1
            total_gateway_sum += source_count
            max_gateways_seen = max(max_gateways_seen, source_count)

        # Compute mesh-wide metrics
        if nodes_with_data > 0:
            mesh_avg = total_gateway_sum / nodes_with_data
            self._deliverability = {
                "avg_gateways": mesh_avg,
                "max_gateways": max_gateways_seen,
                "total_sources": total_sources,
                "meshview_sources": meshview_count,
                "nodes_with_data": nodes_with_data,
                "source": "node_source_overlap",
            }

            # Distribution: how many nodes reach N+ gateways
            dist = {}
            for threshold in range(1, total_sources + 1):
                count = sum(
                    1 for n in self._nodes.values()
                    if n.avg_gateways is not None and n.avg_gateways >= threshold
                )
                dist[f"reaching_{threshold}_plus"] = count

            self._deliverability["distribution"] = dist

            logger.info(
                f"Deliverability: {nodes_with_data} nodes, "
                f"avg {mesh_avg:.2f} gateways/node, "
                f"max {max_gateways_seen}/{total_sources} sources"
            )
        else:
            # Fallback to single-source ratio if no node overlap data
            for name, source in self._sources.items():
                if isinstance(source, MeshviewSource):
                    counts = source.counts
                    if counts:
                        tp = counts.get("total_packets", 0)
                        ts = counts.get("total_seen", 0)
                        if tp > 0:
                            self._deliverability = {
                                "avg_gateways": ts / tp,
                                "total_sources": total_sources,
                                "nodes_with_data": 0,
                                "source": "single_source_fallback",
                            }
                            return

    def get_coverage_gaps(self) -> list[dict]:
        """Get nodes with poor coverage (low gateway reach).

        Returns:
            List of dicts with node info and coverage metrics
        """
        gaps = []
        for node in self._nodes.values():
            # Only check nodes with traffic data
            if node.packets_sent_24h == 0:
                continue

            # No coverage data available
            if node.avg_gateways is None:
                continue

            # Flag nodes with avg_gateways < 1.5
            if node.avg_gateways < 1.5:
                gaps.append({
                    "node_num": node.node_num,
                    "short_name": node.short_name,
                    "avg_gateways": node.avg_gateways,
                    "deliverability_score": node.deliverability_score,
                    "role": node.role,
                    "is_infrastructure": node.role in {
                        "ROUTER", "ROUTER_LATE", "ROUTER_CLIENT", "REPEATER"
                    },
                })

        # Sort by avg_gateways ascending (worst coverage first)
        gaps.sort(key=lambda x: x["avg_gateways"] or 0)
        return gaps

    # =========================================================================
    # SQLite Persistence
    # =========================================================================

    def _store_snapshot(self) -> None:
        """Store current state snapshot to SQLite."""
        if not self._db:
            return

        now = time.time()

        try:
            # Store node snapshots
            for node in self._nodes.values():
                self._db.execute(
                    """
                    INSERT INTO node_snapshots (
                        timestamp, node_num, short_name, long_name, role, hw_model,
                        latitude, longitude, last_heard, is_online,
                        battery_percent, voltage, packets_sent_24h, packets_seen_24h,
                        channel_utilization, air_util_tx, uplink_enabled,
                        neighbor_count, hops_away, snr, source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        now,
                        node.node_num,
                        node.short_name,
                        node.long_name,
                        node.role,
                        node.hw_model,
                        node.latitude,
                        node.longitude,
                        node.last_heard,
                        1 if node.is_online else 0,
                        node.battery_percent,
                        node.voltage,
                        node.packets_sent_24h,
                        node.packets_seen_24h,
                        node.channel_utilization,
                        node.air_util_tx,
                        1 if node.uplink_enabled else 0,
                        node.neighbor_count,
                        node.hops_away,
                        node.snr,
                        ",".join(node.sources),
                    ),
                )

            # Store mesh snapshot
            active_nodes = sum(1 for n in self._nodes.values() if n.is_online)
            infra_nodes = [
                n
                for n in self._nodes.values()
                if n.role in ("ROUTER", "ROUTER_CLIENT", "ROUTER_LATE")
            ]
            infra_online = sum(1 for n in infra_nodes if n.is_online)

            battery_values = [
                n.battery_percent
                for n in self._nodes.values()
                if n.battery_percent is not None
            ]
            avg_battery = (
                sum(battery_values) / len(battery_values) if battery_values else None
            )

            util_values = [
                n.channel_utilization
                for n in self._nodes.values()
                if n.channel_utilization is not None
            ]
            avg_util = sum(util_values) / len(util_values) if util_values else None

            total_packets = sum(n.packets_sent_24h for n in self._nodes.values())

            self._db.execute(
                """
                INSERT INTO mesh_snapshots (
                    timestamp, total_nodes, active_nodes, infra_online, infra_total,
                    total_packets_24h, avg_channel_utilization, avg_battery_percent,
                    source_count, avg_gateways_mesh, total_packets_global, total_seen_global
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    len(self._nodes),
                    active_nodes,
                    infra_online,
                    len(infra_nodes),
                    total_packets,
                    avg_util,
                    avg_battery,
                    len(self._sources),
                    self._deliverability.get("avg_gateways"),
                    self._deliverability.get("total_packets"),
                    self._deliverability.get("total_seen"),
                ),
            )

            # Store packets from MeshMonitor
            for name, source in self._sources.items():
                if isinstance(source, MeshMonitorDataSource):
                    if hasattr(source, "packets"):
                        for pkt in source.packets:
                            self._store_packet(pkt, name)

            # Store telemetry from MeshMonitor
            for name, source in self._sources.items():
                if isinstance(source, MeshMonitorDataSource):
                    for telem in source.telemetry:
                        self._store_telemetry(telem, name)

            # Store daily traffic
            for dt in self._daily_traffic:
                self._db.execute(
                    """
                    INSERT OR REPLACE INTO traffic_daily (date, total_packets, packets_by_type, source)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        dt.date,
                        dt.total_packets,
                        json.dumps(dt.packets_by_type),
                        dt.source,
                    ),
                )

            # Update node daily traffic
            today = datetime.now().strftime("%Y-%m-%d")
            for node in self._nodes.values():
                if node.packets_sent_24h > 0:
                    self._db.execute(
                        """
                        INSERT OR REPLACE INTO node_daily_traffic
                        (date, node_num, packets_sent, packets_by_type, source)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            today,
                            node.node_num,
                            node.packets_sent_24h,
                            json.dumps(node.packets_by_type),
                            ",".join(node.sources),
                        ),
                    )

            self._db.commit()

        except Exception as e:
            logger.error(f"Failed to store snapshot: {e}")
            self._db.rollback()

    def _store_packet(self, pkt: dict, source_name: str) -> None:
        """Store a packet to packet_log (deduped by packet_id)."""
        packet_id = pkt.get("packet_id") or pkt.get("id")
        if packet_id is None:
            return

        # Normalize from_node
        from_node = pkt.get("from_node")
        if isinstance(from_node, str):
            try:
                from_node = int(from_node.lstrip("!"), 16)
            except ValueError:
                from_node = None

        to_node = pkt.get("to_node")
        if isinstance(to_node, str):
            try:
                to_node = int(to_node.lstrip("!"), 16)
            except ValueError:
                to_node = None

        try:
            self._db.execute(
                """
                INSERT OR IGNORE INTO packet_log (
                    packet_id, timestamp, from_node, to_node,
                    portnum, portnum_name, channel, snr, rssi,
                    hop_limit, hop_start, payload_size, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(packet_id),
                    pkt.get("timestamp") or 0,
                    from_node,
                    to_node,
                    pkt.get("portnum"),
                    pkt.get("portnum_name"),
                    pkt.get("channel"),
                    pkt.get("snr"),
                    pkt.get("rssi"),
                    pkt.get("hop_limit"),
                    pkt.get("hop_start"),
                    pkt.get("payload_size"),
                    source_name,
                ),
            )
        except Exception:
            pass  # Ignore duplicates

    def _store_telemetry(self, telem: dict, source_name: str) -> None:
        """Store telemetry to telemetry_log (deduped)."""
        node_num = telem.get("nodeNum")
        if node_num is None:
            # Try to extract from nodeId
            node_id = telem.get("nodeId") or ""
            if node_id.startswith("!"):
                try:
                    node_num = int(node_id[1:], 16)
                except ValueError:
                    return
            else:
                return

        telem_type = telem.get("telemetryType")
        timestamp = telem.get("timestamp") or telem.get("createdAt") or 0
        value = telem.get("value")

        if not telem_type or value is None:
            return

        # Normalize timestamp
        if timestamp > 1e12:
            timestamp = timestamp / 1000

        try:
            self._db.execute(
                """
                INSERT OR IGNORE INTO telemetry_log
                (timestamp, node_num, telemetry_type, value, unit, source)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    node_num,
                    telem_type,
                    value,
                    telem.get("unit"),
                    source_name,
                ),
            )
        except Exception:
            pass  # Ignore duplicates

    def _purge_old_data(self) -> None:
        """Purge data older than 14 days."""
        if not self._db:
            return

        cutoff = time.time() - 1209600  # 14 days
        cutoff_date = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")

        try:
            self._db.execute(
                "DELETE FROM node_snapshots WHERE timestamp < ?", (cutoff,)
            )
            self._db.execute(
                "DELETE FROM mesh_snapshots WHERE timestamp < ?", (cutoff,)
            )
            self._db.execute("DELETE FROM packet_log WHERE timestamp < ?", (cutoff,))
            self._db.execute(
                "DELETE FROM telemetry_log WHERE timestamp < ?", (cutoff,)
            )
            self._db.execute(
                "DELETE FROM traffic_daily WHERE date < ?", (cutoff_date,)
            )
            self._db.execute(
                "DELETE FROM node_daily_traffic WHERE date < ?", (cutoff_date,)
            )
            self._db.commit()
        except Exception as e:
            logger.warning(f"Failed to purge old data: {e}")

    # =========================================================================
    # Time-Windowed Queries
    # =========================================================================

    def _window_to_seconds(self, window: str) -> int:
        """Convert window string to seconds."""
        return TIME_WINDOWS.get(window, 86400)

    def get_node_history(self, node_num: int, window: str = "14d") -> list[dict]:
        """Get historical snapshots for a node."""
        if not self._db:
            return []

        cutoff = time.time() - self._window_to_seconds(window)

        try:
            cursor = self._db.execute(
                """
                SELECT * FROM node_snapshots
                WHERE node_num = ? AND timestamp > ?
                ORDER BY timestamp
                """,
                (node_num, cutoff),
            )
            return [dict(row) for row in cursor]
        except Exception:
            return []

    def get_node_packet_history(
        self, node_num: int, window: str = "14d"
    ) -> dict[str, int]:
        """Get daily packet counts for a node."""
        if not self._db:
            return {}

        cutoff_date = (
            datetime.now() - timedelta(seconds=self._window_to_seconds(window))
        ).strftime("%Y-%m-%d")

        try:
            cursor = self._db.execute(
                """
                SELECT date, packets_sent FROM node_daily_traffic
                WHERE node_num = ? AND date >= ?
                ORDER BY date
                """,
                (node_num, cutoff_date),
            )
            return {row["date"]: row["packets_sent"] for row in cursor}
        except Exception:
            return {}

    def get_node_battery_trend(
        self, node_num: int, window: str = "7d"
    ) -> list[tuple[float, float]]:
        """Get battery readings over time."""
        if not self._db:
            return []

        cutoff = time.time() - self._window_to_seconds(window)

        try:
            cursor = self._db.execute(
                """
                SELECT timestamp, value FROM telemetry_log
                WHERE node_num = ? AND telemetry_type = 'batteryLevel'
                AND timestamp > ?
                ORDER BY timestamp
                """,
                (node_num, cutoff),
            )
            return [(row["timestamp"], row["value"]) for row in cursor]
        except Exception:
            return []

    def get_mesh_traffic_history(self, window: str = "14d") -> list[dict]:
        """Get daily mesh-wide traffic."""
        if not self._db:
            return []

        cutoff_date = (
            datetime.now() - timedelta(seconds=self._window_to_seconds(window))
        ).strftime("%Y-%m-%d")

        try:
            cursor = self._db.execute(
                """
                SELECT date, SUM(total_packets) as total, packets_by_type
                FROM traffic_daily
                WHERE date >= ?
                GROUP BY date
                ORDER BY date
                """,
                (cutoff_date,),
            )
            results = []
            for row in cursor:
                results.append(
                    {
                        "date": row["date"],
                        "total_packets": row["total"],
                        "packets_by_type": json.loads(row["packets_by_type"] or "{}"),
                    }
                )
            return results
        except Exception:
            return []

    def get_top_senders(self, n: int = 10, window: str = "24h") -> list[UnifiedNode]:
        """Get top N nodes by packet count."""
        nodes = sorted(
            self._nodes.values(), key=lambda x: x.packets_sent_24h, reverse=True
        )
        return nodes[:n]

    def get_packets_by_node(self, window: str = "24h") -> dict[int, dict]:
        """Get packet counts per node with portnum breakdown."""
        result = {}
        for node_num, node in self._nodes.items():
            if node.packets_sent_24h > 0:
                result[node_num] = {
                    "total": node.packets_sent_24h,
                    "by_type": node.packets_by_type.copy(),
                }
        return result

    def get_packet_type_breakdown(self, window: str = "24h") -> dict[str, int]:
        """Mesh-wide packet count by portnum type."""
        breakdown: dict[str, int] = {}
        for node in self._nodes.values():
            for portnum, count in node.packets_by_type.items():
                breakdown[portnum] = breakdown.get(portnum, 0) + count
        return breakdown

    # =========================================================================
    # Consumer API
    # =========================================================================

    @property
    def nodes(self) -> dict[int, UnifiedNode]:
        """Get all nodes keyed by node_num."""
        return self._nodes

    @property
    def edges(self) -> list[UnifiedEdge]:
        """Get all edges."""
        return self._edges

    @property
    def traceroutes(self) -> list[UnifiedTraceroute]:
        """Get all traceroutes."""
        return self._traceroutes

    @property
    def channels(self) -> list[UnifiedChannel]:
        """Get all channels."""
        return self._channels

    @property
    def solar(self) -> list[UnifiedSolar]:
        """Get all solar data."""
        return self._solar

    @property
    def data_age_seconds(self) -> float:
        """How old the current live state is."""
        if self._last_refresh == 0:
            return float("inf")
        return time.time() - self._last_refresh

    @property
    def is_loaded(self) -> bool:
        """Check if data has been loaded."""
        return self._is_loaded

    @property
    def source_count(self) -> int:
        """Number of registered sources."""
        return len(self._sources)

    @property
    def node_count(self) -> int:
        """Number of nodes in live state."""
        return len(self._nodes)

    @property
    def total_packets_24h(self) -> int:
        """Total packets in last 24h."""
        return sum(n.packets_sent_24h for n in self._nodes.values())

    def get_node(self, identifier: str | int) -> Optional[UnifiedNode]:
        """Get a node by node_num, hex ID, shortname, or longname."""
        # Try direct node_num lookup
        if isinstance(identifier, int):
            return self._nodes.get(identifier)

        # Try parsing as int
        if isinstance(identifier, str) and identifier.isdigit():
            return self._nodes.get(int(identifier))

        # Try hex ID
        if isinstance(identifier, str) and identifier.startswith("!"):
            try:
                node_num = int(identifier[1:], 16)
                return self._nodes.get(node_num)
            except ValueError:
                pass

        # Search by name (case-insensitive)
        identifier_lower = identifier.lower() if isinstance(identifier, str) else ""
        for node in self._nodes.values():
            if node.short_name.lower() == identifier_lower:
                return node
            if node.long_name.lower() == identifier_lower:
                return node

        return None

    def get_infrastructure_nodes(self) -> list[UnifiedNode]:
        """Get all infrastructure nodes (routers, repeaters)."""
        infra_roles = {"ROUTER", "ROUTER_CLIENT", "ROUTER_LATE", "REPEATER"}
        return [n for n in self._nodes.values() if n.role in infra_roles]

    def get_low_battery_nodes(self, threshold: float = 20.0) -> list[UnifiedNode]:
        """Get nodes with low battery."""
        return [
            n
            for n in self._nodes.values()
            if n.battery_percent is not None and n.battery_percent < threshold
        ]

    def get_high_traffic_nodes(self, threshold: int = 500) -> list[UnifiedNode]:
        """Get nodes with high packet counts."""
        return [n for n in self._nodes.values() if n.packets_sent_24h > threshold]

    def get_mesh_utilization(self) -> dict:
        """Get mesh-wide utilization statistics."""
        util_nodes = [
            n for n in self._nodes.values() if n.channel_utilization is not None
        ]

        if not util_nodes:
            return {"avg": None, "max": None, "max_node": None, "node_count": 0}

        utils = [n.channel_utilization for n in util_nodes]
        max_util = max(utils)
        max_node = next(n for n in util_nodes if n.channel_utilization == max_util)

        return {
            "avg": sum(utils) / len(utils),
            "max": max_util,
            "max_node": max_node.short_name or max_node.node_id_hex,
            "node_count": len(util_nodes),
        }

    def get_mesh_deliverability(self) -> dict:
        """Get mesh-wide deliverability metrics.

        Returns:
            Dict with avg_gateways, max_gateways, total_sources, nodes_with_data, etc.
        """
        result = self._deliverability.copy()

        # Add computed summary if we have per-node data
        nodes_with_gw = [n for n in self._nodes.values() if n.avg_gateways is not None]
        if nodes_with_gw:
            result["computed_avg"] = sum(n.avg_gateways for n in nodes_with_gw) / len(nodes_with_gw)
            result["computed_max"] = max(n.max_gateways or 0 for n in nodes_with_gw)
            result["computed_nodes"] = len(nodes_with_gw)

        return result

    def get_node_deliverability(self, node_num: int) -> Optional[dict]:
        """Get per-node deliverability if available.

        Currently returns None as per-node tracking is not implemented.
        Future: could sample packets_seen endpoint for specific nodes.

        Returns:
            Dict with avg_gateways, deliverability_score or None
        """
        # Per-node deliverability would require sampling packets_seen endpoint
        # Not implemented to avoid excessive API calls
        return None

    def get_status(self) -> list[dict]:
        """Get status of all sources."""
        status_list = []
        for name, source in self._sources.items():
            # Determine source type
            if isinstance(source, MeshviewSource):
                src_type = "meshview"
            elif isinstance(source, MeshMonitorDataSource):
                src_type = "meshmonitor"
            elif isinstance(source, MQTTSource):
                src_type = "mqtt"
            else:
                src_type = "unknown"

            status = {
                "name": name,
                "type": src_type,
                "enabled": True,
                "is_loaded": source.is_loaded,
                "last_refresh": source.last_refresh,
                "last_error": source.last_error,
                "node_count": len(source.nodes),
            }

            if isinstance(source, MeshviewSource):
                status["edge_count"] = len(source.edges)
            elif isinstance(source, MeshMonitorDataSource):
                status["telemetry_count"] = len(source.telemetry)
                status["traceroute_count"] = len(source.traceroutes)
                status["channel_count"] = len(source.channels)
            elif isinstance(source, MQTTSource):
                health = source.health_status
                status["is_connected"] = health.get("is_connected", False)
                status["message_count"] = health.get("message_count", 0)
                status["last_message"] = health.get("last_message", 0)
                status["host"] = health.get("host", "")
                status["port"] = health.get("port", 0)
                status["topic_root"] = health.get("topic_root", "")

            status_list.append(status)

        return status_list

    @property
    def dedup_stats(self) -> dict:
        """Get deduplication statistics."""
        raw_nodes = sum(len(s.nodes) for s in self._sources.values())
        raw_edges = sum(
            len(s.edges)
            for s in self._sources.values()
            if isinstance(s, MeshviewSource)
        )

        return {
            "raw_node_count": raw_nodes,
            "dedup_node_count": len(self._nodes),
            "node_duplicates": raw_nodes - len(self._nodes),
            "raw_edge_count": raw_edges,
            "dedup_edge_count": len(self._edges),
            "edge_duplicates": raw_edges - len(self._edges),
        }

    # =========================================================================
    # Environmental Queries
    # =========================================================================

    def get_sensor_nodes(self, sensor_type: str = "environment") -> list[UnifiedNode]:
        """Get nodes that have sensor data.

        Args:
            sensor_type: "environment", "air_quality", "weather", "power", "health"

        Returns:
            List of nodes with the specified sensor type
        """
        flag_map = {
            "environment": "has_environment_sensor",
            "air_quality": "has_air_quality_sensor",
            "weather": "has_weather_station",
            "power": "has_power_sensor",
            "health": "has_health_sensor",
        }
        flag = flag_map.get(sensor_type, "has_environment_sensor")
        return [n for n in self._nodes.values() if getattr(n, flag, False)]

    def get_regional_environment(self, region: str) -> dict:
        """Get aggregated environment metrics for a region.

        Args:
            region: Region name to query

        Returns:
            Dict with min/max/avg for temp, humidity, pressure, pm2_5
        """
        nodes = [
            n for n in self._nodes.values()
            if n.region == region and n.has_environment_sensor
        ]
        result = {}

        for metric, field in [
            ("temperature", "temperature"),
            ("humidity", "humidity"),
            ("pressure", "barometric_pressure"),
            ("pm2_5", "pm2_5"),
        ]:
            vals = [getattr(n, field) for n in nodes if getattr(n, field) is not None]
            if vals:
                result[metric] = {
                    "min": min(vals),
                    "max": max(vals),
                    "avg": sum(vals) / len(vals),
                    "count": len(vals),
                }

        return result

    # === Backwards compatibility methods for mesh_health.py ===

    def get_all_nodes(self) -> list[dict]:
        """Get all nodes as list of dicts (backwards compatibility).

        Returns nodes in the format expected by mesh_health.py.
        """
        result = []
        for node_num, node in self._nodes.items():
            node_dict = {
                "_node_num": node_num,
                "nodeNum": node_num,
                "id": node.node_id_hex,
                "shortName": node.short_name,
                "short_name": node.short_name,
                "longName": node.long_name,
                "long_name": node.long_name,
                "role": node.role,
                "hwModel": node.hw_model,
                "latitude": node.latitude,
                "longitude": node.longitude,
                "lat": node.latitude,
                "lon": node.longitude,
                "lastHeard": node.last_heard,
                "last_seen": node.last_heard,
                "batteryLevel": node.battery_percent,
                "battery_percent": node.battery_percent,
                "voltage": node.voltage,
                "channelUtilization": node.channel_utilization,
                "airUtilTx": node.air_util_tx,
                "uptime": node.uptime_seconds,
                "mqtt_gateway": node.is_mqtt_gateway,
                "_sources": list(node.sources),
            }
            result.append(node_dict)
        return result

    def get_all_telemetry(self) -> list[dict]:
        """Get all telemetry records (backwards compatibility).

        Returns telemetry in the format expected by mesh_health.py.
        """
        # Return telemetry from all sources
        result = []
        for source in self._sources.values():
            if hasattr(source, "telemetry"):
                result.extend(source.telemetry)
        return result

    def get_all_packets(self) -> list[dict]:
        """Get all packets (backwards compatibility).

        Returns packets in the format expected by mesh_health.py.
        """
        # Return packets from all sources
        result = []
        for source in self._sources.values():
            if hasattr(source, "packets"):
                result.extend(source.packets)
        return result

    def get_environment_history(
        self, node_num: int, metric: str, window: str = "24h"
    ) -> list[tuple[float, float]]:
        """Get historical environment readings for a node.

        Args:
            node_num: Node to query
            metric: Telemetry type name (e.g., "temperature", "humidity")
            window: Time window

        Returns:
            List of (timestamp, value) tuples
        """
        if not self._db:
            return []

        cutoff = time.time() - self._window_to_seconds(window)

        try:
            cursor = self._db.execute(
                """
                SELECT timestamp, value FROM telemetry_log
                WHERE node_num = ? AND telemetry_type = ?
                AND timestamp > ?
                ORDER BY timestamp
                """,
                (node_num, metric, cutoff),
            )
            return [(row["timestamp"], row["value"]) for row in cursor]
        except Exception:
            return []

    @property
    def _history_conn(self) -> Optional[sqlite3.Connection]:
        """Expose database connection for reporter queries."""
        return self._db


    def get_all_edges(self) -> list[dict]:
        """Get all edges as list of dicts (backwards compatibility).

        Returns edges in the format expected by mesh_health.py.
        """
        result = []
        for edge in self._edges:
            edge_dict = {
                "from_node": edge.from_node,
                "to_node": edge.to_node,
                "snr": edge.snr,
                "rssi": edge.rssi,
                "timestamp": edge.last_seen,
            }
            result.append(edge_dict)
        return result

    def get_all_traceroutes(self) -> list[dict]:
        """Get all traceroutes as list of dicts (backwards compatibility).

        Returns traceroutes in the format expected by mesh_health.py.
        """
        result = []
        for tr in self._traceroutes:
            tr_dict = {
                "from_node": tr.from_node,
                "to_node": tr.to_node,
                "route": tr.route,
                "route_back": tr.route_back,
                "snr_towards": tr.snr_towards,
                "snr_back": tr.snr_back,
                "timestamp": tr.timestamp,
                "request_id": tr.request_id,
            }
            result.append(tr_dict)
        return result


    def get_all_channels(self) -> list[dict]:
        """Get all channels as list of dicts (backwards compatibility).

        Returns channels in the format expected by mesh_health.py.
        """
        result = []
        for ch in self._channels:
            ch_dict = {
                "channel_id": ch.channel_id,
                "name": ch.name,
                "role": ch.role,
                "role_name": ch.role_name,
                "uplink_enabled": ch.uplink_enabled,
                "downlink_enabled": ch.downlink_enabled,
                "position_precision": ch.position_precision,
            }
            result.append(ch_dict)
        return result


    def get_source_coverage(self) -> dict:
        """Get per-source node coverage.

        Returns:
            {
                "meshview-local": {"node_count": 200, "unique_nodes": 50, "shared_nodes": 150},
                "meshview-freq51": {"node_count": 800, "unique_nodes": 400, "shared_nodes": 400},
                ...
            }
        """
        coverage = {}
        for name in self._sources:
            nodes_in_source = [n for n in self._nodes.values() if name in n.sources]
            unique = [n for n in nodes_in_source if len(n.sources) == 1]
            coverage[name] = {
                "node_count": len(nodes_in_source),
                "unique_nodes": len(unique),
                "shared_nodes": len(nodes_in_source) - len(unique),
            }
        return coverage

    def get_nodes_by_source(self, source_name: str) -> list:
        """Get all nodes visible to a specific source."""
        return [n for n in self._nodes.values() if source_name in n.sources]

    def get_exclusive_nodes(self, source_name: str) -> list:
        """Get nodes ONLY visible to this source (not seen by any other)."""
        return [n for n in self._nodes.values() if n.sources == [source_name]]

    def get_source_health(self) -> list[dict]:
        """Get health status of all sources."""
        health = []
        for name, source in self._sources.items():
            if hasattr(source, 'health_status'):
                status = source.health_status
                status["name"] = name
                health.append(status)
            else:
                health.append({
                    "name": name,
                    "is_loaded": source.is_loaded,
                    "last_error": source.last_error,
                    "cached_nodes": len(source.nodes),
                })
        return health


    def get_feeder_map(self) -> dict:
        """Get which gateways hear which nodes.

        Returns:
            {
                "gateway_name": {
                    "gateway_id": str,
                    "nodes_heard": int,
                    "avg_rssi": float,
                    "regions_covered": list[str],
                },
                ...
            }
        """
        gw_map = {}
        for node in self._nodes.values():
            for gw in node.feeder_gateways:
                gw_name = gw.get("gateway_name") or gw["gateway_id"]
                if gw_name not in gw_map:
                    gw_map[gw_name] = {
                        "gateway_id": gw["gateway_id"],
                        "nodes_heard": 0,
                        "rssi_values": [],
                        "regions": set(),
                    }
                gw_map[gw_name]["nodes_heard"] += 1
                if gw.get("avg_rssi") is not None:
                    gw_map[gw_name]["rssi_values"].append(gw["avg_rssi"])
                if node.region:
                    gw_map[gw_name]["regions"].add(node.region)

        # Compute averages
        result = {}
        for name, data in gw_map.items():
            result[name] = {
                "gateway_id": data["gateway_id"],
                "nodes_heard": data["nodes_heard"],
                "avg_rssi": sum(data["rssi_values"]) / len(data["rssi_values"]) if data["rssi_values"] else None,
                "regions_covered": sorted(data["regions"]),
            }
        return result

    def get_node_feeders(self, node_num: int) -> list:
        """Get feeder gateways for a specific node, sorted by signal strength."""
        node = self._nodes.get(node_num)
        if node and node.feeder_gateways:
            return sorted(node.feeder_gateways, key=lambda g: g.get("avg_rssi") or -999, reverse=True)
        return []

    def close(self) -> None:
        """Close database connection."""
        if self._db:
            self._db.close()
            self._db = None
