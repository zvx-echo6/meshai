"""Message routing logic for MeshAI."""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
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


# advBBS protocol and notification prefixes to ignore
ADVBBS_PREFIXES = (
    "MAILREQ|", "MAILACK|", "MAILNAK|", "MAILDAT|", "MAILDLV|",
    "BOARDREQ|", "BOARDACK|", "BOARDNAK|", "BOARDDAT|", "BOARDDLV|",
    "advBBS|",
    "[MAIL]",
)

# Patterns that suggest prompt injection attempts
_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?previous", re.IGNORECASE),
    re.compile(r"ignore\s+your\s+instructions", re.IGNORECASE),
    re.compile(r"disregard\s+(all\s+)?previous", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\b", re.IGNORECASE),
    re.compile(r"new\s+instructions?\s*:", re.IGNORECASE),
    re.compile(r"system\s*prompt\s*:", re.IGNORECASE),
]


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

        # Ignore advBBS protocol and notification messages
        if self.config.bot.filter_bbs_protocols:
            if any(message.text.startswith(p) for p in ADVBBS_PREFIXES):
                logger.debug(f"Ignoring advBBS message from {message.sender_id}: {message.text[:40]}...")
                return False

        # Check if DM — conversational mode only, skip !commands
        # (let MeshMonitor or other bots handle bang commands in DMs)
        if message.is_dm:
            if self.dispatcher.is_command(message.text):
                return False
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

        # Get system prompt from config, inject current date
        system_prompt = ""
        if getattr(self.config.llm, 'use_system_prompt', True):
            today = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
            system_prompt = f"{self.config.llm.system_prompt}\nToday's date is {today}."

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
        """Remove @mention and check for prompt injection."""
        # Remove @botname mention
        cleaned = self._mention_pattern.sub("", text)
        # Clean up extra whitespace
        cleaned = " ".join(cleaned.split())
        cleaned = cleaned.strip()

        # Check for prompt injection
        for pattern in _INJECTION_PATTERNS:
            if pattern.search(cleaned):
                logger.warning(
                    f"Possible prompt injection detected: {cleaned[:80]}..."
                )
                match = pattern.search(cleaned)
                cleaned = cleaned[:match.start()].strip()
                if not cleaned:
                    cleaned = "Hello"
                break

        return cleaned

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
