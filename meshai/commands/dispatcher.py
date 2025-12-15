"""Command dispatcher for bang commands."""

import logging
from typing import Optional

from .base import CommandContext, CommandHandler

logger = logging.getLogger(__name__)


class CustomCommandHandler(CommandHandler):
    """Handler for user-defined static response commands."""

    def __init__(self, name: str, response: str, description: str = "Custom command"):
        self._name = name
        self._response = response
        self._description = description

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def usage(self) -> str:
        return f"!{self._name}"

    async def execute(self, args: str, context: CommandContext) -> str:
        return self._response


class CommandDispatcher:
    """Registry and dispatcher for bang commands."""

    def __init__(self, prefix: str = "!", disabled_commands: Optional[list[str]] = None):
        self._commands: dict[str, CommandHandler] = {}
        self._custom_commands: dict[str, str] = {}
        self.prefix = prefix
        self.disabled_commands = set(c.upper() for c in (disabled_commands or []))

    def register(self, handler: CommandHandler) -> None:
        """Register a command handler.

        Args:
            handler: CommandHandler instance to register
        """
        name = handler.name.upper()
        if name in self.disabled_commands:
            logger.debug(f"Skipping disabled command: !{handler.name}")
            return
        self._commands[name] = handler
        logger.debug(f"Registered command: !{handler.name}")

    def register_custom(self, name: str, response: str, description: str = "Custom command") -> None:
        """Register a custom static response command.

        Args:
            name: Command name (without prefix)
            response: Static response text
            description: Command description for help
        """
        handler = CustomCommandHandler(name, response, description)
        self.register(handler)
        self._custom_commands[name.upper()] = response

    def unregister(self, name: str) -> bool:
        """Unregister a command.

        Args:
            name: Command name to remove

        Returns:
            True if command was removed, False if not found
        """
        name = name.upper()
        if name in self._commands:
            del self._commands[name]
            self._custom_commands.pop(name, None)
            return True
        return False

    def get_commands(self) -> list[CommandHandler]:
        """Get all registered command handlers."""
        return list(self._commands.values())

    def is_command(self, text: str) -> bool:
        """Check if text is a bang command.

        Args:
            text: Message text to check

        Returns:
            True if text starts with command prefix
        """
        return text.strip().startswith(self.prefix)

    def parse(self, text: str) -> tuple[Optional[str], str]:
        """Parse command and arguments from text.

        Args:
            text: Message text starting with command prefix

        Returns:
            Tuple of (command_name, arguments) or (None, "") if invalid
        """
        text = text.strip()
        if not text.startswith(self.prefix):
            return None, ""

        # Remove prefix
        text = text[len(self.prefix):]

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


def create_dispatcher(
    prefix: str = "!",
    disabled_commands: Optional[list[str]] = None,
    custom_commands: Optional[dict] = None,
) -> CommandDispatcher:
    """Create and populate command dispatcher with default commands.

    Args:
        prefix: Command prefix (default: "!")
        disabled_commands: List of command names to disable
        custom_commands: Dict of name -> response for custom commands

    Returns:
        Configured CommandDispatcher
    """
    from .help import HelpCommand
    from .ping import PingCommand
    from .reset import ResetCommand
    from .status import StatusCommand
    from .weather import WeatherCommand

    dispatcher = CommandDispatcher(prefix=prefix, disabled_commands=disabled_commands)

    # Register all built-in commands
    dispatcher.register(HelpCommand(dispatcher))
    dispatcher.register(PingCommand())
    dispatcher.register(ResetCommand())
    dispatcher.register(StatusCommand())
    dispatcher.register(WeatherCommand())

    # Register custom commands
    if custom_commands:
        for name, response in custom_commands.items():
            if isinstance(response, dict):
                # Support dict format: {response: "...", description: "..."}
                dispatcher.register_custom(
                    name,
                    response.get("response", ""),
                    response.get("description", "Custom command"),
                )
            else:
                # Simple string response
                dispatcher.register_custom(name, str(response))

    return dispatcher
