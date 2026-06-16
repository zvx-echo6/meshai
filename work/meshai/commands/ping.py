"""Ping command handler."""

from .base import CommandContext, CommandHandler


class PingCommand(CommandHandler):
    """Simple connectivity test."""

    name = "ping"
    description = "Test connectivity"
    usage = "!ping"

    async def execute(self, args: str, context: CommandContext) -> str:
        """Respond with pong."""
        return "pong"
