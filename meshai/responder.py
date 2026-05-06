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
        """Send response messages with ACK waiting and retry.

        For DMs: waits for ACK before sending next message, retries once on failure.
        For broadcasts: uses delay-based pacing (no ACK for broadcasts).
        """
        if isinstance(messages, str):
            messages = [messages]

        if not messages:
            return True

        success = True
        is_dm = destination is not None

        for i, msg in enumerate(messages):
            # Randomized delay before sending (except first message)
            if i > 0:
                delay = random.uniform(self.config.delay_min, self.config.delay_max)
                await asyncio.sleep(delay)

            if is_dm and hasattr(self.connector, 'send_and_wait_ack'):
                # DMs: send and wait for ACK
                ack = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.connector.send_and_wait_ack,
                    msg, destination, channel, 30.0,
                )

                if not ack:
                    # Retry once
                    logger.warning(f"No ACK for msg {i+1}/{len(messages)}, retrying...")
                    await asyncio.sleep(random.uniform(3.0, 5.0))
                    ack = await asyncio.get_event_loop().run_in_executor(
                        None,
                        self.connector.send_and_wait_ack,
                        msg, destination, channel, 30.0,
                    )
                    if not ack:
                        logger.error(f"No ACK after retry for msg {i+1}/{len(messages)}, skipping remaining")
                        success = False
                        break

                logger.debug(f"Sent+ACK msg {i+1}/{len(messages)}: {msg[:50]}...")
            else:
                # Broadcasts or fallback: fire and delay
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

