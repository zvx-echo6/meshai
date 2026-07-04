"""Multi-file configuration loader for MeshAI v0.3.

This module provides:
- !include directive support for splitting config across files
- Environment variable interpolation (${VAR_NAME} and ${VAR_NAME:-default})
- Operator-local value merging from local.yaml
- Secret loading from .env files
- Section-aware save_section() for dashboard write-back

The loader produces the same Config dataclass shape as config.py,
ensuring backward compatibility with all existing consumers.
"""

import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import dotenv_values

# Import existing dataclasses - shape must NOT change
from .config import (
    Config,
    _dict_to_dataclass,
    _dataclass_to_dict,
)

_logger = logging.getLogger(__name__)

# =============================================================================
# SECTION TO FILE MAPPING
# =============================================================================

SECTION_TO_FILE: dict[str, str] = {
    # Inline in orchestrator config.yaml
    "timezone": "config.yaml",
    "bot": "config.yaml",
    "response": "config.yaml",
    "history": "config.yaml",
    "memory": "config.yaml",
    "context": "config.yaml",
    "meshcore_context": "config.yaml",
    "weather": "config.yaml",
    "meshmonitor": "config.yaml",
    "knowledge": "config.yaml",

    # Domain files
    "connection": "meshtastic.yaml",
    "commands": "meshtastic.yaml",
    "mesh_sources": "mesh_sources.yaml",
    "mesh_intelligence": "mesh_intelligence.yaml",
    "environmental": "env_feeds.yaml",
    "notifications": "notifications.yaml",
    "llm": "llm.yaml",
    "dashboard": "dashboard.yaml",
    "danger_zones": "danger_zones.yaml",
}

# Fields that should be written to local.yaml instead of domain files
LOCAL_FIELDS: dict[str, str] = {
    "bot.name": "identity.name",
    "bot.owner": "identity.owner",
    "connection.tcp_host": "infrastructure.tcp_host",
    "knowledge.qdrant_host": "infrastructure.qdrant_host",
    "knowledge.tei_host": "infrastructure.tei_host",
    "knowledge.sparse_host": "infrastructure.sparse_host",
    "meshmonitor.url": "mesh_sources.meshmonitor_url",
    "mesh_intelligence.critical_nodes": "critical_nodes",
    "environmental.ducting.latitude": "env_center.latitude",
    "environmental.ducting.longitude": "env_center.longitude",
}

# Fields that contain secrets - NEVER written, must be in .env
SECRET_FIELDS: set[str] = {
    "llm.api_key",
    "mesh_sources.*.api_token",
    "mesh_sources.*.password",
    "environmental.traffic.api_key",
    "environmental.firms.map_key",
    "notifications.rules.*.smtp_password",
    "notifications.toggles.*.smtp_password",
    "danger_zones.webhook_url",
}

# Secret env var names expected in .env
EXPECTED_SECRETS: list[str] = [
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY",
    "MESHMONITOR_API_TOKEN",
    "MQTT_PASSWORD",
    "TOMTOM_API_KEY",
    "FIRMS_MAP_KEY",
    "SMTP_PASSWORD",
]


# =============================================================================
# YAML !INCLUDE CONSTRUCTOR
# =============================================================================

# Global set for tracking files currently being loaded (cycle detection)
_loading_files: set[Path] = set()


def _make_include_loader(base_path: Path):
    """Create an IncludeLoader class with the given base path."""

    class IncludeLoader(yaml.SafeLoader):
        """YAML loader with !include tag support."""
        pass

    def construct_include(loader: IncludeLoader, node: yaml.Node) -> Any:
        """Handle !include directive."""
        relative_path = loader.construct_scalar(node)
        include_path = (base_path / relative_path).resolve()

        # Cycle detection using global set
        if include_path in _loading_files:
            raise yaml.YAMLError(
                f"Circular include detected: {include_path} is already being loaded. "
                f"Current loading chain: {[str(p) for p in _loading_files]}"
            )

        if not include_path.exists():
            raise yaml.YAMLError(
                f"Include file not found: {include_path} "
                f"(referenced from {base_path / 'config.yaml'})"
            )

        _loading_files.add(include_path)
        try:
            with open(include_path, "r") as f:
                # Recursively load with the include file's directory as new base
                NestedLoader = _make_include_loader(include_path.parent)
                return yaml.load(f, Loader=NestedLoader)
        finally:
            _loading_files.discard(include_path)

    IncludeLoader.add_constructor("!include", construct_include)
    return IncludeLoader


