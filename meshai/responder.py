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
        """Send response messages with ACK-accelerated delivery.

        DMs: Send -> wait for ACK -> if ACK, send next immediately.
             If no ACK, retry once -> if still no ACK, abort.
        Broadcasts: delay-based pacing (no ACK available).
        """
        if isinstance(messages, str):
            messages = [messages]

        if not messages:
            return True

        success = True
        is_dm = destination is not None

        for i, msg in enumerate(messages):
            if is_dm and hasattr(self.connector, 'send_and_wait_ack'):
                # Send and wait for ACK
                ack = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.connector.send_and_wait_ack,
                    msg, destination, channel, 30.0,
                )

                if ack:
                    # ACK received - next message sends immediately (no delay)
                    logger.debug(f"ACK msg {i+1}/{len(messages)}: {msg[:50]}...")
                    continue

                # No ACK - retry same message once after short pause
                logger.warning(f"No ACK for msg {i+1}/{len(messages)}, retrying...")
                await asyncio.sleep(2.0)

                ack = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.connector.send_and_wait_ack,
                    msg, destination, channel, 30.0,
                )

                if ack:
                    logger.debug(f"ACK on retry msg {i+1}/{len(messages)}")
                    continue

                # Double failure - abort
                logger.error(f"No ACK after retry msg {i+1}/{len(messages)}, aborting remaining")
                success = False
                break

            else:
                # Broadcasts or fallback: delay-based pacing
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
