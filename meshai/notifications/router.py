"""Notification router - matches alerts to rules and delivers via channels."""

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
        self._channels: dict[str, NotificationChannel] = {}
        self._rules: list[dict] = []
        self._quiet_start = getattr(config, "quiet_hours_start", "22:00")
        self._quiet_end = getattr(config, "quiet_hours_end", "06:00")
        self._timezone = timezone
        self._dedup_window = getattr(config, "dedup_seconds", 600)
        self._recent: dict[tuple, float] = {}  # (category, event_key) -> last_sent_time
        self._summarizer = MessageSummarizer(llm_backend) if llm_backend else None
        self._connector = connector

        # Create channel instances from config
        channels_config = getattr(config, "channels", [])
        for ch_config in channels_config:
            if hasattr(ch_config, "__dict__"):
                ch_dict = {k: v for k, v in ch_config.__dict__.items() if not k.startswith("_")}
            else:
                ch_dict = ch_config

            if not ch_dict.get("enabled", True):
                continue

            channel_id = ch_dict.get("id", "")
            if not channel_id:
                continue

            try:
                channel = create_channel(ch_dict, connector)
                self._channels[channel_id] = channel
                logger.debug("Created notification channel: %s (%s)", channel_id, ch_dict.get("type"))
            except Exception as e:
                logger.warning("Failed to create channel %s: %s", channel_id, e)

        # Load rules
        rules_config = getattr(config, "rules", [])
        for rule in rules_config:
            if hasattr(rule, "__dict__"):
                rule_dict = {k: v for k, v in rule.__dict__.items() if not k.startswith("_")}
            else:
                rule_dict = rule
            self._rules.append(rule_dict)

        logger.info(
            "Notification router initialized: %d channels, %d rules",
            len(self._channels),
            len(self._rules),
        )

    async def process_alert(self, alert: dict) -> bool:
        """Route an alert through matching rules to channels.

        Returns True if alert was delivered to at least one channel.
        """
        category = alert.get("type", "")
        severity = alert.get("severity", "info")
        delivered = False

        for rule in self._rules:
            # Check category match
            rule_categories = rule.get("categories", [])
            if rule_categories and category not in rule_categories:
                continue

            # Check severity threshold
            min_severity = rule.get("min_severity", "info")
            if not self._severity_meets(severity, min_severity):
                continue

            # Check quiet hours (emergencies and criticals override)
            if self._in_quiet_hours() and severity not in ("emergency", "critical"):
                if not rule.get("override_quiet", False):
                    continue

            # Check dedup
            event_id = alert.get("event_id", alert.get("message", "")[:50])
            dedup_key = (category, event_id)
            now = time.time()
            if dedup_key in self._recent:
                if now - self._recent[dedup_key] < self._dedup_window:
                    logger.debug("Skipping duplicate alert: %s", category)
                    continue
            self._recent[dedup_key] = now

            # Deliver to each channel in the rule
            channel_ids = rule.get("channel_ids", [])
            for channel_id in channel_ids:
                channel = self._channels.get(channel_id)
                if not channel:
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
                        logger.info(
                            "Alert delivered via %s: %s",
                            channel_id,
                            category,
                        )
                except Exception as e:
                    logger.warning("Channel %s delivery failed: %s", channel_id, e)

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

    def get_channels(self) -> list[dict]:
        """Get list of configured channels."""
        return [
            {"id": ch_id, "type": ch.channel_type}
            for ch_id, ch in self._channels.items()
        ]

    def get_rules(self) -> list[dict]:
        """Get list of configured rules."""
        return self._rules

    async def test_channel(self, channel_id: str) -> tuple[bool, str]:
        """Send a test alert to a specific channel."""
        channel = self._channels.get(channel_id)
        if not channel:
            return False, "Channel not found: %s" % channel_id
        return await channel.test()

    def add_mesh_subscription(
        self,
        node_id: str,
        categories: list[str],
        rule_name: Optional[str] = None,
    ) -> str:
        """Add a mesh DM subscription for a node.

        Creates a channel and rule for the node to receive alerts.
        Returns the rule name.
        """
        # Create channel ID
        channel_id = "mesh_dm_%s" % node_id

        # Create channel if it doesn't exist
        if channel_id not in self._channels:
            from .channels import MeshDMChannel
            channel = MeshDMChannel(
                connector=self._connector,
                node_ids=[node_id],
            )
            self._channels[channel_id] = channel

        # Create rule
        if not rule_name:
            rule_name = "sub_%s" % node_id

        # Check if rule already exists
        for rule in self._rules:
            if rule.get("name") == rule_name:
                # Update existing rule
                rule["categories"] = categories if categories else []
                rule["channel_ids"] = [channel_id]
                return rule_name

        # Add new rule
        self._rules.append({
            "name": rule_name,
            "categories": categories if categories else [],  # Empty = all
            "min_severity": "warning",
            "channel_ids": [channel_id],
            "override_quiet": False,
        })

        return rule_name

    def remove_mesh_subscription(self, node_id: str) -> bool:
        """Remove a mesh subscription for a node."""
        channel_id = "mesh_dm_%s" % node_id
        rule_name = "sub_%s" % node_id

        # Remove channel
        if channel_id in self._channels:
            del self._channels[channel_id]

        # Remove rule
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
