"""Main entry point for MeshAI."""

import argparse
import asyncio
import logging
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
from .history import ConversationHistory
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
        self.router: Optional[MessageRouter] = None
        self.responder: Optional[Responder] = None
        self._running = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None

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

        # Write PID file
        self._write_pid()

        logger.info("MeshAI started successfully")

        # Keep running
        while self._running:
            await asyncio.sleep(1)

            # Periodic cleanup
            if int(time.time()) % 3600 == 0:  # Every hour
                await self.history.cleanup_expired()

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

        self._remove_pid()
        logger.info("MeshAI stopped")

    async def _init_components(self) -> None:
        """Initialize all components."""
        # Conversation history
        self.history = ConversationHistory(self.config.history)
        await self.history.initialize()

        # Command dispatcher
        self.dispatcher = create_dispatcher()

        # LLM backend
        api_key = self.config.resolve_api_key()
        if not api_key:
            logger.warning("No API key configured - LLM responses will fail")

        # Memory config
        mem_cfg = self.config.memory
        window_size = mem_cfg.window_size if mem_cfg.enabled else 0
        summarize_threshold = mem_cfg.summarize_threshold

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

        # Meshtastic connector
        self.connector = MeshConnector(self.config.connection)

        # Message router
        self.router = MessageRouter(
            self.config, self.connector, self.history, self.dispatcher, self.llm
        )

        # Responder
        self.responder = Responder(self.config.response, self.connector)

    async def _on_message(self, message: MeshMessage) -> None:
        """Handle incoming message."""
        try:
            # Check if we should respond
            if not self.router.should_respond(message):
                return

            logger.info(
                f"Processing message from {message.sender_name} ({message.sender_id}): "
                f"{message.text[:50]}..."
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

            # Send response
            if message.is_dm:
                # Reply as DM
                await self.responder.send_response(
                    text=response,
                    destination=message.sender_id,
                    channel=message.channel,
                )
            else:
                # Reply on channel
                formatted = self.responder.format_channel_response(
                    response, message.sender_name, mention_sender=True
                )
                await self.responder.send_response(
                    text=formatted,
                    destination=None,
                    channel=message.channel,
                )

        except Exception as e:
            logger.error(f"Error handling message: {e}", exc_info=True)

    def _write_pid(self) -> None:
        """Write PID file."""
        pid_file = Path("/tmp/meshai.pid")
        pid_file.write_text(str(os.getpid()))

    def _remove_pid(self) -> None:
        """Remove PID file."""
        pid_file = Path("/tmp/meshai.pid")
        if pid_file.exists():
            pid_file.unlink()


import os


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
