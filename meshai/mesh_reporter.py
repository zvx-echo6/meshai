"""Mesh health reporting for LLM prompt injection and commands."""

import logging
import time
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


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


def _tier_flag(tier: str) -> str:
    """Get warning flag for health tier."""
    if tier == "Critical":
        return " !!"
    elif tier == "Warning":
        return " !"
    elif tier == "Unhealthy":
        return " !"
    return ""


class MeshReporter:
    """Builds text blocks for mesh health prompt injection."""

    def __init__(self, health_engine, source_manager):
        """Initialize reporter.

        Args:
            health_engine: MeshHealthEngine instance
            source_manager: MeshSourceManager instance
        """
        self.health_engine = health_engine
        self.source_manager = source_manager

    def build_tier1_summary(self) -> str:
        """Build compact mesh summary for LLM injection (~500-800 tokens).

        Returns:
            Formatted summary string
        """
        health = self.health_engine.mesh_health
        if not health:
            return "LIVE MESH HEALTH DATA: No data available yet."

        score = health.score
        ts = datetime.fromtimestamp(health.last_computed).strftime("%H:%M %Z")

        # Infrastructure stats
        infra_online = score.infra_online
        infra_total = score.infra_total
        infra_pct = int((infra_online / infra_total * 100) if infra_total > 0 else 100)

        # Utilization
        util = score.util_percent
        util_data_available = getattr(health, 'has_packet_data', False) or getattr(score, 'util_data_available', False)
        if not util_data_available:
            util_label = "N/A - no packet data"
        elif util < 15:
            util_label = "Low"
        elif util < 20:
            util_label = "Moderate"
        elif util < 25:
            util_label = "Elevated"
        else:
            util_label = "High"

        # Power
        if score.battery_warnings == 0:
            power_label = "Good"
        elif score.battery_warnings <= 2:
            power_label = "Some low batteries"
        else:
            power_label = f"{score.battery_warnings} low batteries"

        lines = [
            f"LIVE MESH HEALTH DATA (as of {ts}):",
            "",
            f"Overall: {score.composite:.0f}/100 ({score.tier})",
            f"Infrastructure: {infra_online}/{infra_total} online ({infra_pct}%)",
        ]

        # Channel Utilization with data availability
        if util_data_available:
            lines.append(f"Channel Utilization: {util:.1f}% avg ({util_label})")
        else:
            lines.append("Channel Utilization: No data available")

        lines.append(f"Node Behavior: {score.flagged_nodes} nodes flagged")
        lines.append(f"Power/Solar: {power_label} ({score.solar_index:.0f}% solar index)")

        # Network topology stats (if available)
        if health.has_traceroute_data:
            lines.append(f"Routing: {health.traceroute_count} traceroutes, avg {health.avg_hop_count:.1f} hops, max {health.max_hop_count}")
        else:
            lines.append("Routing: No traceroute data available")

        # MQTT uplink stats
        lines.append(f"MQTT Uplinks: {health.uplink_node_count} nodes")

        lines.append("")
        lines.append("Regions:")

        # Region summaries
        for region in health.regions:
            rs = region.score
            flag = _tier_flag(rs.tier)
            infra_str = f"{rs.infra_online}/{rs.infra_total} infra"
            lines.append(f"  {region.name}: {rs.composite:.0f}/100 - {infra_str}, {rs.util_percent:.0f}% util{flag}")

        # Top issues
        issues = self._gather_top_issues(health)
        if issues:
            lines.append("")
            lines.append("Top Issues:")
            for i, issue in enumerate(issues[:5], 1):
                lines.append(f"  {i}. {issue}")

        lines.append("")
        lines.append(f"{health.total_nodes} nodes across {health.total_regions} regions. User can ask about any region, locality, or node for details.")

        return "\n".join(lines)

    def _gather_top_issues(self, health) -> list[str]:
        """Gather top issues across all pillars."""
        issues = []

        # Infrastructure issues (offline nodes)
        for region in health.regions:
            offline_infra = []
            for nid in region.node_ids:
                node = health.nodes.get(nid)
                if node and node.is_infrastructure and not node.is_online:
                    offline_infra.append(node.short_name or nid[:4])
            if offline_infra:
                total_infra = sum(1 for nid in region.node_ids
                                  if health.nodes.get(nid) and health.nodes[nid].is_infrastructure)
                online = total_infra - len(offline_infra)
                issues.append(f"{region.name}: {online}/{total_infra} infrastructure nodes offline ({', '.join(offline_infra[:3])})")

        # Utilization issues
        for region in health.regions:
            if region.score.util_percent >= 25:
                issues.append(f"{region.name}: channel utilization at {region.score.util_percent:.0f}% (Warning)")
            elif region.score.util_percent >= 20:
                issues.append(f"{region.name}: channel utilization at {region.score.util_percent:.0f}% (Elevated)")

        # Behavior issues (high packet nodes)
        flagged = self.health_engine.get_flagged_nodes()
        for node in flagged[:3]:
            threshold = self.health_engine.packet_threshold
            ratio = node.non_text_packets / threshold
            issues.append(f"Node {node.short_name or node.node_id[:4]} sending {node.non_text_packets} non-text packets/24h ({ratio:.1f}x threshold)")

        # Battery issues
        battery_warnings = self.health_engine.get_battery_warnings()
        for node in battery_warnings[:2]:
            issues.append(f"Node {node.short_name or node.node_id[:4]} battery at {node.battery_percent:.0f}%")

        return issues

    def build_region_detail(self, region_name: str) -> str:
        """Build detailed breakdown for a specific region.

        Args:
            region_name: Region to get detail for

        Returns:
            Formatted region detail string
        """
        health = self.health_engine.mesh_health
        if not health:
            return f"REGION DETAIL: {region_name}\nNo data available."

        # Find region (fuzzy match)
        region = self._find_region(region_name)
        if not region:
            return f"REGION DETAIL: {region_name}\nRegion not found."

        rs = region.score
        lines = [
            f"REGION DETAIL: {region.name}",
            f"Score: {rs.composite:.0f}/100 ({rs.tier})",
            "",
            f"Infrastructure ({rs.infra_online}/{rs.infra_total}):",
        ]

        # Collect infrastructure nodes and detect duplicate shortnames
        infra_nodes = []
        for nid in region.node_ids:
            node = health.nodes.get(nid)
            if node and node.is_infrastructure:
                infra_nodes.append((nid, node))

        # Count shortname occurrences to detect duplicates
        shortname_counts: dict[str, int] = {}
        for nid, node in infra_nodes:
            sn = node.short_name or nid[:4]
            shortname_counts[sn] = shortname_counts.get(sn, 0) + 1

        # List infrastructure nodes with disambiguation for duplicates
        for nid, node in infra_nodes:
            status = "+" if node.is_online else "X"
            age = _format_age(node.last_seen)
            bat = f", bat {node.battery_percent:.0f}%" if node.battery_percent else ""
            role = node.role or "ROUTER"
            sn = node.short_name or nid[:4]

            # Disambiguate duplicate shortnames with node ID suffix
            if shortname_counts.get(sn, 0) > 1:
                # Use last 4 chars of node_id as disambiguator
                disambig = f", !{nid[-8:]}" if len(nid) >= 8 else f", {nid}"
                name_str = f"{sn} ({role}{disambig})"
            else:
                name_str = f"{sn} ({role})"

            lines.append(f"  {status} {name_str} - last seen {age}{bat}")
            if not node.is_online:
                lines[-1] += " <- OFFLINE"

        # Channel utilization by locality
        lines.append("")
        mesh_health = self.health_engine.mesh_health
        if mesh_health and mesh_health.has_packet_data:
            lines.append(f"Channel Utilization: {rs.util_percent:.0f}%")
            if region.localities:
                lines.append("  Localities:")
                for loc in region.localities:
                    node_count = len(loc.node_ids)
                    lines.append(f"    {loc.name}: {loc.score.util_percent:.0f}% - {node_count} nodes")
        else:
            lines.append("Channel Utilization: No data available")

        # MQTT uplink stats for region
        uplink_nodes = [health.nodes.get(nid) for nid in region.node_ids
                        if health.nodes.get(nid) and health.nodes[nid].uplink_enabled]
        lines.append("")
        lines.append(f"MQTT Uplinks: {len(uplink_nodes)} nodes")

        # Flagged nodes in this region
        flagged_in_region = []
        for nid in region.node_ids:
            node = health.nodes.get(nid)
            if node and node.non_text_packets > self.health_engine.packet_threshold:
                flagged_in_region.append(node)

        if flagged_in_region:
            lines.append("")
            lines.append("Flagged Nodes:")
            for node in flagged_in_region[:5]:
                lines.append(f"  {node.short_name or node.node_id[:4]}: {node.non_text_packets} non-text pkts/24h")

        # Power warnings in this region
        low_bat = []
        for nid in region.node_ids:
            node = health.nodes.get(nid)
            if node and node.battery_percent is not None and node.battery_percent < self.health_engine.battery_warning_percent:
                low_bat.append(node)

        if low_bat:
            lines.append("")
            lines.append("Power:")
            bat_str = ", ".join(f"{n.short_name or n.node_id[:4]} at {n.battery_percent:.0f}%" for n in low_bat[:4])
            lines.append(f"  Low battery: {bat_str}")

        return "\n".join(lines)

    def build_node_detail(self, node_identifier: str) -> str:
        """Build detailed info for a specific node.

        Args:
            node_identifier: Shortname, longname, nodeId, or nodeNum

        Returns:
            Formatted node detail string
        """
        health = self.health_engine.mesh_health
        if not health:
            return f"NODE DETAIL: {node_identifier}\nNo data available."

        # Find node (multiple match strategies)
        node = self._find_node(node_identifier)
        if not node:
            return f"NODE DETAIL: {node_identifier}\nNode not found."

        lines = [
            f"NODE DETAIL: {node.long_name or node.short_name} ({node.short_name})",
            f"ID: {node.node_id}",
            f"Hardware: {node.role or 'Unknown'}",
            f"Role: {'Infrastructure' if node.is_infrastructure else 'Client'}",
            f"Region: {node.region or 'Unknown'} / Locality: {node.locality or 'Unknown'}",
        ]

        if node.latitude and node.longitude:
            lines.append(f"Position: {node.latitude:.4f}, {node.longitude:.4f}")

        age = _format_age(node.last_seen)
        status = "Online" if node.is_online else "OFFLINE"
        lines.append(f"Last Seen: {age} ({status})")

        # Get source info from source manager
        all_nodes = self.source_manager.get_all_nodes()
        sources = []
        for n in all_nodes:
            nid = str(n.get("id") or n.get("nodeId") or n.get("num") or "")
            if nid == node.node_id:
                sources = n.get("_sources", [])
                break
        if sources:
            lines.append(f"Sources: {', '.join(sources)}")

        # Traffic stats
        lines.append("")
        lines.append("Traffic (24h):")
        lines.append(f"  Total packets: {node.packet_count_24h}")
        lines.append(f"  Text messages: {node.text_packet_count_24h}")
        lines.append(f"  Position: {node.position_packet_count_24h}")
        lines.append(f"  Telemetry: {node.telemetry_packet_count_24h}")
        lines.append(f"  Other non-text: {node.non_text_packets - node.position_packet_count_24h - node.telemetry_packet_count_24h}")

        # Estimated intervals
        est_pos = node.estimated_position_interval
        if est_pos is not None:
            if est_pos < 60:
                interval_str = f"{int(est_pos)}s"
            else:
                interval_str = f"{int(est_pos / 60)}m"
            lines.append(f"  Est. position interval: {interval_str}")

        # Channel utilization from device telemetry
        if node.channel_utilization is not None:
            lines.append(f"  Channel util (device): {node.channel_utilization:.1f}%")
        if node.air_util_tx is not None:
            lines.append(f"  TX airtime: {node.air_util_tx:.1f}%")

        # Power
        lines.append("")
        lines.append("Power:")
        if node.battery_percent is not None:
            bat_status = "Low" if node.battery_percent < 20 else "OK"
            lines.append(f"  Battery: {node.battery_percent:.0f}% ({bat_status})")
        else:
            lines.append("  Battery: N/A")
        if node.voltage:
            lines.append(f"  Voltage: {node.voltage:.2f}V")
        lines.append(f"  Solar: {'Yes' if node.has_solar else 'Unknown'}")

        # Connectivity
        lines.append("")
        lines.append("Connectivity:")
        lines.append(f"  MQTT Uplink: {'Enabled' if node.uplink_enabled else 'Disabled'}")

        # Recommendations for this node
        recs = self._node_recommendations(node)
        if recs:
            lines.append("")
            lines.append("Recommendations:")
            for rec in recs:
                lines.append(f"  - {rec}")

        return "\n".join(lines)

    def _node_recommendations(self, node) -> list[str]:
        """Generate recommendations for a specific node."""
        recs = []

        # High packet count
        if node.non_text_packets > self.health_engine.packet_threshold:
            ratio = node.non_text_packets / self.health_engine.packet_threshold
            recs.append(f"Sending {ratio:.1f}x normal packets. Check position/telemetry intervals.")

        # Position interval too frequent (< 300s = 5 min)
        est_interval = node.estimated_position_interval
        if est_interval is not None and est_interval < 300:
            recs.append(f"Position interval ~{int(est_interval)}s is aggressive. Recommend 900s (15 min) for battery life.")

        # High channel utilization on this node
        if node.channel_utilization is not None and node.channel_utilization > 25:
            recs.append(f"Channel utilization {node.channel_utilization:.0f}% - consider moving to less congested frequency.")

        # High air_util_tx (this node transmitting a lot)
        if node.air_util_tx is not None and node.air_util_tx > 10:
            recs.append(f"TX airtime {node.air_util_tx:.1f}% - reduce telemetry frequency to be a better mesh citizen.")

        # Low battery
        if node.battery_percent is not None and node.battery_percent < 20:
            recs.append(f"Battery at {node.battery_percent:.0f}%. Consider charging or adding solar.")

        # Offline
        if not node.is_online:
            age = _format_age(node.last_seen)
            recs.append(f"Node offline since {age}. Check power and connectivity.")

        # Infrastructure node without MQTT uplink
        if node.is_infrastructure and not node.uplink_enabled:
            recs.append("Infrastructure node without MQTT uplink. Consider enabling for better mesh visibility.")

        return recs

    def build_recommendations(self, scope: str, scope_value: str = None) -> str:
        """Generate actionable optimization recommendations.

        Args:
            scope: "mesh", "region", or "node"
            scope_value: Region name or node identifier (for scoped recommendations)

        Returns:
            Formatted recommendations string
        """
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
        for rec in recs[:5]:
            lines.append(f"  - {rec}")

        return "\n".join(lines)

    def _region_recommendations(self, region, health) -> list[str]:
        """Generate recommendations for a region."""
        recs = []

        # High utilization
        if region.score.util_percent >= 20:
            recs.append(f"Channel utilization at {region.score.util_percent:.0f}%. Consider spreading nodes across frequencies or reducing telemetry intervals.")

        # Offline infrastructure
        offline_count = region.score.infra_total - region.score.infra_online
        if offline_count > 0:
            recs.append(f"{offline_count} infrastructure node(s) offline. Check power and connectivity.")

        # Flagged nodes
        flagged = []
        for nid in region.node_ids:
            node = health.nodes.get(nid)
            if node and node.non_text_packets > self.health_engine.packet_threshold:
                flagged.append(node)
        if flagged:
            names = ", ".join(n.short_name or n.node_id[:4] for n in flagged[:3])
            recs.append(f"High-traffic nodes ({names}) impacting channel. Review their telemetry settings.")

        # Check for nodes with aggressive position intervals
        aggressive_interval_nodes = []
        for nid in region.node_ids:
            node = health.nodes.get(nid)
            if node:
                est = node.estimated_position_interval
                if est is not None and est < 300:
                    aggressive_interval_nodes.append(node)
        if aggressive_interval_nodes:
            names = ", ".join(n.short_name or n.node_id[:4] for n in aggressive_interval_nodes[:3])
            recs.append(f"Nodes with frequent position broadcasts ({names}). Recommend 900s interval.")

        # Check MQTT/uplink coverage in region
        infra_nodes = [health.nodes.get(nid) for nid in region.node_ids
                       if health.nodes.get(nid) and health.nodes[nid].is_infrastructure]
        uplink_count = sum(1 for n in infra_nodes if n and n.uplink_enabled)
        if infra_nodes and uplink_count == 0:
            recs.append("No MQTT uplinks in region. Consider enabling on at least one infrastructure node.")
        elif len(infra_nodes) >= 3 and uplink_count == 1:
            recs.append(f"Only 1/{len(infra_nodes)} infrastructure nodes with MQTT uplink. Consider adding redundancy.")

        return recs

    def _mesh_recommendations(self, health) -> list[str]:
        """Generate mesh-wide recommendations."""
        recs = []

        # Overall utilization
        if health.score.util_percent >= 20:
            recs.append(f"Mesh-wide utilization at {health.score.util_percent:.0f}%. Consider reducing position/telemetry broadcast frequency.")

        # Multiple regions with issues
        problem_regions = [r for r in health.regions if r.score.composite < 75]
        if len(problem_regions) > 1:
            names = ", ".join(r.name for r in problem_regions[:3])
            recs.append(f"Multiple regions degraded ({names}). Prioritize infrastructure improvements.")

        # High packet nodes mesh-wide
        flagged = self.health_engine.get_flagged_nodes()
        if len(flagged) > 3:
            total_excess = sum(n.non_text_packets - self.health_engine.packet_threshold for n in flagged)
            recs.append(f"{len(flagged)} nodes exceeding packet threshold ({total_excess} excess packets/day). Review default telemetry intervals.")

        # Battery warnings
        battery_warnings = self.health_engine.get_battery_warnings()
        if len(battery_warnings) > 2:
            recs.append(f"{len(battery_warnings)} nodes with low battery. Consider solar additions for remote nodes.")

        # Hop count recommendation from traceroutes
        if health.has_traceroute_data:
            if health.avg_hop_count > 4:
                recs.append(f"Average hop count {health.avg_hop_count:.1f} is high. Consider adding infrastructure to reduce latency.")
            elif health.max_hop_count > 6:
                recs.append(f"Max hop count {health.max_hop_count} indicates long routes. Strategic node placement could improve reach.")

        # MQTT uplink coverage
        if health.uplink_node_count == 0:
            total_infra = sum(1 for n in health.nodes.values() if n.is_infrastructure)
            if total_infra > 0:
                recs.append("No MQTT uplinks detected. Enable on infrastructure nodes for better mesh visibility.")
        elif health.total_regions > 0:
            uplinks_per_region = health.uplink_node_count / health.total_regions
            if uplinks_per_region < 1:
                recs.append(f"Only {health.uplink_node_count} MQTT uplinks across {health.total_regions} regions. Consider adding redundancy.")

        # Aggressive position intervals mesh-wide
        aggressive_nodes = [n for n in health.nodes.values()
                           if n.estimated_position_interval is not None and n.estimated_position_interval < 300]
        if len(aggressive_nodes) > 5:
            recs.append(f"{len(aggressive_nodes)} nodes with position interval <5min. Recommend 15min (900s) as default.")

        return recs

    def build_lora_compact(self, scope: str, scope_value: str = None) -> str:
        """Build LoRa-optimized compact summary (~200 chars).

        Args:
            scope: "mesh" or "region"
            scope_value: Region name if scope is "region"

        Returns:
            Compact formatted string
        """
        health = self.health_engine.mesh_health
        if not health:
            return "Mesh: No data"

        if scope == "region" and scope_value:
            region = self._find_region(scope_value)
            if not region:
                return f"Region '{scope_value}' not found"
            rs = region.score
            return f"{region.name} {rs.composite:.0f}/100 | {rs.infra_online}/{rs.infra_total} infra | {rs.util_percent:.0f}% util"

        # Mesh summary
        s = health.score
        lines = [f"Mesh {s.composite:.0f}/100 | {s.infra_online}/{s.infra_total} infra | {s.util_percent:.0f}% util"]

        # Add warnings for problem regions/nodes
        warnings = []
        for region in health.regions:
            if region.score.composite < 60:
                offline = region.score.infra_total - region.score.infra_online
                warnings.append(f"! {region.name} {region.score.composite:.0f}/100 - {offline} infra offline")

        battery_warnings = self.health_engine.get_battery_warnings()
        for node in battery_warnings[:2]:
            warnings.append(f"! {node.short_name or node.node_id[:4]} bat {node.battery_percent:.0f}%")

        for w in warnings[:2]:
            lines.append(w)

        return "\n".join(lines)

    def _find_region(self, name: str):
        """Find a region by fuzzy name match."""
        health = self.health_engine.mesh_health
        if not health:
            return None

        name_lower = name.lower().strip()

        # Exact match first
        for region in health.regions:
            if region.name.lower() == name_lower:
                return region

        # Substring match
        for region in health.regions:
            if name_lower in region.name.lower():
                return region

        # Try matching against anchor city names
        for anchor in self.health_engine.regions:
            # Check if search term matches anchor city or region name
            anchor_name_lower = anchor.name.lower()
            if name_lower in anchor_name_lower:
                # Find the corresponding region
                for region in health.regions:
                    if region.name == anchor.name:
                        return region

        return None

    def _find_node(self, identifier: str):
        """Find a node by shortname, longname, nodeId, or nodeNum."""
        health = self.health_engine.mesh_health
        if not health:
            return None

        identifier = identifier.strip()
        id_lower = identifier.lower()

        # Try shortname (case-insensitive)
        for node in health.nodes.values():
            if node.short_name and node.short_name.lower() == id_lower:
                return node

        # Try longname (substring)
        for node in health.nodes.values():
            if node.long_name and id_lower in node.long_name.lower():
                return node

        # Try exact nodeId
        if identifier in health.nodes:
            return health.nodes[identifier]

        # Try hex nodeId with ! prefix
        if identifier.startswith("!"):
            hex_id = identifier[1:]
            for nid, node in health.nodes.items():
                if nid.lower() == hex_id.lower():
                    return node

        # Try decimal nodeNum
        if identifier.isdigit():
            # Convert to hex and search
            try:
                hex_id = format(int(identifier), 'x')
                for nid, node in health.nodes.items():
                    if hex_id in nid.lower():
                        return node
            except ValueError:
                pass

        return None

    def list_regions_compact(self) -> str:
        """List all regions with scores in compact format."""
        health = self.health_engine.mesh_health
        if not health or not health.regions:
            return "No regions configured."

        lines = ["Regions:"]
        for region in health.regions:
            s = region.score
            flag = _tier_flag(s.tier)
            lines.append(f"  {region.name}: {s.composite:.0f}/100{flag}")

        return "\n".join(lines)
