"""Status command handler."""

import time
from datetime import timedelta

from .. import __version__
from .base import CommandContext, CommandHandler

# Track bot start time
_start_time: float = time.time()


def set_start_time(t: float) -> None:
    """Set bot start time (called from main)."""
    global _start_time
    _start_time = t


class StatusCommand(CommandHandler):
    """Show bot status information."""

    name = "status"
    description = "Show bot status"
    usage = "!status"

    async def execute(self, args: str, context: CommandContext) -> str:
        """Return bot status information."""
        # Calculate uptime
        uptime_seconds = int(time.time() - _start_time)
        uptime = str(timedelta(seconds=uptime_seconds))

        # Get history stats
        stats = await context.history.get_stats()

        # Build status message
        parts = [
            f"MeshAI v{__version__}",
            f"Up: {uptime}",
            f"Users: {stats['unique_users']}",
            f"Msgs: {stats['total_messages']}",
        ]

        return " | ".join(parts)
