"""Bounded, time-limited de-duplication cache for inbound mesh messages.

MeshCore client apps re-send a DM (or a channel message) when they don't
see a delivery ACK in time. The re-send carries the same sender, the same
text, and -- critically -- the same ``sender_timestamp`` (the sending
firmware's own clock, embedded in the wire packet at the moment the user
hit send; NOT wall time at receipt, so it stays identical across every
resend of that one message). For Meshtastic, the retransmit keeps the same
packet id. Without a de-dupe gate, each resend looks like a brand new
inbound message and gets answered again -- the "triple reply" symptom.

Used by ``MessageRouter.should_respond()`` (see router.py) to drop a
resend BEFORE it reaches the LLM/history a second time.
"""
from __future__ import annotations

import time as _time
from collections import OrderedDict
from typing import Callable, Optional

DEFAULT_MAX_SIZE = 500
# TTL for an id-keyed entry (stable sender_timestamp / packet id available).
DEFAULT_TTL_SECONDS = 600.0  # 10 minutes
# Window for the fallback key (no stable id available) -- short on purpose,
# so a genuine later repeat of the same text still gets answered.
DEFAULT_FALLBACK_WINDOW_SECONDS = 90.0


class InboundDedupeCache:
    """LRU-bounded, TTL-expiring cache of recently-seen inbound message
    identities.

    Two key shapes:
      - id-keyed: (transport, scope, msg_key, text) when a stable,
        replay-proof identifier is available -- MeshCore's sender_timestamp
        or a Meshtastic packet id. Window: ``ttl_seconds`` (default 600s).
      - fallback: (transport, scope, text) when no such identifier is
        available. Window: ``fallback_window_seconds`` (default 90s).

    ``scope`` is caller-supplied (e.g. the sender id for a DM, or
    "sender#channel" for a channel message) so two different senders -- or
    the same sender on two different channels -- never collide.

    A duplicate hit does NOT extend the window from the moment of the hit;
    the window always counts from the message's first sighting, so a fast
    flood of resends can't keep the entry alive forever.
    """

    def __init__(
        self,
        max_size: int = DEFAULT_MAX_SIZE,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        fallback_window_seconds: float = DEFAULT_FALLBACK_WINDOW_SECONDS,
        clock: Callable[[], float] = _time.monotonic,
    ) -> None:
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        self.fallback_window_seconds = fallback_window_seconds
        self._clock = clock
        self._entries: "OrderedDict[tuple, float]" = OrderedDict()

    def _purge_expired(self, now: float) -> None:
        expired = [
            key for key, seen_at in self._entries.items()
            if (now - seen_at) > self.ttl_seconds
        ]
        for key in expired:
            del self._entries[key]

    def is_duplicate(
        self,
        transport: str,
        scope: str,
        text: str,
        msg_key: Optional[object] = None,
    ) -> bool:
        """Return True if this looks like a resend already recorded within
        its window (the sighting is NOT updated); False -- and the sighting
        IS recorded, fresh -- the first time / after the window expires."""
        now = self._clock()
        self._purge_expired(now)

        if msg_key is not None:
            key = ("id", transport, scope, msg_key, text)
            window = self.ttl_seconds
        else:
            key = ("fallback", transport, scope, text)
            window = self.fallback_window_seconds

        seen_at = self._entries.get(key)
        if seen_at is not None and (now - seen_at) <= window:
            return True

        self._entries[key] = now
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_size:
            self._entries.popitem(last=False)
        return False
