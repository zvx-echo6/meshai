"""Base classes for command handlers."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from ..config import Config
    from ..connector import MeshConnector
    from ..history import ConversationHistory


@dataclass
class CommandContext:
    """Context passed to command handlers."""

    sender_id: str  # Node ID of sender
    sender_name: str  # Display name of sender
    channel: int  # Channel message was received on
    is_dm: bool  # True if direct message
    position: Optional[tuple[float, float]]  # Sender's GPS position (lat, lon)

    # References to shared resources
    config: "Config"
    connector: "MeshConnector"
    history: "ConversationHistory"


class CommandHandler(ABC):
    """Base class for bang command handlers."""

    # Command name (without !)
    name: str = ""

    # Brief description for !help
    description: str = ""

    # Usage example
    usage: str = ""

    @abstractmethod
    async def execute(self, args: str, context: CommandContext) -> str:
        """Execute the command.

        Args:
            args: Arguments passed after the command (may be empty)
            context: Command execution context

        Returns:
            Response string to send back
        """
        pass
