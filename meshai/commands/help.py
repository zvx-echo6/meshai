"""Help command handler."""

from .base import CommandContext, CommandHandler


class HelpCommand(CommandHandler):
    """Display available commands."""

    name = "help"
    description = "Show available commands"
    usage = "!help"

    def __init__(self, dispatcher):
        self._dispatcher = dispatcher

    async def execute(self, args: str, context: CommandContext) -> str:
        """List all available commands."""
        commands = self._dispatcher.get_commands()

        # Build compact help text
        lines = ["Commands:"]
        for cmd in sorted(commands, key=lambda c: c.name):
            lines.append(f"!{cmd.name} - {cmd.description}")

        return " | ".join(lines)
