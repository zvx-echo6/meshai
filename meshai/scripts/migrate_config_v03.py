#!/usr/bin/env python3
"""Migration script for MeshAI config v0.2 → v0.3.

This script converts the monolithic /data/config.yaml into the new
multi-file layout under /data/config/.

Run once: python -m meshai.scripts.migrate_config_v03

The migration:
1. Backs up the original config
2. Splits sections into domain files
3. Extracts operator-identifying values to local.yaml
4. Extracts literal secrets to /data/secrets/.env
5. Creates orchestrator config.yaml with !include directives
6. Verifies the new layout loads identically
"""

import hashlib
import logging
import os
import re
import shutil
import sys
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any

import yaml

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# =============================================================================
# DEEP COMPARISON FOR VERIFICATION
# =============================================================================

def deep_compare(old, new, path=""):
    """Deep compare two values, returning list of difference descriptions."""
    differences = []
    if type(old) != type(new):
        differences.append(f"{path}: type {type(old).__name__} vs {type(new).__name__}")
        return differences
    if isinstance(old, dict):
        for key in sorted(set(old.keys()) | set(new.keys())):
            child_path = f"{path}.{key}" if path else key
            if key not in old:
                differences.append(f"{child_path}: missing in backup")
            elif key not in new:
                differences.append(f"{child_path}: missing in new")
            else:
                differences.extend(deep_compare(old[key], new[key], child_path))
    elif isinstance(old, list):
        if len(old) != len(new):
            differences.append(f"{path}: list length {len(old)} vs {len(new)}")
        else:
            for i, (o, n) in enumerate(zip(old, new)):
                differences.extend(deep_compare(o, n, f"{path}[{i}]"))
    elif old != new:
        differences.append(f"{path}: {repr(old)[:50]} vs {repr(new)[:50]}")
    return differences

# =============================================================================
# CONFIGURATION
# =============================================================================

SOURCE_FILE = Path("/data/config.yaml")
TARGET_DIR = Path("/data/config")
BACKUP_FILE = Path("/data/config.yaml.pre-v03-backup")
SECRETS_DIR = Path("/data/secrets")

# Section to file mapping
SECTION_TO_FILE = {
    "connection": "meshtastic.yaml",
    "commands": "meshtastic.yaml",
    "mesh_sources": "mesh_sources.yaml",
    "mesh_intelligence": "mesh_intelligence.yaml",
    "environmental": "env_feeds.yaml",
    "notifications": "notifications.yaml",
    "llm": "llm.yaml",
    "dashboard": "dashboard.yaml",
}

# Sections that stay inline in orchestrator config.yaml
INLINE_SECTIONS = [
    "timezone",
    "bot",
    "response",
    "history",
    "memory",
    "context",
    "weather",
    "meshmonitor",
    "knowledge",
]

# Fields to extract to local.yaml
LOCAL_EXTRACTIONS = {
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
    "environmental.nws.user_agent": "identity.contact_email",  # Extract email from user_agent
}

# Secret fields - if found as literals, extract to .env
SECRET_PATTERNS = {
    "llm.api_key": "LLM_API_KEY",  # Will be renamed based on backend
    "mesh_sources.*.api_token": "MESHMONITOR_API_TOKEN",
    "mesh_sources.*.password": "MQTT_PASSWORD",
    "environmental.traffic.api_key": "TOMTOM_API_KEY",
    "environmental.firms.map_key": "FIRMS_MAP_KEY",
    "notifications.rules.*.smtp_password": "SMTP_PASSWORD",
}


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def get_nested(data: dict, path: str) -> Any:
    """Get a value from nested dict using dot notation."""
    parts = path.split(".")
    current = data
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def set_nested(data: dict, path: str, value: Any) -> None:
    """Set a value in nested dict using dot notation, creating dicts as needed."""
    parts = path.split(".")
    current = data
    for part in parts[:-1]:
        if part not in current:
            current[part] = {}
        current = current[part]
    current[parts[-1]] = value


def remove_nested(data: dict, path: str) -> bool:
    """Remove a value from nested dict. Returns True if removed."""
    parts = path.split(".")
    current = data
    for part in parts[:-1]:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return False
    if parts[-1] in current:
        del current[parts[-1]]
        return True
    return False


