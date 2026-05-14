"""Configuration management for MeshAI."""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

_config_logger = logging.getLogger(__name__)


@dataclass
class BotConfig:
    """Bot identity and trigger settings."""

    name: str = "ai"
    owner: str = ""
    respond_to_dms: bool = True
    filter_bbs_protocols: bool = True


@dataclass
class ConnectionConfig:
    """Meshtastic connection settings."""

    type: str = "serial"  # serial or tcp
    serial_port: str = "/dev/ttyUSB0"
    tcp_host: str = "192.168.1.100"
    tcp_port: int = 4403


@dataclass
class ResponseConfig:
    """Response behavior settings."""

    delay_min: float = 1.5
    delay_max: float = 2.5
    max_length: int = 200
    max_messages: int = 3


@dataclass
class HistoryConfig:
    """Conversation history settings."""

    database: str = "conversations.db"
    max_messages_per_user: int = 50
    conversation_timeout: int = 86400  # 24 hours

    # Cleanup settings
    auto_cleanup: bool = True
    cleanup_interval_hours: int = 24
    max_age_days: int = 30  # Delete conversations older than this


@dataclass
class MemoryConfig:
    """Rolling summary memory settings."""

    enabled: bool = True  # Enable memory optimization

    # MQTT-specific fields (type=mqtt only)
    host: str = ""              # MQTT broker hostname
    port: int = 1883            # MQTT broker port (1883 plain, 8883 TLS)
    username: str = ""         # MQTT username (optional)
    password: str = ""         # MQTT password (optional, supports )
    topic_root: str = "msh/US"  # Topic root to subscribe to
    use_tls: bool = False       # Enable TLS for MQTT connection
    window_size: int = 4  # Recent message pairs to keep in full
    summarize_threshold: int = 8  # Messages before re-summarizing


@dataclass
class ContextConfig:
    """Passive mesh context settings."""

    enabled: bool = True

    # MQTT-specific fields (type=mqtt only)
    host: str = ""              # MQTT broker hostname
    port: int = 1883            # MQTT broker port (1883 plain, 8883 TLS)
    username: str = ""         # MQTT username (optional)
    password: str = ""         # MQTT password (optional, supports )
    topic_root: str = "msh/US"  # Topic root to subscribe to
    use_tls: bool = False       # Enable TLS for MQTT connection
    observe_channels: list[int] = field(default_factory=list)  # Empty = all channels
    ignore_nodes: list[str] = field(default_factory=list)  # Node IDs to ignore
    max_age: int = 2_592_000  # 30 days in seconds
    max_context_items: int = 20  # Max observations injected into LLM context


@dataclass
class CommandsConfig:
    """Command settings."""

    enabled: bool = True

    # MQTT-specific fields (type=mqtt only)
    host: str = ""              # MQTT broker hostname
    port: int = 1883            # MQTT broker port (1883 plain, 8883 TLS)
    username: str = ""         # MQTT username (optional)
    password: str = ""         # MQTT password (optional, supports )
    topic_root: str = "msh/US"  # Topic root to subscribe to
    use_tls: bool = False       # Enable TLS for MQTT connection
    prefix: str = "!"
    disabled_commands: list[str] = field(default_factory=list)
    custom_commands: dict = field(default_factory=dict)


@dataclass
class LLMConfig:
    """LLM backend settings."""

    backend: str = "openai"  # openai, anthropic, google
    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    timeout: int = 30
    max_response_tokens: int = 8192  # Let LLM generate full responses; chunker handles size

    system_prompt: str = (
        "RESPONSE RULES:\n"
        "- For casual conversation, keep responses brief (1-2 sentences).\n"
        "- For mesh health questions, give detailed data-driven responses.\n"
        "- Be concise but friendly. No markdown formatting.\n"
        "- If asked about mesh activity and no recent traffic is shown, say you haven't "
        "observed any yet.\n"
        "- When asked about yourself or commands, answer conversationally based on "
        "the command list provided below. Don't dump lists unless asked.\n"
        "- You are part of the freq51 mesh.\n"
        "- When asked about yourself or commands, answer conversationally. Don't dump lists.\n"
        "- You are part of the freq51 mesh in the Twin Falls, Idaho area.\n"
        "- NEVER use markdown formatting (no bold, no asterisks, no bullet points, no numbered lists). Plain text only.\n"
        "- NEVER say 'Want me to keep going?' -- the system handles continuation prompts automatically."
    )
    use_system_prompt: bool = True  # Toggle to disable sending system prompt
    web_search: bool = False  # Enable web search (Open WebUI feature)
    google_grounding: bool = False  # Enable Google Search grounding (Gemini only)


