"""Mesh data source manager with deduplication."""

import logging
import time
from typing import Optional

from .config import MeshSourceConfig
from .sources.meshview import MeshviewSource
from .sources.meshmonitor_data import MeshMonitorDataSource

logger = logging.getLogger(__name__)


def _extract_node_num(node: dict) -> int | None:
    """Extract numeric node ID from various formats.

    Handles:
    - nodeNum: 662178887 (numeric)
    - node_id: "!27780c47" (hex with prefix)
    - node_id: "27780c47" (hex without prefix)
    - num: 662178887 (numeric field)

    Args:
        node: Node dict from any source

    Returns:
        Numeric node ID or None if not extractable
    """
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
            # Strip leading ! if present
            hex_str = nid.lstrip("!")
            try:
                return int(hex_str, 16)
            except ValueError:
                pass
        elif isinstance(nid, int):
            return nid

    # Try generic id field
    if "id" in node:
        val = node["id"]
        if isinstance(val, int):
            return val
        if isinstance(val, str):
            if val.isdigit():
                return int(val)
            # Might be hex
            hex_str = val.lstrip("!")
            try:
                return int(hex_str, 16)
            except ValueError:
                pass

    return None


def _normalize_edge_key(edge: dict) -> tuple[int, int] | None:
    """Normalize edge to a canonical (from_num, to_num) tuple.

    Edges are undirected for deduplication purposes, so
    we return a sorted tuple (smaller_id, larger_id).

    Args:
        edge: Edge dict from Meshview

    Returns:
        Sorted tuple of (from_num, to_num) or None if invalid
    """
    from_num = edge.get("from_node") or edge.get("from") or edge.get("from_num")
    to_num = edge.get("to_node") or edge.get("to") or edge.get("to_num")

    if from_num is None or to_num is None:
        return None

    # Convert to int if string
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

    # Return sorted tuple for consistent deduplication
    return (min(from_num, to_num), max(from_num, to_num))


