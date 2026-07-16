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
    """Bot identity and trigger settings.

    mt_mesh_name/mt_node/mc_mesh_name: transport-specific identity used ONLY
    when generating the LLM system prompt for that transport (see
    router.generate_llm_response). Generic OSS defaults are intentionally
    empty/mesh-agnostic -- deployments fill these in via config.
    """

    name: str = "MeshAI"
    owner: str = "Unknown"
    contact_email: str = ""
    respond_to_dms: bool = True
    filter_bbs_protocols: bool = True
    mt_mesh_name: str = ""   # e.g. "freq51 Meshtastic mesh" -- Meshtastic-only identity framing
    mt_node: str = ""        # e.g. "!27780c47 (AIDA-N2)" -- Meshtastic-only physical node id
    mc_mesh_name: str = ""   # e.g. "the MeshCore mesh" -- MeshCore-only identity framing


@dataclass
class ConnectionConfig:
    """Meshtastic connection settings."""

    type: str = "serial"  # serial or tcp
    serial_port: str = "/dev/ttyUSB0"
    tcp_host: str = "192.168.1.100"
    tcp_port: int = 4403
    # --- app-level auto-reconnect (watchdog) ---
    reconnect: bool = True
    reconnect_initial_delay: float = 2.0
    reconnect_max_delay: float = 60.0
    reconnect_health_interval: float = 30.0
    # Universal mesh message budget (MeshCore LCD → 140 for all transports).
    mesh_max_chars: int = 140
    # --- MeshCore transport settings ---
    # MeshCore is active when meshcore_host is a non-empty string; blank = off.
    meshcore_host: str = ""                     # pyMC companion frame server host
    meshcore_port: int = 5050                   # pyMC companion frame server port
    meshcore_auto_reconnect: bool = True        # enable meshcore lib auto-reconnect
    meshcore_max_reconnect_attempts: int = 5    # max reconnect attempts (0 = unlimited)
    meshcore_advert_interval_seconds: int = 10800  # periodic self-advert interval (0 = disabled)
    # MeshCore connection type: tcp | serial | ble (default tcp for back-compat)
    meshcore_conn_type: str = "tcp"
    meshcore_serial_port: str = ""             # prefer stable /dev/serial/by-id/... path
    meshcore_baud: int = 115200
    meshcore_ble_address: str = ""             # optional; for ble
    meshcore_auto_add_contacts: bool = True     # firmware auto-adds every node it hears an advert from (so AIDA can DM anyone)
    meshcore_ack_wait_seconds: float = 6.0       # wait for delivery ACK before falling back to path discovery
    meshcore_discovery_wait_seconds: float = 8.0  # path-discovery timeout on the no-ACK fallback (was hardcoded 25s)

    # --- Send-queue pacing (per-radio serialization) ---
    # Randomized jitter between consecutive outbound sends on each radio:
    # each inter-send gap is drawn from random.uniform(pace_min, pace_max).
    # A floor of 0.25 s is always enforced at runtime; max is clamped to >= min
    # at __post_init__ so a misconfigured max<min is silently corrected.
    meshtastic_send_pacing_min_seconds: float = 2.2
    meshtastic_send_pacing_max_seconds: float = 2.6
    meshcore_send_pacing_min_seconds: float = 2.2
    meshcore_send_pacing_max_seconds: float = 2.6

    # --- MeshCore telemetry auto-poll settings ---
    # Selected contacts (names or pubkeys) to auto-poll for telemetry; empty = none.
    meshcore_telemetry_contacts: list = field(default_factory=list)
    meshcore_telemetry_interval_seconds: int = 1800  # auto-poll interval (0 = disabled; floor 300)

    def __post_init__(self):
        if self.meshcore_conn_type not in {"tcp", "serial", "ble"}:
            raise ValueError(
                f"meshcore_conn_type must be one of 'tcp', 'serial', 'ble', "
                f"got {self.meshcore_conn_type!r}"
            )
        # Clamp pacing min to floor (0.25 s), then ensure max >= min.
        _floor = 0.25
        self.meshtastic_send_pacing_min_seconds = max(_floor, self.meshtastic_send_pacing_min_seconds)
        self.meshtastic_send_pacing_max_seconds = max(
            self.meshtastic_send_pacing_min_seconds, self.meshtastic_send_pacing_max_seconds
        )
        self.meshcore_send_pacing_min_seconds = max(_floor, self.meshcore_send_pacing_min_seconds)
        self.meshcore_send_pacing_max_seconds = max(
            self.meshcore_send_pacing_min_seconds, self.meshcore_send_pacing_max_seconds
        )


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


@dataclass
class MemoryConfig:
    """Rolling summary memory settings."""

    enabled: bool = True  # Enable memory optimization

    window_size: int = 4  # Recent message pairs to keep in full
    summarize_threshold: int = 8  # Messages before re-summarizing


@dataclass
class ContextConfig:
    """Passive mesh context settings."""

    enabled: bool = True

    observe_channels: list[int] = field(default_factory=list)  # Empty = all channels
    ignore_nodes: list[str] = field(default_factory=list)  # Node IDs to ignore
    max_age: int = 1_209_600  # 14 days in seconds
    max_context_items: int = 20  # Max observations injected into LLM context


@dataclass
class MeshCoreContextConfig:
    """MeshCore passive-context / bot-behavior settings (MeshCore-native)."""
    enable_passive_context: bool = True
    observe_channels: list[str] = field(default_factory=list)  # channel NAMES, empty = none (opt-in): only listed channels feed context
    ignore_contacts: list[str] = field(default_factory=list)   # contact names or pubkey prefixes
    respond_to_dms: bool = True


