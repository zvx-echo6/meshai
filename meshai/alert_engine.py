"""Alert engine — detects mesh state changes and dispatches alerts."""

import logging
import time
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .mesh_health import MeshHealthEngine
    from .mesh_reporter import MeshReporter
    from .subscriptions import SubscriptionManager

logger = logging.getLogger(__name__)


class AlertEngine:
    """Detects mesh state changes and dispatches alerts."""

    def __init__(
        self,
        health_engine: "MeshHealthEngine",
        reporter: "MeshReporter",
        subscription_manager: "SubscriptionManager",
        critical_nodes: list[str] = None,
        alert_cooldown_minutes: int = 30,
    ):
        self._health = health_engine
        self._reporter = reporter
        self._subs = subscription_manager
        self._critical_nodes = set(n.upper() for n in (critical_nodes or []))
        self._cooldown_seconds = alert_cooldown_minutes * 60

        # Previous state snapshot for change detection
        self._prev_infra_online: dict[int, bool] = {}  # node_num -> was_online
        self._prev_battery: dict[int, float] = {}  # node_num -> battery_percent

        # Cooldown tracker: condition_key -> last_alert_time
        self._cooldowns: dict[str, float] = {}

        # Queued alerts for delivery
        self._pending_alerts: list[dict] = []

    def check(self) -> list[dict]:
        """Compare current health to previous state. Returns list of alert dicts.

        Each alert dict: {
            "type": "infra_offline" | "infra_recovery" | "battery_critical" | "critical_node_down",
            "node_name": str,
            "node_short": str,
            "node_num": int,
            "region": str,
            "message": str,
            "scope_type": "mesh" | "region" | "node",
            "scope_value": str,
            "is_critical": bool,
        }
        """
        health = self._health.mesh_health
        if not health:
            return []

        now = time.time()
        alerts = []

        for node in health.nodes.values():
            if not node.is_infrastructure:
                continue

            node_num = node.node_num
            name = node.long_name or node.short_name or str(node_num)
            short = node.short_name or str(node_num)
            region = node.region or "Unknown"
            is_critical = short.upper() in self._critical_nodes

            # --- Infrastructure offline detection ---
            was_online = self._prev_infra_online.get(node_num)
            is_online = node.is_online

            if was_online is not None:  # Skip first run (no previous state)
                if was_online and not is_online:
                    # Node went OFFLINE
                    alert_type = "critical_node_down" if is_critical else "infra_offline"
                    cooldown_key = f"offline_{node_num}"

                    if self._check_cooldown(cooldown_key, now):
                        emoji = "\U0001F6A8" if is_critical else "\u274C"  # 🚨 or ❌
                        region_display = self._get_region_display(region)

                        alerts.append({
                            "type": alert_type,
                            "node_name": name,
                            "node_short": short,
                            "node_num": node_num,
                            "region": region,
                            "message": f"{emoji} {name} went offline in {region_display}.",
                            "scope_type": "region",
                            "scope_value": region,
                            "is_critical": is_critical,
                        })
                        self._cooldowns[cooldown_key] = now

                elif not was_online and is_online:
                    # Node came BACK ONLINE
                    cooldown_key = f"recovery_{node_num}"

                    if self._check_cooldown(cooldown_key, now):
                        region_display = self._get_region_display(region)

                        alerts.append({
                            "type": "infra_recovery",
                            "node_name": name,
                            "node_short": short,
                            "node_num": node_num,
                            "region": region,
                            "message": f"\u2705 {name} is back online in {region_display}.",  # ✅
                            "scope_type": "region",
                            "scope_value": region,
                            "is_critical": is_critical,
                        })
                        self._cooldowns[cooldown_key] = now

            # --- Battery critical detection (infra only) ---
            if node.battery_percent is not None and 0 < node.battery_percent <= 100:
                prev_bat = self._prev_battery.get(node_num)
                current_bat = node.battery_percent

                if current_bat < 10 and (prev_bat is None or prev_bat >= 10):
                    # Battery just dropped below 10%
                    cooldown_key = f"battery_{node_num}"

                    if self._check_cooldown(cooldown_key, now):
                        region_display = self._get_region_display(region)

                        alerts.append({
                            "type": "battery_critical",
                            "node_name": name,
                            "node_short": short,
                            "node_num": node_num,
                            "region": region,
                            "message": f"\U0001F50B {name} battery critical at {current_bat:.0f}% in {region_display}.",  # 🔋
                            "scope_type": "region",
                            "scope_value": region,
                            "is_critical": is_critical,
                        })
                        self._cooldowns[cooldown_key] = now

                self._prev_battery[node_num] = current_bat

            # Update state snapshot
            self._prev_infra_online[node_num] = is_online

        self._pending_alerts = alerts
        return alerts

    def _get_region_display(self, region: str) -> str:
        """Get display name for region."""
        if not self._reporter:
            return region
        try:
            context = self._reporter._region_context(region)
            if context:
                return context.split("(")[0].strip()
        except Exception:
            pass
        return region

    def _check_cooldown(self, key: str, now: float) -> bool:
        """Check if enough time has passed since last alert for this condition."""
        last = self._cooldowns.get(key, 0)
        return (now - last) >= self._cooldown_seconds

    def get_pending_alerts(self) -> list[dict]:
        """Get alerts pending delivery."""
        return self._pending_alerts

    def clear_pending(self):
        """Clear pending alerts after delivery."""
        self._pending_alerts = []

    def get_subscribers_for_alert(self, alert: dict) -> list[dict]:
        """Find subscribers matching an alert's scope."""
        if not self._subs:
            return []

        # Get all alert subscribers
        # mesh-scope subscribers get everything
        # region-scope subscribers get alerts for their region
        # node-scope subscribers get alerts for their specific node
        return self._subs.get_alert_subscribers(
            scope_type=alert.get("scope_type"),
            scope_value=alert.get("scope_value"),
        )
