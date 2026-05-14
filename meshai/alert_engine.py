"""Alert engine - detects mesh state changes and dispatches alerts."""

import logging
import time
from datetime import datetime
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .config import AlertRulesConfig, MeshIntelligenceConfig
    from .mesh_health import MeshHealthEngine
    from .mesh_reporter import MeshReporter
    from .subscriptions import SubscriptionManager

logger = logging.getLogger(__name__)

# Scaling cooldown schedule (seconds after first alert)
# Alert 1: immediate, Alert 2: +12h, Alert 3: +24h more, Alert 4: +48h more, then stop
ESCALATION_SCHEDULE = [0, 12 * 3600, 24 * 3600, 48 * 3600]


class AlertState:
    """Tracks escalation state for a single condition."""

    def __init__(self):
        self.first_fired: float = 0
        self.alert_count: int = 0
        self.last_fired: float = 0
        self.resolved: bool = False

    def should_fire(self, now: float) -> bool:
        """Check if this condition should fire based on scaling cooldown."""
        if self.resolved:
            return False
        if self.alert_count == 0:
            return True
        if self.alert_count >= len(ESCALATION_SCHEDULE):
            return False
        elapsed_since_last = now - self.last_fired
        required_wait = ESCALATION_SCHEDULE[self.alert_count]
        return elapsed_since_last >= required_wait

    def fire(self, now: float):
        """Record that an alert was fired."""
        if self.alert_count == 0:
            self.first_fired = now
        self.last_fired = now
        self.alert_count += 1

    def resolve(self):
        """Mark condition as resolved."""
        self.resolved = True

    def reset(self):
        """Full reset for new occurrence."""
        self.first_fired = 0
        self.alert_count = 0
        self.last_fired = 0
        self.resolved = False


