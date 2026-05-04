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
    suggest_cluster_name,
    get_cluster_center,
    assign_to_nearest_cluster,
    haversine_distance,
)

logger = logging.getLogger(__name__)

# Infrastructure roles (auto-detected)
INFRASTRUCTURE_ROLES = {"ROUTER", "ROUTER_LATE", "ROUTER_CLIENT"}

# Default thresholds
DEFAULT_REGION_RADIUS_MILES = 40.0
DEFAULT_LOCALITY_RADIUS_MILES = 8.0
DEFAULT_OFFLINE_THRESHOLD_HOURS = 24
DEFAULT_PACKET_THRESHOLD = 500  # Non-text packets per 24h
DEFAULT_BATTERY_WARNING_PERCENT = 20

# Utilization thresholds (percentage)
UTIL_HEALTHY = 15
UTIL_CAUTION = 20
UTIL_WARNING = 25
UTIL_UNHEALTHY = 35

# Pillar weights
WEIGHT_INFRASTRUCTURE = 0.40
WEIGHT_UTILIZATION = 0.25
WEIGHT_BEHAVIOR = 0.20
WEIGHT_POWER = 0.15


@dataclass
class HealthScore:
    """Health score for a single entity (mesh, region, locality, node)."""

    infrastructure: float = 100.0  # 0-100
    utilization: float = 100.0  # 0-100
    behavior: float = 100.0  # 0-100
    power: float = 100.0  # 0-100

    # Underlying metrics
    infra_online: int = 0
    infra_total: int = 0
    util_percent: float = 0.0
    flagged_nodes: int = 0
    battery_warnings: int = 0
    solar_index: float = 100.0

    @property
    def composite(self) -> float:
        """Calculate weighted composite score."""
        return (
            self.infrastructure * WEIGHT_INFRASTRUCTURE +
            self.utilization * WEIGHT_UTILIZATION +
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
    battery_percent: Optional[float] = None
    voltage: Optional[float] = None
    has_solar: bool = False

    # Scores
    score: HealthScore = field(default_factory=HealthScore)

    @property
    def non_text_packets(self) -> int:
        """Non-text packets in 24h."""
        return self.packet_count_24h - self.text_packet_count_24h


@dataclass
class LocalityHealth:
    """Health data for a locality (sub-region cluster)."""

    name: str
    suggested_name: str = ""
    center_lat: float = 0.0
    center_lon: float = 0.0
    node_ids: list[str] = field(default_factory=list)
    score: HealthScore = field(default_factory=HealthScore)


@dataclass
class RegionHealth:
    """Health data for a region."""

    name: str
    suggested_name: str = ""
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

    @property
    def total_nodes(self) -> int:
        return len(self.nodes)

    @property
    def total_regions(self) -> int:
        return len(self.regions)


class MeshHealthEngine:
    """Computes mesh health scores from aggregated source data."""

    def __init__(
        self,
        region_radius: float = DEFAULT_REGION_RADIUS_MILES,
        locality_radius: float = DEFAULT_LOCALITY_RADIUS_MILES,
        offline_threshold_hours: int = DEFAULT_OFFLINE_THRESHOLD_HOURS,
        packet_threshold: int = DEFAULT_PACKET_THRESHOLD,
        battery_warning_percent: int = DEFAULT_BATTERY_WARNING_PERCENT,
        infra_overrides: Optional[list[str]] = None,
        region_labels: Optional[dict[str, str]] = None,
    ):
        """Initialize health engine.

        Args:
            region_radius: Miles radius for region clustering
            locality_radius: Miles radius for locality clustering
            offline_threshold_hours: Hours before a node is considered offline
            packet_threshold: Non-text packets per 24h to flag a node
            battery_warning_percent: Battery level for warnings
            infra_overrides: Node IDs to exclude from infrastructure
            region_labels: Override labels for regions {suggested_name: custom_label}
        """
        self.region_radius = region_radius
        self.locality_radius = locality_radius
        self.offline_threshold_hours = offline_threshold_hours
        self.packet_threshold = packet_threshold
        self.battery_warning_percent = battery_warning_percent
        self.infra_overrides = set(infra_overrides or [])
        self.region_labels = dict(region_labels or {})

        self._mesh_health: Optional[MeshHealth] = None

    @property
    def mesh_health(self) -> Optional[MeshHealth]:
        """Get last computed mesh health."""
        return self._mesh_health

    def compute(self, source_manager) -> MeshHealth:
        """Compute mesh health from source data.

        Args:
            source_manager: MeshSourceManager with fetched data

        Returns:
            MeshHealth with computed scores
        """
        now = time.time()
        offline_threshold = now - (self.offline_threshold_hours * 3600)

        # Aggregate all nodes from all sources
        all_nodes = source_manager.get_all_nodes()
        all_edges = source_manager.get_all_edges()
        all_telemetry = source_manager.get_all_telemetry()
        all_packets = []

        # Get packets from MeshMonitor sources
        for status in source_manager.get_status():
            if status["type"] == "meshmonitor":
                src = source_manager.get_source(status["name"])
                if src and hasattr(src, "packets"):
                    for pkt in src.packets:
                        tagged = dict(pkt)
                        tagged["_source"] = status["name"]
                        all_packets.append(tagged)

        # Build node health records
        nodes: dict[str, NodeHealth] = {}
        for node in all_nodes:
            node_id = node.get("id") or node.get("nodeId") or node.get("num")
            if not node_id:
                continue
            node_id = str(node_id)

            # Skip if we already have this node from another source
            if node_id in nodes:
                continue

            # Extract fields (handle different API formats)
            short_name = node.get("shortName") or node.get("short_name") or ""
            long_name = node.get("longName") or node.get("long_name") or ""
            role = node.get("role") or node.get("hwModel") or ""

            # Determine if infrastructure
            is_infra = role.upper() in INFRASTRUCTURE_ROLES
            if node_id in self.infra_overrides:
                is_infra = False

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
                is_infrastructure=is_infra,
                last_seen=last_seen,
                is_online=is_online,
                latitude=lat,
                longitude=lon,
            )

        # Add telemetry data
        for telem in all_telemetry:
            node_id = str(telem.get("nodeId") or telem.get("node_id") or "")
            if node_id not in nodes:
                continue

            node = nodes[node_id]
            battery = telem.get("batteryLevel") or telem.get("battery_level")
            voltage = telem.get("voltage")

            if battery is not None:
                node.battery_percent = float(battery)
            if voltage is not None:
                node.voltage = float(voltage)

        # Count packets per node (last 24h)
        twenty_four_hours_ago = now - 86400
        for pkt in all_packets:
            pkt_time = pkt.get("timestamp") or pkt.get("rxTime") or 0
            if pkt_time < twenty_four_hours_ago:
                continue

            from_id = str(pkt.get("from") or pkt.get("fromId") or "")
            if from_id not in nodes:
                continue

            nodes[from_id].packet_count_24h += 1

            # Check if text message
            port_num = pkt.get("portnum") or pkt.get("port_num") or ""
            if "TEXT" in str(port_num).upper():
                nodes[from_id].text_packet_count_24h += 1

        # Cluster infrastructure nodes into regions
        infra_nodes = [n for n in nodes.values() if n.is_infrastructure]
        infra_dicts = [
            {"id": n.node_id, "latitude": n.latitude, "longitude": n.longitude}
            for n in infra_nodes
            if n.latitude and n.longitude
        ]

        region_clusters = cluster_by_distance(
            infra_dicts,
            self.region_radius,
            lat_key="latitude",
            lon_key="longitude",
            id_key="id",
        )

        # Build regions
        regions: list[RegionHealth] = []
        for cluster in region_clusters:
            suggested = suggest_cluster_name(cluster)
            label = self.region_labels.get(suggested, suggested)
            center_lat, center_lon = get_cluster_center(cluster)

            region = RegionHealth(
                name=label,
                suggested_name=suggested,
                center_lat=center_lat,
                center_lon=center_lon,
                node_ids=[n["id"] for n in cluster],
            )
            regions.append(region)

            # Mark nodes with their region
            for n in cluster:
                if n["id"] in nodes:
                    nodes[n["id"]].region = label

        # Assign non-infrastructure nodes to nearest region
        unlocated = []
        for node in nodes.values():
            if node.region:
                continue  # Already assigned

            if node.latitude and node.longitude:
                # Find nearest region
                min_dist = float("inf")
                nearest_region = None
                for region in regions:
                    dist = haversine_distance(
                        node.latitude, node.longitude,
                        region.center_lat, region.center_lon
                    )
                    if dist < min_dist:
                        min_dist = dist
                        nearest_region = region

                if nearest_region:
                    node.region = nearest_region.name
                    nearest_region.node_ids.append(node.node_id)
                else:
                    unlocated.append(node.node_id)
            else:
                unlocated.append(node.node_id)

        # Create localities within each region
        for region in regions:
            region_nodes = [
                {"id": nid, "latitude": nodes[nid].latitude, "longitude": nodes[nid].longitude}
                for nid in region.node_ids
                if nodes[nid].latitude and nodes[nid].longitude
            ]

            locality_clusters = cluster_by_distance(
                region_nodes,
                self.locality_radius,
                lat_key="latitude",
                lon_key="longitude",
                id_key="id",
            )

            for cluster in locality_clusters:
                suggested = suggest_cluster_name(cluster)
                center_lat, center_lon = get_cluster_center(cluster)

                locality = LocalityHealth(
                    name=suggested,
                    suggested_name=suggested,
                    center_lat=center_lat,
                    center_lon=center_lon,
                    node_ids=[n["id"] for n in cluster],
                )
                region.localities.append(locality)

                # Mark nodes with their locality
                for n in cluster:
                    if n["id"] in nodes:
                        nodes[n["id"]].locality = suggested

        # Compute scores at each level
        self._compute_locality_scores(regions, nodes)
        self._compute_region_scores(regions, nodes)
        mesh_score = self._compute_mesh_score(regions, nodes)

        # Build result
        mesh_health = MeshHealth(
            regions=regions,
            unlocated_nodes=unlocated,
            nodes=nodes,
            score=mesh_score,
            last_computed=now,
        )

        self._mesh_health = mesh_health
        logger.info(
            f"Mesh health computed: {mesh_health.total_nodes} nodes, "
            f"{mesh_health.total_regions} regions, score {mesh_score.composite:.0f}/100"
        )

        return mesh_health

    def _compute_locality_scores(
        self,
        regions: list[RegionHealth],
        nodes: dict[str, NodeHealth],
    ) -> None:
        """Compute health scores for each locality."""
        for region in regions:
            for locality in region.localities:
                locality_nodes = [nodes[nid] for nid in locality.node_ids if nid in nodes]
                locality.score = self._compute_node_group_score(locality_nodes)

    def _compute_region_scores(
        self,
        regions: list[RegionHealth],
        nodes: dict[str, NodeHealth],
    ) -> None:
        """Compute health scores for each region."""
        for region in regions:
            region_nodes = [nodes[nid] for nid in region.node_ids if nid in nodes]
            region.score = self._compute_node_group_score(region_nodes)

    def _compute_mesh_score(
        self,
        regions: list[RegionHealth],
        nodes: dict[str, NodeHealth],
    ) -> HealthScore:
        """Compute mesh-wide health score."""
        all_nodes = list(nodes.values())
        return self._compute_node_group_score(all_nodes)

    def _compute_node_group_score(self, node_list: list[NodeHealth]) -> HealthScore:
        """Compute health score for a group of nodes.

        Args:
            node_list: List of NodeHealth objects

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

        # Channel utilization (simplified - based on packet counts)
        # Rough estimate: 1000 packets/day across all nodes = ~15% utilization
        total_packets = sum(n.packet_count_24h for n in node_list)
        # Estimate utilization: packets / (nodes * 500 baseline)
        baseline = len(node_list) * 500
        if baseline > 0:
            util_percent = (total_packets / baseline) * 15  # Scale to percentage
        else:
            util_percent = 0

        if util_percent < UTIL_HEALTHY:
            util_score = 100.0
        elif util_percent < UTIL_CAUTION:
            util_score = 75.0
        elif util_percent < UTIL_WARNING:
            util_score = 50.0
        elif util_percent < UTIL_UNHEALTHY:
            util_score = 25.0
        else:
            util_score = 0.0

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
            power_score = 100.0  # No battery data = assume OK

        # Solar index (placeholder - would need solar data)
        solar_index = 100.0

        return HealthScore(
            infrastructure=infra_score,
            utilization=util_score,
            behavior=behavior_score,
            power=power_score,
            infra_online=infra_online,
            infra_total=infra_total,
            util_percent=util_percent,
            flagged_nodes=flagged_count,
            battery_warnings=battery_warnings,
            solar_index=solar_index,
        )

    def get_region(self, name: str) -> Optional[RegionHealth]:
        """Get a region by name.

        Args:
            name: Region name (case-insensitive)

        Returns:
            RegionHealth or None
        """
        if not self._mesh_health:
            return None

        name_lower = name.lower()
        for region in self._mesh_health.regions:
            if region.name.lower() == name_lower:
                return region
            if region.suggested_name.lower() == name_lower:
                return region
        return None

    def get_node(self, node_id: str) -> Optional[NodeHealth]:
        """Get a node by ID or short name.

        Args:
            node_id: Node ID or short name

        Returns:
            NodeHealth or None
        """
        if not self._mesh_health:
            return None

        # Try direct ID lookup
        if node_id in self._mesh_health.nodes:
            return self._mesh_health.nodes[node_id]

        # Try short name match
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
