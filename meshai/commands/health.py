"""Health and region commands for mesh status."""

from .base import CommandContext, CommandHandler


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