@dataclass
class CommandsConfig:
    """Command settings."""

    enabled: bool = True

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
        "- When asked about yourself or commands, answer conversationally. Don't dump lists.\n"
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
    # TODO: behavior pillar uses wrong scale - see meshai-v03-notification-handoff.md bug #2
    battery_warning_percent: int = 30  # Battery level for warnings

    # Alert settings
    critical_nodes: list[str] = field(default_factory=list)  # Short names of critical nodes (e.g., ["MHR", "HPR"])
    alert_channel: int = -1  # Channel to broadcast alerts on. -1 = disabled, 0+ = channel index
    alert_rules: AlertRulesConfig = field(default_factory=AlertRulesConfig)


# Environmental feed configs
@dataclass
class _SourcedFeed:
    """Mixin: an environmental feed is sourced 'native' (local adapter) or
    'central' (Central NATS firehose). Default 'native' preserves v0.3 behavior."""

    feed_source: str = "native"

    def __post_init__(self):
        if self.feed_source not in ("native", "central"):
            raise ValueError(f"feed_source must be 'native' or 'central', got {self.feed_source!r}")


@dataclass
class NWSConfig(_SourcedFeed):
    """NWS weather alerts settings."""

    enabled: bool = True

    tick_seconds: int = 60
    areas: list = field(default_factory=lambda: ["ID"])
    severity_min: str = "moderate"
    user_agent: str = ""
    base_url: str = "https://api.weather.gov/alerts/active"


@dataclass
class SWPCConfig(_SourcedFeed):
    """NOAA Space Weather settings."""

    enabled: bool = True
    # Per-endpoint URLs (poll intervals are fixed in the adapter, not
    # config-driven). Defaults are the historical hardcoded endpoints.
    endpoints: dict = field(
        default_factory=lambda: {
            "scales": "https://services.swpc.noaa.gov/products/noaa-scales.json",
            "kp": "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json",
            "alerts": "https://services.swpc.noaa.gov/products/alerts.json",
            "f107": "https://services.swpc.noaa.gov/json/f107_cm_flux.json",
        }
    )


@dataclass
class DuctingConfig(_SourcedFeed):
    """Tropospheric ducting settings."""

    enabled: bool = True

    tick_seconds: int = 10800  # 3 hours
    latitude: float = 42.56  # Twin Falls area default
    longitude: float = -114.47
    base_url: str = "https://api.open-meteo.com/v1/gfs"


@dataclass
class NICFFiresConfig(_SourcedFeed):
    """NIFC fire perimeters settings (Phase 2)."""

    enabled: bool = False
    tick_seconds: int = 600
    state: str = "US-ID"
    feed_url: str = (
        "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
        "WFIGS_Interagency_Perimeters_Current/FeatureServer/0/query"
    )
    points_url: str = (
        "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
        "WFIGS_Incident_Locations_Current/FeatureServer/0/query"
    )


@dataclass
class AvalancheConfig(_SourcedFeed):
    """Avalanche advisory settings (Phase 2)."""

    enabled: bool = False
    tick_seconds: int = 1800
    center_ids: list = field(default_factory=lambda: ["SNFAC"])
    season_months: list = field(default_factory=lambda: [12, 1, 2, 3, 4])
    base_url: str = "https://api.avalanche.org/v2/public/products/map-layer"


@dataclass
class USGSConfig(_SourcedFeed):
    """USGS stream gauge settings."""

    enabled: bool = False
    tick_seconds: int = 900  # Minimum 15 min per USGS guidelines
    sites: list = field(default_factory=list)  # Site IDs, e.g. ["13090500"]
    flood_thresholds: dict = field(default_factory=dict)  # {site_id: {flow: X, height: Y}}
    base_url: str = "https://waterservices.usgs.gov/nwis/iv/"
    nwps_base_url: str = "https://api.water.noaa.gov/nwps/v1/gauges"
    site_info_url: str = "https://waterservices.usgs.gov/nwis/site/"


@dataclass
class USGSQuakeConfig(_SourcedFeed):
    """USGS earthquake feed settings (Phase 2.14)."""

    enabled: bool = False
    tick_seconds: int = 300
    feed_url: str = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_day.geojson"
    # Native-path broadcast magnitude floor: the native adapter
    # (env/usgs_quake.py) gates on this. THIS is the GUI-editable quake
    # magnitude floor for the native feed_source. (The adapter_config
    # REGISTRY keys usgs_quake.global_mag_floor / regional_mag_floor apply
    # only to the Central-firehose path in central/quake_handler.py.)
    min_magnitude: float = 2.5
    # [west, south, east, north] -- Magic Valley -> Borah Peak -> Yellowstone
    bbox: list = field(default_factory=lambda: [-115.5, 42.0, -110.0, 45.2])
    region: str = "magic_valley"


@dataclass
class TomTomConfig(_SourcedFeed):
    """TomTom traffic flow settings."""

    enabled: bool = False
    tick_seconds: int = 300
    api_key: str = ""  # Supports ${ENV_VAR}
    corridors: list = field(default_factory=list)  # [{name, lat, lon}, ...]
    base_url: str = "https://api.tomtom.com/traffic/services/4/flowSegmentData/relative0/10/json"


@dataclass
class Roads511Config(_SourcedFeed):
    """511 road conditions settings."""

    enabled: bool = False
    tick_seconds: int = 300
    api_key: str = ""  # Supports ${ENV_VAR}
    base_url: str = ""  # State-specific, e.g. "https://511.idaho.gov/api/v2"
    endpoints: list = field(default_factory=lambda: ["/get/event"])
    bbox: list = field(default_factory=list)  # [west, south, east, north]


