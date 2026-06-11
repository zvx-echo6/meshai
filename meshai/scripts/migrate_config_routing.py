#!/usr/bin/env python3
"""Migration script for MeshAI routing simplification: synthesize sinks + matrix rewrite.

This script reads existing notification toggles and rules, extracts their
inline transport configurations, and synthesizes named sinks.

Run manually: python -m meshai.scripts.migrate_config_routing [--dry-run]

The migration:
1. Backs up the config to <path>.pre-sinks.<epoch>.bak
2. For each toggle with inline transport config, synthesizes a named sink
3. For each enabled rule with inline transport config, synthesizes a named sink
4. Deduplicates identical transports into one sink
5. Writes the sinks block to the config
6. Rewrites severity_channels: channel types → sink names (Phase B)
7. Blanks matrix rows below old min_severity threshold (Phase B)
8. Removes min_severity field from toggles (Phase B)
9. Does NOT remove inline transport fields (done in a later step)

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
            first_node = str(node_ids[0]).lstrip("!")[:8]
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


def synthesize_sinks(notifications: dict) -> tuple[dict, dict]:
    """Synthesize named sinks from toggles and rules.

    Returns:
        (sinks: dict mapping sink names to sink configs,
         hash_to_name: dict mapping sink hashes to names for matrix migration)
    """
    sinks = {}
    hash_to_name = {}  # For deduplication + matrix migration lookup

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

    return sinks, hash_to_name


# ---------- Phase B: Matrix rewrite ----------

SEVERITY_RANK = {"routine": 0, "priority": 1, "immediate": 2}


def build_channel_type_to_sink_map(toggle: dict, hash_to_name: dict) -> dict[str, str]:
    """Build a mapping from channel type to sink name for a toggle.

    Uses the same hash-based lookup as synthesize_sinks to find which sink
    was created from each transport type in this toggle.

    Returns:
        {"mesh_broadcast": "mesh-ch0", "mesh_dm": "dm-abc123", ...}
    """
    mapping = {}
    sink_dicts = extract_sinks_from_toggle(toggle)
    for sink_dict in sink_dicts:
        sink_type = sink_dict["type"]
        sink_hash = compute_sink_hash(sink_dict)
        if sink_hash in hash_to_name:
            mapping[sink_type] = hash_to_name[sink_hash]
    return mapping


def migrate_toggle_matrix(
    toggle_name: str,
    toggle: dict,
    channel_to_sink: dict[str, str],
) -> tuple[dict, list[str]]:
    """Rewrite a toggle's severity_channels from channel types to sink names.

    Also blanks matrix rows below old min_severity (they never fired anyway).

    Args:
        toggle_name: Name of this toggle (for logging)
        toggle: The toggle dict
        channel_to_sink: Mapping from channel type to sink name

    Returns:
        (new_severity_channels, list_of_changes)
    """
    changes = []
    old_matrix = toggle.get("severity_channels", {})
    old_min_severity = toggle.get("min_severity", "routine")
    min_rank = SEVERITY_RANK.get(old_min_severity, 0)

    new_matrix = {}
    for severity in ["routine", "priority", "immediate"]:
        sev_rank = SEVERITY_RANK.get(severity, 0)
        old_channels = old_matrix.get(severity, [])

        # If this severity was below min_severity, blank the row
        if sev_rank < min_rank:
            if old_channels:
                changes.append(f"  {severity}: blanked (was below min_severity={old_min_severity})")
            new_matrix[severity] = []
            continue

        # Convert channel types to sink names
        new_sinks = []
        for ch_type in old_channels:
            # Skip digest pseudo-channel (it's a no-op)
            if ch_type == "digest":
                changes.append(f"  {severity}: removed 'digest' (no-op pseudo-channel)")
                continue
            sink_name = channel_to_sink.get(ch_type)
            if sink_name:
                new_sinks.append(sink_name)
            else:
                # Channel type has no corresponding sink (shouldn't happen if
                # synthesize_sinks ran first, but be defensive)
                changes.append(f"  {severity}: WARNING: no sink for channel type '{ch_type}'")
        new_matrix[severity] = new_sinks

        # Log the conversion
        if old_channels != new_sinks:
            changes.append(f"  {severity}: {old_channels} → {new_sinks}")

    return new_matrix, changes


def migrate_all_matrices(
    notifications: dict,
    hash_to_name: dict,
) -> tuple[dict[str, dict], dict[str, list[str]]]:
    """Rewrite all toggle severity_channels matrices.

    Returns:
        (toggle_name -> new_severity_channels, toggle_name -> list_of_changes)
    """
    new_matrices = {}
    all_changes = {}

    toggles = notifications.get("toggles", {})
    for toggle_name, toggle in toggles.items():
        if not isinstance(toggle, dict):
            continue

        channel_to_sink = build_channel_type_to_sink_map(toggle, hash_to_name)
        if not channel_to_sink:
            # Toggle has no inline transport config, skip matrix rewrite
            continue

        new_matrix, changes = migrate_toggle_matrix(toggle_name, toggle, channel_to_sink)
        if changes:
            new_matrices[toggle_name] = new_matrix
            all_changes[toggle_name] = changes

    return new_matrices, all_changes


def render_matrix_updates_yaml(
    toggle_updates: dict[str, dict],
    min_severity_removals: list[str],
    layout: str,
) -> str:
    """Render YAML snippet showing matrix updates.

    For dry-run display only - actual write uses yaml.safe_load/dump round-trip.
    """
    lines = ["# Matrix updates:"]
    for toggle_name, new_matrix in toggle_updates.items():
        lines.append(f"toggles.{toggle_name}.severity_channels:")
        for sev, sinks in new_matrix.items():
            lines.append(f"    {sev}: {sinks}")
    if min_severity_removals:
        lines.append("# min_severity removed from:")
        for name in min_severity_removals:
            lines.append(f"  - {name}")
    return "\n".join(lines)


def apply_matrix_updates(
    config: dict,
    layout: str,
    toggle_updates: dict[str, dict],
) -> None:
    """Apply matrix updates to config dict in-place.

    Also removes min_severity field from updated toggles.
    """
    notifications = get_notifications_block(config, layout)
    toggles = notifications.get("toggles", {})

    for toggle_name, new_matrix in toggle_updates.items():
        toggle = toggles.get(toggle_name)
        if not isinstance(toggle, dict):
            continue
        toggle["severity_channels"] = new_matrix
        # Remove min_severity - matrix is now the only gate
        if "min_severity" in toggle:
            del toggle["min_severity"]


def detect_config_layout(config: dict) -> str:
    """Detect whether config is monolithic or multi-file layout.

    Returns:
        "monolithic" if config has a notifications: wrapper key
        "multifile" if file root IS the notifications block (toggles/rules at root)
        "empty" if neither pattern matches
    """
    if "notifications" in config:
        return "monolithic"
    elif "toggles" in config or "rules" in config:
        return "multifile"
    else:
        return "empty"


def get_notifications_block(config: dict, layout: str) -> dict:
    """Extract notifications block based on detected layout."""
    if layout == "monolithic":
        return config.get("notifications", {})
    elif layout == "multifile":
        return config
    else:
        return {}


def load_notifications_config(config_path: Path) -> tuple[dict, dict, str]:
    """Load notifications config from file.

    Returns:
        (full_config_dict, notifications_dict, layout_type)
    """
    with open(config_path, "r") as f:
        config = yaml.safe_load(f) or {}

    layout = detect_config_layout(config)
    notifications = get_notifications_block(config, layout)
    return config, notifications, layout


def backup_config(config_path: Path) -> Path:
    """Create a timestamped backup of the config file."""
    epoch = int(time.time())
    backup_path = config_path.with_suffix(f".pre-sinks.{epoch}.bak")
    import shutil
    shutil.copy2(config_path, backup_path)
    return backup_path


def render_sinks_yaml(sinks: dict, layout: str) -> str:
    """Render sinks block as YAML text for appending/inserting.

    For multifile layout: sinks at root level.
    For monolithic layout: sinks indented under notifications (2-space indent).
    """
    sinks_yaml = yaml.dump({"sinks": sinks}, default_flow_style=False, sort_keys=False, allow_unicode=True)

    if layout == "monolithic":
        # Indent everything by 2 spaces to nest under notifications:
        lines = sinks_yaml.split("\n")
        indented_lines = ["  " + line if line.strip() else line for line in lines]
        return "\n".join(indented_lines)
    else:
        # Multi-file: sinks at root level
        return sinks_yaml


def find_notifications_block_end(content: str) -> int:
    """Find the line index where the notifications block ends in monolithic config.

    Returns the index after the last line of notifications block content.
    Notifications block ends when we hit a non-indented line (another top-level key)
    or end of file.
    """
    lines = content.split("\n")
    in_notifications = False
    last_notifications_line = -1

    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue

        # Check if this is the notifications: key
        if line.startswith("notifications:") and not line[0].isspace():
            in_notifications = True
            last_notifications_line = i
            continue

        if in_notifications:
            # Check if still inside notifications (indented)
            if line[0].isspace() or not line.strip():
                last_notifications_line = i
            else:
                # Hit another top-level key, notifications block ended
                break

    return last_notifications_line + 1 if last_notifications_line >= 0 else len(lines)


def verify_sinks_written(config_path: Path, sinks: dict, layout: str, original_keys: set) -> tuple[bool, str]:
    """Verify the written config is valid and sinks are accessible.

    Returns:
        (success, error_message)
    """
    try:
        with open(config_path, "r") as f:
            new_config = yaml.safe_load(f)
    except yaml.YAMLError as e:
        return False, f"YAML parse error: {e}"

    if new_config is None:
        return False, "Config parsed as empty"

    # Check sinks are at the expected path
    new_layout = detect_config_layout(new_config)
    notifications = get_notifications_block(new_config, new_layout)

    if "sinks" not in notifications:
        return False, f"Sinks not found at expected path (layout: {new_layout})"

    written_sinks = notifications["sinks"]
    if set(written_sinks.keys()) != set(sinks.keys()):
        return False, f"Sink names mismatch: expected {set(sinks.keys())}, got {set(written_sinks.keys())}"

    # Verify original top-level keys are intact
    new_top_keys = set(new_config.keys())
    missing_keys = original_keys - new_top_keys - {"sinks"}  # sinks may be new at root
    if missing_keys:
        return False, f"Original keys lost: {missing_keys}"

    return True, ""


def write_sinks_to_config(config_path: Path, sinks: dict, layout: str, backup_path: Path) -> bool:
    """Write synthesized sinks block to the config file.

    Uses append/insert strategy to preserve comments and formatting:
    - multifile: append sinks at end of file
    - monolithic: insert sinks within notifications block

    Verifies the result and restores from backup on failure.

    Returns:
        True on success, False on failure (backup restored)
    """
    import shutil

    # Read original content
    with open(config_path, "r") as f:
        original_content = f.read()

    # Parse to get original top-level keys for verification
    original_config = yaml.safe_load(original_content) or {}
    original_keys = set(original_config.keys())

    # Render sinks block
    sinks_yaml = render_sinks_yaml(sinks, layout)

    # Build new content based on layout
    if layout == "multifile":
        # Simple append at end
        if not original_content.endswith("\n"):
            original_content += "\n"
        new_content = original_content + "\n" + sinks_yaml
    else:
        # Monolithic: insert within notifications block
        lines = original_content.split("\n")
        insert_point = find_notifications_block_end(original_content)

        # Insert the indented sinks block
        sinks_lines = sinks_yaml.rstrip("\n").split("\n")
        new_lines = lines[:insert_point] + sinks_lines + lines[insert_point:]
        new_content = "\n".join(new_lines)

    # Write new content
    with open(config_path, "w") as f:
        f.write(new_content)

    # Verify the result
    success, error = verify_sinks_written(config_path, sinks, layout, original_keys)

    if not success:
        logger.error(f"Post-write verification failed: {error}")
        logger.info("Restoring from backup...")
        shutil.copy2(backup_path, config_path)
        return False

    return True


def write_full_config(config_path: Path, config: dict, layout: str, backup_path: Path) -> bool:
    """Write full config using yaml round-trip (for matrix updates).

    Unlike write_sinks_to_config which preserves comments, this does a full
    dump. Used when we need to modify fields in-place (matrix updates).

    Returns:
        True on success, False on failure (backup restored)
    """
    import shutil

    try:
        with open(config_path, "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
        return True
    except Exception as e:
        logger.error(f"Failed to write config: {e}")
        logger.info("Restoring from backup...")
        shutil.copy2(backup_path, config_path)
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Migrate MeshAI config to use named sinks + matrix rewrite"
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
    parser.add_argument(
        "--phase",
        choices=["a", "b", "all"],
        default="all",
        help="Phase A: sinks only; Phase B: matrix rewrite; all: both (default)",
    )
    args = parser.parse_args()

    config_path = args.config

    if not config_path.exists():
        logger.error(f"Config file not found: {config_path}")
        sys.exit(1)

    logger.info(f"Loading config from {config_path}")
    full_config, notifications, layout = load_notifications_config(config_path)
    logger.info(f"Detected config layout: {layout}")

    # ---------- Phase A: Synthesize sinks ----------
    sinks = {}
    hash_to_name = {}

    if args.phase in ("a", "all"):
        # Check if sinks already exist
        if notifications.get("sinks"):
            if args.phase == "a":
                logger.error("Sinks block already exists. Phase A already complete.")
                sys.exit(1)
            else:
                # Phase "all" with existing sinks: skip Phase A, proceed to B
                logger.info("Sinks already exist; skipping Phase A synthesis")
                # Build hash_to_name from existing sinks for Phase B
                existing_sinks = notifications.get("sinks", {})
                for sink_name, sink_config in existing_sinks.items():
                    if isinstance(sink_config, dict):
                        sink_hash = compute_sink_hash(sink_config)
                        hash_to_name[sink_hash] = sink_name
                sinks = existing_sinks
        else:
            logger.info("Synthesizing sinks from toggles and rules...")
            sinks, hash_to_name = synthesize_sinks(notifications)

            if not sinks:
                logger.info("No sinks to synthesize (no inline transport configs found)")
                if args.phase == "a":
                    sys.exit(0)
            else:
                logger.info(f"Synthesized {len(sinks)} sink(s):")
                for name, sink in sinks.items():
                    logger.info(f"  {name}: {sink}")

    # ---------- Phase B: Matrix rewrite ----------
    toggle_updates = {}
    all_changes = {}

    if args.phase in ("b", "all") and hash_to_name:
        logger.info("Migrating severity_channels matrices...")
        toggle_updates, all_changes = migrate_all_matrices(notifications, hash_to_name)

        if toggle_updates:
            logger.info(f"Matrix updates for {len(toggle_updates)} toggle(s):")
            for toggle_name, changes in all_changes.items():
                logger.info(f"  {toggle_name}:")
                for change in changes:
                    logger.info(f"    {change}")
        else:
            logger.info("No matrix updates needed")

    # Determine where sinks will land
    if layout == "multifile":
        sinks_path = "root-level sinks:"
    else:
        sinks_path = "notifications.sinks"

    # ---------- Dry-run output ----------
    if args.dry_run:
        logger.info("DRY RUN - no changes made")
        print(f"\n--- Config layout: {layout} ---")

        if sinks and args.phase in ("a", "all") and not notifications.get("sinks"):
            print(f"\n--- Phase A: Sinks will be written to: {sinks_path} ---")
            print(render_sinks_yaml(sinks, layout))

        if toggle_updates and args.phase in ("b", "all"):
            print("\n--- Phase B: Matrix updates ---")
            min_severity_removals = list(toggle_updates.keys())
            print(render_matrix_updates_yaml(toggle_updates, min_severity_removals, layout))

        return

    # ---------- Backup ----------
    backup_path = backup_config(config_path)
    logger.info(f"Backed up config to {backup_path}")

    # ---------- Apply changes ----------
    # For Phase A with no Phase B changes, use append strategy (preserves comments)
    # For Phase B or combined, use yaml round-trip (loses comments but handles in-place edits)

    if args.phase == "a" and sinks and not notifications.get("sinks"):
        # Phase A only: use append strategy
        success = write_sinks_to_config(config_path, sinks, layout, backup_path)
        if not success:
            logger.error("Phase A migration failed - config restored from backup")
            sys.exit(1)
        logger.info(f"Wrote sinks block to {config_path} ({sinks_path})")

    elif toggle_updates or (sinks and not notifications.get("sinks")):
        # Phase B or combined: use yaml round-trip
        # Re-parse the config to modify in-place
        with open(config_path, "r") as f:
            config_to_modify = yaml.safe_load(f) or {}

        modify_layout = detect_config_layout(config_to_modify)
        modify_notifications = get_notifications_block(config_to_modify, modify_layout)

        # Add sinks if needed
        if sinks and not modify_notifications.get("sinks"):
            modify_notifications["sinks"] = sinks
            logger.info(f"Added sinks block ({len(sinks)} sink(s))")

        # Apply matrix updates
        if toggle_updates:
            apply_matrix_updates(config_to_modify, modify_layout, toggle_updates)
            logger.info(f"Applied matrix updates to {len(toggle_updates)} toggle(s)")

        # Write the modified config
        success = write_full_config(config_path, config_to_modify, modify_layout, backup_path)
        if not success:
            logger.error("Migration failed - config restored from backup")
            sys.exit(1)

    logger.info("Migration complete!")


if __name__ == "__main__":
    main()
