"""Unified data models for the mesh data pipeline.

These dataclasses represent the normalized, merged data model used by
consumers (health engine, reporter, commands). All field normalization
happens in MeshDataStore before populating these models.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class UnifiedNode:
    """Unified node representation with normalized fields.

    Keyed by node_num (canonical Meshtastic node number).
    All fields have sensible defaults.
    """

    # Identity
    node_num: int
    node_id_hex: str = ""  # "!a3145a04"
    short_name: str = ""
    long_name: str = ""
    role: str = "UNKNOWN"  # ROUTER, CLIENT, etc.
    hw_model: str = ""

    # Position
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    altitude: Optional[float] = None

    # Status (current)
    last_heard: float = 0.0  # Epoch seconds
    is_online: bool = False
    hops_away: Optional[int] = None
    snr: Optional[float] = None
    rssi: Optional[int] = None

    # Power (current)
    battery_percent: Optional[float] = None
    voltage: Optional[float] = None

    # Power (trend - computed from historical store)
    battery_trend: Optional[str] = None  # "charging", "stable", "declining"
    predicted_depletion_hours: Optional[float] = None
    has_solar: bool = False

    # Environment (from sensors - BME280, BME680, SHT31, etc.)
    temperature: Optional[float] = None  # Celsius
    humidity: Optional[float] = None  # Relative humidity %
    barometric_pressure: Optional[float] = None  # hPa
    gas_resistance: Optional[float] = None  # Ohms (AQI proxy)
    iaq: Optional[float] = None  # Indoor Air Quality index

    # Light (BH1750, TSL2591, etc.)
    light_lux: Optional[float] = None  # lux

    # Wind/Weather (DFROBOT_LARK stations)
    wind_speed: Optional[float] = None  # m/s
    wind_direction: Optional[float] = None  # degrees
    rainfall: Optional[float] = None  # mm

    # Air Quality (PMSA003I)
    pm1_0: Optional[float] = None  # µg/m³
    pm2_5: Optional[float] = None  # µg/m³
    pm10: Optional[float] = None  # µg/m³

    # Power monitoring (INA sensors - separate from battery)
    ext_voltage: Optional[float] = None  # External voltage (e.g., solar panel)
    ext_current: Optional[float] = None  # External current (mA)

    # Health (MAX30102, MLX - rare)
    heart_rate: Optional[float] = None  # BPM
    spo2: Optional[float] = None  # Oxygen saturation %
    body_temperature: Optional[float] = None  # Celsius

    # Radiation (RadSens)
    radiation_cpm: Optional[float] = None  # Counts per minute

    # UV (VEML6070, etc.)
    uv_index: Optional[float] = None

    # Sensor type flags (set after populating fields)
    has_environment_sensor: bool = False
    has_air_quality_sensor: bool = False
    has_power_sensor: bool = False
    has_health_sensor: bool = False
    has_weather_station: bool = False

    # Traffic (current, from most recent API data)
    packets_sent_24h: int = 0
    packets_seen_24h: int = 0
    packets_by_type: dict[str, int] = field(default_factory=dict)
    text_messages_24h: int = 0

    # Traffic (historical, from SQLite)
    packets_sent_48h: int = 0
    packets_sent_7d: int = 0
    packets_sent_14d: int = 0
    daily_packet_counts: dict[str, int] = field(default_factory=dict)

    # Device-reported metrics (current)
    channel_utilization: Optional[float] = None
    air_util_tx: Optional[float] = None
    uptime_seconds: Optional[int] = None

    # Connectivity
    uplink_enabled: bool = False
    neighbors: list[int] = field(default_factory=list)
    neighbor_count: int = 0
    traceroute_appearances: int = 0

    # Metadata
    sources: list[str] = field(default_factory=list)
    region: str = ""  # Set by health engine
    locality: str = ""  # Set by health engine

    # Deliverability / Coverage (from Meshview gateway counts)
    avg_gateways: Optional[float] = None  # Avg unique gateways that hear this node's packets
    deliverability_score: Optional[float] = None  # % of packets reaching 2+ gateways (0-100)
    max_gateways: Optional[int] = None  # Max gateways any single packet reached
    source_reach: Optional[float] = None  # Avg number of Meshview sources that see this node's packets

    # Additional MeshMonitor fields
    firmware_version: str = ""
    public_key: str = ""
    is_mqtt_gateway: bool = False
    via_mqtt: bool = False


@dataclass
class UnifiedEdge:
    """Unified edge (link) between two nodes."""

    from_node: int
    to_node: int
    snr: Optional[float] = None
    rssi: Optional[int] = None
    quality: Optional[float] = None
    last_seen: float = 0.0
    sources: list[str] = field(default_factory=list)


@dataclass
class UnifiedTraceroute:
    """Unified traceroute record."""

    from_node: int
    to_node: int
    route: list[int] = field(default_factory=list)  # Node nums in path
    route_back: list[int] = field(default_factory=list)
    snr_towards: list[float] = field(default_factory=list)
    snr_back: list[float] = field(default_factory=list)
    timestamp: float = 0.0
    request_id: int = 0
    sources: list[str] = field(default_factory=list)


@dataclass
class UnifiedChannel:
    """Unified channel configuration."""

    channel_id: int
    name: str = ""
    role: int = 0  # 0=disabled, 1=primary, 2=secondary
    role_name: str = ""
    uplink_enabled: bool = False
    downlink_enabled: bool = False
    position_precision: int = 0
    sources: list[str] = field(default_factory=list)


@dataclass
class UnifiedSolar:
    """Solar estimate for a node."""

    node_num: int
    estimate_watts: Optional[float] = None
    confidence: Optional[float] = None
    timestamp: float = 0.0
    sources: list[str] = field(default_factory=list)


@dataclass
class DailyTraffic:
    """Per-day aggregate traffic counts."""

    date: str  # "2026-05-04"
    total_packets: int = 0
    packets_by_type: dict[str, int] = field(default_factory=dict)
    source: str = ""


@dataclass
class NodeSnapshot:
    """Point-in-time snapshot of a node's metrics."""

    timestamp: float
    node_num: int
    short_name: str = ""
    long_name: str = ""
    role: str = ""
    hw_model: str = ""
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    last_heard: float = 0.0
    is_online: bool = False
    battery_percent: Optional[float] = None
    voltage: Optional[float] = None
    packets_sent_24h: int = 0
    packets_seen_24h: int = 0
    channel_utilization: Optional[float] = None
    air_util_tx: Optional[float] = None
    uplink_enabled: bool = False
    neighbor_count: int = 0
    hops_away: Optional[int] = None
    snr: Optional[float] = None
    sources: str = ""


@dataclass
class MeshSnapshot:
    """Point-in-time snapshot of mesh-wide metrics."""

    timestamp: float
    total_nodes: int = 0
    active_nodes: int = 0
    infra_online: int = 0
    infra_total: int = 0
    total_packets_24h: int = 0
    avg_channel_utilization: Optional[float] = None
    avg_battery_percent: Optional[float] = None
    source_count: int = 0
    # Deliverability metrics
    avg_gateways_mesh: Optional[float] = None
    total_packets_global: Optional[int] = None
    total_seen_global: Optional[int] = None
    unique_feeders: Optional[int] = None
