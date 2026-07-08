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
        transport: Optional[str] = None,
    ) -> bool:
        """Send response messages with randomized delay pacing.

        Args:
            messages: One or more message strings to send.
            destination: Node ID for a DM, or None for broadcast.
            channel: Channel index to send on.
            transport: Optional routing hint threaded from the originating
                       MeshMessage.  Passed through to connector.send_message
                       so CompositeTransport can route DM replies back over
                       the mesh they arrived on.  Single-transport connectors
                       accept and ignore it; defaults to None so all existing
                       call sites are unaffected.
        """
        if isinstance(messages, str):
            messages = [messages]

        if not messages:
            return True

        success = True

        for i, msg in enumerate(messages):
            if i > 0:
                delay = random.uniform(self.config.delay_min, self.config.delay_max)
                await asyncio.sleep(delay)

            sent = await self.connector.send_message_async(
                text=msg,
                destination=destination,
                channel=channel,
                transport=transport,
            )
            if not sent:
                logger.error(f"Failed to send message {i+1}/{len(messages)}")
                success = False
                break

            logger.debug(f"Sent msg {i+1}/{len(messages)}: {msg[:50]}...")

        return success
