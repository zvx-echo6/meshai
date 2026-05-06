"""Mesh health reporting for LLM prompt injection and commands.

Refactored to consume MeshDataStore and UnifiedNode directly.
"""

import logging
import math
import time
from datetime import datetime
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .mesh_data_store import MeshDataStore
    from .mesh_health import MeshHealthEngine, RegionHealth
    from .mesh_models import UnifiedNode

logger = logging.getLogger(__name__)

# Portnum display names (from Meshtastic protobufs)
PORTNUM_DISPLAY = {
    "TEXT_MESSAGE_APP": "Text",
    "POSITION_APP": "Position",
    "NODEINFO_APP": "NodeInfo",
    "TELEMETRY_APP": "Telemetry",
    "TRACEROUTE_APP": "Traceroute",
    "ROUTING_APP": "Routing",
    "ADMIN_APP": "Admin",
    "WAYPOINT_APP": "Waypoint",
    "RANGE_TEST_APP": "RangeTest",
    "STORE_FORWARD_APP": "Store&Fwd",
    "NEIGHBORINFO_APP": "Neighbors",
    "MAP_REPORT_APP": "MapReport",
    "DETECTION_SENSOR_APP": "Sensor",
    "PAXCOUNTER_APP": "PaxCounter",
    "REMOTE_HARDWARE_APP": "RemoteHW",
    "ATAK_PLUGIN": "ATAK",
    "ATAK_FORWARDER": "ATAK",
}

def _clean_portnum(portnum) -> str:
    """Convert raw portnum to display name."""
    if isinstance(portnum, int): portnum = str(portnum)
    return PORTNUM_DISPLAY.get(portnum, portnum.replace("_APP", "").replace("_", " ").title())


def _format_age(timestamp: float) -> str:
    """Format a timestamp as human-readable age."""
    if not timestamp:
        return "never"

    age_seconds = time.time() - timestamp
    if age_seconds < 0:
        return "just now"
    elif age_seconds < 60:
        return f"{int(age_seconds)}s ago"
    elif age_seconds < 3600:
        return f"{int(age_seconds / 60)}m ago"
    elif age_seconds < 86400:
        return f"{int(age_seconds / 3600)}h ago"
    else:
        return f"{int(age_seconds / 86400)}d ago"


def _format_battery(battery_percent: Optional[float], voltage: Optional[float] = None) -> str:
    """Format battery with emoji and USB detection.

    Args:
        battery_percent: 0-100, or 101 for USB/external powered
        voltage: Optional voltage reading

    Returns:
        Formatted string like "USB Powered" or "75% (3.92V)"
    """
    if battery_percent is None:
        return "N/A"

    # 101% = USB/external powered
    if battery_percent > 100:
        return "USB Powered"

    # Build emoji indicator
    pct = int(battery_percent)
    if pct >= 80:
        emoji = ""  # Good, no emoji needed
    elif pct >= 50:
        emoji = ""  # OK, no emoji
    elif pct >= 20:
        emoji = " (low)"
    else:
        emoji = " (critical)"

    # Add voltage if available
    if voltage and voltage > 0:
        return f"{pct}% ({voltage:.2f}V){emoji}"
    return f"{pct}%{emoji}"


def _node_display_name(long_name: Optional[str], short_name: Optional[str], node_id: str) -> str:
    """Format node display name: long name first, shortname in parens.

    Args:
        long_name: Full node name
        short_name: 4-char short name
        node_id: Fallback node ID

    Returns:
        Formatted name like "My Node Name (MYND)" or "MYND" or "!abcd1234"
    """
    if long_name and short_name:
        return f"{long_name} ({short_name})"
    elif long_name:
        return long_name
    elif short_name:
        return short_name
    else:
        return f"!{node_id[-8:]}" if len(node_id) >= 8 else node_id


def _tier_flag(tier: str) -> str:
    """Get warning flag for health tier."""
    if tier == "Critical":
        return " !!"
    elif tier == "Warning":
        return " !"
    elif tier == "Unhealthy":
        return " !"
    return ""


def _format_temperature(temp_c: Optional[float]) -> Optional[str]:
    """Format temperature, flagging suspicious readings.

    Args:
        temp_c: Temperature in Celsius

    Returns:
        Formatted string or None if input is None
    """
    if temp_c is None:
        return None
    temp_f = temp_c * 9/5 + 32
    if temp_c > 60 or temp_c < -50:
        return f"{temp_c:.1f}°C ({temp_f:.1f}°F) ⚠️ suspect"
    return f"{temp_c:.1f}°C ({temp_f:.1f}°F)"


