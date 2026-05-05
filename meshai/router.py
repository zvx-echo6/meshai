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
from .chunker import chunk_response, ContinuationState

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

# Keywords that indicate mesh-related questions
_MESH_KEYWORDS = {
    "mesh", "network", "health", "nodes", "node", "utilization", "signal",
    "coverage", "battery", "solar", "offline", "router", "channel", "packet",
    "hop", "optimize", "optimization", "infrastructure", "infra", "relay",
    "repeater", "region", "locality", "congestion", "collision", "airtime",
    "telemetry", "firmware", "subscribe", "alert", "snr", "rssi",
    # Additional keywords for better detection
    "noisy", "noisiest", "traffic", "packets", "power", "routers",
    "repeaters", "regions", "localities", "score", "status",
}

# Phrases that indicate mesh questions
_MESH_PHRASES = [
    "how's the mesh",
    "hows the mesh",
    "mesh status",
    "what's wrong",
    "whats wrong",
    "check node",
    "node status",
    "network health",
    "mesh health",
    "which node",
    "which nodes",
    "which infra",
    "list nodes",
    "list infra",
    "tell me about",
    "what about",
    "how is",
    "how are",
]

# City name to region mapping (hardcoded fallback)
# City/alias mapping now built from config - see _build_alias_map()

# Mesh awareness instruction for LLM
# Mesh awareness instruction for LLM
_MESH_AWARENESS_PROMPT = """
MESH DATA RESPONSE RULES (OVERRIDE brevity rules for mesh/network questions):

The data blocks above contain detailed information about every region, infrastructure node,
coverage gap, and problem node on the mesh. USE THIS DATA in your response.

RESPONSE STYLE:
- DETAILED, data-driven responses. Reference specific node names, scores, gateway counts.
- Use LOCAL NAMES from the region descriptions (Magic Valley, Treasure Valley, etc.)
- ALWAYS use local region names: say "Treasure Valley" not "South Western ID", say "Magic Valley" not "South Central ID". The code names mean nothing to users.
- When listing nodes, be concise: "BT Base c8d5 — via AIDA" not "BT Base c8d5 (c8d5) is connected via AIDA-MeshMonitor in the South Western ID region."
- Don't repeat the region on every line when listing multiple nodes in the same region. Say the region once at the top, then just list the nodes.
- Don't include shortnames in parentheses when you're already giving the full name — it's noise.
- When discussing infrastructure, name the actual nodes (Mount Harrison Router, not just "5 infra")
- When discussing coverage gaps, explain WHERE and HOW MANY nodes are affected
- When discussing problems, name the node and explain the impact
- You CAN use 3-5 messages. Keep each sentence under 150 characters.
- No markdown formatting - plain text only

QUESTION TYPES:
- "How's the mesh?" -> Lead with composite score. Highlight 1-2 biggest issues by name. Summarize each region briefly.
- "Where do we need coverage?" -> Name regions with single-gateway nodes. Name offline infra. Suggest specific locations.
- "Tell me about [node]" -> Give full detail from the data above.
- "How is [region]?" -> Give that region's infrastructure status, coverage, issues.
- "What's wrong?" -> List problem nodes by name with specifics.

IMPORTANT: Do NOT lump different regions together. Each is a distinct area.
Do NOT recommend infrastructure for "Unlocated" nodes - they have no known position.
"""


