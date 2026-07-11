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
from .chunker import chunk_response, cap_reply_chunks, MAX_REPLY_PACKETS, ContinuationState

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

# v0.6-5: env keywords expand the mesh-question detector so the LLM gets
# env_reporter blocks when the user asks about fires/quakes/weather/etc.
# Each keyword maps to a coarse subtype used by _detect_env_subtype.
_ENV_KEYWORDS_TO_SUBTYPE: dict[str, str] = {
    # fires
    "fire": "fires", "fires": "fires", "wildfire": "fires",
    "wildfires": "fires", "hotspot": "fires", "hotspots": "fires",
    "burning": "fires", "smoke": "fires",
    # quakes
    "quake": "quakes", "quakes": "quakes", "earthquake": "quakes",
    "earthquakes": "quakes", "seismic": "quakes", "tsunami": "quakes",
    # gauges (placed BEFORE weather alerts so "flood" wins over "warning"
    # in cases like "river flood warning")
    "flood": "gauges", "flooding": "gauges",
    "gauge": "gauges", "river": "gauges", "stream": "gauges",
    # weather alerts
    "warning": "alerts", "watch": "alerts", "advisory": "alerts",
    "tornado": "alerts", "thunderstorm": "alerts", "blizzard": "alerts",
    # space weather + band conditions
    "swpc": "swpc", "geomag": "swpc", "solar": "swpc", "kp": "swpc",
    "propagation": "swpc", "aurora": "swpc",
    "band": "swpc", "bands": "swpc", "hf": "swpc",
    # traffic / roads
    "road": "traffic", "roads": "traffic", "jam": "traffic",
    "crash": "traffic", "closure": "traffic", "511": "traffic",
    "incident": "traffic", "incidents": "traffic",
    # v0.7-fire-4-final: "traffic"/"commute"/"highway" added so a
    # query literally mentioning "traffic" hits the traffic subtype.
    "traffic": "traffic", "commute": "traffic", "highway": "traffic",
    # generic
    "storm": "alerts", "weather": "alerts",
}


# v0.7-fire-4-final: multi-word phrase triggers. Matched as whole-
# phrase substrings of the message (NOT single-word membership) so
# they carry multi-word semantics without a single word like "why"
# or "filtered" firing false-positives in unrelated queries.
# Drop-audit phrases unlock the env scope path so
# env_reporter.build_drop_audit lands in the system prompt.
_ENV_PHRASES_TO_SUBTYPE: dict[str, str] = {
    "why didn't":       "drop_audit",
    "why didnt":        "drop_audit",
    "why am i not":     "drop_audit",
    "why am i missing": "drop_audit",
    "what was filtered":"drop_audit",
    "drop audit":       "drop_audit",
    "filtered out":     "drop_audit",
}


def _detect_env_subtype(message_lower: str) -> Optional[str]:
    """Return the env subtype matched by the first env keyword/phrase
    in the message. `None` when no env keyword matches.

    v0.7-fire-4-final: phrase map is checked FIRST so multi-word
    triggers (e.g. "why didn't I hear ...") work without their
    constituent single words (e.g. "why" alone) firing false
    positives. Single-word map then uses set-intersection on
    tokenized words so partial-word collisions ("firearm" / "fire")
    don't fire.
    """
    if not message_lower:
        return None
    # 1) Phrase substring match (multi-word semantics).
    for phrase, subtype in _ENV_PHRASES_TO_SUBTYPE.items():
        if phrase in message_lower:
            return subtype
    # 2) Single-word tokenized match.
    words = set(re.findall(r"\b\w+\b", message_lower))
    for kw, subtype in _ENV_KEYWORDS_TO_SUBTYPE.items():
        if kw in words:
            return subtype
    return None

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

