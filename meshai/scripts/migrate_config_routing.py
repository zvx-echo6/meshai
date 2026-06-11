#!/usr/bin/env python3
"""Migration script for MeshAI routing simplification: synthesize sinks.

This script reads existing notification toggles and rules, extracts their
inline transport configurations, and synthesizes named sinks.

Run manually: python -m meshai.scripts.migrate_config_routing [--dry-run]

The migration:
1. Backs up the config to <path>.pre-sinks.<epoch>.bak
2. For each toggle with inline transport config, synthesizes a named sink
3. For each enabled rule with inline transport config, synthesizes a named sink
4. Deduplicates identical transports into one sink
5. Writes the sinks block to the config
6. Does NOT remove inline fields (done in a later step)

Idempotent: refuses to run if a sinks block already exists.
"""

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import yaml

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def compute_sink_hash(sink_dict: dict) -> str:
    """Compute a stable hash of sink config for deduplication."""
    # Sort keys for stable comparison
    canonical = json.dumps(sink_dict, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:8]


def generate_sink_name(sink_type: str, sink_dict: dict) -> str:
    """Generate a human-readable sink name from its config."""
    if sink_type == "mesh_broadcast":
        channel = sink_dict.get("channel", 0)
        return f"mesh-ch{channel}"
    elif sink_type == "mesh_dm":
        node_ids = sink_dict.get("node_ids", [])
        if node_ids:
            first_node = node_ids[0].lstrip("!")[:8]
            return f"dm-{first_node}"
        return "dm-unknown"
    elif sink_type == "email":
        host = sink_dict.get("smtp_host", "")
        if host:
            # Extract domain
            return f"email-{host.split('.')[0]}"
        return "email-unknown"
    elif sink_type == "webhook":
        url = sink_dict.get("webhook_url", "")
        if url:
            parsed = urlparse(url)
            return f"webhook-{parsed.netloc.split('.')[0]}"
        return "webhook-unknown"
    return f"sink-{sink_type}"


def extract_sinks_from_toggle(toggle: dict) -> list[dict]:
    """Extract ALL sink configs from a NotificationToggle's inline fields.
    
    Returns a list of sink dicts, one per configured transport type.
    No precedence — every transport with non-empty config is extracted.
    """
    sinks = []

    # Check for mesh_broadcast (broadcast_channel field)
    broadcast_channel = toggle.get("broadcast_channel")
    if broadcast_channel is not None:
        sinks.append({
            "type": "mesh_broadcast",
            "channel": int(broadcast_channel),
        })

    # Check for mesh_dm (node_ids field)
    node_ids = toggle.get("node_ids", [])
    if node_ids:
        sinks.append({
            "type": "mesh_dm",
            "node_ids": node_ids,
        })

    # Check for email (smtp_host field)
    smtp_host = toggle.get("smtp_host", "")
    if smtp_host:
        sinks.append({
            "type": "email",
            "smtp_host": smtp_host,
            "smtp_port": toggle.get("smtp_port", 587),
            "smtp_user": toggle.get("smtp_user", ""),
            "smtp_password": toggle.get("smtp_password", ""),
            "smtp_tls": toggle.get("smtp_tls", True),
            "from_address": toggle.get("from_address", ""),
            "recipients": toggle.get("recipients", []),
        })

    # Check for webhook (webhook_url field)
    webhook_url = toggle.get("webhook_url", "")
    if webhook_url:
        sinks.append({
            "type": "webhook",
            "webhook_url": webhook_url,
            "webhook_headers": toggle.get("webhook_headers", {}),
        })

    return sinks


def extract_sink_from_toggle(toggle: dict) -> Optional[dict]:
    """Legacy wrapper - returns first sink or None. Use extract_sinks_from_toggle instead."""
    sinks = extract_sinks_from_toggle(toggle)
    return sinks[0] if sinks else None


def extract_sink_from_rule(rule: dict) -> Optional[dict]:
    """Extract sink config from a NotificationRuleConfig's inline fields."""
    delivery_type = rule.get("delivery_type", "")

    if delivery_type == "mesh_broadcast":
        return {
            "type": "mesh_broadcast",
            "channel": rule.get("broadcast_channel", 0),
        }
    elif delivery_type == "mesh_dm":
        return {
            "type": "mesh_dm",
            "node_ids": rule.get("node_ids", []),
        }
    elif delivery_type == "email":
        return {
            "type": "email",
            "smtp_host": rule.get("smtp_host", ""),
            "smtp_port": rule.get("smtp_port", 587),
            "smtp_user": rule.get("smtp_user", ""),
            "smtp_password": rule.get("smtp_password", ""),
            "smtp_tls": rule.get("smtp_tls", True),
            "from_address": rule.get("from_address", ""),
            "recipients": rule.get("recipients", []),
        }
    elif delivery_type == "webhook":
        return {
            "type": "webhook",
            "webhook_url": rule.get("webhook_url", ""),
            "webhook_headers": rule.get("webhook_headers", {}),
        }

    return None


