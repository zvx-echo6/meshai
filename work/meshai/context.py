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
    transport: str = "meshtastic"


class MeshContext:
    """Rolling buffer of recent mesh traffic for LLM context injection.

    Passively observes all mesh messages (channels, DMs, BBS notifications)
    and makes them available as context when generating LLM responses.
    Observations older than max_age are pruned periodically.

    Durability: every observe() writes through to the mesh_observations
    SQLite table (meshai.sqlite) in addition to the in-memory deque, and
    __init__ loads recent rows back from SQLite so recap context survives
    a container restart. The SQLite path is entirely fail-safe -- any DB
    error is logged and swallowed so it never blocks or drops the
    in-memory observation path (which is what response generation reads
    from on the hot path).
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

        self._load_from_db()

    def _load_from_db(self) -> None:
        """Load recent observations from SQLite into the deque on startup.

        Fail-safe: any DB error is logged and swallowed -- an empty deque
        (today's pre-fix behavior) is an acceptable fallback, never a
        startup failure.
        """
        try:
            from .persistence.mesh_observations import load_recent, prune_older_than

            prune_older_than(self._max_age)
            rows = load_recent(self._max_age, _HARD_CAP)
            for row in rows:
                self._buffer.append(
                    MeshObservation(
                        timestamp=row["ts"],
                        sender_name=row["sender_name"] or "",
                        sender_id=row["sender_id"] or "",
                        channel=row["channel"] if row["channel"] is not None else 0,
                        is_dm=bool(row["is_dm"]),
                        text=row["text"] or "",
                        transport=row["transport"] or "meshtastic",
                    )
                )
            if rows:
                logger.info(f"Loaded {len(rows)} mesh observations from durable store")
        except Exception:
            logger.exception("Failed to load mesh observations from durable store")

    def observe(
        self,
        sender_name: str,
        sender_id: str,
        text: str,
        channel: int,
        is_dm: bool,
        transport: str = "meshtastic",
    ) -> None:
        """Record an observed mesh message.

        Args:
            sender_name: Sender's display name
            sender_id: Sender's node ID
            text: Message text
            channel: Channel index
            is_dm: Whether this was a DM
            transport: Origin transport ("meshtastic" or "meshcore")
        """
        # Filter by node
        if sender_id in self._ignore_nodes:
            return

        # Filter by channel (None = observe all) — applies ONLY to Meshtastic
        # observations because _observe_channels contains Meshtastic channel
        # indices which have no relationship to MeshCore slot indices.
        # MeshCore channel filtering is handled at the transport layer.
        if transport == "meshtastic":
            if self._observe_channels is not None and channel not in self._observe_channels:
                return

        obs = MeshObservation(
            timestamp=time.time(),
            sender_name=sender_name,
            sender_id=sender_id,
            channel=channel,
            is_dm=is_dm,
            text=text,
            transport=transport,
        )
        self._buffer.append(obs)
        logger.debug(f"Observed: ch{channel} {sender_name} [{transport}]: {text[:40]}...")

        # Write-through to durable store. Fail-safe: never let a DB error
        # break ingestion -- the in-memory buffer above already has the
        # observation regardless of what happens here.
        try:
            from .persistence.mesh_observations import insert_observation

            insert_observation(
                ts=obs.timestamp,
                transport=obs.transport,
                channel=obs.channel,
                is_dm=obs.is_dm,
                sender_name=obs.sender_name,
                sender_id=obs.sender_id,
                text=obs.text,
            )
        except Exception:
            logger.exception("Failed to write mesh observation to durable store")

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

        # Durable-store prune rides the same cadence as the in-memory prune
        # (called hourly from main.py's periodic cleanup). Fail-safe.
        try:
            from .persistence.mesh_observations import prune_older_than

            prune_older_than(self._max_age)
        except Exception:
            logger.exception("Failed to prune durable mesh observations store")

        return pruned

    def get_context_block(self, max_items: int = 20, transport: Optional[str] = None) -> str:
        """Format recent observations as a context block for the LLM.

        Args:
            max_items: Maximum observations to include
            transport: When provided, include only observations from this
                transport ("meshtastic" or "meshcore"). When None, include
                all observations (backward-compatible).

        Returns:
            Formatted context string, or empty string if no observations
        """
        now = time.time()

        # Take the most recent max_items (newest first, then reverse)
        recent = []
        for obs in reversed(self._buffer):
            if len(recent) >= max_items:
                break
            if transport is not None and obs.transport != transport:
                continue
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

    def update_settings(
        self,
        *,
        max_age: Optional[int] = None,
        observe_channels: Optional[list] = None,
        ignore_nodes: Optional[list] = None,
    ) -> None:
        """Apply config changes to the live store without a restart.

        Mirrors the normalization rules of __init__ exactly:
          - observe_channels: set(v) if v else None  (empty list → None = observe all)
          - ignore_nodes:     set(v) if v else set() (empty list/None → empty set)
          - max_age:          stored as-is

        Only parameters that are not None are updated; omit a parameter to
        leave its current value unchanged.
        """
        if max_age is not None:
            self._max_age = max_age
        if observe_channels is not None:
            self._observe_channels = set(observe_channels) if observe_channels else None
        if ignore_nodes is not None:
            self._ignore_nodes = set(ignore_nodes) if ignore_nodes else set()

    @property
    def count(self) -> int:
        """Number of observations in buffer."""
        return len(self._buffer)