# Keywords that indicate environmental/weather/propagation questions
_ENV_KEYWORDS = {
    "weather", "alert", "warning", "fire", "wildfire", "smoke", "burn",
    "road", "closure", "snow", "avalanche", "avy", "backcountry",
    "solar", "hf", "propagation", "kp", "aurora", "blackout",
    "flood", "stream", "river", "ducting", "tropo", "duct",
    "uhf", "vhf", "band", "conditions", "forecast", "sfi",
    "ionosphere", "geomagnetic", "storm", "traffic", "highway", "interstate", "gauge",
}

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
- Use LOCAL NAMES from the region descriptions when available.
{region_name_instructions}
- When listing nodes, be concise: "BT Base c8d5 — via AIDA" not "BT Base c8d5 (c8d5) is connected via AIDA-MeshMonitor in the South Western ID region."
- Don't repeat the region on every line when listing multiple nodes in the same region. Say the region once at the top, then just list the nodes.
- Don't include shortnames in parentheses when you're already giving the full name — it's noise.
- When discussing infrastructure, name the actual nodes (Mount Harrison Router, not just "5 infra")
- When discussing coverage gaps, explain WHERE and HOW MANY nodes are affected
- When discussing problems, name the node and explain the impact
- You CAN use 3-5 messages. Keep each sentence under 150 characters.
- No markdown formatting - plain text only
- ABSOLUTELY NO markdown. No asterisks, no bold, no bullet points with * or -, no numbered lists with 1. 2. 3. Just plain text sentences.
- NEVER say "Want me to keep going?" — the message system adds this automatically when needed. If you say it yourself, users see it twice.
- When explaining "X/Y gateways" (like 7/7), explain that it means the node is visible to X out of Y data sources (Meshview and MeshMonitor instances that monitor the mesh). It does NOT mean infrastructure routers or regional gateways.
- When reporting packet types, ALWAYS use the name (Position, NodeInfo, Telemetry) not the number.
- Normal position interval: 15-30 minutes (48-96 packets/day). 400+ Position packets in 24h means aggressive position interval, wasting airtime. Tell the user.
- Normal NodeInfo: every 2-3 hours (8-12/day). 50+ is excessive.
- Normal NeighborInfo: every 6 hours (4/day). 20+ is aggressive.
- If a node has high packet volume, explain WHAT the packets are and WHETHER the rate is abnormal compared to normal intervals.

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


def _build_alert_channels_line(config, transport: str) -> str:
    """Build a short identity-block line describing regional alert channels
    for the given transport, sourced from notifications.region_routes.

    Reads config.notifications.region_routes.cells (family -> region ->
    cell_dict). Only includes cells that are enabled (matrix-level
    mt_enabled/mc_enabled AND the cell's own "enabled" flag) and that have
    a non-empty value in this transport's column ("mt" channel index for
    meshtastic, "mc" channel name for meshcore).

    Region names are used verbatim from region_routes — they are NOT
    reconciled with mesh_intelligence region names.

    Fail-safe: returns "" on any error so a broken/missing config never
    breaks prompt assembly.

    Returns:
        A 1-3 line string (no leading/trailing blank lines), or "" if no
        routed regions are configured/enabled for this transport.
    """
    try:
        region_routes = getattr(config.notifications, "region_routes", None)
        if region_routes is None:
            return ""

        transport_enabled = (
            region_routes.mt_enabled if transport == "meshtastic" else region_routes.mc_enabled
        )
        if not transport_enabled:
            return ""

        cells = getattr(region_routes, "cells", {}) or {}
        col = "mt" if transport == "meshtastic" else "mc"

        # region -> set(family) for families/regions routed on this transport.
        families_by_region: dict = {}
        for family, region_map in cells.items():
            if not isinstance(region_map, dict):
                continue
            for region_name, cell in region_map.items():
                if not isinstance(cell, dict) or not cell.get("enabled", False):
                    continue
                dest = cell.get(col)
                if not dest:
                    continue
                families_by_region.setdefault(region_name, set()).add(family)

        if not families_by_region:
            return ""

        # Distinct hazard families across all routed regions, for the intro line.
        all_families = sorted({f for fams in families_by_region.values() for f in fams})
        _FAMILY_LABELS = {
            "weather": "weather",
            "fire": "fire",
            "roads": "roads",
            "avalanche": "avalanche",
            "seismic": "seismic",
            "power_outage": "power outages",
            "satpass": "satellite passes",
        }
        hazard_words = ", ".join(_FAMILY_LABELS.get(f, f) for f in all_families)

        # region -> destination value (mt index or mc name), for the mapping line.
        # A region may route different families to different destinations in
        # theory; in practice the matrix is uniform per-region, so just take
        # the first cell's destination for the mapping display.
        dest_by_region = {}
        for family, region_map in cells.items():
            if not isinstance(region_map, dict):
                continue
            for region_name, cell in region_map.items():
                if region_name not in families_by_region:
                    continue
                if region_name in dest_by_region:
                    continue
                dest = cell.get(col)
                if dest:
                    dest_by_region[region_name] = dest

        if transport == "meshtastic":
            mapping = ", ".join(
                f"channel {dest_by_region[r]} = {r}" for r in dest_by_region
            )
        else:
            mapping = ", ".join(
                f"{dest_by_region[r]} = {r}" for r in dest_by_region
            )

        if not mapping:
            return ""

        return (
            f"You broadcast regional hazard alerts ({hazard_words}). "
            f"People can join a channel to receive that region's alerts: {mapping}."
        )
    except Exception:
        logger.exception("alert channels line build failed")
        return ""


