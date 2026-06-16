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
        """Send response messages with randomized delay pacing."""
        if isinstance(messages, str):
            messages = [messages]

        if not messages:
            return True

        success = True

        for i, msg in enumerate(messages):
            if i > 0:
                delay = random.uniform(self.config.delay_min, self.config.delay_max)
                await asyncio.sleep(delay)

            sent = self.connector.send_message(
                text=msg,
                destination=destination,
                channel=channel,
            )
            if not sent:
                logger.error(f"Failed to send message {i+1}/{len(messages)}")
                success = False
                break

            logger.debug(f"Sent msg {i+1}/{len(messages)}: {msg[:50]}...")

        return success
