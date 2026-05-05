"""Mesh health scoring engine.

Computes four-pillar health scores at every hierarchy level:
- Infrastructure Uptime (40%)
- Channel Utilization (25%)
- Node Behavior (20%)
- Power Health (15%)
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from .geo import (
    cluster_by_distance,
    get_cluster_center,
    haversine_distance,
)

logger = logging.getLogger(__name__)

# Infrastructure roles (auto-detected)
INFRASTRUCTURE_ROLES = {"ROUTER", "ROUTER_LATE", "ROUTER_CLIENT"}

# Default thresholds
DEFAULT_LOCALITY_RADIUS_MILES = 8.0
DEFAULT_OFFLINE_THRESHOLD_HOURS = 24
DEFAULT_PACKET_THRESHOLD = 500  # Non-text packets per 24h
DEFAULT_BATTERY_WARNING_PERCENT = 20

# Utilization thresholds (percentage)
UTIL_HEALTHY = 15
UTIL_CAUTION = 20
UTIL_WARNING = 25
UTIL_UNHEALTHY = 35

# Pillar weights (5-pillar system)
WEIGHT_INFRASTRUCTURE = 0.30
WEIGHT_UTILIZATION = 0.25
WEIGHT_COVERAGE = 0.20
WEIGHT_BEHAVIOR = 0.15
WEIGHT_POWER = 0.10


@dataclass
class HealthScore:
    """Health score for a single entity (mesh, region, locality, node)."""

    infrastructure: float = 100.0  # 0-100
    utilization: float = 100.0  # 0-100
    coverage: float = 100.0  # 0-100 (NEW: 5th pillar)
    behavior: float = 100.0  # 0-100
    power: float = 100.0  # 0-100

    # Underlying metrics
    infra_online: int = 0
    infra_total: int = 0
    util_percent: float = 0.0
    coverage_avg_gateways: float = 0.0
    coverage_single_gw_count: int = 0
    coverage_full_count: int = 0
    flagged_nodes: int = 0
    battery_warnings: int = 0
    solar_index: float = 100.0

    # Flag to indicate if utilization data is available
    util_data_available: bool = False
    coverage_data_available: bool = False

    @property
    def composite(self) -> float:
        """Calculate weighted composite score."""
        return (
            self.infrastructure * WEIGHT_INFRASTRUCTURE +
            self.utilization * WEIGHT_UTILIZATION +
            self.coverage * WEIGHT_COVERAGE +
            self.behavior * WEIGHT_BEHAVIOR +
            self.power * WEIGHT_POWER
        )

    @property
    def tier(self) -> str:
        """Get health tier label."""
        score = self.composite
        if score >= 90:
            return "Healthy"
        elif score >= 75:
            return "Slight degradation"
        elif score >= 50:
            return "Unhealthy"
        elif score >= 25:
            return "Warning"
        else:
            return "Critical"


@dataclass
class NodeHealth:
    """Health data for a single node."""

    node_id: str
    short_name: str = ""
    long_name: str = ""
    role: str = ""
    hw_model: str = ""
    is_infrastructure: bool = False
    last_seen: float = 0.0
    is_online: bool = True

    # Location
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    region: str = ""
    locality: str = ""

    # Metrics
    packet_count_24h: int = 0
    text_packet_count_24h: int = 0
    position_packet_count_24h: int = 0
    telemetry_packet_count_24h: int = 0
    battery_percent: Optional[float] = None
    voltage: Optional[float] = None
    channel_utilization: Optional[float] = None  # From device telemetry
    air_util_tx: Optional[float] = None  # From device telemetry
    has_solar: bool = False
    uplink_enabled: bool = False
    neighbor_count: int = 0
    packets_sent_24h: int = 0

    # Packet breakdown by portnum
    packets_by_portnum: dict[str, int] = field(default_factory=dict)

    # Scores
    score: HealthScore = field(default_factory=HealthScore)

    @property
    def node_num(self) -> int:
        """Convert node_id hex string to integer."""
        if self.node_id.startswith("!"): 
            return int(self.node_id[1:], 16)
        return int(self.node_id, 16)

    @property
    def non_text_packets(self) -> int:
        """Non-text packets in 24h."""
        return self.packet_count_24h - self.text_packet_count_24h

    @property
    def estimated_position_interval(self) -> Optional[float]:
        """Estimate position broadcast interval in seconds."""
        if self.position_packet_count_24h > 0:
            return 86400 / self.position_packet_count_24h
        return None

    @property
    def node_id_hex(self) -> str:
        """Return node_id in hex format with ! prefix."""
        if self.node_id.startswith("!"):
            return self.node_id
        try:
            return f"!{int(self.node_id):08x}"
        except:
            return self.node_id

    @property
    def battery_trend(self) -> str:
        """Return battery trend indicator."""
        return ""  # Not tracked yet

    @property
    def packets_by_type(self) -> dict:
        """Alias for packets_by_portnum."""
        return self.packets_by_portnum

    @property
    def predicted_depletion_hours(self) -> Optional[float]:
        """Predict hours until battery depletion."""
        return None  # Not tracked yet


@dataclass
class LocalityHealth:
    """Health data for a locality (sub-region cluster)."""

    name: str
    center_lat: float = 0.0
    center_lon: float = 0.0
    node_ids: list[str] = field(default_factory=list)
    score: HealthScore = field(default_factory=HealthScore)


@dataclass
class RegionHealth:
    """Health data for a region."""

    name: str
    center_lat: float = 0.0
    center_lon: float = 0.0
    localities: list[LocalityHealth] = field(default_factory=list)
    node_ids: list[str] = field(default_factory=list)
    score: HealthScore = field(default_factory=HealthScore)


@dataclass
class MeshHealth:
    """Health data for the entire mesh."""

    regions: list[RegionHealth] = field(default_factory=list)
    unlocated_nodes: list[str] = field(default_factory=list)
    nodes: dict[str, NodeHealth] = field(default_factory=dict)
    score: HealthScore = field(default_factory=HealthScore)
    last_computed: float = 0.0

    # Data availability flags for reporting
    has_packet_data: bool = False
    has_telemetry_data: bool = False
    has_traceroute_data: bool = False
    has_channel_data: bool = False

    # Traceroute statistics
    traceroute_count: int = 0
    avg_hop_count: float = 0.0
    max_hop_count: int = 0

    # MQTT/uplink statistics
    uplink_node_count: int = 0

    @property
    def total_nodes(self) -> int:
        return len(self.nodes)

    @property
    def total_regions(self) -> int:
        return len(self.regions)


@dataclass
class RegionAnchor:
    """A fixed region anchor point for assignment."""
    name: str
    lat: float
    lon: float


class MeshHealthEngine:
    """Computes mesh health scores from aggregated source data."""

    def __init__(
        self,
        regions: Optional[list] = None,
        locality_radius: float = DEFAULT_LOCALITY_RADIUS_MILES,
        offline_threshold_hours: int = DEFAULT_OFFLINE_THRESHOLD_HOURS,
        packet_threshold: int = DEFAULT_PACKET_THRESHOLD,
        battery_warning_percent: int = DEFAULT_BATTERY_WARNING_PERCENT,
    ):
        """Initialize health engine.

        Args:
            regions: List of region anchors (dicts or RegionAnchor with name, lat, lon)
            locality_radius: Miles radius for locality clustering within regions
            offline_threshold_hours: Hours before a node is considered offline
            packet_threshold: Non-text packets per 24h to flag a node
            battery_warning_percent: Battery level for warnings
        """
        # Convert region configs to RegionAnchor objects
        self.regions: list[RegionAnchor] = []
        if regions:
            for r in regions:
                if hasattr(r, 'name'):
                    self.regions.append(RegionAnchor(r.name, r.lat, r.lon))
                elif isinstance(r, dict):
                    self.regions.append(RegionAnchor(r['name'], r['lat'], r['lon']))

        self.locality_radius = locality_radius
        self.offline_threshold_hours = offline_threshold_hours
        self.packet_threshold = packet_threshold
        self.battery_warning_percent = battery_warning_percent

        self._mesh_health: Optional[MeshHealth] = None

    @property
    def mesh_health(self) -> Optional[MeshHealth]:
        """Get last computed mesh health."""
        return self._mesh_health

    def _find_nearest_region(self, lat: float, lon: float) -> Optional[str]:
        """Find the nearest region anchor to a GPS point.

        Args:
            lat: Latitude
            lon: Longitude

        Returns:
            Region name or None if no regions defined
        """
        if not self.regions:
            return None

        nearest = None
        min_dist = float("inf")

        for region in self.regions:
            dist = haversine_distance(lat, lon, region.lat, region.lon)
            if dist < min_dist:
                min_dist = dist
                nearest = region.name

        return nearest

    def compute(self, data_store) -> MeshHealth:
        """Compute mesh health from data store.

        Args:
            data_store: MeshDataStore with aggregated mesh data

        Returns:
            MeshHealth with computed scores
        """
        # Store data_store reference for coverage calculations
        self.data_store = data_store
        source_manager = data_store  # Alias for backwards compat with method body
        now = time.time()
        offline_threshold = now - (self.offline_threshold_hours * 3600)

        # Aggregate all nodes from all sources
        all_nodes = source_manager.get_all_nodes()
        all_telemetry = source_manager.get_all_telemetry()

        # FIX: Use aggregator method for deduped packets
        all_packets = source_manager.get_all_packets()

        # Track if we have packet data for utilization calculation
        has_packet_data = len(all_packets) > 0

        # Build node health records
        # BUG 2 FIX: Use _node_num as the canonical key
        nodes: dict[str, NodeHealth] = {}
        for node in all_nodes:
            # Use _node_num set by source manager (canonical Meshtastic node number)
            node_num = node.get("_node_num")
            if node_num is not None:
                node_id = str(node_num)
            else:
                # Fallback for nodes without _node_num
                node_id = node.get("nodeNum") or node.get("id") or node.get("nodeId") or node.get("num")
                if not node_id:
                    continue
                node_id = str(node_id)

            # Skip if we already have this node from another source
            if node_id in nodes:
                continue

            # Extract fields (handle different API formats)
            short_name = node.get("shortName") or node.get("short_name") or ""
            long_name = node.get("longName") or node.get("long_name") or ""
            role = node.get("role") or ""
            hw_model = node.get("hwModel") or node.get("hw_model") or ""

            # Determine if infrastructure
            is_infra = str(role).upper() in INFRASTRUCTURE_ROLES

            # Get position (handle different API formats)
            lat = node.get("latitude") or node.get("lat")
            lon = node.get("longitude") or node.get("lon")
            # Handle nested position object
            if lat is None and "position" in node:
                pos = node["position"]
                lat = pos.get("latitude") or pos.get("lat")
                lon = pos.get("longitude") or pos.get("lon")
            # Handle Meshview scaled integer format (last_lat/last_long)
            if lat is None:
                lat = node.get("last_lat")
                lon = node.get("last_long")
                # Meshview uses 1e7 scaling for GPS coordinates
                if lat is not None and isinstance(lat, int) and abs(lat) > 1000:
                    lat = lat / 1e7
                if lon is not None and isinstance(lon, int) and abs(lon) > 1000:
                    lon = lon / 1e7

            # Get last seen (handle different timestamp formats)
            last_seen = node.get("lastHeard") or node.get("last_heard") or node.get("lastSeen") or 0
            # Handle Meshview microsecond timestamps
            if not last_seen:
                last_seen_us = node.get("last_seen_us")
                if last_seen_us:
                    last_seen = last_seen_us / 1e6  # Convert microseconds to seconds
            if isinstance(last_seen, str):
                try:
                    from datetime import datetime
                    last_seen = datetime.fromisoformat(last_seen.replace("Z", "+00:00")).timestamp()
                except:
                    last_seen = 0

            is_online = last_seen > offline_threshold if last_seen else False

            nodes[node_id] = NodeHealth(
                node_id=node_id,
                short_name=short_name,
                long_name=long_name,
                role=role,
                hw_model=hw_model,
                is_infrastructure=is_infra,
                last_seen=last_seen,
                is_online=is_online,
                latitude=lat,
                longitude=lon,
            )

        # Add telemetry data
        # BUG 4 & 5 FIX: Handle MeshMonitor telemetryType/value structure
        for telem in all_telemetry:
            # Get node number - try decimal first, then hex
            node_num = telem.get("nodeNum")
            if node_num is not None:
                node_id = str(int(node_num))
            else:
                node_hex = telem.get("nodeId") or telem.get("node_id") or ""
                if isinstance(node_hex, str) and node_hex:
                    stripped = node_hex.lstrip("!")
                    try:
                        node_id = str(int(stripped, 16))
                    except ValueError:
                        continue
                else:
                    continue

            if node_id not in nodes:
                continue

            node = nodes[node_id]

            # Handle MeshMonitor telemetryType/value structure
            telem_type = (telem.get("telemetryType") or "").lower()
            value = telem.get("value")

            if telem_type and value is not None:
                try:
                    value = float(value)
                except (ValueError, TypeError):
                    value = None

                if value is not None:
                    if telem_type in ("batterylevel", "battery_level", "battery"):
                        node.battery_percent = value
                    elif telem_type == "voltage":
                        node.voltage = value
                    elif telem_type in ("channelutilization", "channel_utilization"):
                        node.channel_utilization = value
                    elif telem_type in ("airutiltx", "air_util_tx"):
                        node.air_util_tx = value
                    elif telem_type in ("uplinkenabled", "uplink_enabled"):
                        node.uplink_enabled = bool(value)

            # Also try direct field access as fallback (for flat telemetry objects)
            if node.battery_percent is None:
                bat = telem.get("batteryLevel") or telem.get("battery_level")
                if bat is not None:
                    try:
                        node.battery_percent = float(bat)
                    except (ValueError, TypeError):
                        pass
            if node.voltage is None:
                vol = telem.get("voltage")
                if vol is not None:
                    try:
                        node.voltage = float(vol)
                    except (ValueError, TypeError):
                        pass
            if node.channel_utilization is None:
                ch_util = telem.get("channelUtilization") or telem.get("channel_utilization")
                if ch_util is not None:
                    try:
                        node.channel_utilization = float(ch_util)
                    except (ValueError, TypeError):
                        pass
            if node.air_util_tx is None:
                air_tx = telem.get("airUtilTx") or telem.get("air_util_tx")
                if air_tx is not None:
                    try:
                        node.air_util_tx = float(air_tx)
                    except (ValueError, TypeError):
                        pass

            # Check for uplink (MQTT) enabled
            uplink = telem.get("uplinkEnabled") or telem.get("uplink_enabled")
            if uplink:
                node.uplink_enabled = True

        # Count packets per node (last 24h) with portnum breakdown
        # BUG 3 FIX: Use correct MeshMonitor packet field names
        twenty_four_hours_ago = now - 86400
        for pkt in all_packets:
            pkt_time = pkt.get("timestamp") or pkt.get("rxTime") or 0
            if pkt_time < twenty_four_hours_ago:
                continue

            # Extract from_node using multiple possible field names
            from_raw = pkt.get("from_node") or pkt.get("from") or pkt.get("fromId") or pkt.get("from_node_id")
            if from_raw is None:
                continue

            # Normalize to canonical node number string
            if isinstance(from_raw, int):
                from_id = str(from_raw)
            elif isinstance(from_raw, str):
                # Could be hex like "!a1b2c3d4" or decimal string
                stripped = from_raw.lstrip("!")
                try:
                    from_id = str(int(stripped, 16))
                except ValueError:
                    if stripped.isdigit():
                        from_id = stripped
                    else:
                        continue
            else:
                continue

            if from_id not in nodes:
                continue

            nodes[from_id].packet_count_24h += 1

            # Get portnum for breakdown
            port_num = pkt.get("portnum_name") or pkt.get("portnum") or pkt.get("port_num") or ""
            port_name = str(port_num).upper()

            # Track by portnum
            if port_name:
                nodes[from_id].packets_by_portnum[port_name] = \
                    nodes[from_id].packets_by_portnum.get(port_name, 0) + 1

            # Check if text message
            if "TEXT" in port_name:
                nodes[from_id].text_packet_count_24h += 1
            # Count position packets
            elif "POSITION" in port_name:
                nodes[from_id].position_packet_count_24h += 1
            # Count telemetry packets
            elif "TELEMETRY" in port_name:
                nodes[from_id].telemetry_packet_count_24h += 1

        # Initialize regions from anchors
        region_map: dict[str, RegionHealth] = {}
        for anchor in self.regions:
            region_map[anchor.name] = RegionHealth(
                name=anchor.name,
                center_lat=anchor.lat,
                center_lon=anchor.lon,
            )

        # Assign nodes to nearest region (first pass: GPS-based)
        unlocated = []
        for node in nodes.values():
            if node.latitude and node.longitude:
                region_name = self._find_nearest_region(node.latitude, node.longitude)
                if region_name and region_name in region_map:
                    node.region = region_name
                    region_map[region_name].node_ids.append(node.node_id)
                else:
                    unlocated.append(node.node_id)
            else:
                unlocated.append(node.node_id)

        # Build neighbor map from edges
        # First, create a mapping from numeric node_id to hex id
        numeric_to_hex: dict[str, str] = {}
        for node in all_nodes:
            hex_id = node.get("id")
            num_id = node.get("node_id")
            if hex_id and num_id:
                numeric_to_hex[str(num_id)] = str(hex_id)

        all_edges = source_manager.get_all_edges()
        neighbors: dict[str, set[str]] = {}
        for edge in all_edges:
            # Get edge endpoints (may be numeric)
            from_raw = edge.get("from") or edge.get("from_node") or edge.get("source")
            to_raw = edge.get("to") or edge.get("to_node") or edge.get("target")
            if not from_raw or not to_raw:
                continue

            # Convert to hex ID format if numeric
            from_id = numeric_to_hex.get(str(from_raw), str(from_raw))
            to_id = numeric_to_hex.get(str(to_raw), str(to_raw))

            if from_id not in neighbors:
                neighbors[from_id] = set()
            if to_id not in neighbors:
                neighbors[to_id] = set()
            neighbors[from_id].add(to_id)
            neighbors[to_id].add(from_id)

        # Second pass: Assign unlocated nodes based on neighbor regions
        # Repeat until no more assignments
        max_iterations = 10
        for _ in range(max_iterations):
            newly_assigned = []
            for node_id in unlocated:
                if node_id not in nodes:
                    continue
                node = nodes[node_id]
                if node.region:
                    continue  # Already assigned

                # Count neighbor regions
                neighbor_ids = neighbors.get(node_id, set())
                region_counts: dict[str, int] = {}
                for nid in neighbor_ids:
                    if nid in nodes and nodes[nid].region:
                        r = nodes[nid].region
                        region_counts[r] = region_counts.get(r, 0) + 1

                if region_counts:
                    # Assign to most common neighbor region
                    best_region = max(region_counts, key=region_counts.get)
                    node.region = best_region
                    region_map[best_region].node_ids.append(node_id)
                    newly_assigned.append(node_id)

            # Remove newly assigned from unlocated
            for nid in newly_assigned:
                if nid in unlocated:
                    unlocated.remove(nid)

            if not newly_assigned:
                break  # No more progress

        regions = list(region_map.values())

        # Create localities within each region (cluster by proximity)
        for region in regions:
            if not region.node_ids:
                continue

            region_nodes = [
                {"id": nid, "latitude": nodes[nid].latitude, "longitude": nodes[nid].longitude}
                for nid in region.node_ids
                if nodes[nid].latitude and nodes[nid].longitude
            ]

            if not region_nodes:
                continue

            locality_clusters = cluster_by_distance(
                region_nodes,
                self.locality_radius,
                lat_key="latitude",
                lon_key="longitude",
                id_key="id",
            )

            for i, cluster in enumerate(locality_clusters):
                center_lat, center_lon = get_cluster_center(cluster)

                locality = LocalityHealth(
                    name=f"{region.name} L{i+1}",
                    center_lat=center_lat,
                    center_lon=center_lon,
                    node_ids=[n["id"] for n in cluster],
                )
                region.localities.append(locality)

                # Mark nodes with their locality
                for n in cluster:
                    if n["id"] in nodes:
                        nodes[n["id"]].locality = locality.name

        # Compute scores at each level (pass packet data availability flag)
        self._compute_locality_scores(regions, nodes, has_packet_data)
        self._compute_region_scores(regions, nodes, has_packet_data)
        mesh_score = self._compute_mesh_score(regions, nodes, has_packet_data)

        # Get traceroute data for statistics
        all_traceroutes = source_manager.get_all_traceroutes()
        traceroute_count = len(all_traceroutes)
        hop_counts = []
        for tr in all_traceroutes:
            # Extract hop count from traceroute data
            route = tr.get("route") or tr.get("hops") or []
            if isinstance(route, list):
                hop_counts.append(len(route))

        avg_hop_count = sum(hop_counts) / len(hop_counts) if hop_counts else 0.0
        max_hop_count = max(hop_counts) if hop_counts else 0

        # Get channel data and count MQTT/uplink nodes
        all_channels = source_manager.get_all_channels()
        uplink_count = sum(1 for node in nodes.values() if node.uplink_enabled)

        # Build result with data availability flags
        mesh_health = MeshHealth(
            regions=regions,
            unlocated_nodes=unlocated,
            nodes=nodes,
            score=mesh_score,
            last_computed=now,
            has_packet_data=has_packet_data,
            has_telemetry_data=len(all_telemetry) > 0,
            has_traceroute_data=traceroute_count > 0,
            has_channel_data=len(all_channels) > 0,
            traceroute_count=traceroute_count,
            avg_hop_count=avg_hop_count,
            max_hop_count=max_hop_count,
            uplink_node_count=uplink_count,
        )

        self._mesh_health = mesh_health

        # Sync health scores back to UnifiedNode objects
        if data_store:
            for node_id_str, node_health in nodes.items():
                try:
                    node_num = int(node_id_str)
                    unified = data_store.nodes.get(node_num)
                    if unified:
                        unified.is_infrastructure = node_health.is_infrastructure
                        unified.health_score = node_health.score.composite
                        unified.infra_score = node_health.score.infrastructure
                        unified.util_score = node_health.score.utilization
                        unified.coverage_score_node = node_health.score.coverage
                        unified.behavior_score = node_health.score.behavior
                        unified.power_score = node_health.score.power
                except (ValueError, TypeError):
                    pass

        # Log computation summary with data availability
        data_sources = []
        if has_packet_data:
            data_sources.append(f"{len(all_packets)} pkts")
        if len(all_telemetry) > 0:
            data_sources.append(f"{len(all_telemetry)} telem")
        if traceroute_count > 0:
            data_sources.append(f"{traceroute_count} traces")
        if len(all_channels) > 0:
            data_sources.append(f"{len(all_channels)} ch")
        data_str = ", ".join(data_sources) if data_sources else "nodes only"

        logger.info(
            f"Mesh health computed: {mesh_health.total_nodes} nodes, "
            f"{mesh_health.total_regions} regions, score {mesh_score.composite:.0f}/100 "
            f"[{data_str}]"
        )

        return mesh_health

    def _compute_locality_scores(
        self,
        regions: list[RegionHealth],
        nodes: dict[str, NodeHealth],
        has_packet_data: bool = False,
    ) -> None:
        """Compute health scores for each locality."""
        for region in regions:
            for locality in region.localities:
                locality_nodes = [nodes[nid] for nid in locality.node_ids if nid in nodes]
                locality.score = self._compute_node_group_score(locality_nodes, has_packet_data)

    def _compute_region_scores(
        self,
        regions: list[RegionHealth],
        nodes: dict[str, NodeHealth],
        has_packet_data: bool = False,
    ) -> None:
        """Compute health scores for each region."""
        for region in regions:
            region_nodes = [nodes[nid] for nid in region.node_ids if nid in nodes]
            region.score = self._compute_node_group_score(region_nodes, has_packet_data)

    def _compute_mesh_score(
        self,
        regions: list[RegionHealth],
        nodes: dict[str, NodeHealth],
        has_packet_data: bool = False,
    ) -> HealthScore:
        """Compute mesh-wide health score."""
        all_nodes = list(nodes.values())
        return self._compute_node_group_score(all_nodes, has_packet_data)

    def _compute_node_group_score(
        self,
        node_list: list[NodeHealth],
        has_packet_data: bool = False,
    ) -> HealthScore:
        """Compute health score for a group of nodes.

        Args:
            node_list: List of NodeHealth objects
            has_packet_data: Whether packet data is available for utilization calc

        Returns:
            HealthScore for the group
        """
        if not node_list:
            return HealthScore()

        # Infrastructure uptime
        infra_nodes = [n for n in node_list if n.is_infrastructure]
        infra_online = sum(1 for n in infra_nodes if n.is_online)
        infra_total = len(infra_nodes)

        if infra_total > 0:
            infra_score = (infra_online / infra_total) * 100
        else:
            infra_score = 100.0  # No infrastructure = not penalized

        # Channel utilization (based on packet counts if available)
        # BUG 7 FIX: Use actual Meshtastic airtime calculation
        if has_packet_data:
            total_non_text_packets = sum(n.non_text_packets for n in node_list)
            # Average airtime per packet on MediumFast: ~200ms
            # Total available airtime per hour: 3,600,000ms
            # Utilization = (packets_per_hour * airtime_ms) / total_airtime_ms * 100
            packets_per_hour = total_non_text_packets / 24.0  # 24h window
            airtime_per_packet_ms = 200  # ~200ms on MediumFast preset
            util_percent = (packets_per_hour * airtime_per_packet_ms) / 3_600_000 * 100

            # Apply scoring thresholds with interpolation
            if util_percent < UTIL_HEALTHY:  # <15%
                util_score = 100.0
            elif util_percent < UTIL_CAUTION:  # 15-20%
                util_score = 100.0 - ((util_percent - UTIL_HEALTHY) / (UTIL_CAUTION - UTIL_HEALTHY)) * 25
            elif util_percent < UTIL_WARNING:  # 20-25%
                util_score = 75.0 - ((util_percent - UTIL_CAUTION) / (UTIL_WARNING - UTIL_CAUTION)) * 25
            elif util_percent < UTIL_UNHEALTHY:  # 25-35%
                util_score = 50.0 - ((util_percent - UTIL_WARNING) / (UTIL_UNHEALTHY - UTIL_WARNING)) * 25
            else:  # 35%+
                util_score = max(0.0, 25.0 - ((util_percent - UTIL_UNHEALTHY) / 10) * 25)
        else:
            # No packet data available - assume healthy utilization
            # This prevents penalizing the score when we simply don't have data
            util_percent = 0.0
            util_score = 100.0

        # Node behavior (flagged nodes)
        flagged = [n for n in node_list if n.non_text_packets > self.packet_threshold]
        flagged_count = len(flagged)

        if flagged_count == 0:
            behavior_score = 100.0
        elif flagged_count == 1:
            behavior_score = 80.0
        elif flagged_count <= 3:
            behavior_score = 60.0
        elif flagged_count <= 5:
            behavior_score = 40.0
        else:
            behavior_score = 20.0

        # Power health
        battery_warnings = 0
        nodes_with_battery = 0
        for n in node_list:
            if n.battery_percent is not None:
                nodes_with_battery += 1
                if n.battery_percent < self.battery_warning_percent:
                    battery_warnings += 1

        if nodes_with_battery > 0:
            battery_ratio = battery_warnings / nodes_with_battery
            power_score = 100.0 * (1 - battery_ratio)
        else:
            power_score = 100.0

        solar_index = 100.0


        # Coverage scoring (5th pillar) - gateway redundancy
        coverage_score = 100.0
        coverage_avg_gw = 0.0
        coverage_single = 0
        coverage_full = 0
        coverage_available = False

        if hasattr(self, 'data_store') and self.data_store:
            total_sources = len(self.data_store._sources) if hasattr(self.data_store, '_sources') else 0
            nodes_with_coverage = []

            for n in node_list:
                node_num = n.node_num
                unified = self.data_store.nodes.get(node_num)
                if unified and unified.avg_gateways is not None:
                    nodes_with_coverage.append(unified)

            if nodes_with_coverage and total_sources > 0:
                coverage_available = True
                coverage_avg_gw = sum(u.avg_gateways for u in nodes_with_coverage) / len(nodes_with_coverage)
                coverage_single = sum(1 for u in nodes_with_coverage if u.avg_gateways <= 1.0)
                coverage_full = sum(1 for u in nodes_with_coverage if u.avg_gateways >= total_sources)

                # Score: penalize single-gateway nodes heavily
                coverage_ratio = coverage_avg_gw / total_sources
                single_penalty = (coverage_single / len(nodes_with_coverage)) * 40 if nodes_with_coverage else 0

                if coverage_ratio >= 1.0:
                    coverage_score = 100.0 - single_penalty
                elif coverage_ratio >= 0.7:
                    coverage_score = max(0, 90.0 - single_penalty - ((1.0 - coverage_ratio) * 30))
                elif coverage_ratio >= 0.5:
                    coverage_score = max(0, 70.0 - single_penalty - ((0.7 - coverage_ratio) * 50))
                else:
                    coverage_score = max(0, 50.0 - single_penalty - ((0.5 - coverage_ratio) * 100))

        return HealthScore(
            infrastructure=infra_score,
            utilization=util_score,
            coverage=coverage_score,
            behavior=behavior_score,
            power=power_score,
            infra_online=infra_online,
            infra_total=infra_total,
            util_percent=util_percent,
            coverage_avg_gateways=coverage_avg_gw,
            coverage_single_gw_count=coverage_single,
            coverage_full_count=coverage_full,
            flagged_nodes=flagged_count,
            battery_warnings=battery_warnings,
            solar_index=solar_index,
            util_data_available=has_packet_data,
            coverage_data_available=coverage_available,
        )

    def get_region(self, name: str) -> Optional[RegionHealth]:
        """Get a region by name."""
        if not self._mesh_health:
            return None

        name_lower = name.lower()
        for region in self._mesh_health.regions:
            if region.name.lower() == name_lower:
                return region
        return None

    def get_node(self, node_id: str) -> Optional[NodeHealth]:
        """Get a node by ID or short name."""
        if not self._mesh_health:
            return None

        if node_id in self._mesh_health.nodes:
            return self._mesh_health.nodes[node_id]

        node_id_lower = node_id.lower()
        for node in self._mesh_health.nodes.values():
            if node.short_name.lower() == node_id_lower:
                return node
            if node.long_name.lower() == node_id_lower:
                return node

        return None

    def get_infrastructure_nodes(self) -> list[NodeHealth]:
        """Get all infrastructure nodes."""
        if not self._mesh_health:
            return []
        return [n for n in self._mesh_health.nodes.values() if n.is_infrastructure]

    def get_flagged_nodes(self) -> list[NodeHealth]:
        """Get nodes flagged for excessive packets."""
        if not self._mesh_health:
            return []
        return [
            n for n in self._mesh_health.nodes.values()
            if n.non_text_packets > self.packet_threshold
        ]

    def get_battery_warnings(self) -> list[NodeHealth]:
        """Get nodes with low battery."""
        if not self._mesh_health:
            return []
        return [
            n for n in self._mesh_health.nodes.values()
            if n.battery_percent is not None and n.battery_percent < self.battery_warning_percent
        ]
