"""Notification router - matches alerts to rules and delivers via channels."""

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from typing import Optional, TYPE_CHECKING

from .channels import create_channel, NotificationChannel
from .summarizer import MessageSummarizer

if TYPE_CHECKING:
    from ..connector import MeshConnector

logger = logging.getLogger(__name__)

# Severity levels in order
SEVERITY_ORDER = ["routine", "priority", "immediate"]

# State file for rule statistics
RULE_STATS_FILE = "/opt/meshai/data/rule_stats.json"


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

        # Rule statistics: {rule_name: {last_fired, last_test, fire_count}}
        self._rule_stats = self._load_rule_stats()

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

    def _load_rule_stats(self) -> dict:
        """Load rule statistics from persistent storage."""
        try:
            if os.path.exists(RULE_STATS_FILE):
                with open(RULE_STATS_FILE, "r") as f:
                    return json.load(f)
        except Exception as e:
            logger.warning("Failed to load rule stats: %s", e)
        return {}

    def _save_rule_stats(self):
        """Save rule statistics to persistent storage."""
        try:
            os.makedirs(os.path.dirname(RULE_STATS_FILE), exist_ok=True)
            with open(RULE_STATS_FILE, "w") as f:
                json.dump(self._rule_stats, f, indent=2)
        except Exception as e:
            logger.warning("Failed to save rule stats: %s", e)

    def _record_fire(self, rule_name: str):
        """Record that a rule fired."""
        if rule_name not in self._rule_stats:
            self._rule_stats[rule_name] = {"last_fired": None, "last_test": None, "fire_count": 0}
        self._rule_stats[rule_name]["last_fired"] = time.time()
        self._rule_stats[rule_name]["fire_count"] = self._rule_stats[rule_name].get("fire_count", 0) + 1
        self._save_rule_stats()

    def _record_test(self, rule_name: str):
        """Record that a rule was tested."""
        if rule_name not in self._rule_stats:
            self._rule_stats[rule_name] = {"last_fired": None, "last_test": None, "fire_count": 0}
        self._rule_stats[rule_name]["last_test"] = time.time()
        self._save_rule_stats()

    def get_rule_stats(self, rule_name: str) -> dict:
        """Get statistics for a rule."""
        return self._rule_stats.get(rule_name, {"last_fired": None, "last_test": None, "fire_count": 0})

    def _create_channel_for_rule(self, rule: dict) -> Optional[NotificationChannel]:
        """Create a channel instance from a rule's inline delivery config."""
        delivery_type = rule.get("delivery_type", "")

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
        """Route an alert through matching rules."""
        category = alert.get("type", "")
        severity = alert.get("severity", "info")
        delivered = False

        for rule in self._rules:
            rule_name = rule.get("name", "unnamed")

            rule_categories = rule.get("categories", [])
            if rule_categories and category not in rule_categories:
                continue

            min_severity = rule.get("min_severity", "info")
            if not self._severity_meets(severity, min_severity):
                continue

            if self._quiet_enabled and self._in_quiet_hours():
                if severity == "routine":
                    if not rule.get("override_quiet", False):
                        continue

            cooldown = rule.get("cooldown_minutes", 10) * 60
            event_id = alert.get("event_id", alert.get("message", "")[:50])
            dedup_key = (rule_name, category, event_id)
            now = time.time()
            if dedup_key in self._recent:
                if now - self._recent[dedup_key] < cooldown:
                    continue
            self._recent[dedup_key] = now

            logger.info("Rule '%s' matched alert: %s (%s)", rule_name, category, severity)

            delivery_type = rule.get("delivery_type", "")
            if not delivery_type:
                continue

            channel = self._create_channel_for_rule(rule)
            if not channel:
                continue

            try:
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
                    self._record_fire(rule_name)
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
            return True

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
                return start <= current_time <= end
            else:
                return current_time >= start or current_time <= end
        except Exception:
            return False

    def get_rules(self) -> list[dict]:
        """Get list of configured rules with stats."""
        rules_with_stats = []
        for rule in self._rules:
            rule_copy = dict(rule)
            stats = self.get_rule_stats(rule.get("name", ""))
            rule_copy["_stats"] = stats
            rules_with_stats.append(rule_copy)
        return rules_with_stats

    async def test_channel(self, channel_config: dict) -> dict:
        """Test a channel's connectivity.

        Args:
            channel_config: Channel configuration dict with type and settings

        Returns:
            {success, message, error, details}
        """
        try:
            channel = create_channel(channel_config, self._connector)
            return await channel.test_connection()
        except ValueError as e:
            return {
                "success": False,
                "message": "Invalid channel configuration",
                "error": str(e),
                "details": {}
            }
        except Exception as e:
            return {
                "success": False,
                "message": "Channel test failed",
                "error": str(e),
                "details": {}
            }

    def get_source_health(self, rule_categories: list, env_store=None) -> dict:
        """Get health status of data sources for a rule's categories.

        Returns:
            {
                category_id: {
                    "enabled": bool,
                    "active_events": int,
                    "source": str,
                    "status": "ok" | "disabled" | "no_data"
                }
            }
        """
        # Map categories to their data sources
        category_sources = {
            "hf_blackout": "swpc",
            "geomagnetic_storm": "swpc",
            "tropospheric_ducting": "ducting",
            "weather_warning": "nws",
            "fire_proximity": "nifc",
            "wildfire_proximity": "nifc",
            "new_ignition": "firms",
            "stream_flood_warning": "usgs",
            "stream_high_water": "usgs",
            "road_closure": "roads511",
            "traffic_congestion": "traffic",
            "avalanche_warning": "avalanche",
            "avalanche_considerable": "avalanche",
            "infra_offline": "health",
            "critical_node_down": "health",
            "battery_warning": "health",
            "battery_critical": "health",
            "battery_emergency": "health",
            "mesh_score_low": "health",
            "high_utilization": "health",
            "infra_recovery": "health",
            "packet_flood": "health",
        }

        result = {}

        for cat_id in rule_categories:
            source = category_sources.get(cat_id, "unknown")

            if source == "health":
                # Mesh health is always available
                result[cat_id] = {
                    "enabled": True,
                    "active_events": 0,  # Would need health_engine to check
                    "source": "mesh_health",
                    "status": "ok"
                }
            elif env_store is None:
                result[cat_id] = {
                    "enabled": False,
                    "active_events": 0,
                    "source": source,
                    "status": "disabled"
                }
            else:
                # Check if source has an adapter
                adapters = getattr(env_store, '_adapters', {})
                if source in adapters:
                    events = env_store.get_active(source=source)
                    result[cat_id] = {
                        "enabled": True,
                        "active_events": len(events) if events else 0,
                        "source": source,
                        "status": "ok"
                    }
                else:
                    result[cat_id] = {
                        "enabled": False,
                        "active_events": 0,
                        "source": source,
                        "status": "disabled"
                    }

        return result

    async def test_rule_with_conditions(
        self,
        rule_index: int,
        alert_engine=None,
        env_store=None,
        health_engine=None,
        send: bool = False,
        action: str = "preview",
    ) -> dict:
        """Test a rule against current conditions with live data.

        Args:
            rule_index: Index of the rule to test
            alert_engine: AlertEngine instance for pending alerts
            env_store: EnvStore instance for environmental events
            health_engine: MeshHealthEngine for mesh status
            send: Legacy param - use action instead
            action: "preview", "send_test", "send_status", "send_live"
        """
        from .categories import get_category

        rules_config = getattr(self._config, "rules", [])
        if rule_index < 0 or rule_index >= len(rules_config):
            return {
                "conditions_matched": 0,
                "preview_messages": [],
                "is_example": False,
                "delivered": False,
                "delivery_method": "",
                "delivery_result": "Rule index out of range",
            }

        rule = rules_config[rule_index]
        if hasattr(rule, "__dict__"):
            rule_dict = {k: v for k, v in rule.__dict__.items() if not k.startswith("_")}
        else:
            rule_dict = dict(rule)

        rule_name = rule_dict.get("name", f"Rule {rule_index}")
        rule_categories = rule_dict.get("categories", [])
        min_severity = rule_dict.get("min_severity", "info")
        delivery_type = rule_dict.get("delivery_type", "")

        # Legacy support
        if send and action == "preview":
            action = "send_test"

        # ============================================================
        # SECTION 1: Collect LIVE DATA for rule's categories
        # ============================================================
        live_data_lines = []
        feeds_not_enabled = []
        category_sources = {
            "hf_blackout": "swpc", "geomagnetic_storm": "swpc",
            "tropospheric_ducting": "ducting",
            "weather_warning": "nws",
            "fire_proximity": "nifc", "wildfire_proximity": "nifc", "new_ignition": "firms",
            "stream_flood_warning": "usgs", "stream_high_water": "usgs",
            "road_closure": "roads511", "traffic_congestion": "traffic",
            "avalanche_warning": "avalanche", "avalanche_considerable": "avalanche",
            "infra_offline": "health", "critical_node_down": "health",
            "battery_warning": "health", "battery_critical": "health",
            "mesh_score_low": "health", "high_utilization": "health",
        }

        sources_needed = set()
        for cat in rule_categories if rule_categories else []:
            if cat in category_sources:
                sources_needed.add(category_sources[cat])

        # Check which sources are available
        if env_store:
            adapters = getattr(env_store, '_adapters', {})

            if "swpc" in sources_needed:
                if "swpc" in adapters and hasattr(env_store, 'get_swpc_status'):
                    swpc = env_store.get_swpc_status()
                    if swpc:
                        kp = swpc.get("kp_current", "?")
                        sfi = swpc.get("sfi", "?")
                        r = swpc.get("r_scale", 0)
                        s = swpc.get("s_scale", 0)
                        g = swpc.get("g_scale", 0)
                        live_data_lines.append(f"RF: SFI {sfi}, Kp {kp}, R{r}/S{s}/G{g}")
                else:
                    feeds_not_enabled.append("SWPC")

            if "ducting" in sources_needed:
                if "ducting" in adapters and hasattr(env_store, 'get_ducting_status'):
                    ducting = env_store.get_ducting_status()
                    if ducting:
                        condition = ducting.get("condition", "unknown")
                        gradient = ducting.get("min_gradient", "?")
                        live_data_lines.append(f"Tropo: {condition}, dM/dz {gradient}")
                else:
                    feeds_not_enabled.append("Ducting")

            if "nws" in sources_needed:
                if "nws" in adapters:
                    nws = env_store.get_active(source="nws")
                    if nws:
                        live_data_lines.append(f"NWS: {len(nws)} active alert(s)")
                        for a in nws[:2]:
                            headline = a.get('headline', a.get('message', 'Alert'))[:60]
                            live_data_lines.append(f"  - {headline}")
                    else:
                        live_data_lines.append("NWS: No active alerts")
                else:
                    feeds_not_enabled.append("NWS")

            if "nifc" in sources_needed:
                if "nifc" in adapters:
                    fires = env_store.get_active(source="nifc")
                    if fires:
                        live_data_lines.append(f"Fires: {len(fires)} active")
                    else:
                        live_data_lines.append("Fires: None active")
                else:
                    feeds_not_enabled.append("NIFC")

            if "firms" in sources_needed:
                if "firms" in adapters:
                    hotspots = env_store.get_active(source="firms")
                    if hotspots:
                        live_data_lines.append(f"Hotspots: {len(hotspots)} detected")
                    else:
                        live_data_lines.append("Hotspots: None detected")
                else:
                    feeds_not_enabled.append("FIRMS")

            if "usgs" in sources_needed:
                if "usgs" in adapters:
                    streams = env_store.get_active(source="usgs")
                    if streams:
                        live_data_lines.append(f"Streams: {len(streams)} gauge(s) reporting")
                    else:
                        live_data_lines.append("Streams: No alerts")
                else:
                    feeds_not_enabled.append("USGS")

            if "traffic" in sources_needed:
                if "traffic" in adapters:
                    traffic = env_store.get_active(source="traffic")
                    if traffic:
                        live_data_lines.append(f"Traffic: {len(traffic)} corridor(s)")
                    else:
                        live_data_lines.append("Traffic: Normal")
                else:
                    feeds_not_enabled.append("Traffic")

            if "roads511" in sources_needed:
                if "roads511" in adapters:
                    roads = env_store.get_active(source="roads511")
                    if roads:
                        live_data_lines.append(f"Roads: {len(roads)} event(s)")
                    else:
                        live_data_lines.append("Roads: No closures")
                else:
                    feeds_not_enabled.append("511 Roads")
        elif sources_needed - {"health"}:
            feeds_not_enabled.append("Environmental feeds")

        if health_engine and "health" in sources_needed:
            mesh_health = getattr(health_engine, 'mesh_health', None)
            if mesh_health:
                score = mesh_health.score
                live_data_lines.append(f"Mesh: {score.composite:.0f}/100, {score.infra_online}/{score.infra_total} infra")

        # Add warning if feeds not enabled
        if feeds_not_enabled:
            live_data_lines.append(f"[!] Not enabled: {', '.join(feeds_not_enabled)}")

        # ============================================================
        # SECTION 2: Check for MATCHING and NEAR-MISS events
        # ============================================================
        matching_alerts = []
        below_threshold = []
        all_events = []

        if alert_engine and hasattr(alert_engine, "get_pending_alerts"):
            try:
                for alert in alert_engine.get_pending_alerts():
                    all_events.append({
                        "type": alert.get("type", ""),
                        "severity": alert.get("severity", "info"),
                        "message": alert.get("message", ""),
                        "headline": alert.get("message", "")[:80],
                    })
            except Exception:
                pass

        if env_store and hasattr(env_store, "get_active"):
            try:
                for event in env_store.get_active():
                    all_events.append({
                        "type": event.get("type", event.get("category", "")),
                        "severity": event.get("severity", "info"),
                        "message": event.get("message", event.get("headline", str(event))),
                        "headline": event.get("headline", event.get("message", "Event"))[:80],
                    })
            except Exception:
                pass

        for event in all_events:
            event_type = event["type"]
            severity = event["severity"]

            category_match = not rule_categories
            if not category_match:
                for cat in rule_categories:
                    if event_type.startswith(cat.rstrip("_")) or cat in event_type or event_type == cat:
                        category_match = True
                        break

            if category_match:
                if self._severity_meets(severity, min_severity):
                    matching_alerts.append(event)
                else:
                    below_threshold.append(event)

        # ============================================================
        # SECTION 3: Build response
        # ============================================================
        preview_messages = []
        is_example = False
        below_threshold_summary = ""
        below_threshold_events = []
        suggestion = ""

        if matching_alerts:
            for alert in matching_alerts[:5]:
                msg = alert.get("message", "")
                if len(msg) > 200 and delivery_type in ("mesh_broadcast", "mesh_dm"):
                    msg = msg[:195] + "..."
                preview_messages.append(msg)
        else:
            is_example = True
            if below_threshold:
                severity_counts = {}
                for evt in below_threshold:
                    sev = evt["severity"]
                    severity_counts[sev] = severity_counts.get(sev, 0) + 1
                parts = [f"{count} at '{sev}'" for sev, count in severity_counts.items()]
                below_threshold_summary = f"{len(below_threshold)} event(s) filtered by severity: {', '.join(parts)}. Rule requires '{min_severity}' or higher."
                suggestion = f"Lower severity threshold to '{list(severity_counts.keys())[0]}' to match these events"
                below_threshold_events = [{"headline": e["headline"], "severity": e["severity"]} for e in below_threshold[:5]]

            if rule_categories:
                for cat_id in rule_categories[:3]:
                    cat_info = get_category(cat_id)
                    preview_messages.append(f"[EXAMPLE] {cat_info.get('example_message', f'Alert: {cat_id}')}")
            else:
                cat_info = get_category("infra_offline")
                preview_messages.append(f"[EXAMPLE] {cat_info.get('example_message', 'Alert notification')}")

        # Get source health
        source_health = self.get_source_health(rule_categories, env_store)

        # ============================================================
        # SECTION 4: Handle delivery actions
        # ============================================================
        delivered = False
        delivery_result = "Preview only"
        delivery_error = ""

        if action != "preview":
            if not delivery_type:
                delivery_result = "No delivery method configured"
                delivery_error = "Configure a delivery method to send test messages"
            else:
                channel = self._create_channel_for_rule(rule_dict)
                if channel:
                    try:
                        if action == "send_status" and live_data_lines:
                            # Filter out the warning line for status message
                            data_lines = [l for l in live_data_lines if not l.startswith("[!]")]
                            status_msg = "[STATUS] " + " | ".join(data_lines[:4])
                            if len(status_msg) > 200:
                                status_msg = status_msg[:195] + "..."
                            success, result = await channel.deliver_test(status_msg)
                            delivered = success
                            delivery_result = result if success else f"Failed: {result}"
                            if not success:
                                delivery_error = result

                        elif action == "send_live" and matching_alerts:
                            live_msg = f"[LIVE TEST] {matching_alerts[0].get('message', '')}"
                            if len(live_msg) > 200:
                                live_msg = live_msg[:195] + "..."
                            success, result = await channel.deliver_test(live_msg)
                            delivered = success
                            delivery_result = result if success else f"Failed: {result}"
                            if not success:
                                delivery_error = result

                        elif action == "send_test":
                            if preview_messages:
                                test_msg = preview_messages[0]
                                if test_msg.startswith("[EXAMPLE]"):
                                    test_msg = test_msg.replace("[EXAMPLE]", "[TEST]")
                                elif not test_msg.startswith("["):
                                    test_msg = f"[TEST] {test_msg}"
                            else:
                                test_msg = "[TEST] MeshAI notification test"
                            success, result = await channel.deliver_test(test_msg)
                            delivered = success
                            delivery_result = result if success else f"Failed: {result}"
                            if not success:
                                delivery_error = result

                        # Record test
                        if action != "preview":
                            self._record_test(rule_name)

                    except Exception as e:
                        delivery_result = f"Delivery error"
                        delivery_error = str(e)

        # Get rule stats
        stats = self.get_rule_stats(rule_name)

        return {
            "live_data_summary": live_data_lines,
            "conditions_matched": len(matching_alerts),
            "preview_messages": preview_messages,
            "is_example": is_example,
            "conditions_below_threshold": len(below_threshold),
            "below_threshold_summary": below_threshold_summary,
            "below_threshold_events": below_threshold_events,
            "suggestion": suggestion,
            "delivered": delivered,
            "delivery_method": delivery_type,
            "delivery_result": delivery_result,
            "delivery_error": delivery_error,
            "can_send_live": len(matching_alerts) > 0,
            "source_health": source_health,
            "rule_stats": stats,
        }

    async def test_rule(self, rule_index: int) -> tuple[bool, str]:
        """Send a test alert through a specific rule (legacy method)."""
        result = await self.test_rule_with_conditions(rule_index, action="send_test")
        return result.get("delivered", False), result.get("delivery_result", "Unknown")

    async def preview_rule(self, rule_index: int) -> dict:
        """Preview what a rule would match right now."""
        rules_config = getattr(self._config, "rules", [])
        if rule_index < 0 or rule_index >= len(rules_config):
            return {"matches": False, "conditions": [], "preview": "Invalid rule index"}

        rule = rules_config[rule_index]
        if hasattr(rule, "__dict__"):
            rule_dict = {k: v for k, v in rule.__dict__.items() if not k.startswith("_")}
        else:
            rule_dict = dict(rule)

        if rule_dict.get("trigger_type", "condition") == "condition":
            from .categories import get_category
            categories = rule_dict.get("categories", [])

            if not categories:
                example = get_category("infra_offline")
                return {
                    "matches": True,
                    "conditions": ["All alert categories"],
                    "preview": example.get("example_message", "Alert notification"),
                }
            else:
                cat_info = get_category(categories[0])
                return {
                    "matches": True,
                    "conditions": [get_category(c)["name"] for c in categories],
                    "preview": cat_info.get("example_message", f"Alert: {categories[0]}"),
                }

        elif rule_dict.get("trigger_type") == "schedule":
            message_type = rule_dict.get("message_type", "mesh_health_summary")
            return {
                "matches": True,
                "conditions": [f"Scheduled: {rule_dict.get('schedule_frequency', 'daily')}"],
                "preview": f"[{message_type}] Report content would appear here",
            }

        return {"matches": False, "conditions": [], "preview": "Unknown rule type"}

    def add_mesh_subscription(self, node_id: str, categories: list[str], rule_name: Optional[str] = None) -> str:
        """Add a mesh DM subscription for a node."""
        if not rule_name:
            rule_name = "sub_%s" % node_id

        for rule in self._rules:
            if rule.get("name") == rule_name:
                rule["categories"] = categories if categories else []
                rule["node_ids"] = [node_id]
                return rule_name

        self._rules.append({
            "name": rule_name,
            "enabled": True,
            "trigger_type": "condition",
            "categories": categories if categories else [],
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
        self._recent = {k: v for k, v in self._recent.items() if now - v < max_age}
