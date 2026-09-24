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
        meshcore_channel: Optional[str] = None,
        reply_id: Optional[int] = None,
    ) -> bool:
        """Send response messages with randomized delay pacing.

        Args:
            messages: One or more message strings to send.
            destination: Node ID for a DM, or None for broadcast (used for
                       channel-mention replies -- see main.py's _on_message).
            channel: Channel index to send on (Meshtastic semantics).
            transport: Optional routing hint threaded from the originating
                       MeshMessage.  Passed through to connector.send_message
                       so CompositeTransport can route DM replies back over
                       the mesh they arrived on.  Single-transport connectors
                       accept and ignore it; defaults to None so all existing
                       call sites are unaffected.
            meshcore_channel: Per-family/per-reply MeshCore channel NAME for
                       a broadcast (destination=None). None for DMs and for
                       Meshtastic-origin channel replies.
            reply_id: Optional incoming packet id to thread every outgoing
                       chunk as a reply to (Meshtastic channel-mention
                       replies only; see router.py's should_respond).
                       Applied to EVERY chunk (the whole multi-chunk answer
                       threads to the same asker packet), not just the first.
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
                meshcore_channel=meshcore_channel,
                reply_id=reply_id,
            )
            if not sent:
                logger.error(f"Failed to send message {i+1}/{len(messages)}")
                success = False
                break

            logger.debug(f"Sent msg {i+1}/{len(messages)}: {msg[:50]}...")

        return success