def _load_yaml_with_includes(file_path: Path) -> dict:
    """Load a YAML file with !include directive support."""
    global _loading_files
    _loading_files.clear()  # Reset cycle detection

    if not file_path.exists():
        return {}

    # Add the root file to loading set
    file_path = file_path.resolve()
    _loading_files.add(file_path)

    try:
        with open(file_path, "r") as f:
            Loader = _make_include_loader(file_path.parent)
            return yaml.load(f, Loader=Loader) or {}
    finally:
        _loading_files.discard(file_path)


# ---- v0.6-tail-4: !include-preserving load/dump for save_section ----------
#
# save_section() needs to re-read target_path off disk so it can preserve
# secret-ref placeholders and existing keys for sections that share a file.
# When target_path is config.yaml (the orchestrator) the file contains
# !include directives for OTHER sections; plain yaml.safe_load can't parse
# those and the whole save fails. We can't use _load_yaml_with_includes
# because that would substitute the included files in, and then the
# subsequent yaml.dump would flatten them onto disk -- losing the
# multi-file layout permanently the first time anyone PUTs an inline
# section like `bot`. Instead we read with a loader that returns an
# Include() placeholder for each !include node, and dump with a dumper
# that re-emits Include(path) as `!include path`. The round-trip is
# byte-stable for the include directives, and the non-include sections
# (which is everything save_section actually mutates) just round-trip as
# plain dict/list.


class Include:
    """Placeholder preserving an !include directive across read/write."""

    __slots__ = ("path",)

    def __init__(self, path: str):
        self.path = path

    def __repr__(self) -> str:
        return f"Include({self.path!r})"

    def __eq__(self, other) -> bool:
        return isinstance(other, Include) and self.path == other.path

    def __hash__(self) -> int:
        return hash(("Include", self.path))


def _make_preserve_loader():
    """SafeLoader subclass that returns Include() for !include scalars.

    Unlike _make_include_loader, this does NOT recurse into the
    referenced file -- it keeps the directive intact so a subsequent
    dump can emit it back to disk verbatim.
    """

    class PreserveLoader(yaml.SafeLoader):
        pass

    def construct_include(loader: PreserveLoader, node: yaml.Node) -> Include:
        return Include(loader.construct_scalar(node))

    PreserveLoader.add_constructor("!include", construct_include)
    return PreserveLoader


def _make_preserve_dumper():
    """SafeDumper subclass that renders Include() back as `!include path`."""

    class PreserveDumper(yaml.SafeDumper):
        pass

    def represent_include(dumper: PreserveDumper, data: Include):
        # style="" forces plain (unquoted) scalar so the output matches
        # the prod on-disk convention `!include foo.yaml` rather than
        # PyYAML's auto-picked `!include 'foo.yaml'`. The runtime loader
        # parses both, but we want the round-trip to be byte-stable.
        return dumper.represent_scalar("!include", data.path, style="")

    PreserveDumper.add_representer(Include, represent_include)
    return PreserveDumper


def _load_yaml_preserve(file_path: Path):
    """Read a YAML file, keeping !include nodes as Include placeholders.

    Returns {} for missing files (matches the runtime loader's contract).
    """
    if not Path(file_path).exists():
        return {}
    Loader = _make_preserve_loader()
    with open(file_path, "r") as f:
        return yaml.load(f, Loader=Loader) or {}


def _dump_yaml_preserve(data, file_path: Path) -> None:
    """Write a YAML file, re-emitting Include() as `!include path`."""
    Dumper = _make_preserve_dumper()
    with open(file_path, "w") as f:
        yaml.dump(
            data,
            f,
            Dumper=Dumper,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        )