@dataclass
class WZDxConfig(_SourcedFeed):
    """FHWA WZDx (Work Zone Data Exchange) work-zone feed settings.

    Native adapter discovers state DOT WZDx v4 GeoJSON feeds via the FHWA
    WZDx Feed Registry (keyless), filters to `states`, then polls each
    matching feed and parses ``work-zone`` road_events into canonical
    ``work_zone`` events. Keyless: ``api_key`` is retained but unused.
    """

    enabled: bool = False
    tick_seconds: int = 300  # per-feed poll interval
    api_key: str = ""  # unused (keyless); retained for parity / ${ENV_VAR}
    base_url: str = ""  # optional single-feed override (skips registry when set)
    endpoints: list = field(default_factory=lambda: ["/get/event"])
    bbox: list = field(default_factory=list)  # [west, south, east, north] optional filter
    # FHWA WZDx Feed Registry (Socrata) — rows describe every state DOT feed.
    registry_url: str = (
        "https://datahub.transportation.gov/resource/69qe-yiui.json?$limit=200"
    )
    registry_ttl: int = 21600  # re-fetch registry every 6h (it rarely changes)
    states: list = field(default_factory=lambda: ["ID"])  # states to include


@dataclass
class FIRMSConfig(_SourcedFeed):
    """NASA FIRMS satellite fire hotspot settings."""

    enabled: bool = False
    tick_seconds: int = 1800  # 30 min default
    map_key: str = ""  # NASA FIRMS MAP_KEY, get at https://firms.modaps.eosdis.nasa.gov/api/area/
    source: str = "VIIRS_SNPP_NRT"  # VIIRS_SNPP_NRT, VIIRS_NOAA20_NRT, MODIS_NRT
    # Default Idaho region box [west, south, east, north] so native FIRMS
    # fetches/persists even when the universal coverage bbox is OFF. When
    # coverage IS enabled the store passes the coverage enclosing bbox to the
    # adapter (env/firms.py:32 `coverage["bbox"]`), which OVERRIDES this default.
    bbox: list = field(default_factory=lambda: [-115.5, 42.0, -110.0, 45.2])
    day_range: int = 1  # 1-10 days of data
    confidence_min: str = "nominal"  # low, nominal, high
    proximity_km: float = 10.0  # km to match known fire
    base_url: str = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"




@dataclass
class SatpassConfig(_SourcedFeed):
    """Satellite pass prediction settings.

    Historically a Central-only feed (`feed_source="central"`). The native
    path (`feed_source="native"`) adds a Celestrak TLE fetcher
    (`env.tle_fetch`) and a native SGP4 predictor; the fields below feed
    those. `feed_source` default stays "central" — the flip to "native"
    happens at cutover, not here.
    """

    enabled: bool = False
    feed_source: str = "central"

    # -- native path (TLE fetch + SGP4 predictor) -----------------------------
    # Ground stations the predictor computes passes for; each entry is a
    # dict {slug, name, lat, lon, alt_m}. Seeded into observer_locations.
    observers: list = field(default_factory=list)
    # Celestrak GP selectors the fetcher pulls (env.tle_fetch):
    tle_groups: list = field(default_factory=lambda: ["weather", "stations"])
    norad_ids: list = field(default_factory=list)
    tle_base_url: str = "https://celestrak.org/NORAD/elements/gp.php"
    # TLE refresh cadence — TLEs update ~daily, so poll every 6h.
    tle_refresh_seconds: int = 21600
    # Predictor pass filters (used by the next task):
    min_elevation_deg: float = 10.0
    window_hours: int = 24
    # BROADCAST imminence lead (seconds): a predicted pass is broadcast only
    # once its AOS is within this near-term window (and still in the future).
    # `window_hours` governs how far ahead we PREDICT; this governs when a
    # predicted pass is actually announced. Default 60 min.
    broadcast_lead_seconds: int = 3600


@dataclass
class IPAWSConfig(_SourcedFeed):
    """FEMA IPAWS-OPEN EAS civil-alert feed settings.

    Ingests the public IPAWS-OPEN Atom index + per-entry CAP 1.2 documents and
    broadcasts NON-weather civil emergency alerts (evacuation orders, Civil
    Emergency Messages, AMBER alerts, 911 outages, law-enforcement warnings,
    HazMat, shelter-in-place). NWS/NOAA-originated CAP entries are DROPPED so
    meshai never double-broadcasts weather the `nws` adapter already carries.

    Keyless: the FEMA IPAWS-OPEN EAS REST service needs no auth/API key.

    Two-stage fetch (both honour `base_url`):
      1. GET {base_url}/feed        -> Atom index (~13 rolling national entries)
      2. GET {base_url}/eas/<id>    -> full CAP 1.2 alert, per in-scope entry
    The Atom <link href> values are ABSOLUTE FEMA URLs; the adapter extracts the
    trailing ``eas/<id>`` and rebuilds ``{base_url}/eas/<id>`` so stage-2 routes
    through whatever base_url points at (in prod: the Conduit proxy).
    """

    enabled: bool = False
    tick_seconds: int = 60
    base_url: str = "https://apps.fema.gov/IPAWSOPEN_EAS_SERVICE/rest"
    user_agent: str = ""
    # Coarse region gate, applied BEFORE the stage-2 CAP fetch (the "reduce
    # load / don't fetch every linked CAP" gate): keep only index entries whose
    # statefips category term is in this list. Default = Idaho + neighbours
    # (ID=16, WA=53, OR=41, NV=32, UT=49, WY=56, MT=30).
    state_fips: list = field(
        default_factory=lambda: ["16", "53", "41", "32", "49", "56", "30"])
    # Fine region gate (optional): SAME county codes (6-digit, e.g. "016021").
    # Empty = accept every alert within the state_fips states.
    same_codes: list = field(default_factory=list)
    # Drop NWS/NOAA-originated CAP so we never double-broadcast weather.
    exclude_weather: bool = True
    # Sender substrings (case-insensitive) that mark a CAP as weather-sourced.
    drop_senders: list = field(
        default_factory=lambda: ["noaa.gov", "nws", "weather.gov"])
    # Only broadcast status=Actual (skip Test/Exercise/System) when True.
    status_actual_only: bool = True