@dataclass
class OpenMeteoConfig:
    """Open-Meteo weather provider settings."""

    url: str = "https://api.open-meteo.com/v1"


@dataclass
class WttrConfig:
    """wttr.in weather provider settings."""

    url: str = "https://wttr.in"


@dataclass
class WeatherConfig:
    """Weather command settings."""

    primary: str = "openmeteo"  # openmeteo, wttr, llm
    fallback: str = "llm"  # openmeteo, wttr, llm, none
    default_location: str = ""
    openmeteo: OpenMeteoConfig = field(default_factory=OpenMeteoConfig)
    wttr: WttrConfig = field(default_factory=WttrConfig)


@dataclass
class MeshMonitorConfig:
    """MeshMonitor trigger sync settings."""

    enabled: bool = False
    url: str = ""  # e.g., http://100.64.0.11:3333
    inject_into_prompt: bool = True  # Tell LLM about MeshMonitor commands
    refresh_interval: int = 30     # Tick interval in seconds (default 30)
    polite_mode: bool = False      # Reduces polling frequency for shared instances  # Seconds between refreshes


@dataclass
class KnowledgeConfig:
    """Knowledge base settings."""

    enabled: bool = False
    backend: str = "auto"  # "qdrant", "sqlite", or "auto" (try qdrant, fall back to sqlite)

    # Qdrant / RECON settings
    qdrant_host: str = ""           # e.g., "192.168.1.150"
    qdrant_port: int = 6333
    qdrant_collection: str = "recon_knowledge_hybrid"
    tei_host: str = ""              # TEI embedding service host
    tei_port: int = 8090
    sparse_host: str = ""           # Sparse embedding service host
    sparse_port: int = 8091
    use_sparse: bool = True         # Enable hybrid dense+sparse search

    # SQLite fallback settings
    db_path: str = ""
    top_k: int = 5


@dataclass
class MeshSourceConfig:
    """Configuration for a mesh data source."""

    name: str = ""
    type: str = ""  # "meshview", "meshmonitor", or "mqtt"
    url: str = ""
    api_token: str = ""  # MeshMonitor only, supports ${ENV_VAR}
    refresh_interval: int = 30     # Tick interval in seconds (default 30)
    polite_mode: bool = False      # Reduces polling frequency for shared instances
    enabled: bool = True

    # MQTT-specific fields (type=mqtt only)
    host: str = ""              # MQTT broker hostname
    port: int = 1883            # MQTT broker port (1883 plain, 8883 TLS)
    username: str = ""         # MQTT username (optional)
    password: str = ""         # MQTT password (optional, supports )
    topic_root: str = "msh/US"  # Topic root to subscribe to
    use_tls: bool = False       # Enable TLS for MQTT connection


@dataclass
class RegionAnchor:
    """A fixed region anchor point with geographic context."""

    name: str = ""
    lat: float = 0.0
    lon: float = 0.0
    local_name: str = ""           # e.g., "Magic Valley"
    description: str = ""          # e.g., "Twin Falls, Burley, Jerome along I-84/US-93"
    aliases: list[str] = field(default_factory=list)  # e.g., ["southern Idaho", "magic valley"]
    cities: list[str] = field(default_factory=list)   # e.g., ["Twin Falls", "Burley", "Jerome"]


@dataclass
class AlertRulesConfig:
    """Per-condition alert toggles and thresholds."""

    # Infrastructure
    infra_offline: bool = True
    infra_recovery: bool = True
    new_router: bool = True

    # Power
    battery_trend_declining: bool = True
    battery_warning: bool = True
    battery_critical: bool = True
    battery_emergency: bool = True
    battery_warning_threshold: int = 30
    battery_critical_threshold: int = 15
    battery_emergency_threshold: int = 5
    # Voltage-based thresholds (more accurate than percentage)
    battery_warning_voltage: float = 3.60
    battery_critical_voltage: float = 3.50
    battery_emergency_voltage: float = 3.40
    power_source_change: bool = True
    solar_not_charging: bool = True

    # Utilization
    sustained_high_util: bool = True
    high_util_threshold: float = 40.0
    high_util_hours: int = 6
    packet_flood: bool = True
    packet_flood_threshold: int = 10

    # Coverage
    infra_single_gateway: bool = True
    feeder_offline: bool = True
    region_total_blackout: bool = True

    # Health Scores
    mesh_score_alert: bool = True
    mesh_score_threshold: int = 65
    region_score_alert: bool = True
    region_score_threshold: int = 60