def _build_region_abbreviations(region_names: list[str]) -> dict[str, str]:
    """Build abbreviation to region name mapping.

    Generates abbreviations like:
    - "South Central ID" -> "SCID", "SC-ID", "SC ID"
    - "South Western ID" -> "SWID", "SW-ID", "SW ID"

    Args:
        region_names: List of full region names

    Returns:
        Dict mapping lowercase abbreviation to full region name
    """
    abbrevs = {}

    for name in region_names:
        parts = name.replace("???", "-").replace("???", "-").split()
        if not parts:
            continue

        # Get first letter of each word (uppercase)
        initials = "".join(p[0].upper() for p in parts if p)
        abbrevs[initials.lower()] = name

        # If last part is a state abbrev (2 chars), create variants
        if len(parts) >= 2:
            last = parts[-1]
            if len(last) == 2 and last.isupper():
                # "South Central ID" -> prefix is "South Central"
                prefix_parts = parts[:-1]
                prefix_initials = "".join(p[0].upper() for p in prefix_parts)

                # SC-ID, SC ID, SCID variants
                abbrevs[f"{prefix_initials.lower()}-{last.lower()}"] = name
                abbrevs[f"{prefix_initials.lower()} {last.lower()}"] = name
                abbrevs[f"{prefix_initials.lower()}{last.lower()}"] = name

    return abbrevs


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
        knowledge=None,
        source_manager=None,
        health_engine=None,
        mesh_reporter=None,
    ):
        self.config = config
        self.connector = connector
        self.history = history
        self.dispatcher = dispatcher
        self.llm = llm_backend
        self.context = context
        self.meshmonitor_sync = meshmonitor_sync
        self.knowledge = knowledge
        self.source_manager = source_manager
        self.health_engine = health_engine
        self.mesh_reporter = mesh_reporter
        self.continuations = ContinuationState(max_continuations=3)

        # Per-user mesh context tracking for follow-up handling
        # Maps user_id -> {"last_was_mesh": bool, "last_scope": (type, value), "non_mesh_count": int}
        self._user_mesh_context: dict[str, dict] = {}

        # Build region abbreviation map
        self._region_abbrevs: dict[str, str] = {}
        if self.health_engine and self.health_engine.regions:
            region_names = [r.name for r in self.health_engine.regions]
            self._region_abbrevs = _build_region_abbreviations(region_names)
            logger.debug(f"Built region abbreviations: {self._region_abbrevs}")

        # Build city/alias mapping from config
        self._alias_map = self._build_alias_map()
        if self._alias_map:
            logger.debug(f"Built alias map with {len(self._alias_map)} entries")

    def _build_alias_map(self) -> dict[str, str]:
        """Build city/alias to region mapping from config."""
        alias_map = {}
        if self.config.mesh_intelligence and self.config.mesh_intelligence.regions:
            for region in self.config.mesh_intelligence.regions:
                # Add aliases
                for alias in (getattr(region, 'aliases', []) or []):
                    alias_map[alias.lower()] = region.name
                # Add cities  
                for city in (getattr(region, 'cities', []) or []):
                    alias_map[city.lower()] = region.name
                # Add local_name
                local = getattr(region, 'local_name', '') or ''
                if local:
                    alias_map[local.lower()] = region.name
        return alias_map

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

    def check_continuation(self, message) -> list[str] | None:
        """Check if this is a continuation request and return messages if so.

        Returns:
            List of messages to send, or None if not a continuation
        """
        user_id = message.sender_id
        text = message.text.strip()

        logger.debug(f"check_continuation: user={user_id}, text='{text[:30]}', has_pending={self.continuations.has_pending(user_id)}")

        if self.continuations.has_pending(user_id):
            if self.continuations.is_continuation_request(text):
                result = self.continuations.get_continuation(user_id)
                if result:
                    messages, _ = result
                    return messages
                # Max continuations reached, return None to fall through
            else:
                # User asked something new, clear pending continuation
                self.continuations.clear(user_id)

        return None

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

    def _is_mesh_question(self, message: str) -> bool:
        """Check if message is asking about mesh health/status.

        Args:
            message: User message text

        Returns:
            True if this is a mesh-related question
        """
        msg_lower = message.lower()

        # Check for mesh phrases
        for phrase in _MESH_PHRASES:
            if phrase in msg_lower:
                return True

        # Check for mesh keywords
        words = set(re.findall(r'\b\w+\b', msg_lower))
        if words & _MESH_KEYWORDS:
            return True

        return False

    def _detect_mesh_scope(self, message: str) -> tuple[str, Optional[str]]:
        """Detect the scope of a mesh question.

        Args:
            message: User message text

        Returns:
            Tuple of (scope_type, scope_value):
            - ("node", "{identifier}") if asking about specific node
            - ("region", "{region_name}") if asking about specific region
            - ("mesh", None) for general mesh questions
        """
        msg_lower = message.lower()

        # === NODE MATCHING (check first - more specific) ===
        if self.health_engine and self.health_engine.mesh_health:
            health = self.health_engine.mesh_health

            # 1. Exact shortname match (case-insensitive, word boundary)
            for node in health.nodes.values():
                if node.short_name:
                    pattern = r'\b' + re.escape(node.short_name.lower()) + r'\b'
                    if re.search(pattern, msg_lower):
                        return ("node", node.short_name)

            # 2. Longname substring match (case-insensitive)
            for node in health.nodes.values():
                if node.long_name and len(node.long_name) > 3:
                    # Match significant portion of longname
                    if node.long_name.lower() in msg_lower:
                        return ("node", node.short_name or node.node_id)
                    # Also try matching without common suffixes like "Router", "Repeater"
                    clean_name = node.long_name.lower()
                    for suffix in [" router", " repeater", " relay", " base", " v2", " - g2"]:
                        clean_name = clean_name.replace(suffix, "")
                    if len(clean_name) > 4 and clean_name in msg_lower:
                        return ("node", node.short_name or node.node_id)

            # 3. NodeId hex match (with or without ! prefix)
            hex_pattern = r'!?([0-9a-f]{8})'
            hex_match = re.search(hex_pattern, msg_lower)
            if hex_match:
                hex_id = hex_match.group(1)
                for nid, node in health.nodes.items():
                    if hex_id in nid.lower():
                        return ("node", node.short_name or nid)

            # 4. NodeNum decimal match
            num_pattern = r'\b(\d{9,10})\b'
            num_match = re.search(num_pattern, message)
            if num_match:
                node_num = int(num_match.group(1))
                hex_id = format(node_num, 'x')
                for nid, node in health.nodes.items():
                    if hex_id in nid.lower():
                        return ("node", node.short_name or nid)

        # === REGION MATCHING ===
        if self.health_engine:
            # 1. Check abbreviations first (SCID, SWID, etc.)
            for abbrev, region_name in self._region_abbrevs.items():
                # Match as word boundary
                pattern = r'\b' + re.escape(abbrev) + r'\b'
                if re.search(pattern, msg_lower):
                    return ("region", region_name)

            # 2. Check city names and aliases from config
            for alias, region_name in self._alias_map.items():
                if alias in msg_lower:
                    return ("region", region_name)

            # 3. Full region name matching (SORTED BY LENGTH - longest first)
            regions_by_length = sorted(
                self.health_engine.regions,
                key=lambda r: len(r.name),
                reverse=True
            )

            for anchor in regions_by_length:
                anchor_lower = anchor.name.lower()
                # Check full region name
                if anchor_lower in msg_lower:
                    return ("region", anchor.name)

            # 4. Partial region name matching (also longest first)
            for anchor in regions_by_length:
                anchor_lower = anchor.name.lower()
                # Check significant parts of region name
                # Split on common separators
                parts = anchor_lower.replace("-", " ").replace("???", " ").replace("???", " ").split()
                # Only match on significant words (>3 chars, not state abbrevs)
                significant_parts = [p for p in parts if len(p) > 3]

                # Check if ALL significant parts appear in message
                if significant_parts and all(p in msg_lower for p in significant_parts):
                    return ("region", anchor.name)

        return ("mesh", None)

    def _get_user_mesh_context(self, user_id: str) -> dict:
        """Get or create mesh context for a user."""
        if user_id not in self._user_mesh_context:
            self._user_mesh_context[user_id] = {
                "last_was_mesh": False,
                "last_scope": ("mesh", None),
                "non_mesh_count": 0,
            }
        return self._user_mesh_context[user_id]

    def _update_user_mesh_context(
        self,
        user_id: str,
        is_mesh: bool,
        scope: tuple[str, Optional[str]] = None,
    ) -> None:
        """Update mesh context tracking for a user."""
        ctx = self._get_user_mesh_context(user_id)

        if is_mesh:
            ctx["last_was_mesh"] = True
            ctx["non_mesh_count"] = 0
            if scope:
                ctx["last_scope"] = scope
        else:
            ctx["non_mesh_count"] += 1
            # Reset after 2 consecutive non-mesh messages
            if ctx["non_mesh_count"] >= 2:
                ctx["last_was_mesh"] = False
                ctx["last_scope"] = ("mesh", None)

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

        # Build system prompt in order: identity -> static -> meshmonitor -> context -> knowledge -> mesh

        # 1. Dynamic identity from bot config
        bot_name = self.config.bot.name or "MeshAI"
        bot_owner = self.config.bot.owner or "Unknown"

        identity = (
            f"You are {bot_name}, an LLM-powered assistant on the freq51 Meshtastic mesh network. "
            f"Your managing operator is {bot_owner}. "
            f"You are open source at github.com/zvx-echo6/meshai.\n\n"
            f"IDENTITY: Your name is {bot_name}. You ARE a physical node on the mesh — "
            f"node !27780c47 (AIDA-N2). You have a real location, real GPS coordinates, "
            f"and real radio connections. When someone asks how far something is from you, "
            f"check the mesh data for your node's position and calculate. "
            f"You are NOT just software — you are a node that other nodes can see, hear, and route through.\n\n"
        )

        # 2. Static system prompt from config
        static_prompt = ""
        if getattr(self.config.llm, 'use_system_prompt', True):
            static_prompt = self.config.llm.system_prompt

        system_prompt = identity + static_prompt

        # 2b. Dynamic command list (only shows enabled commands)
        if self.dispatcher:
            commands = self.dispatcher.get_commands()
            if commands:
                # Deduplicate aliases
                seen_names = set()
                unique_commands = []
                for cmd in commands:
                    name_lower = cmd.name.lower()
                    if name_lower not in seen_names:
                        seen_names.add(name_lower)
                        unique_commands.append(cmd)

                cmd_lines = [
                    "\nYOUR COMMANDS (only mention these - do NOT mention any commands not listed here):"
                ]
                for cmd in sorted(unique_commands, key=lambda c: c.name):
                    cmd_lines.append(f"  !{cmd.name} - {cmd.description}")
                cmd_lines.append("")
                cmd_lines.append(
                    "CRITICAL: ONLY mention commands in the list above when asked about commands. "
                    "If a command is not listed here, it does NOT exist. Do not invent commands."
                )
                system_prompt += "\n".join(cmd_lines)

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
                "commands are listed below ??? if someone asks what commands are available, "
                "ONLY list YOUR commands from YOUR COMMANDS above. If someone asks where to get "
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

        # 5. Knowledge base retrieval
        if self.knowledge and query:
            results = self.knowledge.search(query)
            if results:
                chunks = "\n\n".join(
                    f"[{r['title']}]: {r['excerpt']}" for r in results
                )
                system_prompt += (
                    "\n\nREFERENCE KNOWLEDGE - Answer using this information:\n"
                    + chunks
                )

        # 6. Mesh Intelligence (inject health data for mesh questions)
        user_ctx = self._get_user_mesh_context(message.sender_id)
        is_direct_mesh_question = self._is_mesh_question(query)
        is_followup = user_ctx["last_was_mesh"] and not is_direct_mesh_question

        should_inject_mesh = is_direct_mesh_question or is_followup

        if self.source_manager and self.mesh_reporter and should_inject_mesh:
            # Detect scope from current message
            scope_type, scope_value = self._detect_mesh_scope(query)

            # For follow-ups with no detected scope, use previous scope
            if is_followup and scope_type == "mesh" and scope_value is None:
                prev_scope = user_ctx.get("last_scope", ("mesh", None))
                if prev_scope[0] != "mesh" or prev_scope[1] is not None:
                    scope_type, scope_value = prev_scope
                    logger.debug(f"Using previous scope for follow-up: {scope_type}, {scope_value}")

            # Always include Tier 1 summary for mesh questions
            tier1 = self.mesh_reporter.build_tier1_summary()
            system_prompt += "\n\n" + tier1

            # Add Tier 2 detail if scoped
            if scope_type == "region" and scope_value:
                region_detail = self.mesh_reporter.build_region_detail(scope_value)
                system_prompt += "\n\n" + region_detail
            elif scope_type == "node" and scope_value:
                node_detail = self.mesh_reporter.build_node_detail(scope_value)
                system_prompt += "\n\n" + node_detail

            # Always include relevant recommendations
            recommendations = self.mesh_reporter.build_recommendations(scope_type, scope_value)
            if recommendations:
                system_prompt += "\n\n" + recommendations

            # Add mesh awareness instructions
            system_prompt += _MESH_AWARENESS_PROMPT

            # Build region geography from config dynamically
            if self.config.mesh_intelligence and self.config.mesh_intelligence.regions:
                geo_lines = ["", "REGION GEOGRAPHY (use local names when discussing these regions):"]
                for region in self.config.mesh_intelligence.regions:
                    local = getattr(region, "local_name", "") or ""
                    local_str = f' "{local}"' if local else ""
                    desc = getattr(region, "description", "") or ""
                    desc_str = f" — {desc}" if desc else ""
                    aliases = getattr(region, "aliases", []) or []
                    alias_str = ""
                    if aliases:
                        alias_str = f'\n    People may call this: {", ".join(aliases)}'
                    geo_lines.append(f"  - {region.name}{local_str}{desc_str}{alias_str}")
                system_prompt += "\n".join(geo_lines)

            # Update mesh context tracking
            self._update_user_mesh_context(
                message.sender_id,
                is_mesh=True,
                scope=(scope_type, scope_value),
            )
        else:
            # Not a mesh question
            self._update_user_mesh_context(message.sender_id, is_mesh=False)

        # DEBUG: Log system prompt status
        logger.debug(f"System prompt length: {len(system_prompt)} chars")

        try:
            response = await self.llm.generate(
                messages=history,
                system_prompt=system_prompt,
                max_tokens=self.config.llm.max_response_tokens,
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

        # Chunk the response with sentence awareness
        messages, remaining = chunk_response(
            response,
            max_chars=self.config.response.max_length,
            max_messages=self.config.response.max_messages,
        )

        # Store remaining content for continuation
        if remaining:
            logger.debug(f"Storing continuation for {message.sender_id}: {len(remaining)} chars remaining")
            self.continuations.store(message.sender_id, remaining)

        return messages

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

