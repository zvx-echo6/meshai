"""Clear command handler (alias for !reset)."""

from .base import CommandContext, CommandHandler


class ClearCommand(CommandHandler):
    """Clear conversation history and summary."""

    name = "clear"
    description = "Clear your chat history"
    usage = "!clear"

    async def execute(self, args: str, context: CommandContext) -> str:
        """Clear conversation history and summary for the sender."""
        await context.history.clear_history(context.sender_id)
        await context.history.clear_summary(context.sender_id)
        return "Conversation memory cleared."