@dataclass
class MeshIntelligenceConfig:
    """Mesh intelligence and health scoring settings."""

    enabled: bool = False
    regions: list[RegionAnchor] = field(default_factory=list)  # Fixed region anchors
    locality_radius_miles: float = 8.0  # Radius for locality clustering within regions
    offline_threshold_hours: int = 2  # Hours before node considered offline
    packet_threshold: int = 500  # Non-text packets per 24h to flag
    battery_warning_percent: int = 30  # Battery level for warnings

    # Alert settings
    critical_nodes: list[str] = field(default_factory=list)  # Short names of critical nodes (e.g., ["MHR", "HPR"])
    alert_channel: int = -1  # Channel to broadcast alerts on. -1 = disabled, 0+ = channel index
    alert_cooldown_minutes: int = 30  # Min minutes between repeated alerts for same condition
    alert_rules: AlertRulesConfig = field(default_factory=AlertRulesConfig)


# Environmental feed configs
@dataclass
class NWSConfig:
    """NWS weather alerts settings."""

    enabled: bool = True

    # MQTT-specific fields (type=mqtt only)
    host: str = ""              # MQTT broker hostname
    port: int = 1883            # MQTT broker port (1883 plain, 8883 TLS)
    username: str = ""         # MQTT username (optional)
    password: str = ""         # MQTT password (optional, supports )
    topic_root: str = "msh/US"  # Topic root to subscribe to
    use_tls: bool = False       # Enable TLS for MQTT connection
    tick_seconds: int = 60
    areas: list = field(default_factory=lambda: ["ID"])
    severity_min: str = "moderate"
    user_agent: str = ""


@dataclass
class SWPCConfig:
    """NOAA Space Weather settings."""

    enabled: bool = True

    # MQTT-specific fields (type=mqtt only)
    host: str = ""              # MQTT broker hostname
    port: int = 1883            # MQTT broker port (1883 plain, 8883 TLS)
    username: str = ""         # MQTT username (optional)
    password: str = ""         # MQTT password (optional, supports )
    topic_root: str = "msh/US"  # Topic root to subscribe to
    use_tls: bool = False       # Enable TLS for MQTT connection


@dataclass
class DuctingConfig:
    """Tropospheric ducting settings."""

    enabled: bool = True

    # MQTT-specific fields (type=mqtt only)
    host: str = ""              # MQTT broker hostname
    port: int = 1883            # MQTT broker port (1883 plain, 8883 TLS)
    username: str = ""         # MQTT username (optional)
    password: str = ""         # MQTT password (optional, supports )
    topic_root: str = "msh/US"  # Topic root to subscribe to
    use_tls: bool = False       # Enable TLS for MQTT connection
    tick_seconds: int = 10800  # 3 hours
    latitude: float = 42.56  # Twin Falls area default
    longitude: float = -114.47


@dataclass
class NICFFiresConfig:
    """NIFC fire perimeters settings (Phase 2)."""

    enabled: bool = False
    tick_seconds: int = 600
    state: str = "US-ID"


@dataclass
class AvalancheConfig:
    """Avalanche advisory settings (Phase 2)."""

    enabled: bool = False
    tick_seconds: int = 1800
    center_ids: list = field(default_factory=lambda: ["SNFAC"])
    season_months: list = field(default_factory=lambda: [12, 1, 2, 3, 4])


@dataclass
class USGSConfig:
    """USGS stream gauge settings."""

    enabled: bool = False
    tick_seconds: int = 900  # Minimum 15 min per USGS guidelines
    sites: list = field(default_factory=list)  # Site IDs, e.g. ["13090500"]
    flood_thresholds: dict = field(default_factory=dict)  # {site_id: {flow: X, height: Y}}


@dataclass
class TomTomConfig:
    """TomTom traffic flow settings."""

    enabled: bool = False
    tick_seconds: int = 300
    api_key: str = ""  # Supports ${ENV_VAR}
    corridors: list = field(default_factory=list)  # [{name, lat, lon}, ...]


