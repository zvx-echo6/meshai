"""Message routing logic for MeshAI."""

import asyncio
import logging
import re
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional

from .backends.base import LLMBackend
from .commands import CommandContext, CommandDispatcher
from .config import Config
from .connector import MeshConnector, MeshMessage
from .context import MeshContext
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
        context: MeshContext = None,
        meshmonitor_sync=None,
    ):
        self.config = config
        self.connector = connector
        self.history = history
        self.dispatcher = dispatcher
        self.llm = llm_backend
        self.context = context
        self.meshmonitor_sync = meshmonitor_sync


    def should_respond(self, message: MeshMessage) -> bool:
        """Determine if we should respond to this message.

        DM-only bot: ignores all public channel messages.
        Commands and conversational LLM responses both work in DMs.

        Args:
            message: Incoming message

        Returns:
            True if we should process this message
        """
        # Always ignore our own messages
        if message.sender_id == self.connector.my_node_id:
            return False

        # Only respond to DMs
        if not message.is_dm:
            return False

        if not self.config.bot.respond_to_dms:
            return False

        # Ignore advBBS protocol and notification messages
        if self.config.bot.filter_bbs_protocols:
            if any(message.text.startswith(p) for p in ADVBBS_PREFIXES):
                logger.debug(f"Ignoring advBBS message from {message.sender_id}: {message.text[:40]}...")
                return False

        # Ignore messages that MeshMonitor will handle
        if self.meshmonitor_sync and self.meshmonitor_sync.matches(message.text):
            logger.debug(f"Ignoring MeshMonitor-handled message: {message.text[:40]}...")
            return False

        return True

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

        # Build system prompt in order: identity -> static -> meshmonitor -> context

        # 1. Dynamic identity from bot config
        bot_name = self.config.bot.name or "MeshAI"
        bot_owner = self.config.bot.owner or "Unknown"

        identity = (
            f"You are {bot_name}, an LLM-powered conversational assistant running on a "
            f"Meshtastic mesh network. Your managing operator is {bot_owner}. "
            f"You are open source at github.com/zvx-echo6/meshai.\n\n"
            f"IDENTITY: Your name is {bot_name}. You respond to DMs only. You connect "
            f"to a Meshtastic node via TCP through meshtasticd.\n\n"
        )

        # 2. Static system prompt from config
        static_prompt = ""
        if getattr(self.config.llm, 'use_system_prompt', True):
            static_prompt = self.config.llm.system_prompt

        system_prompt = identity + static_prompt

        # 3. MeshMonitor info (only when enabled)
        if (
            self.meshmonitor_sync
            and self.config.meshmonitor.enabled
            and self.config.meshmonitor.inject_into_prompt
        ):
            meshmonitor_intro = (
                "\n\nMESHMONITOR: You run alongside MeshMonitor (by Yeraze) on the same "
                "meshtasticd node. MeshMonitor handles web dashboard, maps, telemetry, "
                "traceroutes, security scanning, and auto-responder commands. Its trigger "
                "commands are listed below — if someone asks what commands are available, "
                "mention both yours and MeshMonitor's. If someone asks where to get "
                "MeshMonitor, direct them to github.com/Yeraze/meshmonitor"
            )
            system_prompt += meshmonitor_intro

            commands_summary = self.meshmonitor_sync.get_commands_summary()
            if commands_summary:
                system_prompt += "\n\n" + commands_summary

        # 4. Inject mesh context if available
        if self.context:
            max_items = getattr(self.config.context, 'max_context_items', 20)
            context_block = self.context.get_context_block(max_items=max_items)
            if context_block:
                system_prompt += (
                    "\n\n--- Recent mesh traffic (for context only, not messages to you) ---\n"
                    + context_block
                )
            else:
                system_prompt += (
                    "\n\n[No recent mesh traffic observed yet.]"
                )

        try:
            response = await self.llm.generate(
                messages=history,
                system_prompt=system_prompt,
                max_tokens=500,
            )
        except asyncio.TimeoutError:
            logger.error("LLM request timed out")
            response = "Sorry, request timed out. Try again."
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
        """Clean up query text and check for prompt injection."""
        cleaned = " ".join(text.split())
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
