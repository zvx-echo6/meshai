"""Mesh data source manager with deduplication and normalization."""

import logging
import time
from typing import Optional

from .config import MeshSourceConfig
from .sources.meshview import MeshviewSource
from .sources.meshmonitor_data import MeshMonitorDataSource

logger = logging.getLogger(__name__)

# Meshtastic role enum mapping (integer -> string)
# From meshtastic.protobuf.config_pb2.Config.DeviceConfig.Role
MESHTASTIC_ROLE_MAP = {
    0: "CLIENT",
    1: "CLIENT_MUTE",
    2: "ROUTER",
    3: "ROUTER_CLIENT",
    4: "REPEATER",
    5: "TRACKER",
    6: "SENSOR",
    7: "TAK",
    8: "CLIENT_HIDDEN",
    9: "LOST_AND_FOUND",
    10: "TAK_TRACKER",
    11: "ROUTER_LATE",
    12: "CLIENT_BASE",
}


def _normalize_node(node: dict) -> dict:
    """Normalize a node dict to consistent field names and formats."""
    result = dict(node)

    # === ROLE NORMALIZATION ===
    role = node.get("role")
    if role is None:
        result["role"] = "UNKNOWN"
    elif isinstance(role, int):
        result["role"] = MESHTASTIC_ROLE_MAP.get(role, f"UNKNOWN_{role}")
    elif isinstance(role, str):
        result["role"] = role.upper()
    else:
        result["role"] = str(role).upper()

    # === GPS NORMALIZATION ===
    lat = None
    if "latitude" in node and node["latitude"] is not None:
        lat = node["latitude"]
    elif "last_lat" in node and node["last_lat"] is not None:
        lat = node["last_lat"]
        if isinstance(lat, int) and abs(lat) > 1000:
            lat = lat / 1e7
    elif "lat" in node and node["lat"] is not None:
        lat = node["lat"]

    lon = None
    if "longitude" in node and node["longitude"] is not None:
        lon = node["longitude"]
    elif "last_long" in node and node["last_long"] is not None:
        lon = node["last_long"]
        if isinstance(lon, int) and abs(lon) > 1000:
            lon = lon / 1e7
    elif "lon" in node and node["lon"] is not None:
        lon = node["lon"]
    elif "lng" in node and node["lng"] is not None:
        lon = node["lng"]

    if lat is not None and lon is not None:
        if abs(lat) < 0.001 and abs(lon) < 0.001:
            lat = None
            lon = None

    result["latitude"] = lat
    result["longitude"] = lon

    # === TIMESTAMP NORMALIZATION ===
    ts = None
    if "last_seen_us" in node and node["last_seen_us"] is not None:
        val = node["last_seen_us"]
        if isinstance(val, (int, float)) and val > 0:
            ts = val / 1_000_000

    if ts is None:
        for field in ("lastHeard", "last_heard", "last_seen", "lastSeen", "updated_at"):
            if field in node and node[field] is not None:
                val = node[field]
                if isinstance(val, (int, float)) and val > 0:
                    if val > 1e15:
                        ts = val / 1_000_000
                    elif val > 1e12:
                        ts = val / 1_000
                    else:
                        ts = float(val)
                    break

    result["last_heard"] = ts

    # === HARDWARE MODEL NORMALIZATION ===
    hw = None
    if "hw_model" in node and isinstance(node["hw_model"], str):
        hw = node["hw_model"]
    elif "hwModel" in node and isinstance(node["hwModel"], str):
        hw = node["hwModel"]
    if hw is None:
        if "hw_model" in node and node["hw_model"] is not None:
            hw = node["hw_model"]
        elif "hwModel" in node and node["hwModel"] is not None:
            hw = node["hwModel"]

    if hw is not None:
        result["hw_model"] = hw

    return result


def _extract_node_num(node: dict) -> int | None:
    """Extract numeric node ID from various formats."""
    # Try numeric fields first
    for field in ("nodeNum", "num", "node_num"):
        if field in node:
            val = node[field]
            if isinstance(val, int):
                return val
            if isinstance(val, str) and val.isdigit():
                return int(val)

    # Try hex node_id field
    if "node_id" in node:
        nid = node["node_id"]
        if isinstance(nid, str):
            hex_str = nid.lstrip("!")
            try:
                return int(hex_str, 16)
            except ValueError:
                pass
        elif isinstance(nid, int):
            return nid

    # Try generic id field (but NOT database row IDs)
    if "id" in node:
        val = node["id"]
        if isinstance(val, int):
            # Database row IDs are small; Meshtastic node numbers are large
            if val > 100000:
                return val
        if isinstance(val, str):
            if val.startswith("!"):
                hex_str = val.lstrip("!")
                try:
                    return int(hex_str, 16)
                except ValueError:
                    pass
            elif len(val) == 8:
                try:
                    return int(val, 16)
                except ValueError:
                    pass

    return None


