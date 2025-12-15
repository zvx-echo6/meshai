"""Periodic announcements/broadcasts for MeshAI."""

import asyncio
import logging
import random
from typing import Callable, Optional

from .config import AnnouncementsConfig

logger = logging.getLogger(__name__)


class AnnouncementScheduler:
    """Scheduler for periodic announcements."""

    def __init__(
        self,
        config: AnnouncementsConfig,
        send_callback: Callable[[str, int], asyncio.coroutine],
    ):
        """Initialize the announcement scheduler.

        Args:
            config: Announcements configuration
            send_callback: Async callback to send messages: (text, channel) -> None
        """
        self.config = config
        self._send_callback = send_callback
        self._task: Optional[asyncio.Task] = None
        self._message_index = 0
        self._running = False

    async def start(self):
        """Start the announcement scheduler."""
        if not self.config.enabled or not self.config.messages:
            logger.debug("Announcements disabled or no messages configured")
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            f"Announcement scheduler started (every {self.config.interval_hours}h)"
        )

    async def stop(self):
        """Stop the announcement scheduler."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Announcement scheduler stopped")

    async def _run_loop(self):
        """Main loop for sending periodic announcements."""
        # Wait a bit before first announcement
        await asyncio.sleep(60)  # 1 minute initial delay

        while self._running:
            try:
                # Get next message
                message = self._get_next_message()
                if message:
                    logger.info(f"Sending announcement to channel {self.config.channel}")
                    await self._send_callback(message, self.config.channel)

                # Wait for next interval
                await asyncio.sleep(self.config.interval_hours * 3600)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in announcement loop: {e}")
                await asyncio.sleep(300)  # Wait 5 min on error

    def _get_next_message(self) -> Optional[str]:
        """Get the next announcement message."""
        if not self.config.messages:
            return None

        if self.config.random_order:
            return random.choice(self.config.messages)
        else:
            message = self.config.messages[self._message_index]
            self._message_index = (self._message_index + 1) % len(self.config.messages)
            return message

    async def send_now(self, message: Optional[str] = None) -> bool:
        """Send an announcement immediately.

        Args:
            message: Optional specific message, or use next in rotation

        Returns:
            True if sent successfully
        """
        msg = message or self._get_next_message()
        if not msg:
            return False

        try:
            await self._send_callback(msg, self.config.channel)
            return True
        except Exception as e:
            logger.error(f"Failed to send announcement: {e}")
            return False
