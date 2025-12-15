"""Configuration management for MeshAI."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


@dataclass
class BotConfig:
    """Bot identity and trigger settings."""

    name: str = "ai"
    owner: str = ""
    respond_to_mentions: bool = True
    respond_to_dms: bool = True


@dataclass
class RateLimitsConfig:
    """Rate limiting settings."""

    messages_per_minute: int = 10  # Per-user message limit
    global_messages_per_minute: int = 30  # Total across all users
    cooldown_seconds: float = 5.0  # Min time between responses to same user
    burst_allowance: int = 3  # Allow short bursts before limiting


@dataclass
class LoggingConfig:
    """Logging settings."""

    level: str = "INFO"  # DEBUG | INFO | WARNING | ERROR
    file: str = ""  # Empty = console only
    max_size_mb: int = 10
    backup_count: int = 3
    log_messages: bool = True  # Log incoming messages
    log_responses: bool = True  # Log outgoing responses
    log_api_calls: bool = False  # Log raw LLM API requests (verbose)


@dataclass
class LLMBackendConfig:
    """Single LLM backend configuration."""

    backend: str = "openai"  # openai, anthropic, google
    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    timeout: int = 30


@dataclass
class SafetyConfig:
    """Response filtering and safety settings."""

    max_response_length: int = 250  # Hard cap on response length
    filter_profanity: bool = False  # Basic profanity filter
    blocked_phrases: list[str] = field(default_factory=list)  # Phrases to filter out
    require_mention: bool = True  # Only respond when mentioned by name
    ignore_self: bool = True  # Don't respond to own messages
    emergency_keywords: list[str] = field(
        default_factory=lambda: ["emergency", "help", "sos"]
    )  # Always respond to these


@dataclass
class UsersConfig:
    """User management settings."""

    blocklist: list[str] = field(default_factory=list)  # Never respond to these node IDs
    allowlist_only: bool = False  # If true, only respond to allowlist
    allowlist: list[str] = field(default_factory=list)  # Exclusive users
    admin_nodes: list[str] = field(default_factory=list)  # Nodes with admin commands
    vip_nodes: list[str] = field(default_factory=list)  # Skip rate limits


@dataclass
class CustomCommandConfig:
    """Custom static command definition."""

    response: str = ""


@dataclass
class CommandsConfig:
    """Command customization settings."""

    enabled: bool = True
    prefix: str = "!"  # Command prefix
    custom_commands: dict = field(default_factory=dict)  # name -> response mapping
    disabled_commands: list[str] = field(default_factory=list)  # Built-in commands to disable


@dataclass
class PersonalityConfig:
    """Personality and prompt settings."""

    system_prompt: str = (
        "You are a helpful assistant on a Meshtastic mesh network. "
        "Keep responses VERY brief - under 250 characters total. "
        "Be concise but friendly. No markdown formatting."
    )
    context_injection: str = ""  # Template with {time}, {sender_name}, {channel}
    personas: dict = field(default_factory=dict)  # trigger -> prompt mapping


@dataclass
class WebStatusConfig:
    """Web status page settings."""

    enabled: bool = False
    port: int = 8080
    show_uptime: bool = True
    show_message_count: bool = True
    show_connected_nodes: bool = True
    show_recent_activity: bool = False  # Privacy concern
    require_auth: bool = False
    auth_password: str = ""


@dataclass
class AnnouncementsConfig:
    """Periodic announcement settings."""

    enabled: bool = False
    interval_hours: int = 24
    channel: int = 0
    messages: list[str] = field(default_factory=list)
    random_order: bool = True


@dataclass
class WebhookConfig:
    """Webhook integration settings."""

    enabled: bool = False
    url: str = ""
    events: list[str] = field(
        default_factory=lambda: ["message_received", "response_sent", "error"]
    )


@dataclass
class ConnectionConfig:
    """Meshtastic connection settings."""

    type: str = "serial"  # serial or tcp
    serial_port: str = "/dev/ttyUSB0"
    tcp_host: str = "192.168.1.100"
    tcp_port: int = 4403


@dataclass
class ChannelsConfig:
    """Channel filtering settings."""

    mode: str = "all"  # all or whitelist
    whitelist: list[int] = field(default_factory=lambda: [0])


@dataclass
class ResponseConfig:
    """Response behavior settings."""

    delay_min: float = 2.2
    delay_max: float = 3.0
    max_length: int = 150
    max_messages: int = 2


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
    window_size: int = 4  # Recent message pairs to keep in full
    summarize_threshold: int = 8  # Messages before re-summarizing


@dataclass
class LLMConfig:
    """LLM backend settings with fallback support."""

    # Primary backend (backwards compatible with old config)
    backend: str = "openai"  # openai, anthropic, google
    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    timeout: int = 30

    # System prompt (kept for backwards compat, personality.system_prompt preferred)
    system_prompt: str = (
        "You are a helpful assistant on a Meshtastic mesh network. "
        "Keep responses VERY brief - under 250 characters total. "
        "Be concise but friendly. No markdown formatting."
    )
    use_system_prompt: bool = True  # Toggle to disable sending system prompt

    # Fallback settings
    fallback: Optional[LLMBackendConfig] = None
    retry_attempts: int = 2
    fallback_on_error: bool = True
    fallback_on_timeout: bool = True


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
class IntegrationsConfig:
    """External integrations settings."""

    weather: WeatherConfig = field(default_factory=WeatherConfig)
    webhook: WebhookConfig = field(default_factory=WebhookConfig)


@dataclass
class Config:
    """Main configuration container."""

    bot: BotConfig = field(default_factory=BotConfig)
    connection: ConnectionConfig = field(default_factory=ConnectionConfig)
    channels: ChannelsConfig = field(default_factory=ChannelsConfig)
    response: ResponseConfig = field(default_factory=ResponseConfig)
    history: HistoryConfig = field(default_factory=HistoryConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)

    # New config sections
    rate_limits: RateLimitsConfig = field(default_factory=RateLimitsConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    users: UsersConfig = field(default_factory=UsersConfig)
    commands: CommandsConfig = field(default_factory=CommandsConfig)
    personality: PersonalityConfig = field(default_factory=PersonalityConfig)
    web_status: WebStatusConfig = field(default_factory=WebStatusConfig)
    announcements: AnnouncementsConfig = field(default_factory=AnnouncementsConfig)
    integrations: IntegrationsConfig = field(default_factory=IntegrationsConfig)

    # Keep weather at top level for backwards compatibility
    weather: WeatherConfig = field(default_factory=WeatherConfig)

    _config_path: Optional[Path] = field(default=None, repr=False)

    def get_system_prompt(self) -> str:
        """Get effective system prompt, preferring personality config."""
        if self.personality.system_prompt:
            return self.personality.system_prompt
        return self.llm.system_prompt

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
            result[field_name] = list(value)
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


def get_default_config() -> Config:
    """Get a Config object with all default values."""
    return Config()