def _normalize_edge_key(edge: dict) -> tuple[int, int] | None:
    """Normalize edge to a canonical (from_num, to_num) tuple."""
    from_num = edge.get("from_node") or edge.get("from") or edge.get("from_num")
    to_num = edge.get("to_node") or edge.get("to") or edge.get("to_num")

    if from_num is None or to_num is None:
        return None

    if isinstance(from_num, str):
        if from_num.isdigit():
            from_num = int(from_num)
        else:
            try:
                from_num = int(from_num.lstrip("!"), 16)
            except ValueError:
                return None

    if isinstance(to_num, str):
        if to_num.isdigit():
            to_num = int(to_num)
        else:
            try:
                to_num = int(to_num.lstrip("!"), 16)
            except ValueError:
                return None

    return (min(from_num, to_num), max(from_num, to_num))


class MeshSourceManager:
    """Manages multiple mesh data sources with deduplication."""

    def __init__(self, source_configs: list[MeshSourceConfig]):
        self._sources: dict[str, MeshviewSource | MeshMonitorDataSource] = {}

        for cfg in source_configs:
            if not cfg.enabled:
                continue

            name = cfg.name
            if not name:
                logger.warning("Skipping source with empty name")
                continue

            if name in self._sources:
                logger.warning(f"Duplicate source name '{name}', skipping")
                continue

            try:
                if cfg.type == "meshview":
                    self._sources[name] = MeshviewSource(
                        url=cfg.url,
                        refresh_interval=cfg.refresh_interval,
                    )
                    logger.info(f"Created Meshview source '{name}' -> {cfg.url}")

                elif cfg.type == "meshmonitor":
                    self._sources[name] = MeshMonitorDataSource(
                        url=cfg.url,
                        api_token=cfg.api_token,
                        refresh_interval=cfg.refresh_interval,
                    )
                    logger.info(f"Created MeshMonitor source '{name}' -> {cfg.url}")

                else:
                    logger.warning(f"Unknown source type '{cfg.type}' for '{name}'")

            except Exception as e:
                logger.error(f"Failed to create source '{name}': {e}")

    def refresh_all(self) -> int:
        refreshed = 0
        for name, source in self._sources.items():
            try:
                if source.maybe_refresh():
                    refreshed += 1
            except Exception as e:
                logger.error(f"Error refreshing source '{name}': {e}")
        return refreshed

    def get_source(self, name: str) -> Optional[MeshviewSource | MeshMonitorDataSource]:
        return self._sources.get(name)

    def get_all_nodes(self) -> list[dict]:
        """Get deduplicated nodes from all sources with _node_num field."""
        nodes_by_num: dict[int, dict] = {}

        for name, source in self._sources.items():
            for node in source.nodes:
                normalized = _normalize_node(node)
                node_num = _extract_node_num(normalized)

                if node_num is None:
                    normalized["_sources"] = [name]
                    pseudo_key = -len(nodes_by_num) - 1
                    nodes_by_num[pseudo_key] = normalized
                    continue

                # BUG 1 FIX: Store _node_num on the normalized dict
                normalized["_node_num"] = node_num

                if node_num in nodes_by_num:
                    existing = nodes_by_num[node_num]
                    if name not in existing["_sources"]:
                        existing["_sources"].append(name)
                    for key, value in normalized.items():
                        if key not in ("_sources", "_node_num") and value is not None:
                            existing[key] = value
                else:
                    normalized["_sources"] = [name]
                    nodes_by_num[node_num] = normalized

        return list(nodes_by_num.values())

    def get_all_edges(self) -> list[dict]:
        edges_by_key: dict[tuple[int, int], dict] = {}

        for name, source in self._sources.items():
            if not isinstance(source, MeshviewSource):
                continue

            for edge in source.edges:
                edge_key = _normalize_edge_key(edge)
                if edge_key is None:
                    tagged = dict(edge)
                    tagged["_sources"] = [name]
                    pseudo_key = (-len(edges_by_key) - 1, 0)
                    edges_by_key[pseudo_key] = tagged
                    continue

                if edge_key in edges_by_key:
                    existing = edges_by_key[edge_key]
                    if name not in existing["_sources"]:
                        existing["_sources"].append(name)
                    for key, value in edge.items():
                        if key != "_sources" and value is not None:
                            existing[key] = value
                else:
                    tagged = dict(edge)
                    tagged["_sources"] = [name]
                    edges_by_key[edge_key] = tagged

        return list(edges_by_key.values())

    def get_all_telemetry(self) -> list[dict]:
        telemetry_by_key: dict[tuple[int, float], dict] = {}

        for name, source in self._sources.items():
            if not isinstance(source, MeshMonitorDataSource):
                continue

            for item in source.telemetry:
                node_num = _extract_node_num(item)
                timestamp = item.get("timestamp") or item.get("time") or item.get("ts")

                if node_num is None or timestamp is None:
                    tagged = dict(item)
                    tagged["_sources"] = [name]
                    pseudo_key = (-len(telemetry_by_key) - 1, 0.0)
                    telemetry_by_key[pseudo_key] = tagged
                    continue

                key = (node_num, float(timestamp))

                if key in telemetry_by_key:
                    existing = telemetry_by_key[key]
                    if name not in existing["_sources"]:
                        existing["_sources"].append(name)
                    for k, v in item.items():
                        if k != "_sources" and v is not None:
                            existing[k] = v
                else:
                    tagged = dict(item)
                    tagged["_sources"] = [name]
                    telemetry_by_key[key] = tagged

        return list(telemetry_by_key.values())

    def get_all_traceroutes(self) -> list[dict]:
        all_traceroutes = []
        for name, source in self._sources.items():
            if isinstance(source, MeshMonitorDataSource):
                for item in source.traceroutes:
                    tagged = dict(item)
                    tagged["_sources"] = [name]
                    all_traceroutes.append(tagged)
        return all_traceroutes

    def get_all_channels(self) -> list[dict]:
        all_channels = []
        for name, source in self._sources.items():
            if isinstance(source, MeshMonitorDataSource):
                for item in source.channels:
                    tagged = dict(item)
                    tagged["_sources"] = [name]
                    all_channels.append(tagged)
        return all_channels

    def get_status(self) -> list[dict]:
        status_list = []
        for name, source in self._sources.items():
            status = {
                "name": name,
                "type": "meshview" if isinstance(source, MeshviewSource) else "meshmonitor",
                "enabled": True,
                "is_loaded": source.is_loaded,
                "last_refresh": source.last_refresh,
                "last_error": source.last_error,
                "node_count": len(source.nodes),
            }

            if isinstance(source, MeshviewSource):
                status["edge_count"] = len(source.edges)
            elif isinstance(source, MeshMonitorDataSource):
                status["telemetry_count"] = len(source.telemetry)
                status["traceroute_count"] = len(source.traceroutes)
                status["channel_count"] = len(source.channels)

            status_list.append(status)

        return status_list

    def get_stats_by_source(self) -> dict[str, dict]:
        stats = {}
        for name, source in self._sources.items():
            source_stats = {
                "node_count": len(source.nodes),
                "is_loaded": source.is_loaded,
                "last_refresh": source.last_refresh,
            }

            if isinstance(source, MeshviewSource):
                source_stats["edge_count"] = len(source.edges)
                source_stats["type"] = "meshview"
            elif isinstance(source, MeshMonitorDataSource):
                source_stats["telemetry_count"] = len(source.telemetry)
                source_stats["traceroute_count"] = len(source.traceroutes)
                source_stats["channel_count"] = len(source.channels)
                source_stats["type"] = "meshmonitor"

            stats[name] = source_stats

        return stats

    def get_dedup_stats(self) -> dict:
        raw_nodes = sum(len(s.nodes) for s in self._sources.values())
        raw_edges = sum(
            len(s.edges) for s in self._sources.values()
            if isinstance(s, MeshviewSource)
        )

        dedup_nodes = len(self.get_all_nodes())
        dedup_edges = len(self.get_all_edges())

        return {
            "raw_node_count": raw_nodes,
            "dedup_node_count": dedup_nodes,
            "node_duplicates": raw_nodes - dedup_nodes,
            "raw_edge_count": raw_edges,
            "dedup_edge_count": dedup_edges,
            "edge_duplicates": raw_edges - dedup_edges,
        }

    def get_all_packets(self) -> list[dict]:
        packets_by_id: dict[int, dict] = {}

        for name, source in self._sources.items():
            if not isinstance(source, MeshMonitorDataSource):
                continue

            if not hasattr(source, "packets"):
                continue

            for pkt in source.packets:
                packet_id = pkt.get("packet_id") or pkt.get("id")
                if packet_id is None:
                    from_node = pkt.get("from_node") or pkt.get("from")
                    ts = pkt.get("timestamp") or pkt.get("rxTime")
                    portnum = pkt.get("portnum")
                    if from_node and ts:
                        packet_id = hash((from_node, ts, portnum))
                    else:
                        packet_id = -len(packets_by_id) - 1

                if packet_id in packets_by_id:
                    existing = packets_by_id[packet_id]
                    if name not in existing["_sources"]:
                        existing["_sources"].append(name)
                else:
                    tagged = dict(pkt)
                    tagged["_sources"] = [name]
                    packets_by_id[packet_id] = tagged

        return list(packets_by_id.values())

    def get_traffic_stats(self) -> dict[str, dict]:
        stats = {}

        for name, source in self._sources.items():
            source_stats = {}

            if isinstance(source, MeshviewSource):
                if hasattr(source, "stats") and source.stats:
                    data = source.stats.get("data", [])
                    source_stats["hourly_counts"] = data
                    total = sum(item.get("count", 0) for item in data)
                    source_stats["total_packets"] = total
                    source_stats["packets_per_hour"] = total / len(data) if data else 0

                if hasattr(source, "counts") and source.counts:
                    source_stats["total_seen"] = source.counts.get("total_seen", 0)
                    source_stats["total_packets_all_time"] = source.counts.get("total_packets", 0)

            elif isinstance(source, MeshMonitorDataSource):
                if hasattr(source, "network_stats") and source.network_stats:
                    ns = source.network_stats
                    source_stats["total_nodes"] = ns.get("totalNodes", 0)
                    source_stats["active_nodes"] = ns.get("activeNodes", 0)
                    source_stats["traceroute_count"] = ns.get("tracerouteCount", 0)
                    source_stats["last_updated"] = ns.get("lastUpdated", 0)

                if hasattr(source, "packets") and source.packets:
                    portnum_counts: dict[str, int] = {}
                    for pkt in source.packets:
                        portnum = pkt.get("portnum_name") or str(pkt.get("portnum", "UNKNOWN"))
                        portnum_counts[portnum] = portnum_counts.get(portnum, 0) + 1
                    source_stats["packets_by_portnum"] = portnum_counts
                    source_stats["packet_count"] = len(source.packets)

            if source_stats:
                stats[name] = source_stats

        return stats

    def get_solar_data(self) -> list[dict]:
        all_solar = []
        for name, source in self._sources.items():
            if isinstance(source, MeshMonitorDataSource):
                if hasattr(source, "solar") and source.solar:
                    for item in source.solar:
                        tagged = dict(item)
                        tagged["_sources"] = [name]
                        all_solar.append(tagged)
        return all_solar

    def get_network_stats(self) -> dict[str, dict]:
        stats = {}

        for name, source in self._sources.items():
            source_stats = {}

            if isinstance(source, MeshviewSource):
                if hasattr(source, "counts") and source.counts:
                    source_stats.update(source.counts)
                source_stats["node_count"] = len(source.nodes)
                source_stats["edge_count"] = len(source.edges)

            elif isinstance(source, MeshMonitorDataSource):
                if hasattr(source, "network_stats") and source.network_stats:
                    source_stats.update(source.network_stats)
                if hasattr(source, "topology") and source.topology:
                    source_stats["topology"] = source.topology
                source_stats["node_count"] = len(source.nodes)
                source_stats["telemetry_count"] = len(source.telemetry)
                source_stats["traceroute_count"] = len(source.traceroutes)
                source_stats["channel_count"] = len(source.channels)
                if hasattr(source, "packets"):
                    source_stats["packet_count"] = len(source.packets)

            if source_stats:
                stats[name] = source_stats

        return stats

    @property
    def source_count(self) -> int:
        return len(self._sources)

    @property
    def source_names(self) -> list[str]:
        return list(self._sources.keys())
