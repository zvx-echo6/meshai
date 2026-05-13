"""Notification router - matches alerts to rules and delivers via channels."""

import asyncio
import logging
import time
from datetime import datetime
from typing import Optional, TYPE_CHECKING

from .channels import create_channel, NotificationChannel
from .summarizer import MessageSummarizer

if TYPE_CHECKING:
    from ..connector import MeshConnector

logger = logging.getLogger(__name__)

# Severity levels in order
SEVERITY_ORDER = ["info", "advisory", "watch", "warning", "critical", "emergency"]


class NotificationRouter:
    """Routes alerts through matching rules to notification channels."""

    def __init__(
        self,
        config,
        connector: Optional["MeshConnector"] = None,
        llm_backend=None,
        timezone: str = "America/Boise",
    ):
        self._rules: list[dict] = []
        self._quiet_enabled = getattr(config, "quiet_hours_enabled", True)
        self._quiet_start = getattr(config, "quiet_hours_start", "22:00")
        self._quiet_end = getattr(config, "quiet_hours_end", "06:00")
        self._timezone = timezone
        self._recent: dict[tuple, float] = {}  # (rule_name, category, event_key) -> last_sent_time
        self._summarizer = MessageSummarizer(llm_backend) if llm_backend else None
        self._connector = connector
        self._config = config

        # Load rules from config
        rules_config = getattr(config, "rules", [])
        for rule in rules_config:
            if hasattr(rule, "__dict__"):
                rule_dict = {k: v for k, v in rule.__dict__.items() if not k.startswith("_")}
            else:
                rule_dict = dict(rule) if isinstance(rule, dict) else {}

            # Skip disabled rules
            if not rule_dict.get("enabled", True):
                continue

            # Only load condition-triggered rules (scheduled rules handled by scheduler)
            if rule_dict.get("trigger_type", "condition") == "condition":
                self._rules.append(rule_dict)

        logger.info("Notification router initialized: %d condition rules", len(self._rules))

    def _create_channel_for_rule(self, rule: dict) -> Optional[NotificationChannel]:
        """Create a channel instance from a rule's inline delivery config.

        Returns None if delivery_type is empty or invalid.
        """
        delivery_type = rule.get("delivery_type", "")

        # Empty delivery type is valid - rule exists but doesn't deliver
        if not delivery_type:
            return None

        if delivery_type == "mesh_broadcast":
            config = {
                "type": "mesh_broadcast",
                "channel_index": rule.get("broadcast_channel", 0),
            }
        elif delivery_type == "mesh_dm":
            config = {
                "type": "mesh_dm",
                "node_ids": rule.get("node_ids", []),
            }
        elif delivery_type == "email":
            config = {
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
            config = {
                "type": "webhook",
                "url": rule.get("webhook_url", ""),
                "headers": rule.get("webhook_headers", {}),
            }
        else:
            logger.warning("Unknown delivery type '%s' in rule '%s'", delivery_type, rule.get("name"))
            return None

        try:
            return create_channel(config, self._connector)
        except Exception as e:
            logger.warning("Failed to create channel for rule '%s': %s", rule.get("name"), e)
            return None

    async def process_alert(self, alert: dict) -> bool:
        """Route an alert through matching rules.

        Returns True if alert was delivered to at least one channel.
        """
        category = alert.get("type", "")
        severity = alert.get("severity", "info")
        delivered = False

        for rule in self._rules:
            rule_name = rule.get("name", "unnamed")

            # Check category match
            rule_categories = rule.get("categories", [])
            if rule_categories and category not in rule_categories:
                continue

            # Check severity threshold
            min_severity = rule.get("min_severity", "info")
            if not self._severity_meets(severity, min_severity):
                continue

            # Check quiet hours (only if quiet hours are enabled globally)
            if self._quiet_enabled and self._in_quiet_hours():
                # Emergencies and criticals always go through
                if severity not in ("emergency", "critical"):
                    # Check if rule overrides quiet hours
                    if not rule.get("override_quiet", False):
                        logger.debug("Skipping alert (quiet hours): %s via %s", category, rule_name)
                        continue

            # Check cooldown
            cooldown = rule.get("cooldown_minutes", 10) * 60
            event_id = alert.get("event_id", alert.get("message", "")[:50])
            dedup_key = (rule_name, category, event_id)
            now = time.time()
            if dedup_key in self._recent:
                if now - self._recent[dedup_key] < cooldown:
                    logger.debug("Skipping alert (cooldown): %s via %s", category, rule_name)
                    continue
            self._recent[dedup_key] = now

            # Log rule match
            logger.info("Rule '%s' matched alert: %s (%s)", rule_name, category, severity)

            # Check if rule has delivery configured
            delivery_type = rule.get("delivery_type", "")
            if not delivery_type:
                logger.info("Rule '%s' matched but has no delivery configured", rule_name)
                continue

            # Create channel and deliver
            channel = self._create_channel_for_rule(rule)
            if not channel:
                logger.warning("Rule '%s' failed to create delivery channel", rule_name)
                continue

            try:
                # Summarize for mesh channels if over 200 chars
                delivery_alert = alert
                message = alert.get("message", "")
                if channel.channel_type in ("mesh_broadcast", "mesh_dm"):
                    if len(message) > 200:
                        if self._summarizer:
                            summary = await self._summarizer.summarize(message, max_chars=195)
                            delivery_alert = {**alert, "message": summary}
                        else:
                            delivery_alert = {**alert, "message": message[:195] + "..."}

                success = await channel.deliver(delivery_alert, rule)
                if success:
                    delivered = True
                    logger.info("Alert delivered via rule '%s': %s", rule_name, category)
            except Exception as e:
                logger.warning("Rule '%s' delivery failed: %s", rule_name, e)

        return delivered

    def _severity_meets(self, actual: str, required: str) -> bool:
        """Check if actual severity meets or exceeds required severity."""
        try:
            actual_idx = SEVERITY_ORDER.index(actual.lower())
            required_idx = SEVERITY_ORDER.index(required.lower())
            return actual_idx >= required_idx
        except ValueError:
            return True  # Unknown severity, allow through

    def _in_quiet_hours(self) -> bool:
        """Check if current time is within quiet hours."""
        if not self._quiet_enabled:
            return False

        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(self._timezone)
            now = datetime.now(tz)
            current_time = now.strftime("%H:%M")

            start = self._quiet_start
            end = self._quiet_end

            if start <= end:
                # Simple range (e.g., 01:00 to 06:00)
                return start <= current_time <= end
            else:
                # Crosses midnight (e.g., 22:00 to 06:00)
                return current_time >= start or current_time <= end
        except Exception:
            return False

    def get_rules(self) -> list[dict]:
        """Get list of configured rules."""
        return self._rules

    async def test_rule(self, rule_index: int) -> tuple[bool, str]:
        """Send a test alert through a specific rule."""
        rules_config = getattr(self._config, "rules", [])
        if rule_index < 0 or rule_index >= len(rules_config):
            return False, "Rule index out of range"

        rule = rules_config[rule_index]
        if hasattr(rule, "__dict__"):
            rule_dict = {k: v for k, v in rule.__dict__.items() if not k.startswith("_")}
        else:
            rule_dict = dict(rule)

        # Check if delivery is configured
        if not rule_dict.get("delivery_type"):
            return False, "No delivery method configured for this rule"

        channel = self._create_channel_for_rule(rule_dict)
        if not channel:
            return False, "Failed to create delivery channel"

        return await channel.test()

    async def preview_rule(self, rule_index: int) -> dict:
        """Preview what a rule would match right now.

        Returns:
            {
                "matches": bool,
                "conditions": [...],  # Current conditions that match
                "preview": str,       # Example message
            }
        """
        rules_config = getattr(self._config, "rules", [])
        if rule_index < 0 or rule_index >= len(rules_config):
            return {"matches": False, "conditions": [], "preview": "Invalid rule index"}

        rule = rules_config[rule_index]
        if hasattr(rule, "__dict__"):
            rule_dict = {k: v for k, v in rule.__dict__.items() if not k.startswith("_")}
        else:
            rule_dict = dict(rule)

        # For condition rules, show example based on categories
        if rule_dict.get("trigger_type", "condition") == "condition":
            from .categories import get_category
            categories = rule_dict.get("categories", [])

            if not categories:
                # All categories - show first example
                example = get_category("infra_offline")
                return {
                    "matches": True,
                    "conditions": ["All alert categories"],
                    "preview": example.get("example_message", "Alert notification"),
                }
            else:
                # Show example from first category
                cat_info = get_category(categories[0])
                return {
                    "matches": True,
                    "conditions": [get_category(c)["name"] for c in categories],
                    "preview": cat_info.get("example_message", f"Alert: {categories[0]}"),
                }

        # For schedule rules, generate preview report
        elif rule_dict.get("trigger_type") == "schedule":
            message_type = rule_dict.get("message_type", "mesh_health_summary")
            return {
                "matches": True,
                "conditions": [f"Scheduled: {rule_dict.get('schedule_frequency', 'daily')}"],
                "preview": f"[{message_type}] Report content would appear here",
            }

        return {"matches": False, "conditions": [], "preview": "Unknown rule type"}

    def add_mesh_subscription(
        self,
        node_id: str,
        categories: list[str],
        rule_name: Optional[str] = None,
    ) -> str:
        """Add a mesh DM subscription for a node.

        Creates a rule for the node to receive alerts.
        Returns the rule name.
        """
        if not rule_name:
            rule_name = "sub_%s" % node_id

        # Check if rule already exists
        for rule in self._rules:
            if rule.get("name") == rule_name:
                # Update existing rule
                rule["categories"] = categories if categories else []
                rule["node_ids"] = [node_id]
                return rule_name

        # Add new rule
        self._rules.append({
            "name": rule_name,
            "enabled": True,
            "trigger_type": "condition",
            "categories": categories if categories else [],  # Empty = all
            "min_severity": "warning",
            "delivery_type": "mesh_dm",
            "node_ids": [node_id],
            "cooldown_minutes": 10,
            "override_quiet": False,
        })

        return rule_name

    def remove_mesh_subscription(self, node_id: str) -> bool:
        """Remove a mesh subscription for a node."""
        rule_name = "sub_%s" % node_id
        self._rules = [r for r in self._rules if r.get("name") != rule_name]
        return True

    def get_node_subscriptions(self, node_id: str) -> list[str]:
        """Get categories a node is subscribed to."""
        rule_name = "sub_%s" % node_id
        for rule in self._rules:
            if rule.get("name") == rule_name:
                categories = rule.get("categories", [])
                return categories if categories else ["all"]
        return []

    def cleanup_recent(self, max_age: int = 3600):
        """Clean up old entries from recent alerts cache."""
        now = time.time()
        self._recent = {
            k: v for k, v in self._recent.items()
            if now - v < max_age
        }