def file_hash(path: Path) -> str:
    """Calculate SHA256 hash of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def is_env_var_ref(value: str) -> bool:
    """Check if a string is an env var reference like ${VAR_NAME}."""
    if not isinstance(value, str):
        return False
    return bool(re.match(r"^\$\{[A-Z_][A-Z0-9_]*\}$", value))


def extract_email_from_user_agent(user_agent: str) -> str:
    """Extract email from NWS user_agent format: (app, email@domain.com)"""
    if not user_agent:
        return ""
    match = re.search(r"[\w.+-]+@[\w.-]+\.\w+", user_agent)
    return match.group(0) if match else ""


# =============================================================================
# PRE-FLIGHT CHECKS
# =============================================================================

def preflight_checks() -> bool:
    """Run pre-flight checks before migration."""
    logger.info("Running pre-flight checks...")

    # Check source exists
    if not SOURCE_FILE.exists():
        logger.error(f"Source file not found: {SOURCE_FILE}")
        return False
    logger.info(f"  Source file exists: {SOURCE_FILE}")

    # Check target directory state
    if TARGET_DIR.exists():
        contents = list(TARGET_DIR.iterdir())
        if contents:
            logger.error(
                f"Target directory {TARGET_DIR} already populated with {len(contents)} items. "
                "Manual intervention needed - remove existing files or restore from backup."
            )
            return False
        logger.info(f"  Target directory exists but is empty: {TARGET_DIR}")
    else:
        logger.info(f"  Target directory does not exist: {TARGET_DIR}")

    # Check backup doesn't already exist (indicates previous migration)
    if BACKUP_FILE.exists():
        logger.warning(
            f"Backup file already exists: {BACKUP_FILE}. "
            "This may indicate a previous migration attempt."
        )
        # Continue anyway - user may be re-running after fixing issues

    logger.info("Pre-flight checks passed.")
    return True


# =============================================================================
# BACKUP
# =============================================================================

def create_backup() -> bool:
    """Create backup of original config."""
    logger.info(f"Creating backup: {SOURCE_FILE} → {BACKUP_FILE}")

    shutil.copy2(SOURCE_FILE, BACKUP_FILE)

    # Verify backup
    source_hash = file_hash(SOURCE_FILE)
    backup_hash = file_hash(BACKUP_FILE)

    if source_hash != backup_hash:
        logger.error(
            f"Backup verification failed! Hashes don't match:\n"
            f"  Source: {source_hash}\n"
            f"  Backup: {backup_hash}"
        )
        return False

    source_size = SOURCE_FILE.stat().st_size
    backup_size = BACKUP_FILE.stat().st_size

    if source_size != backup_size:
        logger.error(
            f"Backup verification failed! Sizes don't match:\n"
            f"  Source: {source_size}\n"
            f"  Backup: {backup_size}"
        )
        return False

    logger.info(f"  Backup verified: {backup_size} bytes, hash {backup_hash[:12]}...")
    return True


# =============================================================================
# EXTRACTION LOGIC
# =============================================================================

def extract_local_values(data: dict) -> dict:
    """Extract operator-identifying values to local.yaml structure."""
    local = {}

    for source_path, dest_path in LOCAL_EXTRACTIONS.items():
        value = get_nested(data, source_path)
        if value is not None and value != "" and value != 0:
            # Special handling for user_agent -> email extraction
            if source_path == "environmental.nws.user_agent":
                value = extract_email_from_user_agent(str(value))
                if not value:
                    continue

            set_nested(local, dest_path, value)
            logger.debug(f"  Extracted {source_path} → local.{dest_path}")

    # Extract region coordinates
    mi = data.get("mesh_intelligence", {})
    regions = mi.get("regions", [])
    if regions:
        local["regions"] = {}
        for region in regions:
            if isinstance(region, dict):
                name = region.get("name", "")
                lat = region.get("lat", 0)
                lon = region.get("lon", 0)
                if name and (lat != 0 or lon != 0):
                    local["regions"][name] = {"lat": lat, "lon": lon}

    # Extract mesh source URLs
    mesh_sources = data.get("mesh_sources", [])
    if mesh_sources:
        local["mesh_sources"] = {"sources": {}}
        for source in mesh_sources:
            if isinstance(source, dict):
                name = source.get("name", "")
                url = source.get("url", "")
                host = source.get("host", "")
                if name and (url or host):
                    local["mesh_sources"]["sources"][name] = {}
                    if url:
                        local["mesh_sources"]["sources"][name]["url"] = url
                    if host:
                        local["mesh_sources"]["sources"][name]["host"] = host

    # Extract notification targets
    notifications = data.get("notifications", {})
    rules = notifications.get("rules", [])
    if rules:
        node_ids = set()
        recipients = set()
        for rule in rules:
            if isinstance(rule, dict):
                for nid in rule.get("node_ids", []):
                    node_ids.add(nid)
                for rcpt in rule.get("recipients", []):
                    recipients.add(rcpt)
        if node_ids:
            local["notification_targets"] = local.get("notification_targets", {})
            local["notification_targets"]["alert_node_ids"] = list(node_ids)
        if recipients:
            local["notification_targets"] = local.get("notification_targets", {})
            local["notification_targets"]["smtp_recipients"] = list(recipients)

    return local


def extract_secrets(data: dict) -> dict:
    """Extract literal secrets to .env format."""
    secrets = {}

    # LLM API key
    llm = data.get("llm", {})
    api_key = llm.get("api_key", "")
    if api_key and not is_env_var_ref(api_key):
        backend = llm.get("backend", "openai").upper()
        key_name = f"{backend}_API_KEY"
        if backend == "GOOGLE":
            key_name = "GOOGLE_API_KEY"
        secrets[key_name] = api_key
        logger.info(f"  Extracted llm.api_key → {key_name}")

    # Mesh source tokens/passwords
    for i, source in enumerate(data.get("mesh_sources", [])):
        if isinstance(source, dict):
            token = source.get("api_token", "")
            if token and not is_env_var_ref(token):
                secrets["MESHMONITOR_API_TOKEN"] = token
                logger.info(f"  Extracted mesh_sources[{i}].api_token → MESHMONITOR_API_TOKEN")

            password = source.get("password", "")
            if password and not is_env_var_ref(password):
                secrets["MQTT_PASSWORD"] = password
                logger.info(f"  Extracted mesh_sources[{i}].password → MQTT_PASSWORD")

    # Environmental API keys
    env = data.get("environmental", {})
    traffic = env.get("traffic", {})
    if traffic.get("api_key") and not is_env_var_ref(traffic["api_key"]):
        secrets["TOMTOM_API_KEY"] = traffic["api_key"]
        logger.info("  Extracted environmental.traffic.api_key → TOMTOM_API_KEY")

    firms = env.get("firms", {})
    if firms.get("map_key") and not is_env_var_ref(firms["map_key"]):
        secrets["FIRMS_MAP_KEY"] = firms["map_key"]
        logger.info("  Extracted environmental.firms.map_key → FIRMS_MAP_KEY")

    # Notification SMTP passwords
    for i, rule in enumerate(data.get("notifications", {}).get("rules", [])):
        if isinstance(rule, dict):
            smtp_pass = rule.get("smtp_password", "")
            if smtp_pass and not is_env_var_ref(smtp_pass):
                secrets["SMTP_PASSWORD"] = smtp_pass
                logger.info(f"  Extracted notifications.rules[{i}].smtp_password → SMTP_PASSWORD")

    return secrets


def strip_local_values_from_domain(data: dict) -> dict:
    """Remove local values from domain data, replacing with placeholders."""
    # Remove operator values that went to local.yaml
    # These get merged back at load time

    # Strip bot name/owner (will come from local.yaml)
    if "bot" in data:
        data["bot"].pop("name", None)
        data["bot"].pop("owner", None)

    # Strip connection tcp_host
    if "connection" in data:
        data["connection"].pop("tcp_host", None)

    # Strip knowledge hosts
    if "knowledge" in data:
        data["knowledge"].pop("qdrant_host", None)
        data["knowledge"].pop("tei_host", None)
        data["knowledge"].pop("sparse_host", None)

    # Strip meshmonitor url
    if "meshmonitor" in data:
        data["meshmonitor"].pop("url", None)

    # Strip critical_nodes (comes from local.yaml)
    if "mesh_intelligence" in data:
        data["mesh_intelligence"].pop("critical_nodes", None)

    # Strip region lat/lon (comes from local.yaml)
    if "mesh_intelligence" in data:
        for region in data["mesh_intelligence"].get("regions", []):
            if isinstance(region, dict):
                region.pop("lat", None)
                region.pop("lon", None)

    # Strip mesh source URLs (comes from local.yaml)
    for source in data.get("mesh_sources", []):
        if isinstance(source, dict):
            source.pop("url", None)
            source.pop("host", None)

    # Strip ducting lat/lon (comes from local.yaml)
    if "environmental" in data:
        ducting = data["environmental"].get("ducting", {})
        ducting.pop("latitude", None)
        ducting.pop("longitude", None)
        # Strip nws user_agent (comes from local.yaml identity.contact_email)
        nws = data["environmental"].get("nws", {})
        nws.pop("user_agent", None)

    # Strip notification node_ids and recipients (comes from local.yaml)
    if "notifications" in data:
        for rule in data["notifications"].get("rules", []):
            if isinstance(rule, dict):
                rule.pop("node_ids", None)
                rule.pop("recipients", None)
                rule.pop("from_address", None)

    return data


def replace_secrets_with_refs(data: dict, secrets: dict) -> dict:
    """Replace literal secrets with ${VAR_NAME} references."""
    # LLM API key
    if "llm" in data and data["llm"].get("api_key"):
        backend = data["llm"].get("backend", "openai").upper()
        key_name = f"{backend}_API_KEY"
        if backend == "GOOGLE":
            key_name = "GOOGLE_API_KEY"
        data["llm"]["api_key"] = f"${{{key_name}}}"

    # Mesh sources
    for source in data.get("mesh_sources", []):
        if isinstance(source, dict):
            if source.get("api_token") and not is_env_var_ref(source["api_token"]):
                source["api_token"] = "${MESHMONITOR_API_TOKEN}"
            if source.get("password") and not is_env_var_ref(source["password"]):
                source["password"] = "${MQTT_PASSWORD}"

    # Environmental
    if "environmental" in data:
        traffic = data["environmental"].get("traffic", {})
        if traffic.get("api_key") and not is_env_var_ref(traffic["api_key"]):
            traffic["api_key"] = "${TOMTOM_API_KEY}"

        firms = data["environmental"].get("firms", {})
        if firms.get("map_key") and not is_env_var_ref(firms["map_key"]):
            firms["map_key"] = "${FIRMS_MAP_KEY}"

    # Notifications
    for rule in data.get("notifications", {}).get("rules", []):
        if isinstance(rule, dict):
            if rule.get("smtp_password") and not is_env_var_ref(rule["smtp_password"]):
                rule["smtp_password"] = "${SMTP_PASSWORD}"

    return data


# =============================================================================
# FILE WRITING
# =============================================================================

def write_domain_file(path: Path, data: dict) -> None:
    """Write a domain YAML file."""
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    logger.info(f"  Wrote {path} ({path.stat().st_size} bytes)")


def write_orchestrator(path: Path, data: dict) -> None:
    """Write the orchestrator config.yaml with !include directives."""
    # Build the orchestrator content manually to preserve !include syntax
    lines = [
        "# MeshAI Configuration v0.3",
        "# Multi-file layout with !include directives",
        "",
    ]

    # Add inline sections
    for section in INLINE_SECTIONS:
        if section in data:
            section_yaml = yaml.dump({section: data[section]}, default_flow_style=False, sort_keys=False)
            lines.append(section_yaml.rstrip())
            lines.append("")

    # Add !include directives for domain files
    lines.append("# Domain files (use !include)")
    for section, target_file in SECTION_TO_FILE.items():
        if section in data and section not in ["commands"]:  # commands shares file with connection
            lines.append(f"{section}: !include {target_file}")

    content = "\n".join(lines) + "\n"

    with open(path, "w") as f:
        f.write(content)
    logger.info(f"  Wrote orchestrator {path} ({path.stat().st_size} bytes)")


def write_local_yaml(path: Path, data: dict) -> None:
    """Write local.yaml with restricted permissions."""
    header = """# LOCAL OPERATOR CONFIGURATION
