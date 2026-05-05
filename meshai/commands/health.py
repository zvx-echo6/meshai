"""Health and region commands for mesh status."""

from typing import TYPE_CHECKING

from .base import CommandContext, CommandHandler

if TYPE_CHECKING:
    from ..mesh_data_store import MeshDataStore
    from ..mesh_health import MeshHealthEngine


# Infrastructure roles
INFRA_ROLES = {"ROUTER", "ROUTER_LATE", "ROUTER_CLIENT", "REPEATER"}


class HealthCommand(CommandHandler):
    """Quick mesh health summary."""

    name = "health"
    description = "Show mesh health summary"
    usage = "!health"
    aliases = ["mesh", "status"]

    def __init__(self, mesh_reporter=None):
        """Initialize with optional mesh reporter.

        Args:
            mesh_reporter: MeshReporter instance for health data
        """
        self._mesh_reporter = mesh_reporter

    async def execute(self, args: str, context: CommandContext) -> str:
        """Return compact mesh health summary."""
        if not self._mesh_reporter:
            return "Mesh health not available."

        return self._mesh_reporter.build_lora_compact("mesh")


class RegionCommand(CommandHandler):
    """Region health information."""

    name = "region"
    description = "Show region health info"
    usage = "!region [name]"
    aliases = ["reg"]

    def __init__(self, mesh_reporter=None):
        """Initialize with optional mesh reporter.

        Args:
            mesh_reporter: MeshReporter instance for health data
        """
        self._mesh_reporter = mesh_reporter

    async def execute(self, args: str, context: CommandContext) -> str:
        """Return region health info."""
        if not self._mesh_reporter:
            return "Mesh health not available."

        args = args.strip()

        if not args:
            # List all regions
            return self._mesh_reporter.list_regions_compact()

        # Get specific region detail (compact for LoRa)
        return self._mesh_reporter.build_lora_compact("region", args)


class NeighborCommand(CommandHandler):
    """Show infrastructure neighbors for a node."""

    name = "neighbors"
    description = "Show top infrastructure neighbors"
    usage = "!neighbors [node]"
    aliases = ["nbr", "nb"]

    def __init__(
        self,
        mesh_reporter=None,
        data_store: "MeshDataStore" = None,
        health_engine: "MeshHealthEngine" = None,
    ):
        """Initialize with mesh components.

        Args:
            mesh_reporter: MeshReporter instance
            data_store: MeshDataStore with edge/neighbor data
            health_engine: MeshHealthEngine for infrastructure detection
        """
        self._mesh_reporter = mesh_reporter
        self._data_store = data_store
        self._health_engine = health_engine

    async def execute(self, args: str, context: CommandContext) -> str:
        """Return top 5 infrastructure neighbors for a node."""
        if not self._data_store:
            return "Neighbor data not available."

        # Parse node argument
        node_name = args.strip() if args else None
        
        if not node_name:
            return "Usage: !neighbors <node>\nExample: !neighbors MHR"

        # Find the target node
        target = self._data_store.get_node(node_name)
        if not target:
            return f"Node '{node_name}' not found."

        # Get infrastructure neighbors from the node's neighbor list
        infra_neighbors = []
        for nb_num in target.neighbors:
            nb = self._data_store.get_node(str(nb_num))
            if nb and nb.role in INFRA_ROLES:
                # Try to find signal quality from multiple sources
                snr = None
                rssi = None
                
                # Source 1: Edge data
                for edge in self._data_store.edges:
                    if (edge.from_node == target.node_num and edge.to_node == nb_num) or \
                       (edge.to_node == target.node_num and edge.from_node == nb_num):
                        if edge.snr is not None:
                            snr = edge.snr
                        if edge.rssi is not None:
                            rssi = edge.rssi
                        break
                
                # Source 2: Neighbor node's own SNR field (fallback)
                if snr is None and nb.snr is not None:
                    snr = nb.snr
                if rssi is None and nb.rssi is not None:
                    rssi = nb.rssi

                infra_neighbors.append({
                    "long_name": nb.long_name or nb.short_name,
                    "short_name": nb.short_name,
                    "role": nb.role,
                    "snr": snr,
                    "rssi": rssi,
                })

        if not infra_neighbors:
            return f"{target.short_name} has no infrastructure neighbors."

        # Sort: by SNR descending if available, then alphabetically
        def sort_key(n):
            if n["snr"] is not None:
                return (0, -n["snr"])  # Has SNR, sort by SNR descending
            return (1, n["short_name"].lower())  # No SNR, sort alphabetically
        
        infra_neighbors.sort(key=sort_key)
        
        # Format output - top 5
        total = len(infra_neighbors)
        top5 = infra_neighbors[:5]
        
        lines = [f"{target.short_name} infra neighbors ({total}):"]
        for n in top5:
            line = f"{n['long_name']} ({n['short_name']})"
            if n["snr"] is not None:
                line += f" [SNR {n['snr']:.1f}]"
            lines.append(line)
        
        if total > 5:
            lines.append(f"...and {total - 5} more")

        return "\n".join(lines)