@dataclass
class CentralConsumerConfig:
    """Connection settings for the Central NATS JetStream consumer (v0.4).

    v0.5.4 adds `region` — a dotted v0.9.20 region token (e.g. 'us.id' for
    Idaho) appended to each subscribed Central subject so the firehose is
    filtered server-side. Empty string falls back to bare wildcards (pre-
    v0.9.20 behaviour). One region applies to all central adapters; per-
    adapter overrides can land in v0.6.
    """

    enabled: bool = False
    url: str = "nats://central.echo6.mesh:4222"
    durable: str = "meshai-consumer"
    connect_timeout: float = 10.0
    region: str = "us.id"


@dataclass
class GeocoderConfig:
    """Photon reverse geocoder settings."""
    
    url: str = "https://photon.komoot.io"
    timeout_seconds: float = 2.0
    radius_km: float = 80.0
    limit: int = 10


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
    usgs_quake: USGSQuakeConfig = field(default_factory=USGSQuakeConfig)
    traffic: TomTomConfig = field(default_factory=TomTomConfig)
    roads511: Roads511Config = field(default_factory=Roads511Config)
    wzdx: WZDxConfig = field(default_factory=WZDxConfig)
    firms: FIRMSConfig = field(default_factory=FIRMSConfig)
    satpass: SatpassConfig = field(default_factory=SatpassConfig)
    ipaws: IPAWSConfig = field(default_factory=IPAWSConfig)
    central: CentralConsumerConfig = field(default_factory=CentralConsumerConfig)
    geocoder: GeocoderConfig = field(default_factory=GeocoderConfig)


@dataclass
class NotificationRuleConfig:
    """Self-contained notification rule with inline delivery config."""

    name: str = ""
    enabled: bool = True

    # Trigger type
    trigger_type: str = "condition"  # "condition" or "schedule"

    # Condition trigger fields
    categories: list = field(default_factory=list)  # Empty = all categories
    min_severity: str = "routine"
    region_scope: list = field(default_factory=list)  # [] = all regions

    # Schedule trigger fields
    schedule_frequency: str = "daily"  # daily, twice_daily, weekly, custom
    schedule_time: str = "07:00"
    schedule_time_2: str = "19:00"  # For twice_daily
    schedule_days: list = field(default_factory=list)  # For weekly
    schedule_cron: str = ""  # For custom
    schedule_match: Optional[str] = None  # "digest" for digest deliveries
    message_type: str = "mesh_health_summary"
    custom_message: str = ""

    # Delivery type
    delivery_type: str = ""  # mesh_broadcast, mesh_dm, meshcore_broadcast, meshcore_dm, email, webhook

    # Mesh broadcast fields
    broadcast_channel: int = 0
    # Per-family MeshCore channel NAME on the companion; None = not broadcast on MeshCore.
    meshcore_channel: Optional[str] = None

    # Mesh DM fields
    node_ids: list = field(default_factory=list)
    # MeshCore DM target contacts (names or pubkeys). Parallel to node_ids for Meshtastic.
    meshcore_dm_contacts: list = field(default_factory=list)

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

    # Integration C1: names of shared NotificationDestinations. When NON-EMPTY,
    # the dispatcher delivers via the resolved destinations instead of the inline
    # fields above. When EMPTY (default / all pre-C1 rules) the inline path runs
    # unchanged. Inline fields are retained as the fallback and never deleted.
    destinations: list = field(default_factory=list)

    # Behavior
    cooldown_minutes: int = 10

    # Legacy field for migration (ignored in new format)
    channel_ids: list = field(default_factory=list)



@dataclass
class NotificationToggle:
    """Per-family master toggle: severity threshold + region scope + per-severity
    channel routing (PagerDuty/Grafana-style notification policy)."""

    name: str = ""
    enabled: bool = False
    min_severity: str = "priority"  # routine|priority|immediate
    regions: list = field(default_factory=list)  # [] = all regions
    # severity -> list of channel types (digest|mesh_broadcast|mesh_dm|email|webhook)
    severity_channels: dict = field(default_factory=dict)
    # v0.5.2: staleness drop + per-toggle cooldown (Matt's spam fix)
    freshness_seconds: int = 600   # drop events older than this at dispatcher entrance
    cooldown_seconds: int = 0      # per (toggle, category, region) throttle window; 0 = disabled
    # per-channel delivery config (mirrors NotificationRuleConfig channel fields)
    broadcast_channel: Optional[int] = None
    # Per-family MeshCore channel NAME on the companion; None = not broadcast on MeshCore.
    meshcore_channel: Optional[str] = None
    node_ids: list = field(default_factory=list)
    # MeshCore DM target contacts (names or pubkeys). Parallel to node_ids for Meshtastic.
    meshcore_dm_contacts: list = field(default_factory=list)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_tls: bool = True
    from_address: str = ""
    recipients: list = field(default_factory=list)
    webhook_url: str = ""
    webhook_headers: dict = field(default_factory=dict)
    # Integration C1: names of shared NotificationDestinations. NON-EMPTY =>
    # deliver via resolved destinations; EMPTY => existing inline path unchanged.
    destinations: list = field(default_factory=list)


