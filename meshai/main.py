"""Main entry point for MeshAI."""

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional

from . import __version__
from .announcements import AnnouncementScheduler
from .backends import AnthropicBackend, FallbackBackend, GoogleBackend, LLMBackend, OpenAIBackend
from .cli import run_configurator
from .commands import CommandDispatcher
from .commands.dispatcher import create_dispatcher
from .commands.status import set_start_time
from .config import Config, load_config
from .connector import MeshConnector, MeshMessage
from .history import ConversationHistory
from .memory import ConversationSummary
from .personality import PersonalityManager
from .rate_limiter import RateLimiter
from .responder import Responder
from .router import MessageRouter, RouteType
from .safety import SafetyFilter, UserFilter
from .web_status import WebStatusServer, get_status_data
from .webhook import WebhookClient

logger = logging.getLogger(__name__)


class MeshAI:
    """Main application class."""

    def __init__(self, config: Config):
        self.config = config
        self.connector: Optional[MeshConnector] = None
        self.history: Optional[ConversationHistory] = None
        self.dispatcher: Optional[CommandDispatcher] = None
        self.llm: Optional[LLMBackend] = None
        self.router: Optional[MessageRouter] = None
        self.responder: Optional[Responder] = None
        self.personality: Optional[PersonalityManager] = None
        self.safety_filter: Optional[SafetyFilter] = None
        self.user_filter: Optional[UserFilter] = None
        self.rate_limiter: Optional[RateLimiter] = None
        self.webhook: Optional[WebhookClient] = None
        self.web_status: Optional[WebStatusServer] = None
        self.announcements: Optional[AnnouncementScheduler] = None
        self._running = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._last_cleanup: float = 0.0

    async def start(self) -> None:
        """Start the bot."""
        logger.info(f"Starting MeshAI v{__version__}")
        set_start_time(time.time())

        # Initialize components
        await self._init_components()

        # Connect to Meshtastic
        self.connector.connect()
        self.connector.set_message_callback(self._on_message, asyncio.get_event_loop())

        self._running = True
        self._loop = asyncio.get_event_loop()
        self._last_cleanup = time.time()

        # Start async services
        await self.webhook.start()
        await self.webhook.on_startup()
        await self.announcements.start()

        # Start sync services
        self.web_status.start()

        # Write PID file
        self._write_pid()

        logger.info("MeshAI started successfully")

        # Keep running
        while self._running:
            await asyncio.sleep(1)

            # Periodic cleanup
            if time.time() - self._last_cleanup >= 3600:
                await self.history.cleanup_expired()
                self._last_cleanup = time.time()

    async def stop(self) -> None:
        """Stop the bot."""
        logger.info("Stopping MeshAI...")
        self._running = False

        if self.webhook:
            await self.webhook.on_shutdown()
            await self.webhook.stop()

        if self.announcements:
            await self.announcements.stop()

        if self.web_status:
            self.web_status.stop()

        if self.connector:
            self.connector.disconnect()

        if self.history:
            await self.history.close()

        if self.llm:
            await self.llm.close()

        self._remove_pid()
        logger.info("MeshAI stopped")

    async def _init_components(self) -> None:
        """Initialize all components."""
        # Conversation history
        self.history = ConversationHistory(self.config.history)
        await self.history.initialize()

        # Command dispatcher (2h: pass config)
        self.dispatcher = create_dispatcher(
            prefix=self.config.commands.prefix,
            disabled_commands=self.config.commands.disabled_commands,
            custom_commands=self.config.commands.custom_commands,
        )

        # Safety and user filters (2a)
        self.user_filter = UserFilter(
            blocklist=self.config.users.blocklist,
            allowlist=self.config.users.allowlist,
            allowlist_only=self.config.users.allowlist_only,
            admin_nodes=self.config.users.admin_nodes,
        )
        self.safety_filter = SafetyFilter(self.config.safety)

        # Rate limiter (2b)
        self.rate_limiter = RateLimiter(
            self.config.rate_limits,
            vip_nodes=self.config.users.vip_nodes,
        )

        # Personality manager (2c)
        self.personality = PersonalityManager(self.config.personality)

        # LLM backend
        api_key = self.config.resolve_api_key()
        if not api_key:
            logger.warning("No API key configured - LLM responses will fail")

        # Memory config
        mem_cfg = self.config.memory
        window_size = mem_cfg.window_size if mem_cfg.enabled else 0
        summarize_threshold = mem_cfg.summarize_threshold

        # Create primary backend
        backend = self.config.llm.backend.lower()
        if backend == "openai":
            primary = OpenAIBackend(
                self.config.llm, api_key, window_size, summarize_threshold
            )
        elif backend == "anthropic":
            primary = AnthropicBackend(
                self.config.llm, api_key, window_size, summarize_threshold
            )
        elif backend == "google":
            primary = GoogleBackend(
                self.config.llm, api_key, window_size, summarize_threshold
            )
        else:
            logger.warning(f"Unknown backend '{backend}', defaulting to OpenAI")
            primary = OpenAIBackend(
                self.config.llm, api_key, window_size, summarize_threshold
            )

        # Wrap in FallbackBackend if fallback is configured (2g)
        if self.config.llm.fallback:
            self.llm = FallbackBackend(
                self.config.llm, api_key, window_size, summarize_threshold
            )
        else:
            self.llm = primary

        # Load persisted summaries into memory cache
        await self._load_summaries()

        # Meshtastic connector
        self.connector = MeshConnector(self.config.connection)

        # Message router (pass personality manager)
        self.router = MessageRouter(
            self.config, self.connector, self.history, self.dispatcher, self.llm,
            personality=self.personality,
        )

        # Responder
        self.responder = Responder(self.config.response, self.connector)

        # Webhook client (2d)
        self.webhook = WebhookClient(self.config.integrations.webhook)

        # Web status server (2e)
        self.web_status = WebStatusServer(self.config.web_status)

        # Announcement scheduler (2f)
        async def _send_announcement(text: str, channel: int) -> None:
            self.connector.send_message(text=text, channel=channel)

        self.announcements = AnnouncementScheduler(
            self.config.announcements,
            send_callback=_send_announcement,
        )

    async def _on_message(self, message: MeshMessage) -> None:
        """Handle incoming message."""
        try:
            # Check user filter (2a)
            allowed, reason = self.user_filter.is_allowed(message.sender_id)
            if not allowed:
                logger.debug(f"Blocked message from {message.sender_id}: {reason}")
                return

            # Check if we should respond
            if not self.router.should_respond(message):
                return

            # Check rate limiter (2b)
            allowed, reason = self.rate_limiter.is_allowed(message.sender_id)
            if not allowed:
                logger.debug(f"Rate limited {message.sender_id}: {reason}")
                return

            logger.info(
                f"Processing message from {message.sender_name} ({message.sender_id}): "
                f"{message.text[:50]}..."
            )

            # Record in web status (2e)
            get_status_data().record_message(message.sender_id, message.sender_name)

            # Send webhook event (2d)
            await self.webhook.on_message_received(
                sender_id=message.sender_id,
                sender_name=message.sender_name,
                text=message.text,
                channel=message.channel,
                is_dm=message.is_dm,
            )

            # Route the message
            result = await self.router.route(message)

            if result.route_type == RouteType.IGNORE:
                return

            # Determine response
            if result.route_type == RouteType.COMMAND:
                response = result.response
            elif result.route_type == RouteType.LLM:
                response = await self.router.generate_llm_response(message, result.query)
            else:
                return

            if not response:
                return

            # Apply safety filter to LLM responses (2a)
            if result.route_type == RouteType.LLM:
                response = self.safety_filter.filter_response(response)

            # Send response
            if message.is_dm:
                await self.responder.send_response(
                    text=response,
                    destination=message.sender_id,
                    channel=message.channel,
                )
            else:
                formatted = self.responder.format_channel_response(
                    response, message.sender_name, mention_sender=True
                )
                await self.responder.send_response(
                    text=formatted,
                    destination=None,
                    channel=message.channel,
                )

            # Record response in rate limiter and status (2b, 2e)
            self.rate_limiter.record_message(message.sender_id)
            get_status_data().record_response()

            # Send webhook event (2d)
            await self.webhook.on_response_sent(
                recipient_id=message.sender_id if message.is_dm else None,
                text=response,
                channel=message.channel,
            )

        except Exception as e:
            logger.error(f"Error handling message: {e}", exc_info=True)
            get_status_data().record_error(str(e))
            await self.webhook.on_error(str(e))

    async def _load_summaries(self) -> None:
        """Load persisted summaries from database into memory cache."""
        memory = self.llm.get_memory()
        if not memory:
            return

        if not self.history or not self.history._db:
            return

        try:
            async with self.history._lock:
                cursor = await self.history._db.execute(
                    "SELECT user_id, summary, message_count, updated_at "
                    "FROM conversation_summaries"
                )
                rows = await cursor.fetchall()

            loaded = 0
            for row in rows:
                user_id, summary_text, message_count, updated_at = row
                summary = ConversationSummary(
                    summary=summary_text,
                    last_updated=updated_at,
                    message_count=message_count,
                )
                memory.load_summary(user_id, summary)
                loaded += 1

            if loaded:
                logger.info(f"Loaded {loaded} conversation summaries from database")

        except Exception as e:
            logger.warning(f"Failed to load summaries from database: {e}")

    def _write_pid(self) -> None:
        """Write PID file."""
        pid_file = Path("/tmp/meshai.pid")
        pid_file.write_text(str(os.getpid()))

    def _remove_pid(self) -> None:
        """Remove PID file."""
        pid_file = Path("/tmp/meshai.pid")
        if pid_file.exists():
            pid_file.unlink()


