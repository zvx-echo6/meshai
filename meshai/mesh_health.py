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
from .mesh_models import UnifiedNode

logger = logging.getLogger(__name__)

# Infrastructure roles (auto-detected)
INFRASTRUCTURE_ROLES = {"ROUTER", "ROUTER_LATE", "ROUTER_CLIENT"}

# Default thresholds
DEFAULT_LOCALITY_RADIUS_MILES = 8.0
DEFAULT_OFFLINE_THRESHOLD_HOURS = 2  # Hours before node considered offline
DEFAULT_PACKET_THRESHOLD = 7200  # Non-text packets per 24h (5/min avg)
# TODO: behavior pillar uses wrong scale - see meshai-v03-notification-handoff.md bug #2
# NOTE: This is aligned with notification config's packet_flood threshold.
# 5 packets/min avg × 60 min × 24 hr = 7,200 packets/day.
# A node averaging 5+ non-text packets/min is misbehaving.
DEFAULT_BATTERY_WARNING_PERCENT = 30  # Battery level to warn (30% gives time to respond)

# Utilization thresholds (percentage) - based on real Meshtastic behavior
# Firmware starts throttling GPS at 25%, severe degradation above 35%
UTIL_HEALTHY = 20     # Under 20% = channel is clear
UTIL_CAUTION = 25     # 20-25% = slight degradation, occasional collisions
UTIL_WARNING = 35     # 25-35% = severe degradation, firmware throttling
UTIL_UNHEALTHY = 45   # 35-45% = mesh struggling badly, reliability dropping

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
    util_max_percent: float = 0.0  # Highest node utilization (hotspot indicator)
    util_method: str = "none"  # "telemetry", "packet_estimate", or "none"
    util_node_count: int = 0  # Nodes reporting utilization
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
    nodes: dict[int, UnifiedNode] = field(default_factory=dict)
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

        # Use UnifiedNode objects directly from data_store - NO NodeHealth
        nodes: dict[int, UnifiedNode] = {}
        for node_num, unified in data_store.nodes.items():
            # Set is_infrastructure based on role
            unified.is_infrastructure = str(unified.role).upper() in INFRASTRUCTURE_ROLES
            # Set is_online based on last_heard
            unified.is_online = unified.last_heard > offline_threshold if unified.last_heard else False
            nodes[node_num] = unified

        # Skip all the old NodeHealth creation, telemetry, and packet parsing
        # That data is already on UnifiedNode from MeshDataStore

        # REMOVED: All the telemetry parsing loop
        # REMOVED: All the packet counting loop
        # Data is already available on UnifiedNode:
        # - unified.battery_percent, voltage, channel_utilization, air_util_tx
        # - unified.packets_sent_24h, text_messages_24h, packets_by_type
        # - unified.uplink_enabled, neighbor_count, neighbors
        # - unified.avg_gateways, deliverability_score

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
        for node_num, node in nodes.items():
            if node.latitude and node.longitude:
                region_name = self._find_nearest_region(node.latitude, node.longitude)
                if region_name and region_name in region_map:
                    node.region = region_name
                    region_map[region_name].node_ids.append(str(node_num))
                else:
                    unlocated.append(str(node_num))
            else:
                unlocated.append(str(node_num))

        # Build BIDIRECTIONAL neighbor map from ALL sources:
        # 1. Each node's own neighbor list (from NeighborInfo packets)
        # 2. REVERSE: if A lists B as neighbor, B also sees A
        # 3. Edges from traceroutes and other connections
        all_neighbor_map: dict[int, set[int]] = {}

        # First: add each node's own neighbor list AND reverse relationships
        for node_num, node in nodes.items():
            if node.neighbors:
                if node_num not in all_neighbor_map:
                    all_neighbor_map[node_num] = set()
                for nb_num in node.neighbors:
                    all_neighbor_map[node_num].add(nb_num)
                    # REVERSE: if this node sees nb_num, nb_num also "sees" this node
                    if nb_num not in all_neighbor_map:
                        all_neighbor_map[nb_num] = set()
                    all_neighbor_map[nb_num].add(node_num)

        # Second: add from edges (connections from traceroutes, etc.)
        if hasattr(data_store, 'edges'):
            for edge in data_store.edges:
                from_num = edge.from_node
                to_num = edge.to_node
                if from_num not in all_neighbor_map:
                    all_neighbor_map[from_num] = set()
                if to_num not in all_neighbor_map:
                    all_neighbor_map[to_num] = set()
                all_neighbor_map[from_num].add(to_num)
                all_neighbor_map[to_num].add(from_num)

        # Also add from raw edges API
        all_edges = source_manager.get_all_edges()
        for edge in all_edges:
            from_raw = edge.get("from") or edge.get("from_node") or edge.get("source")
            to_raw = edge.get("to") or edge.get("to_node") or edge.get("target")
            if not from_raw or not to_raw:
                continue
            try:
                from_num = int(from_raw) if not str(from_raw).startswith("!") else int(str(from_raw)[1:], 16)
                to_num = int(to_raw) if not str(to_raw).startswith("!") else int(str(to_raw)[1:], 16)
            except (ValueError, TypeError):
                continue
            if from_num not in all_neighbor_map:
                all_neighbor_map[from_num] = set()
            if to_num not in all_neighbor_map:
                all_neighbor_map[to_num] = set()
            all_neighbor_map[from_num].add(to_num)
            all_neighbor_map[to_num].add(from_num)

        # Second pass: Assign unlocated nodes based on BIDIRECTIONAL neighbor map
        # This catches nodes that OTHER nodes list as neighbors
        max_iterations = 10
        for _ in range(max_iterations):
            newly_assigned = []
            for node_id_str in unlocated:
                try:
                    node_num = int(node_id_str)
                except ValueError:
                    continue
                if node_num not in nodes:
                    continue
                node = nodes[node_num]
                if node.region:
                    continue  # Already assigned

                # Use the BIDIRECTIONAL neighbor map
                neighbor_nums = all_neighbor_map.get(node_num, set())
                region_counts: dict[str, int] = {}
                for neighbor_num in neighbor_nums:
                    neighbor_node = nodes.get(neighbor_num)
                    if neighbor_node and neighbor_node.region:
                        r = neighbor_node.region
                        region_counts[r] = region_counts.get(r, 0) + 1

                if region_counts:
                    # Assign to most common neighbor region
                    best_region = max(region_counts, key=region_counts.get)
                    node.region = best_region
                    region_map[best_region].node_ids.append(node_id_str)
                    newly_assigned.append(node_id_str)

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

            region_nodes = []
            for nid_str in region.node_ids:
                try:
                    nid = int(nid_str)
                except ValueError:
                    continue
                node = nodes.get(nid)
                if node and node.latitude and node.longitude:
                    region_nodes.append({"id": nid_str, "latitude": node.latitude, "longitude": node.longitude})

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
                        try:
                            loc_nid = int(n["id"])
                            if loc_nid in nodes:
                                nodes[loc_nid].locality = locality.name
                        except (ValueError, TypeError):
                            pass

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

        # Health scores are computed for node groups/regions, not individual nodes
        # UnifiedNode objects already have their individual scores set during compute

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

        # Log utilization method used
        util_method = mesh_score.util_method
        if util_method == "telemetry":
            util_info = f"util={mesh_score.util_percent:.1f}% (max={mesh_score.util_max_percent:.1f}%, {mesh_score.util_node_count} nodes reporting)"
        elif util_method == "packet_estimate":
            util_info = f"util={mesh_score.util_percent:.1f}% (packet estimate fallback)"
        else:
            util_info = "util=N/A (no data)"

        logger.info(
            f"Mesh health computed: {mesh_health.total_nodes} nodes, "
            f"{mesh_health.total_regions} regions, score {mesh_score.composite:.0f}/100 "
            f"[{data_str}] [{util_info}]"
        )

        return mesh_health

    def _compute_locality_scores(
        self,
        regions: list[RegionHealth],
        nodes: dict[int, UnifiedNode],
        has_packet_data: bool = False,
    ) -> None:
        """Compute health scores for each locality."""
        for region in regions:
            for locality in region.localities:
                locality_nodes = []
                for nid_str in locality.node_ids:
                    try:
                        nid = int(nid_str)
                    except ValueError:
                        continue
                    if nid in nodes:
                        locality_nodes.append(nodes[nid])
                locality.score = self._compute_node_group_score(locality_nodes, has_packet_data)

    def _compute_region_scores(
        self,
        regions: list[RegionHealth],
        nodes: dict[int, UnifiedNode],
        has_packet_data: bool = False,
    ) -> None:
        """Compute health scores for each region."""
        for region in regions:
            region_nodes = []
            for nid_str in region.node_ids:
                try:
                    nid = int(nid_str)
                except ValueError:
                    continue
                if nid in nodes:
                    region_nodes.append(nodes[nid])
            region.score = self._compute_node_group_score(region_nodes, has_packet_data)

    def _compute_mesh_score(
        self,
        regions: list[RegionHealth],
        nodes: dict[int, UnifiedNode],
        has_packet_data: bool = False,
    ) -> HealthScore:
        """Compute mesh-wide health score."""
        all_nodes = list(nodes.values())
        return self._compute_node_group_score(all_nodes, has_packet_data)

    def _compute_utilization_score(self, util_percent: float) -> float:
        """Convert utilization percentage to health score using thresholds.

        Thresholds based on real Meshtastic behavior:
        - Under 20%: Clear channel (score 100)
        - 20-25%: Slight degradation (score 75-100)
        - 25-35%: Severe degradation, firmware throttling (score 50-75)
        - 35-45%: Mesh struggling badly (score 25-50)
        - Over 45%: Mesh effectively dead (score 0-25)
        """
        if util_percent < UTIL_HEALTHY:  # <20%
            return 100.0
        elif util_percent < UTIL_CAUTION:  # 20-25%
            # Interpolate from 100 to 75
            return 100.0 - ((util_percent - UTIL_HEALTHY) / (UTIL_CAUTION - UTIL_HEALTHY)) * 25
        elif util_percent < UTIL_WARNING:  # 25-35%
            # Interpolate from 75 to 50
            return 75.0 - ((util_percent - UTIL_CAUTION) / (UTIL_WARNING - UTIL_CAUTION)) * 25
        elif util_percent < UTIL_UNHEALTHY:  # 35-45%
            # Interpolate from 50 to 25
            return 50.0 - ((util_percent - UTIL_WARNING) / (UTIL_UNHEALTHY - UTIL_WARNING)) * 25
        else:  # 45%+
            # Interpolate from 25 to 0 over next 10%
            return max(0.0, 25.0 - ((util_percent - UTIL_UNHEALTHY) / 10) * 25)

    def _compute_node_group_score(
        self,
        node_list: list[UnifiedNode],
        has_packet_data: bool = False,
    ) -> HealthScore:
        """Compute health score for a group of nodes.

        Args:
            node_list: List of UnifiedNode objects
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

        # Channel utilization - prefer real telemetry over packet estimate
        #
        # Priority 1: Use firmware-reported channel_utilization from nodes
        # This is the most accurate measure - the firmware calculates this
        # from actual radio activity over the last minute.
        #
        # Priority 2: Fall back to packet count estimate if no telemetry
        # This is a rough approximation using 200ms/packet (MediumFast preset).
        # It's less accurate because different presets have different airtime,
        # and it sums packets across all nodes regardless of channel.

        util_percent = 0.0
        util_max_percent = 0.0
        util_score = 100.0
        util_method = "none"
        util_node_count = 0
        util_data_available = False

        # Try to get real channel_utilization from infrastructure nodes
        # Use infrastructure nodes because they're the routers - they see the most traffic
        util_readings = []
        for n in infra_nodes:
            if n.channel_utilization is not None and n.channel_utilization >= 0:
                util_readings.append(n.channel_utilization)

        # If no infra nodes have it, try all nodes
        if not util_readings:
            for n in node_list:
                if n.channel_utilization is not None and n.channel_utilization >= 0:
                    util_readings.append(n.channel_utilization)

        if util_readings:
            # Use the HIGHEST value - the busiest node is the bottleneck
            # If one router is at 45% utilization, the mesh has a problem
            # even if other nodes are at 10%
            util_max_percent = max(util_readings)
            util_percent = util_max_percent  # Use max for scoring
            util_score = self._compute_utilization_score(util_percent)
            util_method = "telemetry"
            util_node_count = len(util_readings)
            util_data_available = True

            # Also compute average for informational purposes
            # (stored in util_percent, max in util_max_percent)
            # Actually, use max for the score since that's the bottleneck

        elif has_packet_data:
            # Fallback: Estimate from packet counts
            # This is a rough approximation - only use when telemetry unavailable
            #
            # WARNING: This method has known issues:
            # - Assumes 200ms airtime per packet (only correct for MediumFast)
            # - Sums packets across all nodes even on different channels
            # - Can't distinguish retries from new packets
            # Use real channel_utilization from telemetry when available.

            total_non_text_packets = sum((n.packets_sent_24h - n.text_messages_24h) for n in node_list)
            packets_per_hour = total_non_text_packets / 24.0  # 24h window
            airtime_per_packet_ms = 200  # ~200ms on MediumFast preset
            util_percent = (packets_per_hour * airtime_per_packet_ms) / 3_600_000 * 100
            util_max_percent = util_percent  # No per-node data available
            util_score = self._compute_utilization_score(util_percent)
            util_method = "packet_estimate"
            util_node_count = 0
            util_data_available = True

            logger.debug(
                f"Utilization using packet estimate fallback: {util_percent:.1f}% "
                f"({total_non_text_packets} non-text packets/24h)"
            )
        else:
            # No utilization data available - don't penalize
            util_percent = 0.0
            util_max_percent = 0.0
            util_score = 100.0
            util_method = "none"
            util_node_count = 0
            util_data_available = False

        # Node behavior (flagged nodes)
        flagged = [n for n in node_list if (n.packets_sent_24h - n.text_messages_24h) > self.packet_threshold]
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
            util_max_percent=util_max_percent,
            util_method=util_method,
            util_node_count=util_node_count,
            coverage_avg_gateways=coverage_avg_gw,
            coverage_single_gw_count=coverage_single,
            coverage_full_count=coverage_full,
            flagged_nodes=flagged_count,
            battery_warnings=battery_warnings,
            solar_index=solar_index,
            util_data_available=util_data_available,
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

    def get_node(self, identifier: str) -> Optional[UnifiedNode]:
        """Get a node by ID, name, or hex."""
        if not self._mesh_health:
            return None

        # Try as int (node_num)
        try:
            num = int(identifier)
            if num in self._mesh_health.nodes:
                return self._mesh_health.nodes[num]
        except ValueError:
            pass

        # Try shortname/longname
        id_lower = identifier.lower().strip()
        for node in self._mesh_health.nodes.values():
            if node.short_name and node.short_name.lower() == id_lower:
                return node
            if node.long_name and id_lower in node.long_name.lower():
                return node

        # Try hex
        if identifier.startswith("!"):
            try:
                num = int(identifier[1:], 16)
                if num in self._mesh_health.nodes:
                    return self._mesh_health.nodes[num]
            except ValueError:
                pass

        return None

    def get_infrastructure_nodes(self) -> list[UnifiedNode]:
        """Get all infrastructure nodes."""
        if not self._mesh_health:
            return []
        return [n for n in self._mesh_health.nodes.values() if n.is_infrastructure]

    def get_flagged_nodes(self) -> list[UnifiedNode]:
        """Get nodes flagged for excessive packets."""
        if not self._mesh_health:
            return []
        return [
            n for n in self._mesh_health.nodes.values()
            if (n.packets_sent_24h - n.text_messages_24h) > self.packet_threshold
        ]

    def get_battery_warnings(self) -> list[UnifiedNode]:
        """Get nodes with low battery."""
        if not self._mesh_health:
            return []
        return [
            n for n in self._mesh_health.nodes.values()
            if n.battery_percent is not None and n.battery_percent < self.battery_warning_percent
        ]
