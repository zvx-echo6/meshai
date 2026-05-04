"""Mesh data source manager."""

import logging
import time
from typing import Optional

from .config import MeshSourceConfig
from .sources.meshview import MeshviewSource
from .sources.meshmonitor_data import MeshMonitorDataSource

logger = logging.getLogger(__name__)


class MeshSourceManager:
    """Manages multiple mesh data sources."""

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
        """Get nodes from all sources, tagged with source name.

        Returns:
            List of node dicts with '_source' field added
        """
        all_nodes = []
        for name, source in self._sources.items():
            for node in source.nodes:
                tagged = dict(node)
                tagged["_source"] = name
                all_nodes.append(tagged)
        return all_nodes

    def get_all_edges(self) -> list[dict]:
        """Get edges from all Meshview sources, tagged with source name.

        Returns:
            List of edge dicts with '_source' field added
        """
        all_edges = []
        for name, source in self._sources.items():
            if isinstance(source, MeshviewSource):
                for edge in source.edges:
                    tagged = dict(edge)
                    tagged["_source"] = name
                    all_edges.append(tagged)
        return all_edges

    def get_all_telemetry(self) -> list[dict]:
        """Get telemetry from all MeshMonitor sources, tagged with source name.

        Returns:
            List of telemetry dicts with '_source' field added
        """
        all_telemetry = []
        for name, source in self._sources.items():
            if isinstance(source, MeshMonitorDataSource):
                for item in source.telemetry:
                    tagged = dict(item)
                    tagged["_source"] = name
                    all_telemetry.append(tagged)
        return all_telemetry

    def get_all_traceroutes(self) -> list[dict]:
        """Get traceroutes from all MeshMonitor sources, tagged with source name.

        Returns:
            List of traceroute dicts with '_source' field added
        """
        all_traceroutes = []
        for name, source in self._sources.items():
            if isinstance(source, MeshMonitorDataSource):
                for item in source.traceroutes:
                    tagged = dict(item)
                    tagged["_source"] = name
                    all_traceroutes.append(tagged)
        return all_traceroutes

    def get_all_channels(self) -> list[dict]:
        """Get channels from all MeshMonitor sources, tagged with source name.

        Returns:
            List of channel dicts with '_source' field added
        """
        all_channels = []
        for name, source in self._sources.items():
            if isinstance(source, MeshMonitorDataSource):
                for item in source.channels:
                    tagged = dict(item)
                    tagged["_source"] = name
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

    @property
    def source_count(self) -> int:
        """Get number of active sources."""
        return len(self._sources)

    @property
    def source_names(self) -> list[str]:
        """Get list of source names."""
        return list(self._sources.keys())
