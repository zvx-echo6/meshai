"""Passive mesh traffic context buffer."""

import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Hard safety cap — prevents unbounded memory if a node loops.
# 50,000 entries × ~500 bytes = ~25 MB absolute ceiling.
_HARD_CAP = 50_000


@dataclass(frozen=True)
class MeshObservation:
    """A single observed mesh message."""

    timestamp: float
    sender_name: str
    sender_id: str
    channel: int
    is_dm: bool
    text: str


class MeshContext:
    """Rolling buffer of recent mesh traffic for LLM context injection.

    Passively observes all mesh messages (channels, DMs, BBS notifications)
    and makes them available as context when generating LLM responses.
    Observations older than max_age are pruned periodically.
    """

    def __init__(
        self,
        observe_channels: Optional[list[int]] = None,
        ignore_nodes: Optional[list[str]] = None,
        max_age: int = 2_592_000,
    ):
        """Initialize context buffer.

        Args:
            observe_channels: Channel indices to observe (None = all)
            ignore_nodes: Node IDs to exclude (e.g., own bot ID)
            max_age: Max age in seconds for observations (default 30 days)
        """
        self._buffer: deque[MeshObservation] = deque(maxlen=_HARD_CAP)
        self._observe_channels = set(observe_channels) if observe_channels else None
        self._ignore_nodes = set(ignore_nodes) if ignore_nodes else set()
        self._max_age = max_age

    def observe(
        self,
        sender_name: str,
        sender_id: str,
        text: str,
        channel: int,
        is_dm: bool,
    ) -> None:
        """Record an observed mesh message.

        Args:
            sender_name: Sender's display name
            sender_id: Sender's node ID
            text: Message text
            channel: Channel index
            is_dm: Whether this was a DM
        """
        # Filter by node
        if sender_id in self._ignore_nodes:
            return

        # Filter by channel (None = observe all)
        if self._observe_channels is not None and channel not in self._observe_channels:
            return

        obs = MeshObservation(
            timestamp=time.time(),
            sender_name=sender_name,
            sender_id=sender_id,
            channel=channel,
            is_dm=is_dm,
            text=text,
        )
        self._buffer.append(obs)
        logger.debug(f"Observed: ch{channel} {sender_name}: {text[:40]}...")

    def prune(self) -> int:
        """Remove observations older than max_age.

        Call this periodically (e.g., hourly from the main loop).

        Returns:
            Number of observations pruned
        """
        cutoff = time.time() - self._max_age
        before = len(self._buffer)

        # deque is sorted by time (append-only), so pop from the left
        while self._buffer and self._buffer[0].timestamp < cutoff:
            self._buffer.popleft()

        pruned = before - len(self._buffer)
        if pruned > 0:
            logger.info(f"Pruned {pruned} expired mesh observations ({len(self._buffer)} remaining)")
        return pruned

    def get_context_block(self, max_items: int = 20) -> str:
        """Format recent observations as a context block for the LLM.

        Args:
            max_items: Maximum observations to include

        Returns:
            Formatted context string, or empty string if no observations
        """
        now = time.time()

        # Take the most recent max_items (newest first, then reverse)
        recent = []
        for obs in reversed(self._buffer):
            if len(recent) >= max_items:
                break
            recent.append(obs)

        if not recent:
            return ""

        # Reverse back to chronological
        recent.reverse()

        lines = []
        for obs in recent:
            age_mins = int((now - obs.timestamp) / 60)
            if age_mins < 1:
                age_str = "just now"
            elif age_mins < 60:
                age_str = f"{age_mins}m ago"
            elif age_mins < 1440:
                age_str = f"{age_mins // 60}h{age_mins % 60}m ago"
            else:
                age_str = f"{age_mins // 1440}d ago"

            source = "DM" if obs.is_dm else f"ch{obs.channel}"
            lines.append(f"[{age_str}] [{source}] {obs.sender_name}: {obs.text}")

        return "\n".join(lines)

    @property
    def count(self) -> int:
        """Number of observations in buffer."""
        return len(self._buffer)