# =============================================================================
# ENVIRONMENT VARIABLE INTERPOLATION
# =============================================================================

_ENV_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}")


def _interpolate_env_vars(value: Any, env: dict[str, str]) -> Any:
    """Recursively interpolate ${VAR_NAME} and ${VAR_NAME:-default} in strings.

    Args:
        value: The value to interpolate (can be string, dict, list, or other)
        env: Combined environment (os.environ + .env file values)

    Returns:
        The value with environment variables resolved
    """
    if isinstance(value, str):
        def replace_match(match):
            var_name = match.group(1)
            default = match.group(2)

            # os.environ takes precedence over .env file
            resolved = os.environ.get(var_name)
            if resolved is None:
                resolved = env.get(var_name)
            if resolved is None:
                if default is not None:
                    return default
                _logger.warning(
                    f"Environment variable ${{{var_name}}} not found and no default provided. "
                    "Using empty string."
                )
                return ""
            return resolved

        return _ENV_PATTERN.sub(replace_match, value)

    elif isinstance(value, dict):
        return {k: _interpolate_env_vars(v, env) for k, v in value.items()}

    elif isinstance(value, list):
        return [_interpolate_env_vars(item, env) for item in value]

    return value


# =============================================================================
# LOCAL.YAML MERGING
# =============================================================================

def _merge_local_values(data: dict, local: dict) -> dict:
    """Merge operator-local values from local.yaml into the config data.

    This handles:
    - identity.name/owner -> bot.name/owner
    - infrastructure.* -> connection/knowledge hosts
    - regions.{name}.lat/lon -> mesh_intelligence.regions[name].lat/lon
    - critical_nodes -> mesh_intelligence.critical_nodes
    - mesh_sources.sources.{name}.* -> mesh_sources[name].*
    - env_center.* -> environmental.ducting.*
    - notification_targets.* -> notifications rules

    Args:
        data: The loaded config data (will be modified in place)
        local: The local.yaml data

    Returns:
        The merged data dict
    """
    if not local:
        return data

    # Identity -> bot
    identity = local.get("identity", {})
    if "bot" in data:
        if identity.get("name"):
            data["bot"]["name"] = identity["name"]
        if identity.get("owner"):
            data["bot"]["owner"] = identity["owner"]

    # Infrastructure hosts
    infra = local.get("infrastructure", {})
    if infra.get("tcp_host") and "connection" in data:
        data["connection"]["tcp_host"] = infra["tcp_host"]
    if "knowledge" in data:
        if infra.get("qdrant_host"):
            data["knowledge"]["qdrant_host"] = infra["qdrant_host"]
        if infra.get("tei_host"):
            data["knowledge"]["tei_host"] = infra["tei_host"]
        if infra.get("sparse_host"):
            data["knowledge"]["sparse_host"] = infra["sparse_host"]

    # Meshmonitor URL
    mesh_sources_local = local.get("mesh_sources", {})
    if mesh_sources_local.get("meshmonitor_url") and "meshmonitor" in data:
        data["meshmonitor"]["url"] = mesh_sources_local["meshmonitor_url"]

    # Mesh sources URLs
    sources_local = mesh_sources_local.get("sources", {})
    if "mesh_sources" in data and isinstance(data["mesh_sources"], list):
        for source in data["mesh_sources"]:
            if isinstance(source, dict):
                source_name = source.get("name", "")
                local_source = sources_local.get(source_name, {})
                if local_source.get("url"):
                    source["url"] = local_source["url"]
                if local_source.get("host"):
                    source["host"] = local_source["host"]

    # Region coordinates
    regions_local = local.get("regions", {})
    if "mesh_intelligence" in data:
        mi = data["mesh_intelligence"]
        if "regions" in mi and isinstance(mi["regions"], list):
            for region in mi["regions"]:
                if isinstance(region, dict):
                    region_name = region.get("name", "")
                    local_coords = regions_local.get(region_name, {})
                    if "lat" in local_coords:
                        region["lat"] = local_coords["lat"]
                    if "lon" in local_coords:
                        region["lon"] = local_coords["lon"]

        # Critical nodes
        if local.get("critical_nodes"):
            mi["critical_nodes"] = local["critical_nodes"]

    # Environmental center point
    env_center = local.get("env_center", {})
    if "environmental" in data:
        env = data["environmental"]
        if "ducting" in env:
            if env_center.get("latitude") is not None:
                env["ducting"]["latitude"] = env_center["latitude"]
            if env_center.get("longitude") is not None:
                env["ducting"]["longitude"] = env_center["longitude"]

        # NWS user agent from contact email
        if identity.get("contact_email") and "nws" in env:
            email = identity["contact_email"]
            env["nws"]["user_agent"] = f"(meshai, {email})"

    # Notification targets
    notif_targets = local.get("notification_targets", {})
    if "notifications" in data and "rules" in data["notifications"]:
        alert_node_ids = notif_targets.get("alert_node_ids", [])
        smtp_recipients = notif_targets.get("smtp_recipients", [])

        for rule in data["notifications"]["rules"]:
            if isinstance(rule, dict):
                # Apply default node_ids if not set
                if rule.get("delivery_type") == "mesh_dm" and not rule.get("node_ids"):
                    rule["node_ids"] = alert_node_ids
                # Apply default recipients if not set
                if rule.get("delivery_type") == "email" and not rule.get("recipients"):
                    rule["recipients"] = smtp_recipients
                # Apply smtp_from
                if notif_targets.get("smtp_from") and not rule.get("from_address"):
                    rule["from_address"] = notif_targets["smtp_from"]

    return data