TOGGLE_FAMILIES = [
    "mesh_health", "weather", "fire", "rf_propagation", "satpass",
    "roads", "avalanche", "seismic", "tracking",
]


def _default_toggles() -> dict:
    """8 family master-toggles, all opt-in (disabled) by default."""
    return {
        fam: NotificationToggle(
            name=fam,
            enabled=False,
            min_severity="priority",
            regions=[],
            severity_channels={
                "priority": ["mesh_broadcast"],
                "immediate": ["mesh_broadcast", "mesh_dm"],
            },
        )
        for fam in TOGGLE_FAMILIES
    }


def ensure_family_toggles(config, families) -> None:
    """Inject a default (disabled) NotificationToggle for each family in
    `families` not already present in ``config.notifications.toggles``.

    Mirrors ``_default_toggles()`` for a static family (same defaults,
    disabled). Existing toggles are NEVER clobbered — this only ADDS. Used at
    startup so a generic source's dynamically-registered family becomes a real,
    operator-configurable (default-disabled) toggle in the routing set.
    """
    toggles = getattr(config.notifications, "toggles", None)
    if toggles is None:
        toggles = {}
        config.notifications.toggles = toggles
    for fam in families:
        if not fam or fam in toggles:
            continue
        toggles[fam] = NotificationToggle(
            name=fam,
            enabled=False,
            min_severity="priority",
            regions=[],
            severity_channels={
                "priority": ["mesh_broadcast"],
                "immediate": ["mesh_broadcast", "mesh_dm"],
            },
        )


@dataclass
class TogglesConfig:
    """Master toggle filter settings."""

    enabled: list[str] = field(default_factory=list)  # Toggle names that are enabled (empty = all)


@dataclass
class DigestConfig:
    """Digest scheduler settings."""

    schedule: str = "07:00"  # HH:MM time to fire digest
    include: list[str] = field(default_factory=list)  # Toggle names to include (empty = default set)


@dataclass
class NotificationDestination:
    """A named, reusable delivery target (Integration C1).

    Delivery config used to be DUPLICATED inline on every ``NotificationToggle``
    and ``NotificationRuleConfig`` (an operator configured the same SMTP/webhook
    twice). A ``NotificationDestination`` lets a delivery target be defined ONCE
    under ``NotificationsConfig.destinations`` and referenced by name from
    toggles/rules via their ``destinations`` list. The field set mirrors the
    inline delivery fields exactly, so a destination maps to ``create_channel``
    identically to an inline rule (only the fields relevant to ``type`` are used).
    """

    name: str = ""                 # id, referenced by toggles/rules
    type: str = "mesh_broadcast"   # mesh_broadcast|meshcore_broadcast|mesh_dm|meshcore_dm|email|webhook|digest

    # Mesh broadcast fields
    broadcast_channel: Optional[int] = None
    # Per-family MeshCore channel NAME on the companion; None = not on MeshCore.
    meshcore_channel: Optional[str] = None

    # DM fields
    node_ids: list = field(default_factory=list)
    meshcore_dm_contacts: list = field(default_factory=list)

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


@dataclass
class RegionRouteMatrix:
    """Compact region×family routing matrix (P3 primitive).

    mt_enabled: Meshtastic region routing master switch; gates the `mt` column.
    mc_enabled: MeshCore region routing master switch; gates the `mc` column.
                Each is independent; False = that transport is never consulted
                (byte-identical to pre-split behavior for that transport).
    cells: nested dict family -> region -> cell_dict.
           Cell shape (plain dict, not a dataclass):
             cells[family][region] = {
                 "mt": int | null,     # Meshtastic channel index for this cell
                 "mc": str | null,     # MeshCore channel name for this cell
                 "min_severity": str,  # floor: "routine"|"priority"|"immediate"
                 "enabled": bool,      # False = cell ignored even when matrix enabled
             }
           Sparse: only filled cells are stored; omitted cells are no-ops.
    """

    mt_enabled: bool = False   # Meshtastic region routing master switch
    mc_enabled: bool = False   # MeshCore region routing master switch
    cells: dict = field(default_factory=dict)


@dataclass
class NotificationsConfig:
    """Notification system settings."""

    enabled: bool = False
    # v0.5.8b cold-start grace: after the first event the dispatcher sees,
    # suppress mesh broadcasts for N seconds to absorb any JetStream
    # backlog. Persistence rows still get written -- only broadcasts are
    # suppressed. Anchor is "first-event-seen" (not container-boot) so
    # meshai can sit idle for hours with master OFF and the grace only
    # kicks in when adapters actually start producing.
    cold_start_grace_seconds: int = 60
    # v0.5.11 band-conditions scheduled broadcaster (3x/day HF propagation).
    # GUI-editable per Rule 17. Empty schedule list disables; the
    # _enabled flag is the master switch independent of the times.
    band_conditions_enabled: bool = True
    band_conditions_schedule: list = field(
        default_factory=lambda: ["06:00", "14:00", "22:00"])
    band_conditions_tz: str = "America/Boise"
    toggles: dict = field(default_factory=_default_toggles)  # family -> NotificationToggle
    digest: DigestConfig = field(default_factory=DigestConfig)
    rules: list = field(default_factory=list)  # List of NotificationRuleConfig
    # Integration C1: named, reusable delivery targets (name -> NotificationDestination).
    # Toggles/rules reference these by name via their own `destinations` list;
    # empty reference list => the inline delivery fields are used (unchanged).
    destinations: dict = field(default_factory=dict)
    # P3: compact region×family routing matrix (default-inert when enabled=False).
    region_routes: RegionRouteMatrix = field(default_factory=RegionRouteMatrix)