@dataclass
class Roads511Config:
    """511 road conditions settings."""

    enabled: bool = False
    tick_seconds: int = 300
    api_key: str = ""  # Supports ${ENV_VAR}
    base_url: str = ""  # State-specific, e.g. "https://511.idaho.gov/api/v2"
    endpoints: list = field(default_factory=lambda: ["/get/event"])
    bbox: list = field(default_factory=list)  # [west, south, east, north]


@dataclass
class FIRMSConfig:
    """NASA FIRMS satellite fire hotspot settings."""

    enabled: bool = False
    tick_seconds: int = 1800  # 30 min default
    map_key: str = ""  # NASA FIRMS MAP_KEY, get at https://firms.modaps.eosdis.nasa.gov/api/area/
    source: str = "VIIRS_SNPP_NRT"  # VIIRS_SNPP_NRT, VIIRS_NOAA20_NRT, MODIS_NRT
    bbox: list = field(default_factory=list)  # [west, south, east, north]
    day_range: int = 1  # 1-10 days of data
    confidence_min: str = "nominal"  # low, nominal, high
    proximity_km: float = 10.0  # km to match known fire


@dataclass
class EnvironmentalConfig:
    """Environmental feeds settings."""

    enabled: bool = False
    nws_zones: list = field(default_factory=lambda: ["IDZ016", "IDZ030"])
    nws: NWSConfig = field(default_factory=NWSConfig)
    swpc: SWPCConfig = field(default_factory=SWPCConfig)
    ducting: DuctingConfig = field(default_factory=DuctingConfig)
    fires: NICFFiresConfig = field(default_factory=NICFFiresConfig)
    avalanche: AvalancheConfig = field(default_factory=AvalancheConfig)
    usgs: USGSConfig = field(default_factory=USGSConfig)
    traffic: TomTomConfig = field(default_factory=TomTomConfig)
    roads511: Roads511Config = field(default_factory=Roads511Config)
    firms: FIRMSConfig = field(default_factory=FIRMSConfig)


@dataclass
class NotificationRuleConfig:
    """Self-contained notification rule with inline delivery config."""

    name: str = ""
    enabled: bool = True

    # Trigger type
    trigger_type: str = "condition"  # "condition" or "schedule"

    # Condition trigger fields
    categories: list = field(default_factory=list)  # Empty = all categories
    min_severity: str = "warning"

    # Schedule trigger fields
    schedule_frequency: str = "daily"  # daily, twice_daily, weekly, custom
    schedule_time: str = "07:00"
    schedule_time_2: str = "19:00"  # For twice_daily
    schedule_days: list = field(default_factory=list)  # For weekly
    schedule_cron: str = ""  # For custom
    message_type: str = "mesh_health_summary"
    custom_message: str = ""

    # Delivery type
    delivery_type: str = ""  # mesh_broadcast, mesh_dm, email, webhook

    # Mesh broadcast fields
    broadcast_channel: int = 0

    # Mesh DM fields
    node_ids: list = field(default_factory=list)

    # Email fields
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_tls: bool = True
    from_address: str = ""
    recipients: list = field(default_factory=list)

    # Webhook fields
    webhook_url: str = ""
    webhook_headers: dict = field(default_factory=dict)

    # Behavior
    cooldown_minutes: int = 10
    override_quiet: bool = False

    # Legacy field for migration (ignored in new format)
    channel_ids: list = field(default_factory=list)


@dataclass
class NotificationsConfig:
    """Notification system settings."""

    enabled: bool = False
    quiet_hours_enabled: bool = True  # Master toggle for quiet hours
    quiet_hours_start: str = "22:00"
    quiet_hours_end: str = "06:00"
    rules: list = field(default_factory=list)  # List of NotificationRuleConfig

@dataclass
class DashboardConfig:
    """Web dashboard settings."""

    enabled: bool = True
    port: int = 8080
    host: str = "0.0.0.0"