def _is_valid_temperature(temp_c: Optional[float]) -> bool:
    """Check if temperature is within valid range for aggregation."""
    if temp_c is None:
        return False
    return -50 <= temp_c <= 60


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance between two GPS points in km."""
    R = 6371  # Earth radius in km
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    c = 2 * math.asin(math.sqrt(a))
    return R * c


def _format_distance(km: float) -> str:
    """Format distance as km/miles."""
    miles = km * 0.621371
    if km < 1:
        return f"{km*1000:.0f}m ({miles*5280:.0f}ft)"
    return f"{km:.1f}km ({miles:.1f}mi)"


class MeshReporter:
    """Builds text blocks for mesh health prompt injection."""

    def __init__(self, health_engine: "MeshHealthEngine", data_store: "MeshDataStore", region_configs=None):
        """Initialize reporter.

        Args:
            health_engine: MeshHealthEngine instance
            data_store: MeshDataStore instance
            region_configs: Optional list of RegionAnchor configs for local names
        """
        self.health_engine = health_engine
        self.data_store = data_store
        self._region_configs = {r.name: r for r in (region_configs or [])}

    def _region_context(self, region_name: str) -> str:
        """Get display context for a region from config."""
        cfg = self._region_configs.get(region_name)
        if not cfg:
            return ""
        local = getattr(cfg, "local_name", "") or ""
        desc = getattr(cfg, "description", "") or ""
        if local and desc:
            return f"{local} ({desc})"
        return local or desc


    def _find_node(self, identifier: str) -> "UnifiedNode | None":
        """Find a node by any identifier (shortname, longname, node_num, hex ID).

        Delegates to health_engine.get_node() which searches all formats.
        """
        return self.health_engine.get_node(identifier)

    def _find_region(self, region_name: str) -> "RegionHealth | None":
        """Find a region by name (fuzzy match).

        Tries exact match, then substring, then alias from config.
        """
        health = self.health_engine.mesh_health
        if not health:
            return None

        name_lower = region_name.lower().strip()

        # Exact match
        for region in health.regions:
            if region.name.lower() == name_lower:
                return region

        # Substring match (longest region name first to avoid partial matches)
        for region in sorted(health.regions, key=lambda r: len(r.name), reverse=True):
            if name_lower in region.name.lower() or region.name.lower() in name_lower:
                return region

        # Check config aliases
        for region_cfg_name, cfg in self._region_configs.items():
            aliases = getattr(cfg, 'aliases', []) or []
            cities = getattr(cfg, 'cities', []) or []
            local_name = getattr(cfg, 'local_name', '') or ''

            all_matches = [a.lower() for a in aliases] + [c.lower() for c in cities]
            if local_name:
                all_matches.append(local_name.lower())

            if name_lower in all_matches or any(name_lower in m for m in all_matches):
                # Found config match, now find the region
                for region in health.regions:
                    if region.name == region_cfg_name:
                        return region

        return None

    def _build_source_health_section(self) -> list[str]:
        """Build source health section for Tier 1."""
        lines = []
        lines.append("")
        lines.append("DATA SOURCES:")

        for name, source in self.data_store._sources.items():
            if hasattr(source, 'health_status'):
                status = source.health_status
                err_str = f" - {status['last_error']}" if status.get('last_error') else ""
                backed = " [BACKED OFF]" if status.get('backed_off') else ""
                polite = " [POLITE]" if status.get('polite_mode') else ""
                lines.append(
                    f"  {name}: {status.get('cached_nodes', 0)} nodes, "
                    f"{status.get('cached_packets', 0)} pkts, "
                    f"avg {status.get('avg_response_ms', 0)}ms"
                    f"{polite}{backed}{err_str}"
                )
            else:
                # Legacy source without health_status
                node_count = len(source.nodes) if hasattr(source, 'nodes') else 0
                loaded = "OK" if source.is_loaded else "ERR"
                err = f" - {source.last_error}" if source.last_error else ""
                lines.append(f"  {name}: [{loaded}] {node_count} nodes{err}")

        return lines

    def build_tier1_summary(self) -> str:
        """Build comprehensive mesh health summary with full data for LLM context."""
        health = self.health_engine.mesh_health
        if not health:
            return "MESH HEALTH: No data available."

        import time
        now = time.time()
        age_seconds = now - health.last_computed if health.last_computed else 0
        age_str = f"{int(age_seconds)}s ago" if age_seconds < 120 else f"{int(age_seconds/60)}m ago"

        score = health.score

        # Get AIDA's position for distance calculations
        aida_node = health.nodes.get(0x27780c47)  # AIDA-N2
        aida_lat = aida_node.latitude if aida_node else None
        aida_lon = aida_node.longitude if aida_node else None

        lines = [
            f"LIVE MESH HEALTH DATA (as of {age_str}):",
            "",
            f"OVERALL: {score.composite:.0f}/100 ({score.tier})",
            f"  Infrastructure: {score.infrastructure:.0f}/100 ({score.infra_online}/{score.infra_total} online) - weight 30%",
            f"  Utilization: {score.utilization:.0f}/100 ({score.util_percent:.1f}% avg) - weight 25%",
            f"  Coverage: {score.coverage:.0f}/100 - weight 20%",
            f"  Behavior: {score.behavior:.0f}/100 ({score.flagged_nodes} flagged) - weight 15%",
            f"  Power: {score.power:.0f}/100 - weight 10%",
        ]

        lines.append("")
        lines.append("REGIONS:")

        for region in health.regions:
            if not region.node_ids:
                continue
            rs = region.score
            context = self._region_context(region.name)
            context_str = f" - {context}" if context else ""

            lines.append("")
            lines.append(f"  {region.name}{context_str}: {rs.composite:.0f}/100")

            infra_nodes = []
            client_nodes = []
            for nid_str in region.node_ids:
                try:
                    nid = int(nid_str)
                except (ValueError, TypeError):
                    continue
                node = health.nodes.get(nid)
                if not node:
                    continue
                if node.is_infrastructure:
                    infra_nodes.append(node)
                else:
                    client_nodes.append(node)

            if infra_nodes:
                online = sum(1 for n in infra_nodes if n.is_online)
                lines.append(f"    Infrastructure ({online}/{len(infra_nodes)}):")
                for node in sorted(infra_nodes, key=lambda n: (not n.is_online, n.short_name or '')):
                    status = "OK" if node.is_online else "OFFLINE"
                    name = _node_display_name(node.long_name, node.short_name, str(node.node_num))
                    age = _format_age(node.last_heard) if node.last_heard else "?"

                    parts = [f"seen {age}"]
                    if node.battery_percent is not None:
                        parts.append(_format_battery(node.battery_percent, node.voltage))
                    if node.channel_utilization is not None:
                        parts.append(f"util {node.channel_utilization:.1f}%")
                    if node.avg_gateways is not None:
                        total_gw = len(self.data_store._sources)
                        parts.append(f"{node.avg_gateways:.0f}/{total_gw} gw")
                    if node.neighbor_count > 0:
                        parts.append(f"{node.neighbor_count} neighbors")
                    if node.packets_sent_24h > 0:
                        parts.append(f"{node.packets_sent_24h} pkts/24h")
                    if node.uplink_enabled:
                        parts.append("MQTT")

                    if aida_lat and aida_lon and node.latitude and node.longitude and node.node_num != 0x27780c47:
                        km = _haversine_km(node.latitude, node.longitude, aida_lat, aida_lon)
                        parts.append(f"{_format_distance(km)} from AIDA")

                    lines.append(f"      [{status}] {name}: {', '.join(parts)}")
            else:
                lines.append(f"    Infrastructure: none in region")

            region_gw = []
            for nid_str in region.node_ids:
                try:
                    nid = int(nid_str)
                except (ValueError, TypeError):
                    continue
                node = health.nodes.get(nid)
                if node and node.avg_gateways is not None:
                    region_gw.append(node)

            if region_gw:
                avg_gw = sum(n.avg_gateways for n in region_gw) / len(region_gw)
                single = sum(1 for n in region_gw if n.avg_gateways <= 1.0)
                total_gw = len(self.data_store._sources)
                lines.append(f"    Coverage: {len(region_gw)} nodes, avg {avg_gw:.1f}/{total_gw} gw, {single} single-gateway")

                # Name single-gateway INFRASTRUCTURE nodes (critical risks)
                if single > 0:
                    single_gw_nodes = [n for n in region_gw if n.avg_gateways is not None and n.avg_gateways <= 1.0]
                    single_infra = [n for n in single_gw_nodes if n.is_infrastructure]
                    single_clients = len(single_gw_nodes) - len(single_infra)

                    if single_infra:
                        for sgn in single_infra:
                            sgn_name = _node_display_name(sgn.long_name, sgn.short_name, str(sgn.node_num))
                            src_info = f" via {sgn.sources[0]}" if len(sgn.sources) == 1 else ""
                            lines.append(f"      INFRA at risk: {sgn_name} - only 1 gateway{src_info}")
                    if single_clients > 0:
                        single_client_nodes = [n for n in single_gw_nodes if not n.is_infrastructure]
                        # Find nearest infra with GPS for distance reference
                        ref_node = None
                        for inf in single_infra if single_infra else []:
                            if inf.latitude and inf.longitude:
                                ref_node = inf
                                break
                        if not ref_node:
                            for nid_str in region.node_ids:
                                try:
                                    nid = int(nid_str)
                                except (ValueError, TypeError):
                                    continue
                                inf = health.nodes.get(nid)
                                if inf and inf.is_infrastructure and inf.latitude and inf.longitude:
                                    ref_node = inf
                                    break

                        lines.append(f"      Single-gw clients ({single_clients}):")
                        for scn in single_client_nodes[:10]:
                            scn_name = _node_display_name(scn.long_name, scn.short_name, str(scn.node_num))
                            src_info = f" via {scn.sources[0]}" if len(scn.sources) == 1 else ""

                            dist_str = ""
                            if scn.latitude and scn.longitude and ref_node:
                                km = _haversine_km(scn.latitude, scn.longitude, ref_node.latitude, ref_node.longitude)
                                dist_str = f" ~{_format_distance(km)} from {ref_node.short_name}"
                            elif scn.hops_away is not None:
                                dist_str = f" {scn.hops_away} hops"

                            lines.append(f"        {scn_name}{src_info}{dist_str}")
                        if len(single_client_nodes) > 10:
                            lines.append(f"        ...and {len(single_client_nodes) - 10} more")

            env_in_region = []
            for nid_str in region.node_ids:
                try:
                    nid = int(nid_str)
                except (ValueError, TypeError):
                    continue
                node = health.nodes.get(nid)
                if node and node.has_environment_sensor:
                    env_in_region.append(node)

            if env_in_region:
                temps = [n.temperature for n in env_in_region if _is_valid_temperature(n.temperature)]
                if temps:
                    avg_t = sum(temps) / len(temps)
                    lines.append(f"    Environment: {len(env_in_region)} sensors, temp {min(temps):.1f}-{max(temps):.1f}C (avg {avg_t:.1f}C)")

            lines.append(f"    Clients: {len(client_nodes)} nodes")

        unlocated_count = len(health.unlocated_nodes)
        if unlocated_count > 0:
            lines.append(f"")
            lines.append(f"  Unlocated: {unlocated_count} nodes (no GPS or neighbor data)")

        lines.append("")
        lines.append("PROBLEM NODES:")

        problems_found = False

        offline_infra = [n for n in health.nodes.values() if n.is_infrastructure and not n.is_online]
        if offline_infra:
            problems_found = True
            for node in offline_infra:
                name = _node_display_name(node.long_name, node.short_name, str(node.node_num))
                age = _format_age(node.last_heard) if node.last_heard else "unknown"
                region = node.region or "Unlocated"
                lines.append(f"  [OFFLINE] {name}: infra offline for {age}, {region}")

        critical_bat = [n for n in health.nodes.values()
                        if n.is_infrastructure and n.battery_percent is not None and 0 < n.battery_percent < 10]
        if critical_bat:
            problems_found = True
            for node in sorted(critical_bat, key=lambda n: n.battery_percent):
                name = _node_display_name(node.long_name, node.short_name, str(node.node_num))
                region = node.region or "Unlocated"
                trend = f", trend: {node.battery_trend}" if node.battery_trend else ""
                lines.append(f"  [CRITICAL] {name}: battery {node.battery_percent:.0f}%{trend}, {region}")

        low_bat = [n for n in health.nodes.values()
                   if n.is_infrastructure and n.battery_percent is not None and 10 <= n.battery_percent < 20]
        if low_bat:
            problems_found = True
            for node in sorted(low_bat, key=lambda n: n.battery_percent)[:5]:
                name = _node_display_name(node.long_name, node.short_name, str(node.node_num))
                region = node.region or "Unlocated"
                lines.append(f"  [LOW BAT] {name}: battery {node.battery_percent:.0f}%, {region}")
            if len(low_bat) > 5:
                lines.append(f"  ...and {len(low_bat) - 5} more low battery nodes")

        high_util = [n for n in health.nodes.values()
                     if n.channel_utilization is not None and n.channel_utilization > 15]
        if high_util:
            problems_found = True
            for node in sorted(high_util, key=lambda n: n.channel_utilization or 0, reverse=True)[:5]:
                name = _node_display_name(node.long_name, node.short_name, str(node.node_num))
                region = node.region or "Unlocated"
                lines.append(f"  [HIGH UTIL] {name}: channel util {node.channel_utilization:.1f}%, {region}")

        single_gw_infra = [n for n in health.nodes.values()
                           if n.is_infrastructure and n.is_online and n.avg_gateways is not None and n.avg_gateways <= 1.0]
        if single_gw_infra:
            problems_found = True
            for node in single_gw_infra:
                name = _node_display_name(node.long_name, node.short_name, str(node.node_num))
                region = node.region or "Unlocated"
                lines.append(f"  [1-GW RISK] {name}: infra only reaches 1 gateway, {region}")

        if not problems_found:
            lines.append("  None - all nodes healthy")

        top_senders = self.data_store.get_top_senders(5)
        if top_senders:
            lines.append("")
            lines.append("TOP SENDERS (24h):")
            for node in top_senders:
                if node.packets_sent_24h > 0:
                    breakdown = []
                    for portnum, count in sorted(node.packets_by_type.items(), key=lambda x: -x[1])[:3]:
                        breakdown.append(f"{_clean_portnum(portnum)}: {count}")
                    breakdown_str = f" ({', '.join(breakdown)})" if breakdown else ""
                    name = _node_display_name(node.long_name, node.short_name, node.node_id_hex or "")
                    region = node.region or ""
                    region_str = f" [{region}]" if region else ""
                    lines.append(f"  {name}: {node.packets_sent_24h} pkts{breakdown_str}{region_str}")

        all_nodes = list(health.nodes.values())
        nodes_with_gw = [n for n in all_nodes if n.avg_gateways is not None]
        if nodes_with_gw:
            total_sources = len(self.data_store._sources)
            mesh_avg = sum(n.avg_gateways for n in nodes_with_gw) / len(nodes_with_gw)
            single_gw = sum(1 for n in nodes_with_gw if n.avg_gateways <= 1.0)
            full_gw = sum(1 for n in nodes_with_gw if n.avg_gateways >= total_sources)

            lines.append("")
            lines.append(f"COVERAGE SUMMARY: {mesh_avg:.1f} avg gateways across {total_sources} sources")
            lines.append(f"  Full coverage ({total_sources}/{total_sources} gw): {full_gw} nodes")
            lines.append(f"  Single gateway (1/{total_sources} gw): {single_gw} nodes - at risk if gateway drops")

            # Name single-gateway infra nodes (these are critical risks)
            single_gw_infra = [n for n in nodes_with_gw
                              if n.avg_gateways is not None and n.avg_gateways <= 1.0 and n.is_infrastructure]
            if single_gw_infra:
                lines.append(f"  Single-gateway INFRASTRUCTURE (critical - if gateway drops, these go dark):")
                for n in single_gw_infra:
                    name = _node_display_name(n.long_name, n.short_name, str(n.node_num))
                    region = n.region or "Unlocated"
                    src_info = f" via {n.sources[0]}" if len(n.sources) == 1 else ""
                    lines.append(f"    {name} [{region}]{src_info}")

        if health.has_traceroute_data:
            lines.append("")
            lines.append(f"NETWORK TOPOLOGY: {health.traceroute_count} traceroutes, avg {health.avg_hop_count:.1f} hops, max {health.max_hop_count}")

        lines.append(f"MQTT UPLINKS: {health.uplink_node_count} nodes")

        env_nodes = self.data_store.get_sensor_nodes("environment")
        if env_nodes:
            valid_temps = [n.temperature for n in env_nodes if _is_valid_temperature(n.temperature)]
            lines.append("")
            lines.append(f"SENSORS: {len(env_nodes)} environment")
            if valid_temps:
                lines.append(f"  Temp range: {min(valid_temps):.1f}-{max(valid_temps):.1f}C")

        pb = self._get_power_breakdown()
        if pb["total"] > 0:
            lines.append("")
            parts = []
            if pb["usb"]: parts.append(f"{pb['usb']} USB powered")
            if pb["ok"]: parts.append(f"{pb['ok']} battery ok")
            if pb["low"]: parts.append(f"{pb['low']} battery low")
            if pb["critical"]: parts.append(f"{pb['critical']} battery critical")
            lines.append(f"POWER (infra): {', '.join(parts)}")

        # Feeder gateway summary
        all_nodes = list(health.nodes.values())
        nodes_with_feeders = [n for n in all_nodes if getattr(n, 'feeder_count', 0) > 0]
        if nodes_with_feeders:
            total_unique_gw = set()
            for n in nodes_with_feeders:
                for gw in n.feeder_gateways:
                    total_unique_gw.add(gw["gateway_id"])

            avg_feeders = sum(n.feeder_count for n in nodes_with_feeders) / len(nodes_with_feeders)
            lines.append("")
            lines.append(f"FEEDERS: {len(total_unique_gw)} physical gateways, avg {avg_feeders:.1f} per node ({len(nodes_with_feeders)} nodes sampled)")

        # Source health section
        lines.extend(self._build_source_health_section())

        lines.append("")
        lines.append(f"TOTAL: {health.total_nodes} nodes across {health.total_regions} regions.")

        return "\n".join(lines)

    def _get_power_breakdown(self) -> dict:
        """Get power breakdown counts.

        Returns:
            Dict with usb, ok, low, critical, total counts
        """
        health = self.health_engine.mesh_health
        if not health:
            return {"usb": 0, "ok": 0, "low": 0, "critical": 0, "total": 0}

        usb = 0
        ok = 0
        low = 0
        critical = 0

        for node in health.nodes.values():
            if not node.is_infrastructure:
                continue  # Only track power for infrastructure
            if node.battery_percent is None:
                continue
            if node.battery_percent > 100:
                usb += 1
            elif node.battery_percent >= 50:
                ok += 1
            elif node.battery_percent >= 20:
                low += 1
            else:
                critical += 1

        return {
            "usb": usb,
            "ok": ok,
            "low": low,
            "critical": critical,
            "total": usb + ok + low + critical
        }

    def _get_traffic_trend_summary(self) -> str:
        """Get mesh-wide traffic trend from historical data.

        Returns:
            Trend string like "up 15% vs yesterday" or empty string
        """
        try:
            history = self._get_daily_traffic_history(days=3)
            if len(history) < 2:
                return ""

            # Compare today vs yesterday
            today = history.get("day_0", 0)
            yesterday = history.get("day_1", 0)

            if yesterday == 0:
                return ""

            pct_change = ((today - yesterday) / yesterday) * 100

            if abs(pct_change) < 5:
                return "stable"
            elif pct_change > 0:
                return f"up {pct_change:.0f}% vs yesterday"
            else:
                return f"down {abs(pct_change):.0f}% vs yesterday"

        except Exception as e:
            logger.debug(f"Traffic trend error: {e}")
            return ""

    def _get_daily_traffic_history(self, days: int = 7) -> dict:
        """Query SQLite for daily packet counts.

        Args:
            days: Number of days to look back

        Returns:
            Dict like {"day_0": 1234, "day_1": 1100, ...}
        """
        result = {}

        try:
            conn = self.data_store._history_conn
            if not conn:
                return result

            cursor = conn.cursor()

            # Get packet counts per day from packet_log
            for i in range(days):
                start_ts = time.time() - ((i + 1) * 86400)
                end_ts = time.time() - (i * 86400)

                cursor.execute("""
                    SELECT COUNT(*) FROM packet_log
                    WHERE timestamp >= ? AND timestamp < ?
                """, (start_ts, end_ts))

                row = cursor.fetchone()
                result[f"day_{i}"] = row[0] if row else 0

        except Exception as e:
            logger.debug(f"Daily traffic history error: {e}")

        return result

    def _gather_top_issues(self, health) -> list[str]:
        """Gather top issues across all pillars."""
        issues = []

        # Infrastructure issues (offline nodes)
        for region in health.regions:
            offline_infra = []
            for nid_str in region.node_ids:
                try:
                    nid = int(nid_str)
                except (ValueError, TypeError):
                    continue
                node = health.nodes.get(nid)
                if node and node.is_infrastructure and not node.is_online:
                    name = _node_display_name(node.long_name, node.short_name, str(nid))
                    offline_infra.append(name)
            if offline_infra:
                total_infra = 0
                for nid_str in region.node_ids:
                    try:
                        nid = int(nid_str)
                    except (ValueError, TypeError):
                        continue
                    node = health.nodes.get(nid)
                    if node and node.is_infrastructure:
                        total_infra += 1
                issues.append(
                    f"{region.name}: {len(offline_infra)}/{total_infra} infrastructure offline "
                    f"({', '.join(offline_infra[:3])})"
                )

        # Utilization issues
        for region in health.regions:
            if not region.node_ids:
                continue
            if region.score.util_percent >= 25:
                issues.append(
                    f"{region.name}: channel utilization at {region.score.util_percent:.0f}% (High)"
                )
            elif region.score.util_percent >= 20:
                issues.append(
                    f"{region.name}: channel utilization at {region.score.util_percent:.0f}% (Elevated)"
                )

        # Behavior issues (high packet nodes)
        flagged = self.health_engine.get_flagged_nodes()
        for node in flagged[:3]:
            threshold = self.health_engine.packet_threshold
            ratio = (node.packets_sent_24h - node.text_messages_24h) / threshold
            name = _node_display_name(node.long_name, node.short_name, str(node.node_num))
            issues.append(
                f"Node {name} sending "
                f"{(node.packets_sent_24h - node.text_messages_24h)} non-text packets/24h ({ratio:.1f}x threshold)"
            )

        # Battery issues (skip USB-powered nodes)
        battery_warnings = self.health_engine.get_battery_warnings()
        for node in battery_warnings[:2]:
            if node.battery_percent is not None and node.battery_percent <= 100:
                name = _node_display_name(node.long_name, node.short_name, str(node.node_num))
                issues.append(
                    f"Node {name} battery at {node.battery_percent:.0f}%"
                )

        return issues

    def build_region_detail(self, region_name: str) -> str:
        """Build detailed breakdown for a specific region."""
        health = self.health_engine.mesh_health
        if not health:
            return f"REGION DETAIL: {region_name}\nNo data available."

        # Find region (fuzzy match)
        region = self._find_region(region_name)
        if not region:
            return f"REGION DETAIL: {region_name}\nRegion not found."

        rs = region.score
        context = self._region_context(region.name)
        context_str = f" — {context}" if context else ""
        lines = [
            f"REGION DETAIL: {region.name}{context_str}",
            f"Score: {rs.composite:.0f}/100 ({rs.tier})",
            "",
            f"Infrastructure ({rs.infra_online}/{rs.infra_total}):",
        ]

        # Collect infrastructure nodes
        infra_nodes = []
        for nid_str in region.node_ids:
            try:
                nid = int(nid_str)
            except ValueError:
                continue
            node = health.nodes.get(nid)
            if node and node.is_infrastructure:
                infra_nodes.append((nid, node))

        # List infrastructure nodes with battery, packets, and utilization
        for nid, node in infra_nodes:
            status = "+" if node.is_online else "X"
            age = _format_age(node.last_heard)
            role = node.role or "ROUTER"
            hw = f", {node.hw_model}" if node.hw_model else ""

            # Use long name first format
            display_name = _node_display_name(node.long_name, node.short_name, nid)
            name_str = f"{display_name} ({role}{hw})"

            # Build metrics string with formatted battery
            metrics = []
            metrics.append(f"seen {age}")
            if node.battery_percent is not None:
                metrics.append(f"bat {_format_battery(node.battery_percent, node.voltage)}")
            if node.packets_sent_24h > 0:
                metrics.append(f"{node.packets_sent_24h} pkts/24h")
            if node.channel_utilization is not None:
                metrics.append(f"util {node.channel_utilization:.1f}%")
            # Add neighbor count from unified node
            if node.neighbor_count > 0:
                metrics.append(f"{node.neighbor_count} neighbors")

            line = f"  {status} {name_str} - {', '.join(metrics)}"
            if not node.is_online:
                line += " <- OFFLINE"
            lines.append(line)

        # Channel utilization by locality
        lines.append("")
        if health.has_packet_data or rs.util_data_available:
            lines.append(f"Channel Utilization: {rs.util_percent:.0f}%")
            if region.localities:
                lines.append("  Localities:")
                for loc in region.localities:
                    node_count = len(loc.node_ids)
                    lines.append(
                        f"    {loc.name}: {loc.score.util_percent:.0f}% - {node_count} nodes"
                    )
        else:
            lines.append("Channel Utilization: No data available")

        # MQTT uplink stats for region
        uplink_nodes = []
        for nid_str in region.node_ids:
            try:
                nid = int(nid_str)
            except (ValueError, TypeError):
                continue
            node = health.nodes.get(nid)
            if node and node.uplink_enabled:
                uplink_nodes.append(node)
        lines.append("")
        lines.append(f"MQTT Uplinks: {len(uplink_nodes)} nodes")

        # Coverage in region
        region_nodes_gw = [
            self.data_store.nodes.get(nid) for nid in region.node_ids
            if self.data_store.nodes.get(nid) and self.data_store.nodes.get(nid).avg_gateways is not None
        ]
        if region_nodes_gw:
            total_sources = len(self.data_store._sources)
            avg = sum(n.avg_gateways for n in region_nodes_gw) / len(region_nodes_gw)
            single = [n for n in region_nodes_gw if n.avg_gateways <= 1.0]
            full = [n for n in region_nodes_gw if n.avg_gateways >= total_sources]

            lines.append("")
            lines.append(f"Coverage ({len(region_nodes_gw)} nodes):")
            lines.append(f"  Avg gateways: {avg:.1f} / {total_sources}")
            lines.append(f"  Full coverage: {len(full)} nodes")
            if single:
                lines.append(f"  Single gateway ({len(single)}):")
                for n in single[:5]:
                    name = f"{n.long_name} ({n.short_name})" if n.long_name else n.short_name
                    src_info = f" via {n.sources[0]}" if len(n.sources) == 1 else ""
                    lines.append(f"    {name}{src_info}")
                if len(single) > 5:
                    lines.append(f"    ...and {len(single) - 5} more")

        # Flagged nodes in this region
        flagged_in_region = []
        for nid_str in region.node_ids:
            try:
                nid = int(nid_str)
            except ValueError:
                continue
            node = health.nodes.get(nid)
            if node and (node.packets_sent_24h - node.text_messages_24h) > self.health_engine.packet_threshold:
                flagged_in_region.append(node)

        if flagged_in_region:
            lines.append("")
            lines.append("Flagged Nodes (high packet senders):")
            for node in flagged_in_region[:5]:
                name = _node_display_name(node.long_name, node.short_name, str(node.node_num))
                lines.append(
                    f"  {name}: {(node.packets_sent_24h - node.text_messages_24h)} non-text pkts/24h"
                )

        # Power warnings in this region (skip USB-powered)
        low_bat = []
        for nid_str in region.node_ids:
            try:
                nid = int(nid_str)
            except ValueError:
                continue
            node = health.nodes.get(nid)
            if (
                node
                and node.battery_percent is not None
                and node.battery_percent <= 100  # Skip USB powered
                and node.battery_percent < self.health_engine.battery_warning_percent
            ):
                low_bat.append(node)

        if low_bat:
            lines.append("")
            lines.append("Power Warnings:")
            bat_str = ", ".join(
                f"{_node_display_name(n.long_name, n.short_name, n.node_id_hex)} at {n.battery_percent:.0f}%"
                for n in low_bat[:4]
            )
            lines.append(f"  Low battery: {bat_str}")

        # Regional environment summary
        env_nodes = [
            self.data_store.nodes.get(nid)
            for nid in region.node_ids
            if self.data_store.nodes.get(nid) and self.data_store.nodes[nid].has_environment_sensor
        ]
        env_nodes = [n for n in env_nodes if n]  # Filter None

        if env_nodes:
            # Filter outlier temperatures for aggregation
            temps = [n.temperature for n in env_nodes if _is_valid_temperature(n.temperature)]
            humids = [n.humidity for n in env_nodes if n.humidity is not None]
            pressures = [n.barometric_pressure for n in env_nodes if n.barometric_pressure is not None]

            lines.append("")
            lines.append(f"Environment ({len(env_nodes)} sensors):")
            if temps:
                avg_t = sum(temps) / len(temps)
                avg_f = avg_t * 9/5 + 32
                lines.append(f"  Temp: {min(temps):.1f}-{max(temps):.1f}C (avg {avg_t:.1f}C / {avg_f:.1f}F)")
            if humids:
                lines.append(f"  Humidity: {min(humids):.0f}-{max(humids):.0f}% (avg {sum(humids)/len(humids):.0f}%)")
            if pressures:
                lines.append(f"  Pressure: {min(pressures):.1f}-{max(pressures):.1f} hPa")

        # Air quality summary
        aq_nodes = [
            self.data_store.nodes.get(nid)
            for nid in region.node_ids
            if self.data_store.nodes.get(nid) and self.data_store.nodes[nid].has_air_quality_sensor
        ]
        aq_nodes = [n for n in aq_nodes if n]

        if aq_nodes:
            pm25s = [n.pm2_5 for n in aq_nodes if n.pm2_5 is not None]
            if pm25s:
                avg_pm = sum(pm25s) / len(pm25s)
                aqi_label = "Good" if avg_pm < 12 else "Moderate" if avg_pm < 35 else "Unhealthy"
                lines.append(f"  Air Quality: PM2.5 avg {avg_pm:.1f} ug/m3 ({aqi_label})")

        return "\n".join(lines)

    def build_node_detail(self, node_identifier: str) -> str:
        """Build detailed info for a specific node with historical data."""
        health = self.health_engine.mesh_health
        if not health:
            return f"NODE DETAIL: {node_identifier}\nNo data available."

        # Find node (multiple match strategies)
        node = self._find_node(node_identifier)
        if not node:
            return f"NODE DETAIL: {node_identifier}\nNode not found."

        # Get corresponding unified node from data store for historical data
        # All fields now directly on node (UnifiedNode)
        unified = node

        # Header with long name first
        display_name = _node_display_name(node.long_name, node.short_name, str(node.node_num))
        lines = [
            f"NODE DETAIL: {display_name}",
            f"ID: !{node.node_num:08x} (dec: {node.node_num})",
            f"Hardware: {node.hw_model or 'Unknown'}",
            f"Role: {node.role} ({'Infrastructure' if node.is_infrastructure else 'Client'})",
            f"Region: {node.region or 'Unknown'}{' — ' + self._region_context(node.region) if node.region and self._region_context(node.region) else ''} / Locality: {node.locality or 'Unknown'}",
        ]

        if node.latitude and node.longitude:
            lines.append(f"Position: {node.latitude:.4f}, {node.longitude:.4f}")

            # Distance from nearest infra node
            nearest_infra = None
            nearest_dist = float('inf')
            for n in self.health_engine.mesh_health.nodes.values():
                if n.is_infrastructure and n.latitude and n.longitude and n.node_num != node.node_num:
                    d = _haversine_km(node.latitude, node.longitude, n.latitude, n.longitude)
                    if d < nearest_dist:
                        nearest_dist = d
                        nearest_infra = n
            if nearest_infra and nearest_dist < 500:
                lines.append(f"  Nearest infra: {nearest_infra.short_name} at {_format_distance(nearest_dist)}")

        age = _format_age(node.last_heard)
        status = "Online" if node.is_online else "OFFLINE"
        lines.append(f"Last Seen: {age} ({status})")

        # Sources from unified node
        if node.sources:
            lines.append(f"Sources: {', '.join(unified.sources)}")

        # Traffic stats with historical data
        lines.append("")
        lines.append("Traffic History:")
        lines.append(f"  24h: {node.packets_sent_24h} pkts")
        if unified:
            lines.append(f"  48h: {unified.packets_sent_48h}")
            lines.append(f"  7d: {node.packets_sent_7d}")
            lines.append(f"  14d: {unified.packets_sent_14d}")

        # Packet breakdown with clean portnum names
        if node.packets_by_type:
            lines.append("")
            lines.append("Packet Breakdown (24h):")
            for portnum, count in sorted(
                node.packets_by_type.items(), key=lambda x: -x[1]
            )[:5]:
                clean_name = _clean_portnum(portnum)
                lines.append(f"  {clean_name}: {count}")

        # Estimated intervals
        pos_count = node.packets_by_type.get("POSITION_APP", 0)
        est_pos = 86400 / pos_count if pos_count > 0 else None
        if est_pos is not None:
            if est_pos < 60:
                interval_str = f"{int(est_pos)}s"
            else:
                interval_str = f"{int(est_pos / 60)}m"
            lines.append(f"  Est. position interval: {interval_str}")

        # RF Metrics section - distinguish channel util from TX airtime
        lines.append("")
        lines.append("RF Metrics:")
        if node.channel_utilization is not None:
            lines.append(f"  Channel Utilization: {node.channel_utilization:.1f}% (RF busyness this node hears)")
        else:
            lines.append("  Channel Utilization: N/A")
        if node.air_util_tx is not None:
            lines.append(f"  TX Airtime: {node.air_util_tx:.1f}% (this node's transmissions)")
        else:
            lines.append("  TX Airtime: N/A")

        # Power with battery trend and formatted display
        lines.append("")
        lines.append("Battery:")
        if node.battery_percent is not None:
            bat_display = _format_battery(node.battery_percent, node.voltage)
            lines.append(f"  Current: {bat_display}")
        else:
            lines.append("  Current: N/A")

        if node.battery_trend:
            lines.append(f"  Trend: {node.battery_trend}")
            if node.predicted_depletion_hours:
                lines.append(
                    f"  Predicted depletion: {node.predicted_depletion_hours:.0f} hours"
                )

        lines.append(f"  Solar: {'Yes' if node.has_solar else 'Unknown'}")

        # Connectivity
        lines.append("")
        lines.append("Connectivity:")
        if node.hops_away is not None:
            lines.append(f"  Hops: {node.hops_away}")
        lines.append(f"  MQTT Uplink: {'Enabled' if node.uplink_enabled else 'Disabled'}")

        # Coverage
        if node.avg_gateways is not None:
            total_gw = len(self.data_store._sources)
            pct = (node.avg_gateways / total_gw * 100) if total_gw > 0 else 0
            if node.avg_gateways >= total_gw:
                status = "Full"
            elif node.avg_gateways >= 2:
                status = "Partial"
            else:
                status = "Single gateway - node goes dark if that gateway fails"
            lines.append(f"  Coverage: {node.avg_gateways:.0f}/{total_gw} gateways ({pct:.0f}%) - {status}")

            # Source visibility
            if node.sources:
                lines.append(f"  Seen by: {', '.join(node.sources)} ({len(node.sources)} sources)")

            # Feeder gateways
            if node.feeder_gateways:
                lines.append(f"  Feeders ({node.feeder_count} gateways):")
                # Sort by signal strength (best RSSI first, closest to 0)
                sorted_gw = sorted(
                    node.feeder_gateways,
                    key=lambda g: g.get("avg_rssi") or -999,
                    reverse=True
                )
                for gw in sorted_gw[:8]:  # Top 8
                    name_str = gw.get("gateway_name") or gw["gateway_id"]
                    parts = []
                    if gw.get("avg_rssi") is not None:
                        parts.append(f"RSSI {gw['avg_rssi']:.0f}")
                    if gw.get("avg_snr") is not None:
                        parts.append(f"SNR {gw['avg_snr']:.1f}")
                    sig_str = f" [{', '.join(parts)}]" if parts else ""
                    lines.append(f"    {name_str}{sig_str}")
                if len(sorted_gw) > 8:
                    lines.append(f"    ...and {len(sorted_gw) - 8} more")

        # Neighbors section
        if node.neighbors:
            lines.append("")
            lines.append(f"Neighbors ({node.neighbor_count}):")

            # Build edge lookup for signal quality
            edge_lookup = {}
            for e in self.data_store.edges:
                edge_lookup[(e.from_node, e.to_node)] = e
                edge_lookup[(e.to_node, e.from_node)] = e

            # Build neighbor list with SNR for sorting
            neighbor_data = []
            for neighbor_num in unified.neighbors:
                neighbor = self.data_store.get_node(neighbor_num)
                edge = edge_lookup.get((node.node_num, neighbor_num))
                snr = edge.snr if edge else None
                rssi = edge.rssi if edge else None
                neighbor_data.append((neighbor_num, neighbor, snr, rssi))

            # Sort by best SNR first (None values last)
            neighbor_data.sort(key=lambda x: (x[2] is None, -(x[2] or -999)))

            # Show first 10
            for neighbor_num, neighbor, snr, rssi in neighbor_data[:10]:
                if neighbor:
                    name = _node_display_name(neighbor.long_name, neighbor.short_name, str(neighbor_num))
                else:
                    name = f"!{neighbor_num:08x} (unknown)"

                # Build signal info - only SNR and RSSI
                parts = []
                if snr is not None:
                    parts.append(f"SNR {snr:.1f}")
                if rssi is not None:
                    parts.append(f"RSSI {rssi}")

                if parts:
                    lines.append(f"  {name} [{', '.join(parts)}]")
                else:
                    lines.append(f"  {name}")

            if len(neighbor_data) > 10:
                lines.append(f"  ...and {len(neighbor_data) - 10} more")

        # Environment section (from unified node sensor data)
        if unified:
            env_lines = []
            if node.temperature is not None:
                temp_str = _format_temperature(node.temperature)
                env_lines.append(f"Temp: {temp_str}")
            if node.humidity is not None:
                env_lines.append(f"Humidity: {node.humidity:.1f}%")
            if unified.barometric_pressure is not None:
                env_lines.append(f"Pressure: {unified.barometric_pressure:.1f} hPa")
            if unified.gas_resistance is not None:
                env_lines.append(f"Gas Resistance: {unified.gas_resistance:.0f} Ohm")
            if unified.iaq is not None:
                iaq_label = "Good" if unified.iaq < 50 else "Moderate" if unified.iaq < 100 else "Poor"
                env_lines.append(f"IAQ: {unified.iaq:.0f} ({iaq_label})")
            if unified.light_lux is not None:
                env_lines.append(f"Light: {unified.light_lux:.0f} lux")
            if node.wind_speed is not None:
                env_lines.append(f"Wind: {node.wind_speed:.1f} m/s")
            if unified.wind_direction is not None:
                env_lines.append(f"Wind Dir: {unified.wind_direction:.0f} deg")
            if unified.rainfall is not None:
                env_lines.append(f"Rainfall: {unified.rainfall:.1f} mm")
            if node.pm2_5 is not None:
                aqi_label = "Good" if node.pm2_5 < 12 else "Moderate" if node.pm2_5 < 35 else "Unhealthy"
                env_lines.append(f"PM2.5: {node.pm2_5:.1f} ug/m3 ({aqi_label})")
            if unified.pm10 is not None:
                env_lines.append(f"PM10: {unified.pm10:.1f} ug/m3")
            if unified.ext_voltage is not None:
                env_lines.append(f"Ext Voltage: {unified.ext_voltage:.2f}V")
            if unified.ext_current is not None:
                env_lines.append(f"Ext Current: {unified.ext_current:.1f}mA")
            if unified.uv_index is not None:
                env_lines.append(f"UV Index: {unified.uv_index:.1f}")
            if unified.radiation_cpm is not None:
                env_lines.append(f"Radiation: {unified.radiation_cpm:.0f} CPM")

            if env_lines:
                lines.append("")
                lines.append("Environment:")
                for el in env_lines:
                    lines.append(f"  {el}")

        # Recommendations for this node (trend-aware)
        recs = self._node_recommendations(node)
        if recs:
            lines.append("")
            lines.append("Recommendations:")
            for rec in recs:
                lines.append(f"  - {rec}")

        return "\n".join(lines)

    def _node_recommendations(self, node: "UnifiedNode") -> list[str]:
        """Generate recommendations for a specific node.

        Args:
            node: UnifiedNode instance with all fields
        """
        recs = []

        # High packet count with trend context
        if (node.packets_sent_24h - node.text_messages_24h) > self.health_engine.packet_threshold:
            ratio = (node.packets_sent_24h - node.text_messages_24h) / self.health_engine.packet_threshold

            # Check if trending up
            trend_note = ""
            if node:
                avg_7d = node.packets_sent_7d / 7 if node.packets_sent_7d else 0
                if avg_7d > 0 and node.packets_sent_24h > avg_7d * 1.5:
                    trend_note = " (trending up vs 7d avg)"

            recs.append(
                f"Sending {ratio:.1f}x normal packets{trend_note}. Check position/telemetry intervals."
            )

        # Position interval too frequent (< 300s = 5 min)
        pos_count = node.packets_by_type.get("POSITION_APP", 0)
        est_interval = 86400 / pos_count if pos_count > 0 else None
        if est_interval is not None and est_interval < 300:
            recs.append(
                f"Position interval ~{int(est_interval)}s is aggressive. "
                f"Recommend 900s (15 min) for battery life."
            )

        # High channel utilization on this node (RF busyness it hears)
        if node.channel_utilization is not None and node.channel_utilization > 25:
            recs.append(
                f"Channel utilization {node.channel_utilization:.0f}% (RF busyness) - "
                f"this node's RF environment is congested."
            )

        # High air_util_tx (this node transmitting a lot)
        if node.air_util_tx is not None and node.air_util_tx > 10:
            recs.append(
                f"TX airtime {node.air_util_tx:.1f}% - "
                f"reduce telemetry frequency to be a better mesh citizen."
            )

        # Low battery (skip USB-powered)
        if node.battery_percent is not None and node.battery_percent <= 100 and node.battery_percent < 20:
            recs.append(
                f"Battery at {node.battery_percent:.0f}%. Consider charging or adding solar."
            )

        # Declining battery trend
        if node.battery_trend == "declining":
            hrs = node.predicted_depletion_hours
            if hrs and hrs < 48:
                recs.append(
                    f"Battery declining - estimated depletion in {hrs:.0f} hours."
                )

        # Offline
        if not node.is_online:
            age = _format_age(node.last_heard)
            recs.append(f"Node offline since {age}. Check power and connectivity.")

        # Infrastructure node without MQTT uplink
        if node.is_infrastructure and not node.uplink_enabled:
            recs.append(
                "Infrastructure node without MQTT uplink. "
                "Consider enabling for better mesh visibility."
            )

        # Environmental recommendations
        # Freezing temperature warning for battery nodes
        if node.temperature is not None and node.temperature < 0:
            if node.battery_percent is not None and node.battery_percent <= 100:
                recs.append(
                    f"Temperature {node.temperature:.1f}C - below freezing reduces battery capacity 20-40%."
                )

        # High humidity condensation risk
        if node.humidity is not None and node.humidity > 90:
            recs.append(
                f"Humidity at {node.humidity:.0f}% - condensation risk. Ensure enclosure is sealed."
            )

        # Poor air quality
        if node.pm2_5 is not None and node.pm2_5 > 35:
            recs.append(
                f"PM2.5 at {node.pm2_5:.1f} ug/m3 - unhealthy air quality in this area."
            )

        # High wind
        if node.wind_speed is not None and node.wind_speed > 20:
            recs.append(
                f"Wind speed {node.wind_speed:.1f} m/s - check antenna mounting and cable strain relief."
            )

        return recs

    def build_recommendations(self, scope: str, scope_value: str = None) -> str:
        """Generate actionable optimization recommendations."""
        health = self.health_engine.mesh_health
        if not health:
            return ""

        recs = []

        if scope == "node" and scope_value:
            node = self._find_node(scope_value)
            if node:
                recs.extend(self._node_recommendations(node))

        elif scope == "region" and scope_value:
            region = self._find_region(scope_value)
            if region:
                recs.extend(self._region_recommendations(region, health))

        else:  # mesh scope
            recs.extend(self._mesh_recommendations(health))

        if not recs:
            return ""

        lines = ["OPTIMIZATION RECOMMENDATIONS:"]
        for rec in recs[:10]:
            lines.append(f"  - {rec}")

        return "\n".join(lines)

    def _region_recommendations(
        self, region: "RegionHealth", health
    ) -> list[str]:
        """Generate recommendations for a region."""
        recs = []

        # High utilization with trend context
        if region.score.util_percent >= 20:
            recs.append(
                f"Channel utilization at {region.score.util_percent:.0f}%. "
                f"Consider spreading nodes across frequencies or reducing telemetry intervals."
            )

        # Offline infrastructure
        offline_count = region.score.infra_total - region.score.infra_online
        if offline_count > 0:
            recs.append(
                f"{offline_count} infrastructure node(s) offline. Check power and connectivity."
            )

        # Flagged nodes (high packet senders)
        flagged = []
        for nid_str in region.node_ids:
            try:
                nid = int(nid_str)
            except ValueError:
                continue
            node = health.nodes.get(nid)
            if node and (node.packets_sent_24h - node.text_messages_24h) > self.health_engine.packet_threshold:
                flagged.append(node)
        if flagged:
            names = ", ".join(
                _node_display_name(n.long_name, n.short_name, n.node_id_hex)
                for n in flagged[:3]
            )
            recs.append(
                f"High-traffic nodes ({names}) impacting channel. Review their telemetry settings."
            )

        # Check for nodes with aggressive position intervals
        aggressive_interval_nodes = []
        for nid_str in region.node_ids:
            try:
                nid = int(nid_str)
            except (ValueError, TypeError):
                continue
            node = health.nodes.get(nid)
            if node:
                pos_count = node.packets_by_type.get("POSITION_APP", 0)
                est = 86400 / pos_count if pos_count > 0 else None
                if est is not None and est < 300:
                    aggressive_interval_nodes.append(node)
        if aggressive_interval_nodes:
            names = ", ".join(
                _node_display_name(n.long_name, n.short_name, n.node_id_hex)
                for n in aggressive_interval_nodes[:3]
            )
            recs.append(
                f"Nodes with frequent position broadcasts ({names}). Recommend 900s interval."
            )

        # Check MQTT/uplink coverage in region
        infra_nodes = []
        for nid_str in region.node_ids:
            try:
                nid = int(nid_str)
            except (ValueError, TypeError):
                continue
            node = health.nodes.get(nid)
            if node and node.is_infrastructure:
                infra_nodes.append(node)
        uplink_count = sum(1 for n in infra_nodes if n and n.uplink_enabled)
        if infra_nodes and uplink_count == 0:
            recs.append(
                "No MQTT uplinks in region. Consider enabling on at least one infrastructure node."
            )
        elif len(infra_nodes) >= 3 and uplink_count == 1:
            recs.append(
                f"Only 1/{len(infra_nodes)} infrastructure nodes with MQTT uplink. "
                f"Consider adding redundancy."
            )

        return recs

    def _mesh_recommendations(self, health) -> list[str]:
        """Generate mesh-wide recommendations with specifics."""
        recs = []

        # Coverage gaps by region - be SPECIFIC
        for region in health.regions:
            region_nodes = []
            for nid_str in region.node_ids:
                try:
                    nid = int(nid_str)
                except (ValueError, TypeError):
                    continue
                node = health.nodes.get(nid)
                if node and node.avg_gateways is not None:
                    region_nodes.append(node)

            if not region_nodes:
                continue

            avg_gw = sum(n.avg_gateways for n in region_nodes) / len(region_nodes)
            single_gw = [n for n in region_nodes if n.avg_gateways <= 1.0]
            offline_infra = [n for n in region_nodes if n.is_infrastructure and not n.is_online]

            context = self._region_context(region.name)
            region_label = f"{region.name} ({context})" if context else region.name

            # Single-gateway concentration
            if len(single_gw) >= 3:
                recs.append(
                    f"Coverage gap in {region_label}: {len(single_gw)} nodes only reach 1 gateway. "
                    f"A new MQTT feeder in this area would add monitoring redundancy."
                )

            # Offline infrastructure
            if offline_infra:
                names = ", ".join(_node_display_name(n.long_name, n.short_name, str(n.node_num)) for n in offline_infra[:3])
                recs.append(
                    f"{region_label}: {len(offline_infra)} infrastructure offline ({names}). "
                    f"Restoring these would improve routing and coverage."
                )

        # Battery predictions
        critical = [n for n in health.nodes.values()
                    if n.battery_percent is not None and 0 < n.battery_percent < 10]
        for node in critical[:3]:
            name = _node_display_name(node.long_name, node.short_name, str(node.node_num))
            trend = f" and {node.battery_trend}" if node.battery_trend else ""
            recs.append(f"{name} at {node.battery_percent:.0f}% battery{trend}. Likely offline soon.")

        # High utilization
        high_util = [n for n in health.nodes.values()
                     if n.channel_utilization is not None and n.channel_utilization > 18]
        if high_util:
            names = ", ".join(_node_display_name(n.long_name, n.short_name, str(n.node_num)) for n in high_util[:3])
            recs.append(
                f"High channel utilization on {names}. "
                f"Check for aggressive broadcast intervals or nearby interference."
            )

        # MQTT uplink gaps
        for region in health.regions:
            infra_in_region = []
            for nid_str in region.node_ids:
                try:
                    nid = int(nid_str)
                except (ValueError, TypeError):
                    continue
                node = health.nodes.get(nid)
                if node and node.is_infrastructure:
                    infra_in_region.append(node)

            uplinks = [n for n in infra_in_region if n and n.uplink_enabled]
            if infra_in_region and not uplinks:
                context = self._region_context(region.name)
                recs.append(
                    f"No MQTT uplinks in {region.name} ({context}). "
                    f"Enable on at least one infrastructure node for monitoring visibility."
                )

        # Overall deliverability
        deliver = self.data_store.get_mesh_deliverability()
        if deliver.get("avg_gateways") is not None and deliver["avg_gateways"] < 2.0:
            recs.append(
                f"Mesh-wide average is {deliver['avg_gateways']:.1f} gateways per packet. "
                f"Adding MQTT feeders would improve monitoring reliability across the mesh."
            )

        return recs

    def build_lora_compact(self, scope: str, scope_value: str = None) -> list[str]:
        """Build LoRa-optimized summary. Returns list of messages (one per line)."""
        health = self.health_engine.mesh_health
        if not health:
            return ["📡 No mesh data yet."]

        if scope == "region" and scope_value:
            region = self._find_region(scope_value)
            if not region:
                return [f"Region '{scope_value}' not found."]
            return self._region_lora_compact(region)

        if scope == "node" and scope_value:
            return [self.build_node_compact(scope_value)]

        s = health.score

        # Color dot — no numbers
        if s.composite >= 100:
            dot = "🔵"
            mood = "perfect"
        elif s.composite >= 75:
            dot = "🟢"
            mood = "healthy"
        elif s.composite >= 50:
            dot = "🟠"
            mood = "needs attention"
        else:
            dot = "🔴"
            mood = "critical"

        lines = [f"📡 Mesh {dot} {mood}"]

        offline_infra = [n for n in health.nodes.values() if n.is_infrastructure and not n.is_online]
        if offline_infra:
            offline_names = ", ".join(
                (n.long_name or n.short_name or str(n.node_num)) for n in offline_infra[:3]
            )
            more = f" +{len(offline_infra)-3}" if len(offline_infra) > 3 else ""
            lines.append(f"🏗️ {s.infra_online}/{s.infra_total} routers up")
            lines.append(f"❌ Down: {offline_names}{more}")
        else:
            lines.append(f"🏗️ All {s.infra_total} routers up ✅")

        nodes_with_gw = [n for n in health.nodes.values() if n.avg_gateways is not None]
        if nodes_with_gw:
            total_sources = len(self.data_store._sources)
            full = sum(1 for n in nodes_with_gw if n.avg_gateways >= total_sources)
            single = sum(1 for n in nodes_with_gw if n.avg_gateways <= 1.0)
            if single > 0:
                lines.append(f"📶 {full} nodes full coverage, {single} on thin ice with 1 gw")
            else:
                lines.append(f"📶 {full} nodes full coverage ✅")

        high_util = [n for n in health.nodes.values()
                     if n.channel_utilization is not None and n.channel_utilization > 15]
        if high_util:
            worst = max(high_util, key=lambda n: n.channel_utilization)
            worst_name = worst.long_name or worst.short_name or str(worst.node_num)
            lines.append(f"🔥 {worst_name} running hot at {worst.channel_utilization:.0f}% util")

        infra_nodes = [n for n in health.nodes.values() if n.is_infrastructure]
        bat_low = [n for n in infra_nodes if n.battery_percent is not None and 0 < n.battery_percent < 20]
        if bat_low:
            low_names = ", ".join(n.long_name or n.short_name or str(n.node_num) for n in bat_low)
            lines.append(f"🔋 ⚠️ {low_names} low battery")
        else:
            lines.append(f"🔋 All infra powered up ✅")

        env_nodes = [n for n in health.nodes.values() if n.has_environment_sensor]
        if env_nodes:
            temps = [n.temperature for n in env_nodes if _is_valid_temperature(n.temperature)]
            if temps:
                lines.append(f"🌡️ {min(temps):.0f}–{max(temps):.0f}°C across {len(env_nodes)} sensors")

        region_parts = []
        for region in health.regions:
            if not region.node_ids:
                continue
            rs = region.score
            context = self._region_context(region.name)
            name = context.split("(")[0].strip() if context else region.name
            if rs.composite >= 100:
                region_parts.append(f"{name} 🔵")
            elif rs.composite >= 75:
                region_parts.append(f"{name} 🟢")
            elif rs.composite >= 50:
                region_parts.append(f"{name} 🟠")
            else:
                region_parts.append(f"{name} 🔴")
        if region_parts:
            lines.append(" | ".join(region_parts))

        # Pack lines into 2-3 messages under 220 bytes each
        MAX_BYTES = 220
        messages = []
        current_lines = []
        current_bytes = 0

        for line in lines:
            line_bytes = len(line.encode('utf-8'))
            separator_bytes = 1 if current_lines else 0

            if current_bytes + separator_bytes + line_bytes > MAX_BYTES and current_lines:
                messages.append("\n".join(current_lines))
                current_lines = [line]
                current_bytes = line_bytes
            else:
                current_lines.append(line)
                current_bytes += separator_bytes + line_bytes

        if current_lines:
            messages.append("\n".join(current_lines))

        return messages

    def _region_lora_compact(self, region) -> list[str]:
        """Compact region display. Returns list of messages."""
        rs = region.score
        context = self._region_context(region.name)
        name = context.split("(")[0].strip() if context else region.name

        if rs.composite >= 100:
            dot = "🔵"
        elif rs.composite >= 75:
            dot = "🟢"
        elif rs.composite >= 50:
            dot = "🟠"
        else:
            dot = "🔴"

        lines = [f"📡 {name} {dot}"]
        lines.append(f"🏗️ {rs.infra_online}/{rs.infra_total} infra | {rs.util_percent:.0f}% util")

        offline = []
        for nid_str in region.node_ids:
            try:
                nid = int(nid_str)
            except (ValueError, TypeError):
                continue
            node = self.health_engine.mesh_health.nodes.get(nid)
            if node and node.is_infrastructure and not node.is_online:
                offline.append(node.long_name or node.short_name or str(nid))
        if offline:
            lines.append(f"❌ Down: {', '.join(offline)}")

        return lines

    def list_regions_compact(self) -> str:
        """List all regions with scores for !region command."""
        health = self.health_engine.mesh_health
        if not health or not health.regions:
            return "No regions configured."
        lines = ["Regions:"]
        for region in health.regions:
            if not region.node_ids:
                continue
            s = region.score
            flag = _tier_flag(s.tier)
            context = self._region_context(region.name)
            name = context.split("(")[0].strip() if context else region.name
            lines.append(f"  {name}: {s.composite:.0f}/100{flag}")
        return "\n".join(lines)

    def build_region_compact(self, region_name: str) -> str:
        """Build compact region summary for subscription delivery."""
        region = self._find_region(region_name)
        if not region:
            return f"Region '{region_name}' not found"
        rs = region.score
        context = self._region_context(region.name)
        name = context.split("(")[0].strip() if context else region.name
        lines = [f"{name}: {rs.composite:.0f}/100"]
        lines.append(f"Infra: {rs.infra_online}/{rs.infra_total} | Util: {rs.util_percent:.0f}%")
        if rs.infra_online < rs.infra_total:
            lines.append(f"! {rs.infra_total - rs.infra_online} infra offline")
        return "\n".join(lines)

    def build_node_compact(self, node_identifier: str) -> str:
        """Build compact node summary for subscription delivery."""
        node = self._find_node(node_identifier)
        if not node:
            return f"Node '{node_identifier}' not found"
        name = node.long_name or node.short_name or str(node.node_num)
        status = "OK" if node.is_online else "OFFLINE"
        parts = [f"{name}: {status}"]
        if node.battery_percent is not None:
            parts.append(_format_battery(node.battery_percent, node.voltage))
        if node.packets_sent_24h > 0:
            parts.append(f"{node.packets_sent_24h} pkts/24h")
        if node.channel_utilization is not None:
            parts.append(f"util {node.channel_utilization:.1f}%")
        return " | ".join(parts)



    def build_distance(self, node1_id: str, node2_id: str) -> str:
        """Compute and format distance between two nodes."""
        n1 = self._find_node(node1_id)
        n2 = self._find_node(node2_id)

        if not n1:
            return f"Node '{node1_id}' not found."
        if not n2:
            return f"Node '{node2_id}' not found."

        if not n1.latitude or not n1.longitude:
            return f"{n1.short_name} has no GPS position."
        if not n2.latitude or not n2.longitude:
            return f"{n2.short_name} has no GPS position."

        km = _haversine_km(n1.latitude, n1.longitude, n2.latitude, n2.longitude)

        name1 = _node_display_name(n1.long_name, n1.short_name, str(n1.node_num))
        name2 = _node_display_name(n2.long_name, n2.short_name, str(n2.node_num))

        return f"{name1} ↔ {name2}: {_format_distance(km)}"
