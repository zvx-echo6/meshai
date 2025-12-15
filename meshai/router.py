"""Message routing logic for MeshAI."""

import logging
import re
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional

from .backends.base import LLMBackend
from .commands import CommandContext, CommandDispatcher
from .config import Config
from .connector import MeshConnector, MeshMessage
from .history import ConversationHistory

logger = logging.getLogger(__name__)


class RouteType(Enum):
    """Type of message routing."""

    IGNORE = auto()  # Don't respond
    COMMAND = auto()  # Bang command
    LLM = auto()  # Route to LLM


@dataclass
class RouteResult:
    """Result of routing decision."""

    route_type: RouteType
    response: Optional[str] = None  # For commands, the response
    query: Optional[str] = None  # For LLM, the cleaned query


class MessageRouter:
    """Routes incoming messages to appropriate handlers."""

    def __init__(
        self,
        config: Config,
        connector: MeshConnector,
        history: ConversationHistory,
        dispatcher: CommandDispatcher,
        llm_backend: LLMBackend,
    ):
        self.config = config
        self.connector = connector
        self.history = history
        self.dispatcher = dispatcher
        self.llm = llm_backend

        # Compile mention pattern
        bot_name = re.escape(config.bot.name)
        self._mention_pattern = re.compile(rf"@{bot_name}\b", re.IGNORECASE)

    def should_respond(self, message: MeshMessage) -> bool:
        """Determine if we should respond to this message.

        Args:
            message: Incoming message

        Returns:
            True if we should process this message
        """
        # Always ignore our own messages
        if message.sender_id == self.connector.my_node_id:
            return False

        # Check if DM
        if message.is_dm:
            return self.config.bot.respond_to_dms

        # Check channel filtering
        if self.config.channels.mode == "whitelist":
            if message.channel not in self.config.channels.whitelist:
                return False

        # Check for @mention
        if self.config.bot.respond_to_mentions:
            if self._mention_pattern.search(message.text):
                return True

        # Check for bang command (always respond to commands)
        if self.dispatcher.is_command(message.text):
            return True

        # Not a DM, no mention, no command - ignore
        return False

    async def route(self, message: MeshMessage) -> RouteResult:
        """Route a message and generate response.

        Args:
            message: Incoming message to route

        Returns:
            RouteResult with routing decision and any response
        """
        text = message.text.strip()

        # Check for bang command first
        if self.dispatcher.is_command(text):
            context = self._make_command_context(message)
            response = await self.dispatcher.dispatch(text, context)
            return RouteResult(RouteType.COMMAND, response=response)

        # Clean up the message (remove @mention)
        query = self._clean_query(text)

        if not query:
            return RouteResult(RouteType.IGNORE)

        # Route to LLM
        return RouteResult(RouteType.LLM, query=query)

    async def generate_llm_response(self, message: MeshMessage, query: str) -> str:
        """Generate LLM response for a message.

        Args:
            message: Original message
            query: Cleaned query text

        Returns:
            Generated response
        """
        # Add user message to history
        await self.history.add_message(message.sender_id, "user", query)

        # Get conversation history
        history = await self.history.get_history_for_llm(message.sender_id)

        # Generate response with user_id for memory optimization
        # Use system prompt only if enabled in config
        system_prompt = ""
        if getattr(self.config.llm, 'use_system_prompt', True):
            system_prompt = self.config.llm.system_prompt

        try:
            response = await self.llm.generate(
                messages=history,
                system_prompt=system_prompt,
                max_tokens=500,  # Increased for web search/RAG responses
                user_id=message.sender_id,  # Enable memory optimization
            )
        except Exception as e:
            logger.error(f"LLM generation error: {e}")
            response = "Sorry, I encountered an error. Please try again."

        # Add assistant response to history
        await self.history.add_message(message.sender_id, "assistant", response)

        # Persist summary if one was created/updated
        await self._persist_summary(message.sender_id)

        return response

    async def _persist_summary(self, user_id: str) -> None:
        """Persist any cached summary to the database.

        Args:
            user_id: User identifier
        """
        memory = self.llm.get_memory()
        if not memory:
            return

        summary = memory.get_cached_summary(user_id)
        if summary:
            await self.history.store_summary(
                user_id,
                summary.summary,
                summary.message_count,
            )
            logger.debug(f"Persisted summary for {user_id}")

    def _clean_query(self, text: str) -> str:
        """Remove @mention from query text."""
        # Remove @botname mention
        cleaned = self._mention_pattern.sub("", text)
        # Clean up extra whitespace
        cleaned = " ".join(cleaned.split())
        return cleaned.strip()

    def _make_command_context(self, message: MeshMessage) -> CommandContext:
        """Create command context from message."""
        return CommandContext(
            sender_id=message.sender_id,
            sender_name=message.sender_name,
            channel=message.channel,
            is_dm=message.is_dm,
            position=message.sender_position,
            config=self.config,
            connector=self.connector,
            history=self.history,
        )
