"""Command dispatcher for bang commands."""

import logging
from typing import Optional

from .base import CommandContext, CommandHandler

logger = logging.getLogger(__name__)


class CommandDispatcher:
    """Registry and dispatcher for bang commands."""

    def __init__(self):
        self._commands: dict[str, CommandHandler] = {}

    def register(self, handler: CommandHandler) -> None:
        """Register a command handler.

        Args:
            handler: CommandHandler instance to register
        """
        name = handler.name.upper()
        self._commands[name] = handler
        logger.debug(f"Registered command: !{handler.name}")

    def get_commands(self) -> list[CommandHandler]:
        """Get all registered command handlers."""
        return list(self._commands.values())

    def is_command(self, text: str) -> bool:
        """Check if text is a bang command.

        Args:
            text: Message text to check

        Returns:
            True if text starts with !
        """
        return text.strip().startswith("!")

    def parse(self, text: str) -> tuple[Optional[str], str]:
        """Parse command and arguments from text.

        Args:
            text: Message text starting with !

        Returns:
            Tuple of (command_name, arguments) or (None, "") if invalid
        """
        text = text.strip()
        if not text.startswith("!"):
            return None, ""

        # Remove ! prefix
        text = text[1:]

        # Split into command and args
        parts = text.split(maxsplit=1)
        if not parts:
            return None, ""

        cmd = parts[0].upper()
        args = parts[1] if len(parts) > 1 else ""

        return cmd, args

    async def dispatch(self, text: str, context: CommandContext) -> Optional[str]:
        """Dispatch a command and return response.

        Args:
            text: Message text (must start with !)
            context: Command execution context

        Returns:
            Response string, or None if command not found
        """
        cmd, args = self.parse(text)

        if cmd is None:
            return None

        handler = self._commands.get(cmd)

        if handler is None:
            # Unknown command
            return f"Unknown command: !{cmd.lower()}. Try !help"

        try:
            logger.debug(f"Dispatching !{cmd.lower()} from {context.sender_id}")
            response = await handler.execute(args, context)
            return response

        except Exception as e:
            logger.error(f"Error executing !{cmd.lower()}: {e}")
            return f"Error: {str(e)[:100]}"


def create_dispatcher() -> CommandDispatcher:
    """Create and populate command dispatcher with default commands."""
    from .help import HelpCommand
    from .ping import PingCommand
    from .reset import ResetCommand
    from .status import StatusCommand
    from .weather import WeatherCommand

    dispatcher = CommandDispatcher()

    # Register all commands
    dispatcher.register(HelpCommand(dispatcher))
    dispatcher.register(PingCommand())
    dispatcher.register(ResetCommand())
    dispatcher.register(StatusCommand())
    dispatcher.register(WeatherCommand())

    return dispatcher
