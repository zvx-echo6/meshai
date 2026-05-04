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
from .backends import AnthropicBackend, GoogleBackend, LLMBackend, OpenAIBackend
from .cli import run_configurator
from .commands import CommandDispatcher
from .commands.dispatcher import create_dispatcher
from .commands.status import set_start_time
from .config import Config, load_config
from .connector import MeshConnector, MeshMessage
from .context import MeshContext
from .history import ConversationHistory
from .memory import ConversationSummary
from .responder import Responder
from .router import MessageRouter, RouteType

logger = logging.getLogger(__name__)


class MeshAI:
    """Main application class."""

    def __init__(self, config: Config):
        self.config = config
        self.connector: Optional[MeshConnector] = None
        self.history: Optional[ConversationHistory] = None
        self.dispatcher: Optional[CommandDispatcher] = None
        self.llm: Optional[LLMBackend] = None
        self.context: Optional[MeshContext] = None
        self.meshmonitor_sync = None
        self.knowledge = None
        self.source_manager = None
        self.health_engine = None
        self.router: Optional[MessageRouter] = None
        self.responder: Optional[Responder] = None
        self._running = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._last_cleanup: float = 0.0
        self._last_health_compute: float = 0.0

    async def start(self) -> None:
        """Start the bot."""
        logger.info(f"Starting MeshAI v{__version__}")
        set_start_time(time.time())

        # Initialize components
        await self._init_components()

        # Connect to Meshtastic
        self.connector.connect()
        self.connector.set_message_callback(self._on_message, asyncio.get_event_loop())

        # Add own node ID to context ignore list
        if self.context and self.connector.my_node_id:
            self.context._ignore_nodes.add(self.connector.my_node_id)

        self._running = True
        self._loop = asyncio.get_event_loop()
        self._last_cleanup = time.time()
        self._last_health_compute = 0.0

        # Write PID file
        self._write_pid()

        logger.info("MeshAI started successfully")

        # Keep running
        while self._running:
            await asyncio.sleep(1)

            # Periodic MeshMonitor refresh
            if self.meshmonitor_sync:
                self.meshmonitor_sync.maybe_refresh()

            # Periodic mesh source refresh and health computation
            if self.source_manager:
                refreshed = self.source_manager.refresh_all()
                # Recompute health after source refresh
                if refreshed > 0 and self.health_engine:
                    self.health_engine.compute(self.source_manager)
                    self._last_health_compute = time.time()

            # Periodic cleanup
            if time.time() - self._last_cleanup >= 3600:
                await self.history.cleanup_expired()
                if self.context:
                    self.context.prune()
                self._last_cleanup = time.time()

    async def stop(self) -> None:
        """Stop the bot."""
        logger.info("Stopping MeshAI...")
        self._running = False

        if self.connector:
            self.connector.disconnect()

        if self.history:
            await self.history.close()

        if self.llm:
            await self.llm.close()
        if self.knowledge:
            self.knowledge.close()

        self._remove_pid()
        logger.info("MeshAI stopped")

    async def _init_components(self) -> None:
        """Initialize all components."""
        # Conversation history
        self.history = ConversationHistory(self.config.history)
        await self.history.initialize()

        # Command dispatcher
        self.dispatcher = create_dispatcher(
            prefix=self.config.commands.prefix,
            disabled_commands=self.config.commands.disabled_commands,
            custom_commands=self.config.commands.custom_commands,
        )

        # LLM backend
        api_key = self.config.resolve_api_key()
        if not api_key:
            logger.warning("No API key configured - LLM responses will fail")

        # Memory config
        mem_cfg = self.config.memory
        window_size = mem_cfg.window_size if mem_cfg.enabled else 0
        summarize_threshold = mem_cfg.summarize_threshold

        # Create backend
        backend = self.config.llm.backend.lower()
        if backend == "openai":
            self.llm = OpenAIBackend(
                self.config.llm, api_key, window_size, summarize_threshold
            )
        elif backend == "anthropic":
            self.llm = AnthropicBackend(
                self.config.llm, api_key, window_size, summarize_threshold
            )
        elif backend == "google":
            self.llm = GoogleBackend(
                self.config.llm, api_key, window_size, summarize_threshold
            )
        else:
            logger.warning(f"Unknown backend '{backend}', defaulting to OpenAI")
            self.llm = OpenAIBackend(
                self.config.llm, api_key, window_size, summarize_threshold
            )

        # Load persisted summaries into memory cache
        await self._load_summaries()

        # Meshtastic connector
        self.connector = MeshConnector(self.config.connection)

        # Passive mesh context buffer
        ctx_cfg = self.config.context
        if ctx_cfg.enabled:
            self.context = MeshContext(
                observe_channels=ctx_cfg.observe_channels or None,
                ignore_nodes=ctx_cfg.ignore_nodes or None,
                max_age=ctx_cfg.max_age,
            )
            logger.info("Mesh context buffer enabled")
        else:
            self.context = None

        # MeshMonitor trigger sync
        mm_cfg = self.config.meshmonitor
        if mm_cfg.enabled and mm_cfg.url:
            from .meshmonitor import MeshMonitorSync
            self.meshmonitor_sync = MeshMonitorSync(
                url=mm_cfg.url,
                refresh_interval=mm_cfg.refresh_interval,
            )
            count = self.meshmonitor_sync.load()
            logger.info(f"MeshMonitor sync enabled, loaded {count} triggers")
        else:
            self.meshmonitor_sync = None

        # Mesh data sources
        enabled_sources = [s for s in self.config.mesh_sources if s.enabled]
        if enabled_sources:
            from .mesh_sources import MeshSourceManager
            self.source_manager = MeshSourceManager(enabled_sources)
            # Initial fetch
            self.source_manager.refresh_all()
            # Log status
            for status in self.source_manager.get_status():
                if status["is_loaded"]:
                    logger.info(
                        f"Mesh source '{status['name']}' ({status['type']}): "
                        f"{status['node_count']} nodes"
                    )
                else:
                    logger.warning(
                        f"Mesh source '{status['name']}' ({status['type']}): "
                        f"failed - {status.get('last_error', 'unknown error')}"
                    )
        else:
            self.source_manager = None

        # Mesh health engine
        mi_cfg = self.config.mesh_intelligence
        if mi_cfg.enabled and self.source_manager:
            from .mesh_health import MeshHealthEngine
            self.health_engine = MeshHealthEngine(
                region_radius=mi_cfg.region_radius_miles,
                locality_radius=mi_cfg.locality_radius_miles,
                offline_threshold_hours=mi_cfg.offline_threshold_hours,
                packet_threshold=mi_cfg.packet_threshold,
                battery_warning_percent=mi_cfg.battery_warning_percent,
                infra_overrides=mi_cfg.infra_overrides,
                region_labels=mi_cfg.region_labels,
            )
            # Initial health computation
            mesh_health = self.health_engine.compute(self.source_manager)
            self._last_health_compute = time.time()
            logger.info(
                f"Mesh intelligence enabled: {mesh_health.total_nodes} nodes, "
                f"{mesh_health.total_regions} regions, "
                f"score {mesh_health.score.composite:.0f}/100 ({mesh_health.score.tier})"
            )
        else:
            self.health_engine = None

        # Knowledge base
        kb_cfg = self.config.knowledge
        if kb_cfg.enabled and kb_cfg.db_path:
            from .knowledge import KnowledgeSearch
            self.knowledge = KnowledgeSearch(
                db_path=kb_cfg.db_path,
                top_k=kb_cfg.top_k,
            )
        else:
            self.knowledge = None

        # Message router
        self.router = MessageRouter(
            self.config, self.connector, self.history, self.dispatcher, self.llm,
            context=self.context,
            meshmonitor_sync=self.meshmonitor_sync,
            knowledge=self.knowledge,
            source_manager=self.source_manager,
            health_engine=self.health_engine,
        )

        # Responder
        self.responder = Responder(self.config.response, self.connector)

    async def _on_message(self, message: MeshMessage) -> None:
        """Handle incoming message."""
        try:
            # Passively observe channel broadcasts for context (before filtering)
            if self.context and not message.is_dm and message.text:
                self.context.observe(
                    sender_name=message.sender_name,
                    sender_id=message.sender_id,
                    text=message.text,
                    channel=message.channel,
                    is_dm=False,
                )

            # Check if we should respond
            if not self.router.should_respond(message):
                return

            logger.info(
                f"Processing message from {message.sender_name} ({message.sender_id}): "
                f"{message.text[:50]}..."
            )

            # Route the message
            # Check for continuation request first
            continuation_messages = self.router.check_continuation(message)
            if continuation_messages:
                await self.responder.send_response(
                    continuation_messages,
                    destination=message.sender_id,
                    channel=message.channel,
                )
                return

            result = await self.router.route(message)

            if result.route_type == RouteType.IGNORE:
                return

            # Determine response
            if result.route_type == RouteType.COMMAND:
                messages = result.response  # Commands return single string
            elif result.route_type == RouteType.LLM:
                messages = await self.router.generate_llm_response(message, result.query)
            else:
                return

            if not messages:
                return

            # Send DM response
            await self.responder.send_response(
                messages,
                destination=message.sender_id,
                channel=message.channel,
            )

        except Exception as e:
            logger.error(f"Error handling message: {e}", exc_info=True)

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

    try:
        loop.run_until_complete(bot.start())
    except KeyboardInterrupt:
        pass
    finally:
        loop.run_until_complete(bot.stop())
        loop.close()


if __name__ == "__main__":
    main()