def synthesize_sinks(notifications: dict) -> dict:
    """Synthesize named sinks from toggles and rules.

    Returns:
        dict mapping sink names to sink configs
    """
    sinks = {}
    hash_to_name = {}  # For deduplication

    # Process toggles - extract ALL configured transports per toggle
    toggles = notifications.get("toggles", {})
    for toggle_name, toggle in toggles.items():
        if not isinstance(toggle, dict):
            continue

        sink_dicts = extract_sinks_from_toggle(toggle)
        if not sink_dicts:
            continue

        for sink_dict in sink_dicts:
            sink_hash = compute_sink_hash(sink_dict)
            if sink_hash in hash_to_name:
                logger.info(f"  Toggle '{toggle_name}' reuses existing sink '{hash_to_name[sink_hash]}'")
                continue

            sink_name = generate_sink_name(sink_dict["type"], sink_dict)
            # Handle name collisions
            base_name = sink_name
            counter = 2
            while sink_name in sinks:
                sink_name = f"{base_name}-{counter}"
                counter += 1

            sinks[sink_name] = sink_dict
            hash_to_name[sink_hash] = sink_name
            logger.info(f"  Toggle '{toggle_name}' → sink '{sink_name}'")

    # Process rules
    rules = notifications.get("rules", [])
    for i, rule in enumerate(rules):
        if not isinstance(rule, dict):
            continue

        # Only process enabled rules
        if not rule.get("enabled", True):
            continue

        sink_dict = extract_sink_from_rule(rule)
        if not sink_dict:
            continue

        sink_hash = compute_sink_hash(sink_dict)
        if sink_hash in hash_to_name:
            rule_name = rule.get("name", f"rule-{i}")
            logger.info(f"  Rule '{rule_name}' reuses existing sink '{hash_to_name[sink_hash]}'")
            continue

        sink_name = generate_sink_name(sink_dict["type"], sink_dict)
        # Handle name collisions
        base_name = sink_name
        counter = 2
        while sink_name in sinks:
            sink_name = f"{base_name}-{counter}"
            counter += 1

        sinks[sink_name] = sink_dict
        hash_to_name[sink_hash] = sink_name
        rule_name = rule.get("name", f"rule-{i}")
        logger.info(f"  Rule '{rule_name}' → sink '{sink_name}'")

    return sinks


def load_notifications_config(config_path: Path) -> tuple[dict, dict]:
    """Load notifications config from file.

    Returns:
        (full_config_dict, notifications_dict)
    """
    with open(config_path, "r") as f:
        config = yaml.safe_load(f) or {}

    # Handle both monolithic (has notifications: key) and multi-file (IS notifications) layouts
    if "notifications" in config:
        notifications = config["notifications"]
    elif "toggles" in config or "rules" in config:
        notifications = config
    else:
        notifications = {}
    return config, notifications


def backup_config(config_path: Path) -> Path:
    """Create a timestamped backup of the config file."""
    epoch = int(time.time())
    backup_path = config_path.with_suffix(f".pre-sinks.{epoch}.bak")
    import shutil
    shutil.copy2(config_path, backup_path)
    return backup_path


def write_sinks_to_config(config_path: Path, sinks: dict):
    """Write synthesized sinks block to the config file."""
    with open(config_path, "r") as f:
        content = f.read()

    # Parse YAML to find where to insert
    # Insert sinks after notifications.rules or at end of notifications block
    # This is a simplified approach - in production you'd want proper YAML manipulation

    # For now, read as dict, add sinks, write back
    config = yaml.safe_load(content) or {}

    if "notifications" not in config:
        config["notifications"] = {}

    config["notifications"]["sinks"] = sinks

    with open(config_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


def main():
    parser = argparse.ArgumentParser(
        description="Migrate MeshAI config to use named sinks"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("/data/config/notifications.yaml"),
        help="Path to notifications config file",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes",
    )
    args = parser.parse_args()

    config_path = args.config

    if not config_path.exists():
        logger.error(f"Config file not found: {config_path}")
        sys.exit(1)

    logger.info(f"Loading config from {config_path}")
    full_config, notifications = load_notifications_config(config_path)

    # Check if sinks already exist
    if notifications.get("sinks"):
        logger.error("Sinks block already exists in config. Migration already complete.")
        logger.info("Existing sinks: %s", list(notifications["sinks"].keys()))
        sys.exit(1)

    # Synthesize sinks
    logger.info("Synthesizing sinks from toggles and rules...")
    sinks = synthesize_sinks(notifications)

    if not sinks:
        logger.info("No sinks to synthesize (no inline transport configs found)")
        sys.exit(0)

    logger.info(f"Synthesized {len(sinks)} sink(s):")
    for name, sink in sinks.items():
        logger.info(f"  {name}: {sink}")

    if args.dry_run:
        logger.info("DRY RUN - no changes made")
        print("\n--- Would write sinks block: ---")
        print(yaml.dump({"sinks": sinks}, default_flow_style=False))
        return

    # Backup and write
    backup_path = backup_config(config_path)
    logger.info(f"Backed up config to {backup_path}")

    write_sinks_to_config(config_path, sinks)
    logger.info(f"Wrote sinks block to {config_path}")

    logger.info("Migration complete!")


if __name__ == "__main__":
    main()