# =============================================================================
# VALIDATION
# =============================================================================

def _validate_config(data: dict, local: dict, env: dict[str, str]) -> None:
    """Validate config and log warnings for missing values.

    This does NOT raise errors - MeshAI starts in degraded mode with missing values.
    """
    # Check regions for missing coordinates
    if "mesh_intelligence" in data:
        mi = data["mesh_intelligence"]
        if mi.get("enabled") and "regions" in mi:
            regions_local = local.get("regions", {}) if local else {}
            for region in mi["regions"]:
                if isinstance(region, dict):
                    region_name = region.get("name", "unknown")
                    if not region.get("lat") or not region.get("lon"):
                        if region_name not in regions_local:
                            _logger.warning(
                                f"Region '{region_name}' has no coordinates in local.yaml - "
                                "geographic features disabled for this region"
                            )

    # Check for missing secrets
    missing_secrets = []
    for secret in EXPECTED_SECRETS:
        if not os.environ.get(secret) and not env.get(secret):
            missing_secrets.append(secret)

    if missing_secrets:
        _logger.warning(
            f"Missing secret environment variables: {', '.join(missing_secrets)}. "
            "Some features may be disabled."
        )

    # Check LLM API key
    if "llm" in data:
        api_key = data["llm"].get("api_key", "")
        if not api_key or (api_key.startswith("${") and api_key.endswith("}")):
            # It's a reference, check if resolved
            backend = data["llm"].get("backend", "openai").lower()
            key_var = {
                "openai": "OPENAI_API_KEY",
                "anthropic": "ANTHROPIC_API_KEY",
                "google": "GOOGLE_API_KEY",
            }.get(backend, "LLM_API_KEY")
            if not os.environ.get(key_var) and not env.get(key_var):
                _logger.warning(
                    f"LLM backend '{backend}' configured but {key_var} not found. "
                    "LLM responses will fail."
                )


# =============================================================================
# MAIN LOADER
# =============================================================================

