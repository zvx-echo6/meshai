"""Reset command handler."""

from .base import CommandContext, CommandHandler


class ResetCommand(CommandHandler):
    """Clear conversation history and summary."""

    name = "reset"
    description = "Clear your chat history"
    usage = "!reset"

    async def execute(self, args: str, context: CommandContext) -> str:
        """Clear conversation history and summary for the sender."""
        deleted = await context.history.clear_history(context.sender_id)

        # Also clear the conversation summary
        await context.history.clear_summary(context.sender_id)

        if deleted > 0:
            return f"Cleared {deleted} messages from history"
        else:
            return "No history to clear"
