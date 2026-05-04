"""Response handling - delays and message delivery."""

import asyncio
import logging
import random
from typing import Optional

from .config import ResponseConfig
from .connector import MeshConnector

logger = logging.getLogger(__name__)


class Responder:
    """Handles response delivery with pacing."""

    def __init__(self, config: ResponseConfig, connector: MeshConnector):
        self.config = config
        self.connector = connector

    async def send_response(
        self,
        messages: list[str] | str,
        destination: Optional[str] = None,
        channel: int = 0,
    ) -> bool:
        """Send response messages with human-pacing delays.

        Args:
            messages: Pre-chunked messages list, or single string (legacy)
            destination: Node ID for DM, or None for channel broadcast
            channel: Channel to send on

        Returns:
            True if all messages sent successfully
        """
        # Handle legacy single string
        if isinstance(messages, str):
            messages = [messages]

        if not messages:
            return True

        success = True
        for i, msg in enumerate(messages):
            # Apply delay before sending (except first message)
            if i > 0:
                delay = random.uniform(self.config.delay_min, self.config.delay_max)
                await asyncio.sleep(delay)

            # Send message
            sent = self.connector.send_message(
                text=msg,
                destination=destination,
                channel=channel,
            )

            if not sent:
                logger.error(f"Failed to send message {i + 1}/{len(messages)}")
                success = False
                break

            logger.debug(f"Sent message {i + 1}/{len(messages)}: {msg[:50]}...")

        return success