# This file is gitignored - contains operator-identifying values
# Edit this file to customize for your deployment

"""
    with open(path, "w") as f:
        f.write(header)
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    path.chmod(0o600)
    logger.info(f"  Wrote {path} ({path.stat().st_size} bytes, mode 600)")


def write_env_file(path: Path, secrets: dict) -> None:
    """Write .env file with restricted permissions."""
    header = """# MeshAI Secrets
# This file is gitignored - contains API keys and passwords
# Never commit this file to version control

"""
    lines = [header]
    for key, value in sorted(secrets.items()):
        lines.append(f"{key}={value}")

    content = "\n".join(lines) + "\n"

    with open(path, "w") as f:
        f.write(content)

    path.chmod(0o600)
    logger.info(f"  Wrote {path} ({len(secrets)} secrets, mode 600)")


# =============================================================================
# MAIN MIGRATION
# =============================================================================

def run_migration() -> bool:
    """Run the full migration process."""
    logger.info("=" * 60)
    logger.info("MeshAI Config Migration v0.2 → v0.3")
    logger.info("=" * 60)

    # Pre-flight
    if not preflight_checks():
        return False

    # Backup
    if not create_backup():
        return False

    # Load original config
    logger.info(f"Loading original config: {SOURCE_FILE}")
    with open(SOURCE_FILE, "r") as f:
        original_data = yaml.safe_load(f)

    if not original_data:
        logger.error("Original config is empty or invalid!")
        return False

    # Make a working copy
    import copy
    data = copy.deepcopy(original_data)

    # Extract local values
    logger.info("Extracting operator-local values...")
    local_data = extract_local_values(data)
    local_count = sum(1 for _ in _count_values(local_data))
    logger.info(f"  Extracted {local_count} local values")

    # Extract secrets
    logger.info("Extracting secrets...")
    secrets = extract_secrets(data)
    logger.info(f"  Extracted {len(secrets)} secrets")

    # Replace secrets with env var references
    data = replace_secrets_with_refs(data, secrets)

    # Strip local values from domain data
    data = strip_local_values_from_domain(data)

    # Create directories
    logger.info("Creating directories...")
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    SECRETS_DIR.chmod(0o700)
    logger.info(f"  Created {TARGET_DIR}")
    logger.info(f"  Created {SECRETS_DIR} (mode 700)")

    # Write domain files
    logger.info("Writing domain files...")

    # Group sections by target file
    file_contents: dict[str, dict] = {}
    for section, target_file in SECTION_TO_FILE.items():
        if section in data:
            if target_file not in file_contents:
                file_contents[target_file] = {}
            # For meshtastic.yaml, wrap in section name
            if target_file == "meshtastic.yaml":
                file_contents[target_file][section] = data[section]
            else:
                # For dedicated files, the whole file IS the section content
                file_contents[target_file] = data[section]

    # Handle meshtastic.yaml specially (has both connection and commands)
    for target_file, content in file_contents.items():
        write_domain_file(TARGET_DIR / target_file, content)

    # Write orchestrator
    write_orchestrator(TARGET_DIR / "config.yaml", data)

    # Write local.yaml
    write_local_yaml(TARGET_DIR / "local.yaml", local_data)

    # Write .env
    if secrets:
        write_env_file(SECRETS_DIR / ".env", secrets)
    else:
        logger.info("  No secrets to write (all were already env var refs)")

    # Verification
    logger.info("=" * 60)
    logger.info("Verifying migration...")

    try:
        # Import here to use the newly deployed module
        sys.path.insert(0, "/app")
        from meshai.config_loader import load_config as new_load
        from meshai.config import load_config as old_load, _dataclass_to_dict

        # Load with new loader
        new_config = new_load(TARGET_DIR)

        # Load backup with old loader
        old_config = old_load(BACKUP_FILE)
        # Deep compare all fields using asdict()
        old_dict = asdict(old_config)
        new_dict = asdict(new_config)
        # Remove internal fields that will differ
        for d in [old_dict, new_dict]:
            d.pop("_config_path", None)
        
        differences = deep_compare(old_dict, new_dict)
        
        if differences:
            logger.error("Verification FAILED! Differences found:")
            for diff in differences:
                logger.error(f"  {diff}")
            return False
        logger.info("  Verification PASSED - config loads correctly")

    except Exception as e:
        logger.error(f"Verification failed with exception: {e}")
        import traceback
        traceback.print_exc()
        return False

    # Summary
    logger.info("=" * 60)
    logger.info("MIGRATION COMPLETE")
    logger.info("=" * 60)
    logger.info("")
    logger.info("Files created:")
    for f in sorted(TARGET_DIR.iterdir()):
        logger.info(f"  {f} ({f.stat().st_size} bytes)")
    if (SECRETS_DIR / ".env").exists():
        logger.info(f"  {SECRETS_DIR / '.env'} ({len(secrets)} secrets)")
    logger.info("")
    logger.info(f"Local values extracted: {local_count}")
    logger.info(f"Secrets extracted: {len(secrets)} ({', '.join(secrets.keys()) if secrets else 'none'})")
    logger.info("")
    logger.info(f"Backup at: {BACKUP_FILE}")
    logger.info("Delete the backup manually after verifying things work.")
    logger.info("")
    logger.info("ROLLBACK COMMAND (if needed):")
    logger.info(f"  rm -rf {TARGET_DIR} {SECRETS_DIR}")
    logger.info(f"  cp {BACKUP_FILE} {SOURCE_FILE}")
    logger.info("  # Then revert main.py loader wiring if changed")

    return True


def _count_values(d: dict, prefix: str = "") -> Any:
    """Generator to count leaf values in a nested dict."""
    for key, value in d.items():
        if isinstance(value, dict):
            yield from _count_values(value, f"{prefix}.{key}")
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    yield from _count_values(item, f"{prefix}.{key}[{i}]")
                else:
                    yield f"{prefix}.{key}[{i}]"
        else:
            yield f"{prefix}.{key}"


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    success = run_migration()
    sys.exit(0 if success else 1)