def _build_meshcore_channel_block(config, channel_details, position=None) -> str:
    """Build the MeshCore channel-recommendation context block.

    Single-shot context-stuffing for the LLM (no tools): lists ONLY the
    channels the operator has opted to observe (``meshcore_context.observe_channels``),
    each annotated with its PSK key (from ``channel_details``) and — where a
    region routing cell maps to it — the covered region's human geography
    (local name, cities, centroid) so the model can match a user's town/GPS
    to the right channel.

    Args:
        config: the loaded Config object (reads meshcore_context.observe_channels,
            notifications.region_routes, mesh_intelligence.regions).
        channel_details: list of {name, hash, key} dicts from
            CompositeTransport.channel_details() (key = PSK hex).
        position: optional (lat, lon) tuple for the requesting user; when
            present a "USER LOCATION" line is emitted so the LLM can auto-match.

    Returns:
        The block as a string, or "" if no observed channels are configured
        (caller should skip injection in that case).
    """
    # 1. Observed set — the ONLY channels we may disclose/recommend.
    observe = list(getattr(config.meshcore_context, "observe_channels", []) or [])
    if not observe:
        return ""
    observe_set = set(observe)

    # 2. key lookup: channel name -> PSK hex (from live channel_details()).
    key_by_name = {}
    for d in (channel_details or []):
        name = d.get("name")
        if name:
            key_by_name[name] = d.get("key")

    # 3. Reverse map: channel name -> set(route region names), scanning every
    #    family/region in region_routes for a cell whose `mc` names the channel.
    regions_by_channel = {}
    try:
        cells = getattr(config.notifications.region_routes, "cells", {}) or {}
        for _family, region_map in cells.items():
            if not isinstance(region_map, dict):
                continue
            for region_name, cell in region_map.items():
                if not isinstance(cell, dict):
                    continue
                mc = cell.get("mc")
                if mc:
                    regions_by_channel.setdefault(mc, set()).add(region_name)
    except Exception:
        logger.exception("region_routes reverse-map build failed")

    # 4. Human geography from mesh_intelligence regions (best-effort attach).
    #    region_routes uses "SW/SC/East Idaho"; mesh_intelligence uses names
    #    like "South Western ID" — we do a loose direction-word match rather
    #    than a strict reconcile, and let the LLM reason over the raw metadata.
    geo_regions = []
    try:
        geo_regions = list(getattr(config.mesh_intelligence, "regions", []) or [])
    except Exception:
        logger.exception("mesh_intelligence regions read failed")

    _DIRECTION_TOKENS = {
        "sw": "south west", "sc": "south central", "se": "south east",
        "nw": "north west", "ne": "north east",
        "n": "north", "s": "south", "e": "east", "w": "west", "c": "central",
    }

    def _direction_words(route_region: str) -> set:
        """Expand the leading direction abbreviation of a route region name
        (e.g. "SW Idaho" -> {south, west}; "East Idaho" -> {east})."""
        words = set()
        for tok in route_region.lower().replace("-", " ").split():
            if tok in ("idaho", "id"):
                continue
            expanded = _DIRECTION_TOKENS.get(tok, tok)
            words.update(expanded.split())
        return words

    def _match_geo(route_region: str):
        """Fuzzy/substring match a route region name to a mesh_intelligence
        region; returns the RegionAnchor or None."""
        rr_words = _direction_words(route_region)
        best = None
        best_score = 0
        for r in geo_regions:
            name = (getattr(r, "name", "") or "").lower()
            geo_words = set(name.replace("-", " ").split()) - {"idaho", "id"}
            score = len(rr_words & geo_words)
            if score > best_score:
                best_score, best = score, r
        return best if best_score > 0 else None

    # 5. Emit one concise line per OBSERVED channel.
    lines = ["", "MESHCORE CHANNELS YOU CAN RECOMMEND (name + join key):"]
    for name in observe:
        if name not in observe_set:
            continue
        key = key_by_name.get(name)
        key_str = key if key else "(key unavailable)"
        route_regions = sorted(regions_by_channel.get(name, set()))
        region_str = ""
        if route_regions:
            details = []
            for rr in route_regions:
                geo = _match_geo(rr)
                if geo is not None:
                    local = getattr(geo, "local_name", "") or ""
                    cities = getattr(geo, "cities", []) or []
                    lat = getattr(geo, "lat", None)
                    lon = getattr(geo, "lon", None)
                    bits = [rr]
                    inner = []
                    if local:
                        inner.append(local)
                    if cities:
                        inner.append(", ".join(cities[:4]))
                    if inner:
                        bits.append(f"({'; '.join(inner)}")
                        if lat is not None and lon is not None:
                            bits[-1] += f"; ~{lat:.2f},{lon:.2f}"
                        bits[-1] += ")"
                    elif lat is not None and lon is not None:
                        bits.append(f"(~{lat:.2f},{lon:.2f})")
                    details.append(" ".join(bits))
                else:
                    details.append(rr)
            region_str = " — region: " + " / ".join(details)
        lines.append(f"  {name}{region_str} — key: {key_str}")

    # 6. Optional user location for auto-matching.
    if position is not None:
        try:
            lat, lon = position
            lines.append(f"USER LOCATION: {lat},{lon}")
        except Exception:
            pass

    # 7. Instruction for the model.
    lines.append(
        "If the user asks which MeshCore channel to join: determine their area "
        "from USER LOCATION if given, otherwise ask which Idaho town/area they're "
        "in, then tell them the channel NAME and KEY to join. Only recommend "
        "channels listed above; never invent a channel or key."
    )
    return "\n".join(lines)


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
        env_store=None,
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
        self.env_store = env_store
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

        # bot.respond_to_dms is the Meshtastic-only toggle; MeshCore DMs are
        # governed solely by meshcore_context.respond_to_dms, enforced at the
        # transport level before the message ever reaches here.
        if message.transport != "meshcore" and not self.config.bot.respond_to_dms:
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

        # v0.7-fire-tracker-4-revised: the LLM DM path with env_reporter
        # injection is the natural-language interface. No bolt-on
        # structured-command parallel.
        return RouteResult(RouteType.LLM, query=query)

    def _is_mesh_question(self, message: str) -> bool:
        """Check if message is asking about mesh health/status OR env state.

        v0.6-5: env keywords (fire/quake/flood/etc.) also trigger the
        mesh-question path so the env_reporter blocks land in the system
        prompt. Single detector per Matt\'s spec.
        """
        msg_lower = message.lower()

        # Mesh phrases.
        for phrase in _MESH_PHRASES:
            if phrase in msg_lower:
                return True

        # Mesh keywords + env keywords.
        words = set(re.findall(r'\b\w+\b', msg_lower))
        if words & _MESH_KEYWORDS:
            return True
        if _detect_env_subtype(msg_lower) is not None:
            return True

        return False

    def _detect_mesh_scope(self, message: str) -> tuple[str, Optional[str]]:
        """Detect the scope of a mesh question.

        Returns one of:
          - ("env", subtype)  : fires/quakes/alerts/gauges/traffic/swpc
          - ("node", id)      : specific node
          - ("region", name)  : specific region
          - ("mesh", None)    : general mesh question
        """
        msg_lower = message.lower()

        # === ENV (v0.6-5: check first; env scope routes through env_reporter) ===
        env_subtype = _detect_env_subtype(msg_lower)
        if env_subtype is not None:
            return ("env", env_subtype)

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

    def _try_compute_distance(self, query: str) -> str:
        """Extract two node names from a distance question and compute distance."""
        if not self.mesh_reporter:
            return ""

        health = self.mesh_reporter.health_engine.mesh_health
        if not health:
            return ""

        query_lower = query.lower()

        # Build name -> node lookup (include partial long_name matches)
        node_names = {}
        for node in health.nodes.values():
            if node.short_name:
                node_names[node.short_name.lower()] = node
            if node.long_name:
                full = node.long_name.lower()
                node_names[full] = node
                # Add partial matches: "TVM Pearl Relay" also matches "TVM Pearl"
                words = full.split()
                if len(words) >= 2:
                    for i in range(2, len(words) + 1):
                        partial = " ".join(words[:i])
                        if partial not in node_names:
                            node_names[partial] = node

        # AIDA aliases
        aida_node = health.nodes.get(0x27780c47)
        if aida_node:
            for alias in ["aida", "aida-n2", "me", "my node", "yourself", "your position", "you"]:
                node_names[alias] = aida_node

        # Find mentioned nodes (longest names first)
        found_nodes = []

        for name in sorted(node_names.keys(), key=len, reverse=True):
            if name in query_lower and len(name) >= 2:
                node = node_names[name]
                if not any(n.node_num == node.node_num for n in found_nodes):
                    found_nodes.append(node)
                if len(found_nodes) >= 2:
                    break

        # If we only found one or zero nodes, check for ambiguous short terms
        if len(found_nodes) < 2:
            query_words = query_lower.replace("?", "").replace("!", "").split()
            candidate_terms = list(query_words)
            for i in range(len(query_words) - 1):
                candidate_terms.append(f"{query_words[i]} {query_words[i+1]}")

            skip_words = {"how", "far", "is", "from", "the", "to", "and", "between", "what",
                         "distance", "away", "are", "apart", "tell", "me", "about", "a", "an"}

            for term in candidate_terms:
                if term in skip_words or len(term) < 2:
                    continue
                matches = []
                seen_nums = set()
                for node in health.nodes.values():
                    if node.node_num in seen_nums:
                        continue
                    name_lower = (node.long_name or "").lower()
                    short_lower = (node.short_name or "").lower()
                    if term in name_lower or term == short_lower:
                        matches.append(node)
                        seen_nums.add(node.node_num)

                if len(matches) > 1:
                    names = [f"  - {n.long_name or n.short_name} ({n.short_name})"
                             for n in matches[:6]]
                    return (
                        f"AMBIGUOUS: '{term}' matches multiple nodes. "
                        f"Ask the user which one they mean:\n" + "\n".join(names)
                    )

        if len(found_nodes) == 2:
            return self.mesh_reporter.build_distance(
                str(found_nodes[0].node_num),
                str(found_nodes[1].node_num)
            )
        elif len(found_nodes) == 1 and aida_node:
            return self.mesh_reporter.build_distance(
                str(found_nodes[0].node_num),
                str(aida_node.node_num)
            )

        return ""


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

        # Transport of the originating message -- drives identity framing and
        # gating of transport-specific prompt blocks below.
        transport = getattr(message, "transport", "meshtastic")

        # 1. Dynamic identity from bot config, branched by transport.
        bot_name = self.config.bot.name or "MeshAI"
        bot_owner = self.config.bot.owner or "Unknown"

        if transport == "meshcore":
            mc_mesh_name = self.config.bot.mc_mesh_name or "the MeshCore mesh"
            identity = (
                f"You are {bot_name}, an LLM-powered assistant on {mc_mesh_name}, "
                f"connected via a MeshCore companion radio. "
                f"Your managing operator is {bot_owner}. "
                f"You are open source at github.com/zvx-echo6/meshai.\n\n"
                f"IDENTITY: Your name is {bot_name}. You have a real MeshCore radio "
                f"presence — you send and receive over an actual MeshCore companion "
                f"radio, not just software. You do NOT have a Meshtastic node identity "
                f"and you are NOT part of MeshMonitor.\n\n"
            )
        else:
            mt_mesh_name = self.config.bot.mt_mesh_name
            mt_node = self.config.bot.mt_node
            if mt_mesh_name or mt_node:
                mesh_label = mt_mesh_name or "the mesh"
                identity = (
                    f"You are {bot_name}, an LLM-powered assistant on {mesh_label}. "
                    f"Your managing operator is {bot_owner}. "
                    f"You are open source at github.com/zvx-echo6/meshai.\n\n"
                )
                if mt_node:
                    identity += (
                        f"IDENTITY: Your name is {bot_name}. You ARE a physical node on "
                        f"the mesh — node {mt_node}. You have a real location, real GPS "
                        f"coordinates, and real radio connections. When someone asks how "
                        f"far something is from you, check the mesh data for your node's "
                        f"position and calculate. You are NOT just software — you are a "
                        f"node that other nodes can see, hear, and route through.\n\n"
                    )
            else:
                # No transport identity configured -- generic non-MT-specific fallback.
                identity = (
                    f"You are {bot_name}, an LLM-powered assistant on a mesh network. "
                    f"Your managing operator is {bot_owner}. "
                    f"You are open source at github.com/zvx-echo6/meshai.\n\n"
                )

        alert_channels_line = _build_alert_channels_line(self.config, transport)
        if alert_channels_line:
            identity += alert_channels_line + "\n\n"

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
                    "If a command is not listed here, it does NOT exist. Do not invent commands. "
                    "If no command list appears above, you have NO commands -- say so plainly "
                    "instead of guessing names."
                )
                system_prompt += "\n".join(cmd_lines)

        # 3. MeshMonitor info (only when enabled -- Meshtastic-only, MeshMonitor
        # has no MeshCore concept)
        if (
            transport == "meshtastic"
            and self.meshmonitor_sync
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

        # 4. Inject mesh context if available, scoped to the originating mesh
        # (with explicit override if the query names the other mesh).
        if self.context:
            max_items = getattr(self.config.context, 'max_context_items', 20)
            origin = getattr(message, "transport", "meshtastic")
            # Determine target mesh: default to origin. Override if the query
            # explicitly names the other mesh and NOT the origin mesh.
            target = origin
            q_lower = query.lower() if query else ""
            names_meshcore = "meshcore" in q_lower or "mc mesh" in q_lower
            names_meshtastic = "meshtastic" in q_lower or "mt mesh" in q_lower
            if names_meshcore and not names_meshtastic:
                target = "meshcore"
            elif names_meshtastic and not names_meshcore:
                target = "meshtastic"
            context_block = self.context.get_context_block(max_items=max_items, transport=target)
            mesh_label = "MeshCore" if target == "meshcore" else "Meshtastic"
            if context_block:
                system_prompt += (
                    f"\n\n--- Recent {mesh_label} mesh traffic (for context only, not messages to you) ---\n"
                    + context_block
                )
            else:
                system_prompt += (
                    f"\n\n[No recent {mesh_label} mesh traffic observed yet.]"
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

        # v0.7-fire-tracker-4: scope detection hoisted above its first
        # use. Pre-fix, the env_reporter check below referenced scope_type
        # while the assignment lived ~15 lines later inside the
        # source_manager branch -- UnboundLocalError on every env query
        # ("are there any fires?", "what's the weather?", etc.), the
        # exception got caught in main.py and the bot went silent.
        scope_type: str = "mesh"
        scope_value = None
        if should_inject_mesh:
            scope_type, scope_value = self._detect_mesh_scope(query)
            # For follow-ups with no detected scope, use previous scope.
            if is_followup and scope_type == "mesh" and scope_value is None:
                prev_scope = user_ctx.get("last_scope", ("mesh", None))
                if prev_scope[0] != "mesh" or prev_scope[1] is not None:
                    scope_type, scope_value = prev_scope
                    logger.debug(
                        f"Using previous scope for follow-up: "
                        f"{scope_type}, {scope_value}"
                    )

        # v0.6-5 env_reporter: when scope is "env" OR when injecting mesh
        # context, append the env_reporter blocks. The reporter itself gates
        # per-adapter via adapter_meta.include_in_llm_context.
        if should_inject_mesh and scope_type == "env":
            try:
                from meshai.notifications.env_reporter import env_reporter
                env_block = env_reporter.build_all()
                if env_block:
                    system_prompt += "\n\n" + env_block
                # Drop audit is useful for "why didn\'t I hear about X?" --
                # always include the most-recent hour when env scope.
                drop_block = env_reporter.build_drop_audit(hours=1)
                if drop_block:
                    system_prompt += "\n\n" + drop_block
                # v0.7-fire-4-final: positive-framed grounding clause.
                # Closes Class B hallucination (LLM inventing counts
                # / place names when an env block is empty -- e.g.
                # "144 earthquakes worldwide" against an empty
                # quake_events 24h window).
                system_prompt += "\n\n" + ENV_GROUNDING_CLAUSE
            except Exception:
                logger.exception("env_reporter injection failed")

        if self.source_manager and self.mesh_reporter and should_inject_mesh:
            # v0.7-fire-tracker-4: scope already detected above; no
            # second call needed.

            # Meshtastic-only: node-health/gateway/packet reporting. This whole
            # sub-block is built from meshtasticd/MeshMonitor-derived node data
            # (mesh_reporter tracks Meshtastic node IDs, infra/gateway scoring,
            # packet cadence, etc.) and has no MeshCore equivalent.
            if transport == "meshtastic":
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

                # Add mesh awareness instructions with dynamic region name mappings
                region_name_instructions = ""
                if self.config.mesh_intelligence and self.config.mesh_intelligence.regions:
                    # Build region name mappings for the prompt
                    mappings = []
                    for region in self.config.mesh_intelligence.regions:
                        local = getattr(region, "local_name", "") or ""
                        if local and local != region.name:
                            mappings.append(f'say "{local}" not "{region.name}"')
                    if mappings:
                        region_name_instructions = f"- ALWAYS use local region names: {', '.join(mappings)}. The code names mean nothing to users."

                system_prompt += _MESH_AWARENESS_PROMPT.format(
                    region_name_instructions=region_name_instructions
                )

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

            # MeshCore channel-recommendation block: for "what channel
            # should I join?" style questions. Transport-neutral (works for
            # both meshes -- the block itself is scoped to MeshCore channel
            # data). Fail-safe — never break the LLM path if transport/config
            # access throws.
            try:
                channel_details = self.connector.channel_details()
                position = None
                try:
                    position = self.connector.get_node_position(message.sender_id)
                except Exception:
                    position = None
                mc_block = _build_meshcore_channel_block(
                    self.config, channel_details, position
                )
                if mc_block:
                    system_prompt += "\n\n" + mc_block
            except Exception:
                logger.exception("meshcore channel block injection failed")

            # Update mesh context tracking
            self._update_user_mesh_context(
                message.sender_id,
                is_mesh=True,
                scope=(scope_type, scope_value),
            )
        else:
            # Not a mesh question
            self._update_user_mesh_context(message.sender_id, is_mesh=False)

        # 7. Environmental context injection
        if self.env_store:
            query_lower = query.lower() if query else ""
            env_relevant = any(kw in query_lower for kw in _ENV_KEYWORDS)
            # Also inject env context if mesh context is being injected
            if env_relevant or should_inject_mesh:
                env_summary = self.env_store.get_summary()
                if env_summary:
                    system_prompt += "\n\n" + env_summary

        # Terse-reply guidance for slow LoRa links (always appended last for
        # interactive replies so it isn't buried by mesh-data blocks).
        system_prompt += (
            "\n\nYou're replying over a slow LoRa mesh. "
            "Answer in 1-2 short messages. Be terse and direct; omit preamble and filler."
        )

        # DEBUG: Log system prompt status
        logger.debug(f"System prompt length: {len(system_prompt)} chars")

        # Detect distance questions and inject computed distance
        distance_keywords = ["how far", "distance", "how close", "miles from", "km from", "away from"]
        if any(kw in query.lower() for kw in distance_keywords):
            distance_result = self._try_compute_distance(query)
            if distance_result:
                system_prompt += f"\n\nDISTANCE CALCULATION:\n{distance_result}\n"

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

        # Strip any markdown the LLM ignored instructions about
        from .chunker import strip_markdown
        response = strip_markdown(response)

        # Chunk the response with sentence awareness
        messages, remaining = chunk_response(
            response,
            max_chars=min(self.config.response.max_length, self.connector.max_chars),
            max_messages=self.config.response.max_messages,
        )

        # Hard cap: LLM interactive replies must not exceed MAX_REPLY_PACKETS.
        # This is a safety ceiling on top of config.response.max_messages so
        # LoRa airtime is protected even if config grows the message budget.
        # Broadcast/notification chunking is NOT affected by this cap.
        messages = cap_reply_chunks(messages, MAX_REPLY_PACKETS, self.connector.max_chars)

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


# v0.7-fire-4-final: positive-framed grounding clause appended to
# the system prompt whenever env scope is detected. Frames the
# constraint as "answer from the blocks" rather than "do not
# hallucinate" (Matt's mitigation guidance) so the LLM doesn't
# default to a blanket apology disclaimer every other message.
ENV_GROUNDING_CLAUSE = (
    "ENVIRONMENTAL CONTEXT GROUNDING:\n"
    "Answer only from the environmental context blocks above. If a "
    "block is empty or missing for an adapter the user asked about "
    "(e.g. no NWS alerts in the block), say something like \"No "
    "active <category> right now\" -- never invent specific numbers, "
    "place names, or counts. If you do not have a relevant block for "
    "the question, say so briefly."
)