# Fields copied verbatim from an inline (toggle/rule) delivery config onto a
# NotificationDestination of a given type. Used only by synthesize_destinations.
_DEST_TYPE_FIELDS = {
    "mesh_broadcast": ("broadcast_channel",),
    "meshcore_broadcast": ("meshcore_channel",),
    "mesh_dm": ("node_ids",),
    "meshcore_dm": ("meshcore_dm_contacts",),
    "email": ("smtp_host", "smtp_port", "smtp_user", "smtp_password",
              "smtp_tls", "from_address", "recipients"),
    "webhook": ("webhook_url", "webhook_headers"),
}


def synthesize_destinations(config) -> None:
    """Populate ``config.notifications.destinations`` from the inline delivery
    fields on toggles/rules, and set each entry's ``destinations`` reference
    list -- a DISPLAY/opt-in convenience for the C2 UI ("convert to
    destinations").

    IMPORTANT — this is NOT wired into the dispatcher and MUST NOT be
    auto-run as part of load/save. It is intentionally decoupled because it
    is NOT guaranteed byte-identical for toggles: a toggle's per-severity
    ``severity_channels`` routing (different channel TYPES at different
    severities) collapses into a flat destination set, and the dispatcher's
    destination path fires all referenced destinations subject only to the
    ``min_severity`` floor (see dispatcher._dispatch_toggles). Only invoke it
    from the UI on an explicit operator action, and only where that flattening
    is acceptable. Default delivery stays on the inline fallback path.

    Idempotent: entries that already reference destinations are left untouched;
    identical inline delivery configs are de-duplicated to a shared destination.
    """
    notif = getattr(config, "notifications", None)
    if notif is None:
        return
    registry = getattr(notif, "destinations", None)
    if not isinstance(registry, dict):
        registry = {}
        notif.destinations = registry

    # signature -> destination name, so identical configs share one destination.
    sig_to_name: dict = {}
    for nm, dest in registry.items():
        sig_to_name.setdefault(_dest_signature(dest), nm)

    def _intern(dtype: str, source) -> str:
        """Create-or-find a destination of dtype from source's inline fields."""
        dest = NotificationDestination(type=dtype)
        for f in _DEST_TYPE_FIELDS.get(dtype, ()):  # copy only relevant fields
            setattr(dest, f, getattr(source, f, getattr(dest, f)))
        sig = _dest_signature(dest)
        if sig in sig_to_name:
            return sig_to_name[sig]
        name = f"{dtype}_{len([n for n in registry if registry[n].type == dtype]) + 1}"
        while name in registry:
            name += "_x"
        dest.name = name
        registry[name] = dest
        sig_to_name[sig] = name
        return name

    # Toggles: union of channel types across all severity rows (minus digest).
    for tog in (getattr(notif, "toggles", None) or {}).values():
        if getattr(tog, "destinations", None):
            continue
        types = []
        for row in (getattr(tog, "severity_channels", None) or {}).values():
            for t in row:
                if t != "digest" and t not in types:
                    types.append(t)
        refs = [_intern(t, tog) for t in types if t in _DEST_TYPE_FIELDS]
        if refs:
            tog.destinations = refs

    # Rules: single delivery type.
    for rule in (getattr(notif, "rules", None) or []):
        if getattr(rule, "destinations", None):
            continue
        dtype = getattr(rule, "delivery_type", "")
        if dtype in _DEST_TYPE_FIELDS:
            rule.destinations = [_intern(dtype, rule)]


def _dest_signature(dest) -> tuple:
    """Hashable identity of a destination's delivery-relevant fields."""
    dtype = getattr(dest, "type", "")
    parts = [dtype]
    for f in _DEST_TYPE_FIELDS.get(dtype, ()):
        v = getattr(dest, f, None)
        if isinstance(v, list):
            v = tuple(v)
        elif isinstance(v, dict):
            v = tuple(sorted(v.items()))
        parts.append((f, v))
    return tuple(parts)


@dataclass
class DashboardConfig:
    """Web dashboard settings."""

    enabled: bool = True
    port: int = 8080
    host: str = "0.0.0.0"

# v0.8 danger_zones: infrastructure-node hazard correlation. A standalone,
# isolated config section (its own dataclass tree + danger_zones.yaml + its own
# GET/PUT) so it never touches the complex notifications dataclass.

# Hardcoded Meshtastic role-name vocabulary (mirror of
# mesh_data_store.MESHTASTIC_ROLE_MAP values). Defined locally to avoid an
# import cycle (config.py must not import mesh_data_store).
_DZ_VALID_ROLES = frozenset({
    "ROUTER", "ROUTER_LATE", "CLIENT_BASE", "ROUTER_CLIENT", "REPEATER",
    "CLIENT", "CLIENT_MUTE", "TRACKER", "TAK",
})

_DZ_VALID_DELIVERY = frozenset({
    "mesh_broadcast", "mesh_dm", "meshcore_broadcast", "meshcore_dm",
    "email", "webhook", "none",
})
# Hazard families that map onto categories.VALID_TOGGLES. snow is a sub-gate of
# weather and flood a sub-gate of seismic (resolved in the correlator), so they
# are NOT validated against VALID_TOGGLES.
_DZ_PARENT_FAMILIES = ("fire", "weather", "avalanche", "seismic")


@dataclass
class DangerZoneHazardConfig:
    """Per-hazard-family danger-zone tuning (distances in MILES)."""

    enabled: bool = True
    buffer_mi: float = 5.0


