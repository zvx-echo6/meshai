"""Response handling - delays and message chunking."""

import asyncio
import logging
import random
from typing import Optional

from .config import ResponseConfig
from .connector import MeshConnector

logger = logging.getLogger(__name__)


class Responder:
    """Handles response formatting, chunking, and delivery."""

    def __init__(self, config: ResponseConfig, connector: MeshConnector):
        self.config = config
        self.connector = connector

    async def send_response(
        self,
        text: str,
        destination: Optional[str] = None,
        channel: int = 0,
    ) -> bool:
        """Send a response with delay and chunking.

        Args:
            text: Response text (will be chunked if too long)
            destination: Node ID for DM, or None for channel broadcast
            channel: Channel to send on

        Returns:
            True if all chunks sent successfully
        """
        # Chunk the message
        chunks = self._chunk_message(text)

        # Limit to max messages
        if len(chunks) > self.config.max_messages:
            chunks = chunks[: self.config.max_messages]
            # Truncate last chunk to indicate more was cut
            if chunks:
                last = chunks[-1]
                if len(last) > self.config.max_length - 3:
                    chunks[-1] = last[: self.config.max_length - 3] + "..."

        success = True
        for i, chunk in enumerate(chunks):
            # Apply delay before sending
            delay = random.uniform(self.config.delay_min, self.config.delay_max)
            await asyncio.sleep(delay)

            # Send chunk
            sent = self.connector.send_message(
                text=chunk,
                destination=destination,
                channel=channel,
            )

            if not sent:
                logger.error(f"Failed to send chunk {i + 1}/{len(chunks)}")
                success = False
                break

            logger.debug(f"Sent chunk {i + 1}/{len(chunks)}: {chunk[:50]}...")

        return success

    def _chunk_message(self, text: str) -> list[str]:
        """Split message into chunks respecting max_length.

        Tries to break at word boundaries when possible.

        Args:
            text: Text to chunk

        Returns:
            List of chunks
        """
        max_len = self.config.max_length

        if len(text) <= max_len:
            return [text]

        chunks = []
        remaining = text

        while remaining:
            if len(remaining) <= max_len:
                chunks.append(remaining)
                break

            # Find a good break point
            chunk = remaining[:max_len]

            # Try to break at word boundary
            break_point = self._find_break_point(chunk)

            if break_point > 0:
                chunks.append(remaining[:break_point].rstrip())
                remaining = remaining[break_point:].lstrip()
            else:
                # No good break point, hard cut
                chunks.append(chunk)
                remaining = remaining[max_len:]

        return chunks

    def _find_break_point(self, text: str) -> int:
        """Find best break point in text.

        Prefers: sentence end > comma/semicolon > space

        Args:
            text: Text to find break in

        Returns:
            Index to break at, or 0 if no good break found
        """
        # Look for sentence endings
        for char in ".!?":
            pos = text.rfind(char)
            if pos > len(text) // 2:  # Only if in second half
                return pos + 1

        # Look for clause breaks
        for char in ",;:":
            pos = text.rfind(char)
            if pos > len(text) // 2:
                return pos + 1

        # Look for word boundary
        pos = text.rfind(" ")
        if pos > len(text) // 3:  # Only if past first third
            return pos

        return 0

    def format_dm_response(self, text: str, sender_name: str) -> str:
        """Format response for DM context.

        Args:
            text: Response text
            sender_name: Name of recipient

        Returns:
            Formatted response (currently unchanged)
        """
        # Could prefix with name or add other formatting
        return text

    def format_channel_response(
        self, text: str, sender_name: str, mention_sender: bool = False
    ) -> str:
        """Format response for channel context.

        Args:
            text: Response text
            sender_name: Name of sender being replied to
            mention_sender: Whether to prefix with sender's name

        Returns:
            Formatted response
        """
        if mention_sender:
            # Check if adding prefix would exceed max length
            prefix = f"@{sender_name}: "
            if len(prefix) + len(text) <= self.config.max_length * self.config.max_messages:
                return prefix + text

        return text