def load_config(config_dir: Path = Path("/data/config")) -> Config:
    """Load configuration from multi-file layout.

    This function:
    1. Reads config.yaml (orchestrator) with !include directives
    2. Reads local.yaml if present (operator-local values)
    3. Reads /data/secrets/.env if present (secret values)
    4. Interpolates ${VAR_NAME} references
    5. Merges local values into config
    6. Validates and logs warnings for missing values
    7. Returns the same Config dataclass shape

    Args:
        config_dir: Path to config directory (default: /data/config)

    Returns:
        Config dataclass instance
    """
    config_dir = Path(config_dir)

    # Determine config file path
    # Support both new layout (/data/config/config.yaml) and legacy (/data/config.yaml)
    orchestrator_path = config_dir / "config.yaml"
    legacy_path = config_dir.parent / "config.yaml" if config_dir.name == "config" else None

    if not orchestrator_path.exists():
        if legacy_path and legacy_path.exists():
            # Fall back to legacy single-file config
            _logger.info(f"Using legacy config at {legacy_path}")
            from .config import load_config as legacy_load
            return legacy_load(legacy_path)
        else:
            _logger.warning(
                f"Config file not found at {orchestrator_path}. "
                "Using default configuration."
            )
            config = Config()
            config._config_path = orchestrator_path
            return config

    # Load orchestrator with !include support
    _logger.debug(f"Loading config from {orchestrator_path}")
    data = _load_yaml_with_includes(orchestrator_path)
    # Hoist meshtastic.connection and meshtastic.commands to top level
    # meshtastic.yaml contains both sections under wrapper keys
    if "meshtastic" in data and isinstance(data["meshtastic"], dict):
        meshtastic = data.pop("meshtastic")
        if "connection" in meshtastic:
            data["connection"] = meshtastic["connection"]
        if "commands" in meshtastic:
            data["commands"] = meshtastic["commands"]

    # Load local.yaml
    local_path = config_dir / "local.yaml"
    local_data = {}
    if local_path.exists():
        with open(local_path, "r") as f:
            local_data = yaml.safe_load(f) or {}
        _logger.debug(f"Loaded local config from {local_path}")
    else:
        _logger.warning(
            f"No local.yaml found at {local_path}. "
            "MeshAI is in no-location mode - geographic features disabled."
        )

    # Load secrets from .env
    secrets_path = config_dir.parent / "secrets" / ".env"
    env_data = {}
    if secrets_path.exists():
        env_data = dotenv_values(secrets_path)
        _logger.debug(f"Loaded {len(env_data)} secrets from {secrets_path}")
    else:
        # Try alternate location
        alt_secrets_path = Path("/data/secrets/.env")
        if alt_secrets_path.exists():
            env_data = dotenv_values(alt_secrets_path)
            _logger.debug(f"Loaded {len(env_data)} secrets from {alt_secrets_path}")
        else:
            _logger.warning(
                f"No .env file found at {secrets_path}. "
                "API keys must be set via environment variables."
            )

    # Interpolate environment variables
    data = _interpolate_env_vars(data, env_data)

    # Merge local values
    data = _merge_local_values(data, local_data)

    # Validate and warn
    _validate_config(data, local_data, env_data)

    # Convert to Config dataclass
    config = _dict_to_dataclass(Config, data)
    config._config_path = orchestrator_path

    return config


# =============================================================================
# SECTION SAVER
# =============================================================================

def _is_secret_field(section: str, field_path: str) -> bool:
    """Check if a field path matches a secret field pattern."""
    full_path = f"{section}.{field_path}" if field_path else section

    for pattern in SECRET_FIELDS:
        # Convert pattern to regex
        regex = pattern.replace(".", r"\.").replace("*", r"[^.]+")
        if re.match(f"^{regex}$", full_path):
            return True
    return False


def _extract_local_fields(section: str, data: dict) -> tuple[dict, dict]:
    """Extract local fields from data.

    Returns:
        (domain_data, local_data) - data split by destination
    """
    domain_data = dict(data)
    local_data = {}

    # Check each LOCAL_FIELDS pattern
    for field_pattern, local_path in LOCAL_FIELDS.items():
        if not field_pattern.startswith(f"{section}."):
            continue

        # Extract field name from pattern
        field_name = field_pattern[len(section) + 1:]  # Remove "section."

        if ".*." in field_name:
            # Array field pattern - handle specially
            continue

        if field_name in domain_data:
            # Move to local_data using the local_path
            value = domain_data.pop(field_name)
            # Build nested structure in local_data
            parts = local_path.split(".")
            current = local_data
            for part in parts[:-1]:
                if part not in current:
                    current[part] = {}
                current = current[part]
            current[parts[-1]] = value

    return domain_data, local_data