@dataclass
class DangerZoneFireConfig(DangerZoneHazardConfig):
    """Fire-family danger-zone tuning; adds an acreage floor.

    ``min_acres`` is fire-only (read at danger_zone_correlator.py in the
    ``fam == "fire"`` branch); the non-fire families use the plain
    DangerZoneHazardConfig, which no longer carries it.
    """

    min_acres: float = 0.0  # skip fires smaller than this (0 = no floor)


@dataclass
class DangerZonesConfig:
    """Infrastructure-node hazard danger-zone subsystem settings.

    Requires notifications.enabled (the EventBus only exists under that guard).
    Ships disabled; enabling without turning off dry_run is log-only (no RF).
    """

    enabled: bool = False
    dry_run: bool = True
    monitor_roles: list[str] = field(
        default_factory=lambda: ["ROUTER", "ROUTER_LATE", "CLIENT_BASE"])
    default_buffer_mi: float = 5.0
    cooldown_minutes: int = 360

    # Per-family sub-configs. snow->weather, flood->seismic resolved in the
    # correlator; both still exposed here for distinct GUI tuning.
    fire: DangerZoneFireConfig = field(default_factory=DangerZoneFireConfig)
    weather: DangerZoneHazardConfig = field(default_factory=DangerZoneHazardConfig)
    snow: DangerZoneHazardConfig = field(default_factory=DangerZoneHazardConfig)
    flood: DangerZoneHazardConfig = field(default_factory=DangerZoneHazardConfig)
    avalanche: DangerZoneHazardConfig = field(default_factory=DangerZoneHazardConfig)
    seismic: DangerZoneHazardConfig = field(default_factory=DangerZoneHazardConfig)

    # Delivery
    delivery_type: str = "mesh_dm"  # mesh_broadcast|mesh_dm|email|webhook|none
    node_ids: list = field(default_factory=list)
    broadcast_channel: Optional[int] = None
    webhook_url: str = ""
    webhook_headers: dict = field(default_factory=dict)

    def __post_init__(self):
        # Lazy import: categories.py imports only `typing`, so no cycle; kept
        # function-local per plan to stay defensive against future imports.
        from meshai.notifications.categories import VALID_TOGGLES

        if self.delivery_type not in _DZ_VALID_DELIVERY:
            raise ValueError(
                f"danger_zones.delivery_type must be one of "
                f"{sorted(_DZ_VALID_DELIVERY)}, got {self.delivery_type!r}")

        for role in self.monitor_roles:
            if role not in _DZ_VALID_ROLES:
                raise ValueError(
                    f"danger_zones.monitor_roles contains invalid role {role!r}; "
                    f"valid roles: {sorted(_DZ_VALID_ROLES)}")

        # Parent families must exist in the canonical toggle vocabulary.
        for fam in _DZ_PARENT_FAMILIES:
            if fam not in VALID_TOGGLES:
                raise ValueError(
                    f"danger_zones parent family {fam!r} is not a valid toggle "
                    f"({sorted(VALID_TOGGLES)})")


@dataclass
class Coverage:
    """Universal geographic coverage bounding box.

    A single [west, south, east, north] bbox that drives every native adapter's
    effective scope in Phase 2+. Phase 1 defines the shape; no adapter is wired yet.

    bbox: [west, south, east, north] = [min_lon, min_lat, max_lon, max_lat].
          Empty list means "not configured" — adapters fall back to their own scope.
    enabled: master switch; when False, adapters fall back to their own scope even
             if bbox is populated.
    areas: multi-box list for the Shapely geometry gate (Phase 3+). Each entry is
           {"name": str, "west": float, "south": float, "east": float, "north": float}.
           When non-empty, takes precedence over bbox for the CoverageFilter gate.
           When empty, CoverageFilter falls back to bbox (if set) for back-compat.
    """

    bbox: list = field(default_factory=list)   # [west, south, east, north]; empty = not configured
    enabled: bool = True                        # master switch for deriving adapter scope from bbox
    excluded_adapters: list = field(default_factory=list)  # adapters that OPT OUT of coverage bbox and use their own config
    areas: list = field(default_factory=list)  # multi-box list [{name,west,south,east,north}]; empty = use bbox fallback
    region_tagging: bool = False               # P1: when True, CoverageFilter stamps event.region/regions from named areas


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
    meshcore_context: MeshCoreContextConfig = field(default_factory=MeshCoreContextConfig)
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
    danger_zones: DangerZonesConfig = field(default_factory=DangerZonesConfig)
    coverage: Coverage = field(default_factory=Coverage)
    # Config-driven REST/GeoJSON sources for the universal GenericHttpAdapter.
    # Each entry is a PLAIN DICT (not a nested dataclass — the adapter parses
    # and validates it) so the loader passes it through verbatim in both
    # directions. Keys: name, enabled, url, items_path, id_path, lat_path,
    # lon_path, geometry_path, title_path, time_path, category, poll_seconds,
    # severity, field_mappings:[{source_path,dest_key}], summary_template, emoji.
    generic_sources: list = field(default_factory=list)

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
                min_severity=old_rule.get("min_severity", "priority"),
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
            )
            migrated_rules.append(new_rule)

    # Replace rules with migrated ones (migrated rules come first, then any new-format rules)
    if migrated_rules:
        # Keep only non-migrated rules (those without channel_ids)
        existing_new_rules = [r for r in notifications.rules if not getattr(r, 'channel_ids', [])]
        notifications.rules = migrated_rules + existing_new_rules
        _config_logger.info("Migrated to %d self-contained rules", len(notifications.rules))


