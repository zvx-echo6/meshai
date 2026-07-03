"""Help command handler."""

from .base import CommandContext, CommandHandler


class HelpCommand(CommandHandler):
    """Display available commands."""

    name = "help"
    description = "Show available commands"
    usage = "!help [command]"

    def __init__(self, dispatcher):
        self._dispatcher = dispatcher

    async def execute(self, args: str, context: CommandContext) -> str:
        if args and args.strip():
            return self._command_help(args.strip().lower())
        return self._list_all()

    def _list_all(self) -> str:
        """List all commands grouped by category."""
        commands = self._dispatcher.get_commands()

        # Deduplicate aliases
        seen = set()
        unique = []
        for cmd in commands:
            if cmd.name.lower() not in seen:
                seen.add(cmd.name.lower())
                unique.append(cmd)

        # Group by category
        health_names = {"health", "region", "neighbors"}

        health_cmds = [c for c in unique if c.name.lower() in health_names]
        other_cmds = [c for c in unique if c.name.lower() not in health_names and c.name.lower() != "help"]

        lines = ["Commands:"]

        if health_cmds:
            lines.append("")
            lines.append("Mesh Health:")
            for c in sorted(health_cmds, key=lambda x: x.name):
                lines.append(f"  !{c.name} - {c.description}")

        if other_cmds:
            lines.append("")
            lines.append("Other:")
            for c in sorted(other_cmds, key=lambda x: x.name):
                lines.append(f"  !{c.name} - {c.description}")

        lines.append("")
        lines.append("!help [cmd] for details")
        lines.append("Or just ask me naturally!")

        return "\n".join(lines)

    def _command_help(self, cmd_name: str) -> str:
        """Detailed help for a specific command."""
        aliases = {
            "health": "health", "mesh": "health",
            "region": "region", "reg": "region",
            "neighbors": "neighbors", "nbr": "neighbors", "nb": "neighbors",
            "clear": "clear", "reset": "clear",
        }
        resolved = aliases.get(cmd_name, cmd_name)

        # Check if this command is actually registered
        registered = {c.name.lower() for c in self._dispatcher.get_commands()}

        texts = {
            "health": (
                "Mesh Health\n\n"
                "  !health     - 5-pillar health summary\n"
                "  !health now - force fresh data\n\n"
                "Or ask: 'how's the mesh?'"
            ),
            "region": (
                "Region Info\n\n"
                "  !region       - list all regions\n"
                "  !region SCID  - South Central ID\n"
                "  !region boise - South Western ID"
            ),
            "neighbors": (
                "Neighbors\n\n"
                "  !neighbors MHR - infra neighbors + signal\n"
                "  !nb T2T        - alias"
            ),
            "clear": "!clear or !reset - clears conversation history",
            "ping": "!ping - connectivity test, responds with pong",
            "status": "!status - shows version, uptime, message count",
            "weather": "!weather [location] - weather lookup",
        }

        help_text = texts.get(resolved)
        if help_text and resolved in registered:
            return help_text
        elif help_text and resolved not in registered:
            return f"The !{resolved} command is not currently enabled."
        else:
            return f"No help available for '{cmd_name}'. Try !help"
