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
            return None

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
    mesh_reporter=None,
    data_store=None,
    health_engine=None,
    env_store=None,
) -> CommandDispatcher:
    """Create and populate command dispatcher with default commands.

    Args:
        prefix: Command prefix (default: "!")
        disabled_commands: List of command names to disable
        custom_commands: Dict of name -> response for custom commands
        mesh_reporter: MeshReporter instance for health commands
        data_store: MeshDataStore for neighbor data
        health_engine: MeshHealthEngine for infrastructure detection
        env_store: EnvironmentalStore for weather/propagation commands

    Returns:
        Configured CommandDispatcher
    """
    from .clear import ClearCommand
    from .help import HelpCommand
    from .ping import PingCommand
    from .reset import ResetCommand
    from .status import StatusCommand
    from .weather import WeatherCommand
    from .health import HealthCommand, RegionCommand, NeighborCommand

    dispatcher = CommandDispatcher(prefix=prefix, disabled_commands=disabled_commands)

    # Register all built-in commands
    dispatcher.register(ClearCommand())
    dispatcher.register(HelpCommand(dispatcher))
    dispatcher.register(PingCommand())
    dispatcher.register(ResetCommand())
    dispatcher.register(StatusCommand())
    dispatcher.register(WeatherCommand())

    # Register mesh health commands
    health_cmd = HealthCommand(mesh_reporter)
    dispatcher.register(health_cmd)
    # Register aliases for health command
    for alias in getattr(health_cmd, 'aliases', []):
        alias_handler = HealthCommand(mesh_reporter)
        alias_handler.name = alias
        dispatcher.register(alias_handler)

    region_cmd = RegionCommand(mesh_reporter)
    dispatcher.register(region_cmd)
    # Register aliases for region command
    for alias in getattr(region_cmd, 'aliases', []):
        alias_handler = RegionCommand(mesh_reporter)
        alias_handler.name = alias
        dispatcher.register(alias_handler)

    # Register neighbors command
    neighbor_cmd = NeighborCommand(mesh_reporter, data_store, health_engine)
    dispatcher.register(neighbor_cmd)
    # Register aliases for neighbors command
    for alias in getattr(neighbor_cmd, 'aliases', []):
        alias_handler = NeighborCommand(mesh_reporter, data_store, health_engine)
        alias_handler.name = alias
        dispatcher.register(alias_handler)

    # Register environmental commands
    if env_store:
        from .alerts_cmd import AlertsCommand
        from .solar_cmd import SolarCommand

        alerts_cmd = AlertsCommand(env_store)
        dispatcher.register(alerts_cmd)

        solar_cmd = SolarCommand(env_store)
        dispatcher.register(solar_cmd)

        # Register !hf as an alias for !solar
        hf_cmd = SolarCommand(env_store)
        hf_cmd.name = "hf"
        dispatcher.register(hf_cmd)

        # Register !wx-alerts as an alias for !alerts
        wx_cmd = AlertsCommand(env_store)
        wx_cmd.name = "wx-alerts"
        dispatcher.register(wx_cmd)

        # Register fire command
        from .fire_cmd import FireCommand
        fire_cmd = FireCommand(env_store)
        dispatcher.register(fire_cmd)

        # Register satellite pass prediction command
        from .satpass_cmd import SatpassCommand
        satpass_cmd = SatpassCommand()
        dispatcher.register(satpass_cmd)

        # Register avalanche command
        from .avy_cmd import AvalancheCommand
        avy_cmd = AvalancheCommand(env_store)
        dispatcher.register(avy_cmd)

        # Register !avalanche as alias for !avy
        avalanche_cmd = AvalancheCommand(env_store)
        avalanche_cmd.name = "avalanche"
        dispatcher.register(avalanche_cmd)

        # Register streams command
        from .streams_cmd import StreamsCommand
        streams_cmd = StreamsCommand(env_store)
        dispatcher.register(streams_cmd)
        for alias in getattr(streams_cmd, 'aliases', []):
            alias_handler = StreamsCommand(env_store)
            alias_handler.name = alias
            dispatcher.register(alias_handler)

        # Register roads command
        from .roads_cmd import RoadsCommand
        roads_cmd = RoadsCommand(env_store)
        dispatcher.register(roads_cmd)
        for alias in getattr(roads_cmd, 'aliases', []):
            alias_handler = RoadsCommand(env_store)
            alias_handler.name = alias
            dispatcher.register(alias_handler)

        # Register hotspots command (NASA FIRMS satellite fire detection)
        from .hotspots_cmd import HotspotsCommand
        hotspots_cmd = HotspotsCommand(env_store)
        dispatcher.register(hotspots_cmd)
        for alias in getattr(hotspots_cmd, 'aliases', []):
            alias_handler = HotspotsCommand(env_store)
            alias_handler.name = alias
            dispatcher.register(alias_handler)

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