def setup_logging(verbose: bool = False) -> None:
    """Configure logging."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="MeshAI - LLM-powered Meshtastic assistant",
        prog="meshai",
    )
    parser.add_argument(
        "--version", "-V", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--config", "-c", action="store_true", help="Launch configuration tool"
    )
    parser.add_argument(
        "--config-file",
        "-f",
        type=Path,
        default=Path("config.yaml"),
        help="Path to config file (default: config.yaml)",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")

    args = parser.parse_args()

    setup_logging(args.verbose)

    # Launch configurator if requested
    if args.config:
        run_configurator(args.config_file)
        return

    # Load config
    config = load_config(args.config_file)

    # Check if config exists
    if not args.config_file.exists():
        logger.warning(f"Config file not found: {args.config_file}")
        logger.info("Run 'meshai --config' to create one, or copy config.example.yaml")
        sys.exit(1)

    # Create and run bot
    bot = MeshAI(config)

    # Handle signals
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def signal_handler(sig, frame):
        logger.info(f"Received signal {sig}")
        loop.create_task(bot.stop())

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Handle SIGHUP for config reload
    def reload_handler(sig, frame):
        logger.info("Received SIGHUP - reloading config")
        # For now, just log - full reload would require more work
        # Could reload config and reinitialize components

    signal.signal(signal.SIGHUP, reload_handler)

    try:
        loop.run_until_complete(bot.start())
    except KeyboardInterrupt:
        pass
    finally:
        loop.run_until_complete(bot.stop())
        loop.close()


if __name__ == "__main__":
    main()