@dataclass
class Config:
    """Main configuration container."""

    # Global settings
    timezone: str = "America/Boise"  # IANA timezone for local time display

    bot: BotConfig = field(default_factory=BotConfig)
    connection: ConnectionConfig = field(default_factory=ConnectionConfig)
    response: ResponseConfig = field(default_factory=ResponseConfig)
    history: HistoryConfig = field(default_factory=HistoryConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    commands: CommandsConfig = field(default_factory=CommandsConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    weather: WeatherConfig = field(default_factory=WeatherConfig)
    meshmonitor: MeshMonitorConfig = field(default_factory=MeshMonitorConfig)
    knowledge: KnowledgeConfig = field(default_factory=KnowledgeConfig)
    mesh_sources: list[MeshSourceConfig] = field(default_factory=list)
    mesh_intelligence: MeshIntelligenceConfig = field(default_factory=MeshIntelligenceConfig)
    environmental: EnvironmentalConfig = field(default_factory=EnvironmentalConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)

    _config_path: Optional[Path] = field(default=None, repr=False)

    def resolve_api_key(self) -> str:
        """Resolve API key from config or environment."""
        if self.llm.api_key:
            # Check if it's an env var reference like ${LLM_API_KEY}
            if self.llm.api_key.startswith("${") and self.llm.api_key.endswith("}"):
                env_var = self.llm.api_key[2:-1]
                return os.environ.get(env_var, "")
            return self.llm.api_key
        # Fall back to common env vars
        for env_var in ["LLM_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"]:
            if value := os.environ.get(env_var):
                return value
        return ""


def _migrate_legacy_channels(notifications, data: dict):
    """Migrate legacy channels+rules format to self-contained rules."""
    old_channels = data.get("channels", [])
    old_rules = data.get("rules", [])

    if not old_channels:
        return

    _config_logger.info("Migrating %d legacy notification channels to inline rules", len(old_channels))

    # Build channel lookup
    channel_map = {}
    for ch in old_channels:
        if isinstance(ch, dict):
            channel_map[ch.get("id", "")] = ch

    # Convert each old rule + referenced channels to new format
    migrated_rules = []
    for old_rule in old_rules:
        if not isinstance(old_rule, dict):
            continue

        channel_ids = old_rule.get("channel_ids", [])
        if not channel_ids:
            continue

        for ch_id in channel_ids:
            ch = channel_map.get(ch_id)
            if not ch:
                continue

            # Create new rule with inline delivery config
            new_rule = NotificationRuleConfig(
                name=old_rule.get("name", "") or ch_id,
                enabled=ch.get("enabled", True),
                trigger_type="condition",
                categories=old_rule.get("categories", []),
                min_severity=old_rule.get("min_severity", "warning"),
                delivery_type=ch.get("type", "mesh_broadcast"),
                broadcast_channel=ch.get("channel_index", 0),
                node_ids=ch.get("node_ids", []),
                smtp_host=ch.get("smtp_host", ""),
                smtp_port=ch.get("smtp_port", 587),
                smtp_user=ch.get("smtp_user", ""),
                smtp_password=ch.get("smtp_password", ""),
                smtp_tls=ch.get("smtp_tls", True),
                from_address=ch.get("from_address", ""),
                recipients=ch.get("recipients", []),
                webhook_url=ch.get("url", ""),
                webhook_headers=ch.get("headers", {}),
                cooldown_minutes=10,
                override_quiet=old_rule.get("override_quiet", False),
            )
            migrated_rules.append(new_rule)

    # Replace rules with migrated ones (migrated rules come first, then any new-format rules)
    if migrated_rules:
        # Keep only non-migrated rules (those without channel_ids)
        existing_new_rules = [r for r in notifications.rules if not getattr(r, 'channel_ids', [])]
        notifications.rules = migrated_rules + existing_new_rules
        _config_logger.info("Migrated to %d self-contained rules", len(notifications.rules))


def _dict_to_dataclass(cls, data: dict):
    """Recursively convert dict to dataclass, handling nested structures."""
    if data is None:
        return cls()

    field_types = {f.name: f.type for f in cls.__dataclass_fields__.values()}
    kwargs = {}

    for key, value in data.items():
        if key.startswith("_"):
            continue
        if key not in field_types:
            continue

        field_type = field_types[key]

        # Handle nested dataclasses
        if hasattr(field_type, "__dataclass_fields__") and isinstance(value, dict):
            kwargs[key] = _dict_to_dataclass(field_type, value)
        # Handle list of MeshSourceConfig
        elif key == "mesh_sources" and isinstance(value, list):
            kwargs[key] = [
                _dict_to_dataclass(MeshSourceConfig, item)
                if isinstance(item, dict) else item
                for item in value
            ]
        # Handle list of RegionAnchor
        elif key == "regions" and isinstance(value, list):
            kwargs[key] = [
                _dict_to_dataclass(RegionAnchor, item)
                if isinstance(item, dict) else item
                for item in value
            ]
        # Handle AlertRulesConfig
        elif key == "alert_rules" and isinstance(value, dict):
            kwargs[key] = _dict_to_dataclass(AlertRulesConfig, value)
        # Handle nested environmental configs
        elif key == "nws" and isinstance(value, dict):
            kwargs[key] = _dict_to_dataclass(NWSConfig, value)
        elif key == "swpc" and isinstance(value, dict):
            kwargs[key] = _dict_to_dataclass(SWPCConfig, value)
        elif key == "ducting" and isinstance(value, dict):
            kwargs[key] = _dict_to_dataclass(DuctingConfig, value)
        elif key == "fires" and isinstance(value, dict):
            kwargs[key] = _dict_to_dataclass(NICFFiresConfig, value)
        elif key == "avalanche" and isinstance(value, dict):
            kwargs[key] = _dict_to_dataclass(AvalancheConfig, value)
        elif key == "usgs" and isinstance(value, dict):
            kwargs[key] = _dict_to_dataclass(USGSConfig, value)
        elif key == "traffic" and isinstance(value, dict):
            kwargs[key] = _dict_to_dataclass(TomTomConfig, value)
        elif key == "roads511" and isinstance(value, dict):
            kwargs[key] = _dict_to_dataclass(Roads511Config, value)
        elif key == "firms" and isinstance(value, dict):
            kwargs[key] = _dict_to_dataclass(FIRMSConfig, value)
        elif key == "dashboard" and isinstance(value, dict):
            kwargs[key] = _dict_to_dataclass(DashboardConfig, value)
        elif key == "notifications" and isinstance(value, dict):
            notifications = _dict_to_dataclass(NotificationsConfig, value)
            if "rules" in value and isinstance(value["rules"], list):
                notifications.rules = [_dict_to_dataclass(NotificationRuleConfig, r) if isinstance(r, dict) else r for r in value["rules"]]
            # Migrate old channels+rules format if present
            if "channels" in value and isinstance(value["channels"], list) and value["channels"]:
                _migrate_legacy_channels(notifications, value)
            kwargs[key] = notifications
        else:
            kwargs[key] = value

    return cls(**kwargs)


def _dataclass_to_dict(obj) -> dict:
    """Recursively convert dataclass to dict for YAML serialization."""
    if not hasattr(obj, "__dataclass_fields__"):
        return obj

    result = {}
    for field_name in obj.__dataclass_fields__:
        if field_name.startswith("_"):
            continue
        value = getattr(obj, field_name)
        if hasattr(value, "__dataclass_fields__"):
            result[field_name] = _dataclass_to_dict(value)
        elif isinstance(value, list):
            # Handle list of dataclasses (like mesh_sources)
            result[field_name] = [
                _dataclass_to_dict(item) if hasattr(item, "__dataclass_fields__") else item
                for item in value
            ]
        else:
            result[field_name] = value
    return result


def load_config(config_path: Optional[Path] = None) -> Config:
    """Load configuration from YAML file.

    Args:
        config_path: Path to config file. Defaults to ./config.yaml

    Returns:
        Config object with loaded settings
    """
    if config_path is None:
        config_path = Path("config.yaml")

    config_path = Path(config_path)

    if not config_path.exists():
        # Return default config if file doesn't exist
        config = Config()
        config._config_path = config_path
        return config

    with open(config_path, "r") as f:
        data = yaml.safe_load(f) or {}

    config = _dict_to_dataclass(Config, data)
    config._config_path = config_path
    return config


def save_config(config: Config, config_path: Optional[Path] = None) -> None:
    """Save configuration to YAML file.

    Args:
        config: Config object to save
        config_path: Path to save to. Uses config._config_path if not specified
    """
    if config_path is None:
        config_path = config._config_path or Path("config.yaml")

    config_path = Path(config_path)

    data = _dataclass_to_dict(config)

    # Add header comment
    header = "# MeshAI Configuration\n# Generated by meshai --config\n\n"

    with open(config_path, "w") as f:
        f.write(header)
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