class AlertEngine:
    """Detects mesh state changes and dispatches alerts."""

    def __init__(
        self,
        health_engine: "MeshHealthEngine",
        reporter: "MeshReporter",
        subscription_manager: "SubscriptionManager",
        config: "MeshIntelligenceConfig",
        db_path: str = "",
        timezone: str = "America/Boise",
    ):
        self._health = health_engine
        self._reporter = reporter
        self._subs = subscription_manager
        self._rules = config.alert_rules
        self._critical_nodes = set(n.upper() for n in (config.critical_nodes or []))
        self._db_path = db_path
        self._timezone = timezone

        self._states: dict[str, AlertState] = {}
        self._prev_infra_online: dict[int, bool] = {}
        self._prev_battery: dict[int, float] = {}
        self._prev_power_source: dict[int, str] = {}
        self._prev_gateways: dict[int, float] = {}
        self._prev_mesh_score: Optional[float] = None
        self._prev_region_scores: dict[str, float] = {}
        self._prev_feeder_gateways: set[str] = set()
        self._known_routers: set[int] = set()
        self._util_exceeded_since: dict[int, float] = {}
        self._first_run = True
        self._pending_alerts: list[dict] = []

    def _get_state(self, key: str) -> AlertState:
        if key not in self._states:
            self._states[key] = AlertState()
        return self._states[key]

    def check(self) -> list[dict]:
        """Run all alert checks. Returns list of alert dicts."""
        health = self._health.mesh_health
        if not health:
            return []

        now = time.time()
        alerts = []
        alerts.extend(self._check_infrastructure(health, now))
        alerts.extend(self._check_power(health, now))
        alerts.extend(self._check_utilization(health, now))
        alerts.extend(self._check_coverage(health, now))
        alerts.extend(self._check_health_scores(health, now))

        self._first_run = False
        self._pending_alerts = alerts
        return alerts

    def _check_infrastructure(self, health, now: float) -> list[dict]:
        alerts = []
        for node in health.nodes.values():
            if not node.is_infrastructure:
                continue

            node_num = node.node_num
            name = node.long_name or node.short_name or str(node_num)
            short = (node.short_name or str(node_num)).upper()
            region = node.region or "Unknown"
            is_critical = short in self._critical_nodes
            region_display = self._get_region_display(region)

            was_online = self._prev_infra_online.get(node_num)
            is_online = node.is_online

            if not self._first_run and was_online is not None:
                if was_online and not is_online and self._rules.infra_offline:
                    key = f"offline_{node_num}"
                    state = self._get_state(key)
                    state.resolved = False
                    if state.should_fire(now):
                        alert_type = "critical_node_down" if is_critical else "infra_offline"
                        emoji = "\U0001F6A8" if is_critical else "\u274C"
                        escalation = f" (alert {state.alert_count + 1}/4)" if state.alert_count > 0 else ""
                        alerts.append(self._make_alert(
                            alert_type, name, short, node_num, region,
                            f"{emoji} {name} went offline in {region_display}.{escalation}",
                            "immediate" if is_critical else "priority",
                        ))
                        state.fire(now)

                elif not was_online and is_online and self._rules.infra_recovery:
                    key = f"offline_{node_num}"
                    state = self._get_state(key)
                    if state.alert_count > 0:
                        alerts.append(self._make_alert(
                            "infra_recovery", name, short, node_num, region,
                            f"\u2705 {name} is back online in {region_display}.",
                            "immediate" if is_critical else "priority",
                        ))
                        state.resolve()

            if self._rules.new_router and not self._first_run:
                if node_num not in self._known_routers:
                    alerts.append(self._make_alert(
                        "new_router", name, short, node_num, region,
                        f"\U0001F4E1 New router appeared: {name} in {region_display}.",
                        False,
                    ))

            self._prev_infra_online[node_num] = is_online
            self._known_routers.add(node_num)

        return alerts

    def _check_power(self, health, now: float) -> list[dict]:
        alerts = []
        for node in health.nodes.values():
            if not node.is_infrastructure:
                continue
            if node.battery_percent is None:
                continue

            node_num = node.node_num
            name = node.long_name or node.short_name or str(node_num)
            short = (node.short_name or str(node_num)).upper()
            region = node.region or "Unknown"
            is_critical = short in self._critical_nodes
            region_display = self._get_region_display(region)
            bat = node.battery_percent

            if self._rules.power_source_change and not self._first_run:
                current_source = "usb" if bat > 100 else "battery"
                prev_source = self._prev_power_source.get(node_num)
                if prev_source == "usb" and current_source == "battery":
                    key = f"power_change_{node_num}"
                    state = self._get_state(key)
                    if state.should_fire(now):
                        alerts.append(self._make_alert(
                            "power_source_change", name, short, node_num, region,
                            f"\u26A1 {name} switched from USB to battery in {region_display}. Possible power outage.",
                            "immediate" if is_critical else "priority",
                        ))
                        state.fire(now)
                elif prev_source == "battery" and current_source == "usb":
                    key = f"power_change_{node_num}"
                    state = self._get_state(key)
                    if state.alert_count > 0:
                        state.resolve()
                self._prev_power_source[node_num] = current_source

            if 0 < bat <= 100 and not self._first_run:
                prev_bat = self._prev_battery.get(node_num)

                if self._rules.battery_emergency and bat < self._rules.battery_emergency_threshold:
                    if prev_bat is None or prev_bat >= self._rules.battery_emergency_threshold:
                        key = f"bat_emergency_{node_num}"
                        state = self._get_state(key)
                        if state.should_fire(now):
                            alerts.append(self._make_alert(
                                "battery_emergency", name, short, node_num, region,
                                f"\U0001F6A8 {name} battery EMERGENCY at {bat:.0f}% in {region_display}.",
                                "immediate" if is_critical else "priority",
                            ))
                            state.fire(now)

                elif self._rules.battery_critical and bat < self._rules.battery_critical_threshold:
                    if prev_bat is None or prev_bat >= self._rules.battery_critical_threshold:
                        key = f"bat_critical_{node_num}"
                        state = self._get_state(key)
                        if state.should_fire(now):
                            alerts.append(self._make_alert(
                                "battery_critical", name, short, node_num, region,
                                f"\U0001F50B {name} battery critical at {bat:.0f}% in {region_display}.",
                                "immediate" if is_critical else "priority",
                            ))
                            state.fire(now)

                elif self._rules.battery_warning and bat < self._rules.battery_warning_threshold:
                    if prev_bat is None or prev_bat >= self._rules.battery_warning_threshold:
                        key = f"bat_warning_{node_num}"
                        state = self._get_state(key)
                        if state.should_fire(now):
                            alerts.append(self._make_alert(
                                "battery_warning", name, short, node_num, region,
                                f"\U0001F50B {name} battery low at {bat:.0f}% in {region_display}.",
                                "immediate" if is_critical else "priority",
                            ))
                            state.fire(now)

                if prev_bat is not None and bat > prev_bat + 5:
                    for prefix in ["bat_emergency", "bat_critical", "bat_warning"]:
                        key = f"{prefix}_{node_num}"
                        state = self._get_state(key)
                        if state.alert_count > 0:
                            state.resolve()

            if self._rules.battery_trend_declining and 0 < bat <= 100:
                trend = self._get_battery_trend(node_num, days=7)
                if trend and trend["direction"] == "declining" and trend["total_drop"] > 10:
                    key = f"bat_trend_{node_num}"
                    state = self._get_state(key)
                    if state.alert_count == 0 and state.should_fire(now):
                        alerts.append(self._make_alert(
                            "battery_trend", name, short, node_num, region,
                            f"\U0001F50B {name} battery declining: {trend['start']:.0f}% \u2192 {trend['end']:.0f}% over 7 days ({trend['rate']:.1f}%/day) in {region_display}.",
                            "immediate" if is_critical else "priority",
                        ))
                        state.fire(now)

            # NOTE: has_solar is never populated in current version.
            # Solar Quality Engine (v0.3) will replace this with real solar
            # monitoring based on location, weather, and inversion data.
            # For now this check effectively never fires.
            if self._rules.solar_not_charging and getattr(node, "has_solar", False) and 0 < bat <= 100:
                try:
                    from zoneinfo import ZoneInfo
                    tz = ZoneInfo(self._timezone)
                    hour = datetime.now(tz).hour
                    if 8 <= hour <= 18:
                        prev_bat = self._prev_battery.get(node_num)
                        if prev_bat is not None and bat < prev_bat - 2:
                            key = f"solar_{node_num}"
                            state = self._get_state(key)
                            if state.should_fire(now):
                                alerts.append(self._make_alert(
                                    "solar_not_charging", name, short, node_num, region,
                                    f"\u2600\uFE0F {name} solar not charging in {region_display}.",
                                    "immediate" if is_critical else "priority",
                                ))
                                state.fire(now)
                except Exception:
                    pass

            self._prev_battery[node_num] = bat

        return alerts

    def _check_utilization(self, health, now: float) -> list[dict]:
        alerts = []
        for node in health.nodes.values():
            node_num = node.node_num
            name = node.long_name or node.short_name or str(node_num)
            short = (node.short_name or str(node_num)).upper()
            region = node.region or "Unknown"
            region_display = self._get_region_display(region)

            if self._rules.sustained_high_util and node.channel_utilization is not None:
                threshold = self._rules.high_util_threshold
                required_hours = self._rules.high_util_hours
                if node.channel_utilization > threshold:
                    if node_num not in self._util_exceeded_since:
                        self._util_exceeded_since[node_num] = now
                    else:
                        duration_hours = (now - self._util_exceeded_since[node_num]) / 3600
                        if duration_hours >= required_hours:
                            key = f"util_{node_num}"
                            state = self._get_state(key)
                            if state.should_fire(now):
                                alerts.append(self._make_alert(
                                    "sustained_high_util", name, short, node_num, region,
                                    f"\U0001F525 {name} at {node.channel_utilization:.0f}% util for {duration_hours:.0f}+ hours in {region_display}.",
                                    False,
                                ))
                                state.fire(now)
                else:
                    if node_num in self._util_exceeded_since:
                        del self._util_exceeded_since[node_num]
                    key = f"util_{node_num}"
                    state = self._get_state(key)
                    if state.alert_count > 0:
                        state.resolve()

            if self._rules.packet_flood and not self._first_run:
                if getattr(node, "packets_sent_24h", 0) > self._rules.packet_flood_threshold:
                    key = f"flood_{node_num}"
                    state = self._get_state(key)
                    if state.alert_count == 0:
                        alerts.append(self._make_alert(
                            "packet_flood", name, short, node_num, region,
                            f"\U0001F4E1 {name} sent {node.packets_sent_24h} packets in 24h (threshold: {self._rules.packet_flood_threshold}) in {region_display}.",
                            False,
                        ))
                        state.fire(now)

        return alerts

    def _check_coverage(self, health, now: float) -> list[dict]:
        alerts = []
        for node in health.nodes.values():
            if not node.is_infrastructure:
                continue

            node_num = node.node_num
            name = node.long_name or node.short_name or str(node_num)
            short = (node.short_name or str(node_num)).upper()
            region = node.region or "Unknown"
            is_critical = short in self._critical_nodes
            region_display = self._get_region_display(region)

            if self._rules.infra_single_gateway and node.avg_gateways is not None and not self._first_run:
                prev_gw = self._prev_gateways.get(node_num)
                if prev_gw is not None and prev_gw > 1.0 and node.avg_gateways <= 1.0:
                    key = f"single_gw_{node_num}"
                    state = self._get_state(key)
                    if state.should_fire(now):
                        alerts.append(self._make_alert(
                            "infra_single_gateway", name, short, node_num, region,
                            f"\u26A0\uFE0F {name} dropped to single gateway in {region_display}. At risk if gateway fails.",
                            "immediate" if is_critical else "priority",
                        ))
                        state.fire(now)
                elif prev_gw is not None and prev_gw <= 1.0 and node.avg_gateways > 1.0:
                    key = f"single_gw_{node_num}"
                    state = self._get_state(key)
                    if state.alert_count > 0:
                        state.resolve()
                self._prev_gateways[node_num] = node.avg_gateways

        if self._rules.feeder_offline and not self._first_run:
            current_feeders = set()
            for node in health.nodes.values():
                for gw in getattr(node, "feeder_gateways", []):
                    gw_name = gw.get("gateway_name") or gw.get("gateway_id", "")
                    if gw_name:
                        current_feeders.add(gw_name)

            if self._prev_feeder_gateways:
                lost_feeders = self._prev_feeder_gateways - current_feeders
                for feeder in lost_feeders:
                    key = f"feeder_{feeder}"
                    state = self._get_state(key)
                    if state.should_fire(now):
                        alerts.append({
                            "type": "feeder_offline",
                            "node_name": feeder,
                            "node_short": feeder,
                            "node_num": 0,
                            "region": "",
                            "message": f"\U0001F4E1 Feeder gateway {feeder} stopped responding.",
                            "scope_type": "mesh",
                            "scope_value": None,
                            "severity": "routine",
                        })
                        state.fire(now)

                recovered_feeders = current_feeders - self._prev_feeder_gateways
                for feeder in recovered_feeders:
                    key = f"feeder_{feeder}"
                    state = self._get_state(key)
                    if state.alert_count > 0:
                        state.resolve()

            self._prev_feeder_gateways = current_feeders

        if self._rules.region_total_blackout and not self._first_run:
            for region in health.regions:
                if not region.node_ids:
                    continue
                infra_in_region = []
                for nid_str in region.node_ids:
                    try:
                        nid = int(nid_str)
                    except (ValueError, TypeError):
                        continue
                    node = health.nodes.get(nid)
                    if node and node.is_infrastructure:
                        infra_in_region.append(node)

                if infra_in_region and all(not n.is_online for n in infra_in_region):
                    key = f"blackout_{region.name}"
                    state = self._get_state(key)
                    if state.should_fire(now):
                        region_display = self._get_region_display(region.name)
                        alerts.append({
                            "type": "region_total_blackout",
                            "node_name": region.name,
                            "node_short": region.name,
                            "node_num": 0,
                            "region": region.name,
                            "message": f"\U0001F6A8 TOTAL BLACKOUT: All infrastructure in {region_display} is offline!",
                            "scope_type": "region",
                            "scope_value": region.name,
                            "severity": "immediate",
                        })
                        state.fire(now)

        return alerts

    def _check_health_scores(self, health, now: float) -> list[dict]:
        alerts = []

        if self._first_run:
            self._prev_mesh_score = health.score.composite
            for region in health.regions:
                self._prev_region_scores[region.name] = region.score.composite
            return alerts

        if self._rules.mesh_score_alert:
            current = health.score.composite
            threshold = self._rules.mesh_score_threshold
            if current < threshold and (self._prev_mesh_score is None or self._prev_mesh_score >= threshold):
                key = "mesh_score"
                state = self._get_state(key)
                if state.should_fire(now):
                    alerts.append({
                        "type": "mesh_score_low",
                        "node_name": "Mesh",
                        "node_short": "MESH",
                        "node_num": 0,
                        "region": "",
                        "message": f"\U0001F4C9 Mesh health dropped to {current:.0f}/100 (threshold: {threshold}).",
                        "scope_type": "mesh",
                        "scope_value": None,
                        "severity": "routine",
                    })
                    state.fire(now)
            elif current >= threshold:
                key = "mesh_score"
                state = self._get_state(key)
                if state.alert_count > 0:
                    state.resolve()
            self._prev_mesh_score = current

        if self._rules.region_score_alert:
            threshold = self._rules.region_score_threshold
            for region in health.regions:
                current = region.score.composite
                prev = self._prev_region_scores.get(region.name)
                if current < threshold and (prev is None or prev >= threshold):
                    key = f"region_score_{region.name}"
                    state = self._get_state(key)
                    if state.should_fire(now):
                        region_display = self._get_region_display(region.name)
                        alerts.append({
                            "type": "region_score_low",
                            "node_name": region.name,
                            "node_short": region.name,
                            "node_num": 0,
                            "region": region.name,
                            "message": f"\U0001F4C9 {region_display} health dropped to {current:.0f}/100 (threshold: {threshold}).",
                            "scope_type": "region",
                            "scope_value": region.name,
                            "severity": "routine",
                        })
                        state.fire(now)
                elif current >= threshold:
                    key = f"region_score_{region.name}"
                    state = self._get_state(key)
                    if state.alert_count > 0:
                        state.resolve()
                self._prev_region_scores[region.name] = current

        return alerts

    def _get_battery_trend(self, node_num: int, days: int = 7) -> Optional[dict]:
        """Query SQLite for battery trend over N days."""
        if not self._db_path:
            return None
        try:
            import sqlite3
            conn = sqlite3.connect(self._db_path)
            cursor = conn.cursor()
            cutoff = time.time() - (days * 86400)
            rows = cursor.execute("""
                SELECT battery_percent, timestamp
                FROM node_snapshots
                WHERE node_num = ? AND timestamp > ? AND battery_percent IS NOT NULL
                AND battery_percent > 0 AND battery_percent <= 100
                ORDER BY timestamp ASC
            """, (node_num, cutoff)).fetchall()
            conn.close()

            if len(rows) < 10:
                return None

            start_bat = rows[0][0]
            end_bat = rows[-1][0]
            total_drop = start_bat - end_bat
            duration_days = (rows[-1][1] - rows[0][1]) / 86400
            if duration_days < 1:
                return None
            rate = total_drop / duration_days
            return {
                "start": start_bat,
                "end": end_bat,
                "total_drop": total_drop,
                "duration_days": duration_days,
                "rate": rate,
                "direction": "declining" if rate > 1.0 else "stable" if abs(rate) < 1.0 else "charging",
            }
        except Exception as e:
            logger.debug(f"Battery trend query error: {e}")
            return None

    def _make_alert(self, alert_type, name, short, node_num, region, message, severity="priority"):
        return {
            "type": alert_type,
            "node_name": name,
            "node_short": short,
            "node_num": node_num,
            "region": region,
            "message": message,
            "scope_type": "region" if region and region != "Unknown" else "mesh",
            "scope_value": region if region and region != "Unknown" else None,
            "severity": severity,
        }

    def _get_region_display(self, region: str) -> str:
        if not self._reporter:
            return region
        try:
            context = self._reporter._region_context(region)
            if context:
                return context.split("(")[0].strip()
        except Exception:
            pass
        return region

    def get_pending_alerts(self) -> list[dict]:
        return self._pending_alerts

    def clear_pending(self):
        self._pending_alerts = []

    def get_subscribers_for_alert(self, alert: dict) -> list[dict]:
        if not self._subs:
            return []
        return self._subs.get_alert_subscribers(
            scope_type=alert.get("scope_type"),
            scope_value=alert.get("scope_value"),
        )

    def check_environmental(self, env_store) -> list[dict]:
        """Check environmental feeds for alertable conditions.

        Args:
            env_store: EnvironmentalStore instance

        Returns:
            List of alert dicts
        """
        alerts = []
        now = time.time()

        # NWS severe weather affecting mesh zones
        mesh_zones = set(getattr(env_store, "_mesh_zones", []))
        for evt in env_store.get_active(source="nws"):
            if evt.get("severity") not in ("severe", "extreme", "warning"):
                continue
            event_zones = set(evt.get("areas", []))
            if mesh_zones and not (event_zones & mesh_zones):
                continue
            key = f"env_nws_{evt['event_id']}"
            state = self._get_state(key)
            if not state.should_fire(now):
                continue
            state.fire(now)
            alerts.append({
                "type": "weather_warning",
                "message": f"Warning: {evt['event_type']}: {evt.get('headline', '')[:150]}",
                "node_num": None,
                "node_name": evt["event_type"],
                "node_short": "NWS",
                "region": "",
                "scope_type": "mesh",
                "scope_value": None,
                "severity": "immediate" if evt["severity"] == "extreme" else "priority",
            })

        # SWPC R-scale >= 3 (HF blackout affecting mesh backhaul)
        swpc = env_store.get_swpc_status()
        if swpc and swpc.get("r_scale", 0) >= 3:
            r_scale = swpc["r_scale"]
            key = f"env_swpc_r{r_scale}"
            state = self._get_state(key)
            if state.should_fire(now):
                state.fire(now)
                alerts.append({
                    "type": "hf_blackout",
                    "message": f"Warning: R{r_scale} HF Radio Blackout -- mesh backhaul links may degrade",
                    "severity": "priority",
                    "node_num": None,
                    "node_name": f"R{r_scale} Blackout",
                    "node_short": "SWPC",
                    "region": "",
                    "scope_type": "mesh",
                    "scope_value": None,
                    "severity": "immediate" if r_scale >= 4 else "priority",
                })

        # Tropospheric ducting (informational -- not critical but operators want to know)
        ducting = env_store.get_ducting_status()
        if ducting and ducting.get("condition") in ("surface_duct", "elevated_duct"):
            key = "env_ducting_active"
            state = self._get_state(key)
            if state.should_fire(now):
                state.fire(now)
                condition = ducting.get("condition", "ducting").replace("_", " ")
                gradient = ducting.get("min_gradient", "?")
                alerts.append({
                    "type": "tropospheric_ducting",
                    "message": f"Tropospheric {condition} detected (dM/dz {gradient} M-units/km)",
                    "severity": "routine",
                    "node_num": None,
                    "node_name": "Ducting",
                    "node_short": "TROPO",
                    "region": "",
                    "scope_type": "mesh",
                    "scope_value": None,
                    "severity": "routine",
                })

        # Wildfire proximity alerts
        fires = env_store.get_active(source="nifc")
        for fire in fires:
            distance_km = fire.get("distance_km")
            if distance_km is None:
                continue

            name = fire.get("name", "Unknown")
            acres = fire.get("acres", 0)
            pct = fire.get("pct_contained", 0)
            anchor = fire.get("nearest_anchor", "mesh area")

            if distance_km < 25:
                # Critical - fire within 25km
                key = f"env_fire_critical_{name}"
                state = self._get_state(key)
                if state.should_fire(now):
                    state.fire(now)
                    alerts.append({
                        "type": "wildfire_proximity",
                        "message": f"Wildfire '{name}' within {int(distance_km)} km of {anchor} -- {int(acres):,} ac, {int(pct)}% contained",
                        "severity": "immediate",
                        "node_num": None,
                        "node_name": name,
                        "node_short": "FIRE",
                        "region": anchor,
                        "scope_type": "mesh",
                        "scope_value": None,
                        "severity": "immediate",
                    })

            elif distance_km < 50:
                # Warning - fire within 50km
                key = f"env_fire_warning_{name}"
                state = self._get_state(key)
                if state.should_fire(now):
                    state.fire(now)
                    alerts.append({
                        "type": "wildfire_proximity",
                        "message": f"Wildfire '{name}' {int(distance_km)} km from {anchor} -- {int(acres):,} ac, {int(pct)}% contained",
                        "severity": "priority",
                        "node_num": None,
                        "node_name": name,
                        "node_short": "FIRE",
                        "region": anchor,
                        "scope_type": "mesh",
                        "scope_value": None,
                        "severity": "routine",
                    })

        return alerts