class MeshSourceManager:
    """Manages multiple mesh data sources with deduplication."""

    def __init__(self, source_configs: list[MeshSourceConfig]):
        """Initialize source manager.

        Args:
            source_configs: List of source configurations
        """
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
        """Call maybe_refresh() on all sources.

        Returns:
            Number of sources that refreshed
        """
        refreshed = 0
        for name, source in self._sources.items():
            try:
                if source.maybe_refresh():
                    refreshed += 1
            except Exception as e:
                logger.error(f"Error refreshing source '{name}': {e}")
        return refreshed

    def get_source(self, name: str) -> Optional[MeshviewSource | MeshMonitorDataSource]:
        """Get a specific source by name.

        Args:
            name: Source name

        Returns:
            Source instance or None if not found
        """
        return self._sources.get(name)

    def get_all_nodes(self) -> list[dict]:
        """Get deduplicated nodes from all sources.

        Nodes are deduplicated by their numeric node ID. When a node appears
        in multiple sources, data is merged with the following rules:
        - Most fields: last source wins
        - _sources: accumulates all source names

        Returns:
            List of deduplicated node dicts with '_sources' field (list)
        """
        nodes_by_num: dict[int, dict] = {}

        for name, source in self._sources.items():
            for node in source.nodes:
                node_num = _extract_node_num(node)
                if node_num is None:
                    # Can't deduplicate, include as-is with source tag
                    tagged = dict(node)
                    tagged["_sources"] = [name]
                    # Use a negative counter as pseudo-key to avoid collisions
                    pseudo_key = -len(nodes_by_num) - 1
                    nodes_by_num[pseudo_key] = tagged
                    continue

                if node_num in nodes_by_num:
                    # Merge: update existing with new data
                    existing = nodes_by_num[node_num]
                    # Add new source to sources list
                    if name not in existing["_sources"]:
                        existing["_sources"].append(name)
                    # Update all fields except _sources
                    for key, value in node.items():
                        if key != "_sources" and value is not None:
                            existing[key] = value
                else:
                    # New node
                    tagged = dict(node)
                    tagged["_sources"] = [name]
                    nodes_by_num[node_num] = tagged

        return list(nodes_by_num.values())

    def get_all_edges(self) -> list[dict]:
        """Get deduplicated edges from all Meshview sources.

        Edges are deduplicated by (from_num, to_num) sorted tuple.
        When an edge appears in multiple sources, data is merged.

        Returns:
            List of deduplicated edge dicts with '_sources' field
        """
        edges_by_key: dict[tuple[int, int], dict] = {}

        for name, source in self._sources.items():
            if not isinstance(source, MeshviewSource):
                continue

            for edge in source.edges:
                edge_key = _normalize_edge_key(edge)
                if edge_key is None:
                    # Can't deduplicate, include as-is
                    tagged = dict(edge)
                    tagged["_sources"] = [name]
                    # Use a tuple with negative to avoid collision
                    pseudo_key = (-len(edges_by_key) - 1, 0)
                    edges_by_key[pseudo_key] = tagged
                    continue

                if edge_key in edges_by_key:
                    # Merge: update existing
                    existing = edges_by_key[edge_key]
                    if name not in existing["_sources"]:
                        existing["_sources"].append(name)
                    # Update fields
                    for key, value in edge.items():
                        if key != "_sources" and value is not None:
                            existing[key] = value
                else:
                    # New edge
                    tagged = dict(edge)
                    tagged["_sources"] = [name]
                    edges_by_key[edge_key] = tagged

        return list(edges_by_key.values())

    def get_all_telemetry(self) -> list[dict]:
        """Get deduplicated telemetry from all MeshMonitor sources.

        Telemetry is deduplicated by (node_num, timestamp) tuple.

        Returns:
            List of deduplicated telemetry dicts with '_sources' field
        """
        # Key: (node_num, timestamp)
        telemetry_by_key: dict[tuple[int, float], dict] = {}

        for name, source in self._sources.items():
            if not isinstance(source, MeshMonitorDataSource):
                continue

            for item in source.telemetry:
                node_num = _extract_node_num(item)
                timestamp = item.get("timestamp") or item.get("time") or item.get("ts")

                if node_num is None or timestamp is None:
                    # Can't deduplicate
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
        """Get traceroutes from all MeshMonitor sources, tagged with source name.

        Returns:
            List of traceroute dicts with '_sources' field
        """
        all_traceroutes = []
        for name, source in self._sources.items():
            if isinstance(source, MeshMonitorDataSource):
                for item in source.traceroutes:
                    tagged = dict(item)
                    tagged["_sources"] = [name]
                    all_traceroutes.append(tagged)
        return all_traceroutes

    def get_all_channels(self) -> list[dict]:
        """Get channels from all MeshMonitor sources, tagged with source name.

        Returns:
            List of channel dicts with '_sources' field
        """
        all_channels = []
        for name, source in self._sources.items():
            if isinstance(source, MeshMonitorDataSource):
                for item in source.channels:
                    tagged = dict(item)
                    tagged["_sources"] = [name]
                    all_channels.append(tagged)
        return all_channels

    def get_status(self) -> list[dict]:
        """Get status of all sources for TUI display.

        Returns:
            List of status dicts with source info
        """
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
        """Get per-source statistics without summing across sources.

        Returns:
            Dict mapping source name to stats dict containing:
            - node_count: Number of nodes from this source
            - edge_count: Number of edges (Meshview only)
            - telemetry_count: Number of telemetry records (MeshMonitor only)
            - is_loaded: Whether source has data
        """
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
        """Get deduplication statistics.

        Returns:
            Dict with raw and deduplicated counts
        """
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

    @property
    def source_count(self) -> int:
        """Get number of active sources."""
        return len(self._sources)

    @property
    def source_names(self) -> list[str]:
        """Get list of source names."""
        return list(self._sources.keys())