def save_section(
    section_name: str,
    data: dict,
    config_dir: Path = Path("/data/config"),
) -> dict:
    """Save a configuration section to the appropriate file(s).

    This function:
    1. Determines which file(s) the section belongs to
    2. Extracts local-identifying fields to local.yaml
    3. Rejects attempts to save secret fields
    4. Writes domain data to the appropriate file
    5. Writes local data to local.yaml

    Args:
        section_name: Name of the section (e.g., "notifications", "llm")
        data: The section data as a dict
        config_dir: Path to config directory

    Returns:
        Dict with status: {"saved": True, "files_written": [...], "rejected_secrets": [...]}

    Raises:
        ValueError: If section_name is not recognized
    """
    config_dir = Path(config_dir)

    if section_name not in SECTION_TO_FILE:
        raise ValueError(
            f"Unknown section '{section_name}'. "
            f"Valid sections: {', '.join(sorted(SECTION_TO_FILE.keys()))}"
        )

    target_file = SECTION_TO_FILE[section_name]
    target_path = config_dir / target_file
    local_path = config_dir / "local.yaml"

    files_written = []
    rejected_secrets = []

    # Check for secret fields and reject them
    # --- secret-ref preservation (v0.4 C.3.1) -------------------------------
    # A GUI save round-trips the *interpolated* value of a ${VAR} secret (the
    # GET returns the resolved key string). Without this, save_section would
    # drop the on-disk ${VAR} placeholder and lose the secret reference. So we
    # read the raw on-disk values (pre-interpolation) and, for each secret
    # field, decide:
    #   on-disk ${VAR} and new value == resolved(VAR) -> keep the ${VAR} ref
    #   on-disk ${VAR} and new value != resolved(VAR) -> intentional change, store it
    #   no on-disk ${VAR} ref                          -> reject (never write a raw
    #                                                     secret to a domain file)
    _raw_on_disk = {}
    if target_path.exists():
        try:
            # v0.6-tail-4: read with Include() preservation so config.yaml
            # (which has !include directives for sibling sections) parses
            # without choking. Plain yaml.safe_load used to die here.
            _raw_on_disk = _load_yaml_preserve(target_path) or {}
        except Exception:
            _raw_on_disk = {}
    if target_file in ("meshtastic.yaml", "config.yaml") and isinstance(_raw_on_disk, dict):
        _raw_section = _raw_on_disk.get(section_name) or {}
    else:
        # v0.5.5: list-shaped sections (mesh_sources.yaml) load as a top-level
        # list; carry the list through so _ondisk_ref can walk it by integer
        # index. dict|list|None covered; anything else falls back to {}.
        if isinstance(_raw_on_disk, (dict, list)):
            _raw_section = _raw_on_disk
        else:
            _raw_section = {}

    _secrets_path = config_dir.parent / "secrets" / ".env"
    if not _secrets_path.exists():
        _secrets_path = Path("/data/secrets/.env")
    _env_file = dotenv_values(_secrets_path) if _secrets_path.exists() else {}

    _VAR_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")

    def _resolve_var(name: str):
        v = os.environ.get(name)
        return v if v is not None else _env_file.get(name)

    def _ondisk_ref(field_path: str):
        # v0.5.5: walk dicts by key, lists by integer index so paths like
        # `0.api_token` (mesh_sources) and `rules.0.smtp_password`
        # (notifications) resolve to their on-disk ${VAR} ref correctly.
        node = _raw_section
        for part in field_path.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            elif isinstance(node, list):
                try:
                    node = node[int(part)]
                except (ValueError, IndexError, TypeError):
                    return None
            else:
                return None
        return node

    def check_secrets(d: dict, path: str = "") -> dict:
        cleaned = {}
        for key, value in d.items():
            field_path = f"{path}.{key}" if path else key
            if _is_secret_field(section_name, field_path):
                ref = _ondisk_ref(field_path)
                m = _VAR_RE.match(ref) if isinstance(ref, str) else None
                if m:
                    if _resolve_var(m.group(1)) == (value if isinstance(value, str) else str(value)):
                        cleaned[key] = ref  # unchanged secret -> preserve ${VAR} placeholder
                    else:
                        cleaned[key] = value  # intentional change -> store new value
                else:
                    rejected_secrets.append(field_path)
                    _logger.error(
                        f"Rejected attempt to save secret field '{section_name}.{field_path}'. "
                        "Secret fields must be set via /data/secrets/.env"
                    )
            elif isinstance(value, dict):
                cleaned[key] = check_secrets(value, field_path)
            elif isinstance(value, list):
                # v0.5.5: dotted-index form (`<field>.<i>.<key>`) so list-item
                # secret paths match SECRET_FIELDS entries like
                # `notifications.rules.*.smtp_password` — the `*` regex token
                # matches a single dot-separated token, not a `[i]` suffix.
                cleaned[key] = [
                    check_secrets(item, f"{field_path}.{i}")
                    if isinstance(item, dict) else item
                    for i, item in enumerate(value)
                ]
            else:
                cleaned[key] = value
        return cleaned

    # List sections (e.g. mesh_sources) have no top-level dict to scan for
    # local fields; clean each item for secrets and write the list directly.
    # v0.5.5: each item carries its index as the section-relative path root so
    # `_is_secret_field("mesh_sources", "<i>.api_token")` matches the pattern
    # `mesh_sources.*.api_token` (previously it stripped to bare `api_token`
    # and let raw secrets through).
    if isinstance(data, list):
        domain_data = [
            check_secrets(item, str(i)) if isinstance(item, dict) else item
            for i, item in enumerate(data)
        ]
        local_updates = {}
    else:
        data = check_secrets(data)
        domain_data, local_updates = _extract_local_fields(section_name, data)

    # Load existing target file (v0.6-tail-4: preserve !include directives
    # for inline-section saves to config.yaml; safe_load would crash).
    if target_path.exists():
        existing = _load_yaml_preserve(target_path) or {}
    else:
        existing = {}

    # Handle sections that share a file (meshtastic.yaml has both connection and commands)
    if target_file == "meshtastic.yaml":
        existing[section_name] = domain_data
    elif target_file == "config.yaml":
        # For orchestrator, update the section in place
        existing[section_name] = domain_data
    else:
        # For dedicated files, the whole file IS the section
        existing = domain_data

    # Write domain file (v0.6-tail-4: preserve dumper re-emits Include()
    # placeholders as `!include path` so multi-file layouts survive the
    # round-trip. Plain yaml.dump would crash on Include objects.)
    _dump_yaml_preserve(existing, target_path)
    files_written.append(str(target_path))
    _logger.info(f"Saved {section_name} to {target_path}")

    # Update local.yaml if there are local fields
    if local_updates:
        if local_path.exists():
            with open(local_path, "r") as f:
                local_existing = yaml.safe_load(f) or {}
        else:
            local_existing = {}

        # Deep merge local_updates into local_existing
        def deep_merge(base: dict, updates: dict) -> dict:
            for key, value in updates.items():
                if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                    deep_merge(base[key], value)
                else:
                    base[key] = value
            return base

        deep_merge(local_existing, local_updates)

        with open(local_path, "w") as f:
            yaml.dump(local_existing, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

        # Set restrictive permissions on local.yaml
        local_path.chmod(0o600)
        files_written.append(str(local_path))
        _logger.info(f"Updated local values in {local_path}")

    return {
        "saved": True,
        "files_written": files_written,
        "rejected_secrets": rejected_secrets,
    }


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def get_config_dir_from_path(config_path: Path) -> Path:
    """Determine config directory from a config file path.

    Args:
        config_path: Path to config.yaml (could be legacy or new layout)

    Returns:
        Path to config directory
    """
    config_path = Path(config_path)

    if config_path.is_dir():
        return config_path

    # If pointing to config.yaml in new layout
    if config_path.name == "config.yaml" and config_path.parent.name == "config":
        return config_path.parent

    # If pointing to legacy /data/config.yaml
    if config_path.name == "config.yaml":
        new_layout = config_path.parent / "config"
        if new_layout.exists() and (new_layout / "config.yaml").exists():
            return new_layout

    return config_path.parent