# Keys that are legitimately present in raw config dicts but intentionally have
# NO matching dataclass field on the target class -- they're consumed by
# special-case logic elsewhere in _dict_to_dataclass (e.g. legacy-format
# migration) rather than becoming a field. Warning about these would be a
# false positive: the key isn't a typo, it's a known, still-supported legacy
# shape. Keyed by (dataclass, key name).
_KNOWN_LEGACY_DROP_KEYS = {
    # Pre-v0.5 notifications.channels list; migrated into self-contained
    # rules by _migrate_legacy_channels (reads straight from the raw dict,
    # not from the coerced NotificationsConfig).
    (NotificationsConfig, "channels"): (
        "legacy notifications.channels format, handled by _migrate_legacy_channels"
    ),
    # Pre-region-routing-split master switch. The explicit region_routes
    # handler (in the "notifications" branch below) reads this directly via
    # rr.get("enabled", ...) as the default for mt_enabled; it never becomes
    # a RegionRouteMatrix field.
    (RegionRouteMatrix, "enabled"): (
        "legacy region_routes.enabled (pre-mt/mc split), mapped to mt_enabled"
    ),
}


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
            if (cls, key) not in _KNOWN_LEGACY_DROP_KEYS:
                _config_logger.warning(
                    "Config key '%s' is not a recognized field on %s -- it will "
                    "be IGNORED (dropped) on load. Check for a typo, or a "
                    "renamed/removed field.",
                    key, cls.__name__,
                )
            continue

        field_type = field_types[key]

        # Notifications needs special rules/channels coercion -- must run
        # BEFORE the generic nested-dataclass handler, which would otherwise
        # shadow it and leave rules as raw dicts (Phase 2.16.1 fix).
        if key == "notifications" and isinstance(value, dict):
            notifications = _dict_to_dataclass(NotificationsConfig, value)
            if "rules" in value and isinstance(value["rules"], list):
                notifications.rules = [
                    _dict_to_dataclass(NotificationRuleConfig, r) if isinstance(r, dict) else r
                    for r in value["rules"]
                ]
            if "toggles" in value and isinstance(value["toggles"], dict):
                notifications.toggles = {
                    name: _dict_to_dataclass(NotificationToggle, t) if isinstance(t, dict) else t
                    for name, t in value["toggles"].items()
                }
            # Integration C1: destinations is a dict of name -> NotificationDestination.
            # field_type is a bare `dict`, so the generic nested-dataclass handler
            # never coerces it -- do it explicitly here (mirrors toggles above).
            if "destinations" in value and isinstance(value["destinations"], dict):
                notifications.destinations = {
                    name: _dict_to_dataclass(NotificationDestination, d) if isinstance(d, dict) else d
                    for name, d in value["destinations"].items()
                }
            # P3: coerce region_routes dict -> RegionRouteMatrix (mirrors destinations above).
            if "region_routes" in value and isinstance(value["region_routes"], dict):
                rr = value["region_routes"]
                _legacy = bool(rr.get("enabled", False))  # pre-split single master switch
                notifications.region_routes = RegionRouteMatrix(
                    mt_enabled=bool(rr.get("mt_enabled", _legacy)),
                    mc_enabled=bool(rr.get("mc_enabled", False)),
                    cells=rr.get("cells", {}),
                )
            if "channels" in value and isinstance(value["channels"], list) and value["channels"]:
                _migrate_legacy_channels(notifications, value)
            kwargs[key] = notifications
        # Handle nested dataclasses
        elif hasattr(field_type, "__dataclass_fields__") and isinstance(value, dict):
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
        elif key == "usgs_quake" and isinstance(value, dict):
            kwargs[key] = _dict_to_dataclass(USGSQuakeConfig, value)
        elif key == "traffic" and isinstance(value, dict):
            kwargs[key] = _dict_to_dataclass(TomTomConfig, value)
        elif key == "roads511" and isinstance(value, dict):
            kwargs[key] = _dict_to_dataclass(Roads511Config, value)
        elif key == "wzdx" and isinstance(value, dict):
            kwargs[key] = _dict_to_dataclass(WZDxConfig, value)
        elif key == "firms" and isinstance(value, dict):
            kwargs[key] = _dict_to_dataclass(FIRMSConfig, value)
        elif key == "satpass" and isinstance(value, dict):
            kwargs[key] = _dict_to_dataclass(SatpassConfig, value)
        elif key == "ipaws" and isinstance(value, dict):
            kwargs[key] = _dict_to_dataclass(IPAWSConfig, value)
        elif key == "environmental" and isinstance(value, dict):
            kwargs[key] = _dict_to_dataclass(EnvironmentalConfig, value)
        elif key == "dashboard" and isinstance(value, dict):
            kwargs[key] = _dict_to_dataclass(DashboardConfig, value)
        elif key == "coverage" and isinstance(value, dict):
            kwargs[key] = _dict_to_dataclass(Coverage, value)
        elif key == "toggles" and isinstance(value, dict):
            # v0.5: notifications.toggles is a dict of family -> NotificationToggle
            kwargs[key] = {
                fam: _dict_to_dataclass(NotificationToggle, t) if isinstance(t, dict) else t
                for fam, t in value.items()
            }
        elif key == "digest" and isinstance(value, dict):
            kwargs[key] = _dict_to_dataclass(DigestConfig, value)
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
        elif isinstance(value, dict):
            # Handle dict of dataclasses (like notifications.toggles)
            result[field_name] = {
                k: _dataclass_to_dict(v) if hasattr(v, "__dataclass_fields__") else v
                for k, v in value.items()
            }
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
    header = "# MeshAI Configuration\n# Generated by meshai\n\n"

    with open(config_path, "w") as f:
        f.write(header)
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
