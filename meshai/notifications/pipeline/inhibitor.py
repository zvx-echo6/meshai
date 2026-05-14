"""Severity-based event inhibitor.

Suppresses lower-severity events when a higher-severity event for the
same logical incident (matching inhibit_keys) is already active.

Inhibit keys are operator-defined strings on the Event that identify
the underlying incident. Two events sharing an inhibit_key refer to
the same situation. If a critical event for "battery:BLD-MTN" fired
recently, a subsequent warning event with the same key gets suppressed.
"""

import logging
import time
from typing import Callable

from meshai.notifications.events import Event


class Inhibitor:
    """Suppress lower-severity events when higher-severity is active."""

    SEVERITY_RANK = {"routine": 0, "priority": 1, "immediate": 2}

    def __init__(
        self,
        next_handler: Callable[[Event], None],
        ttl_seconds: float = 1800.0,
    ):
        """Initialize.

        Args:
            next_handler: Callable that receives non-suppressed events.
            ttl_seconds: How long an inhibit_key remains active after
                the originating event (default 30 minutes).
        """
        self._next = next_handler
        self._ttl = ttl_seconds
        # {inhibit_key: (rank, expires_at)}
        self._active: dict[str, tuple[int, float]] = {}
        self._logger = logging.getLogger("meshai.pipeline.inhibitor")

    def _now(self) -> float:
        # Hookable for tests
        return time.time()

    def _prune_expired(self, now: float) -> None:
        expired = [k for k, (_, exp) in self._active.items() if exp <= now]
        for k in expired:
            del self._active[k]

    def handle(self, event: Event) -> None:
        """Process an event: either suppress it or pass it on.

        If any of the event's inhibit_keys is currently active at a
        higher-or-equal rank, the event is suppressed. Otherwise, the
        event's inhibit_keys are recorded/upgraded, and the event is
        passed to the next handler.
        """
        now = self._now()
        self._prune_expired(now)

        event_rank = self.SEVERITY_RANK.get(event.severity, 0)

        # Check suppression
        for key in event.inhibit_keys:
            entry = self._active.get(key)
            if entry is not None:
                active_rank, _ = entry
                if active_rank >= event_rank:
                    self._logger.info(
                        f"SUPPRESSED event {event.id} ({event.severity}) "
                        f"by active key {key!r} at rank {active_rank}"
                    )
                    return

        # Record / upgrade entries
        new_expires = now + self._ttl
        for key in event.inhibit_keys:
            existing = self._active.get(key)
            if existing is None or existing[0] < event_rank:
                self._active[key] = (event_rank, new_expires)

        # Pass through
        self._next(event)

    def active_keys(self) -> dict[str, tuple[int, float]]:
        """For tests: snapshot of currently-active inhibit keys."""
        return dict(self._active)

    def clear(self) -> None:
        """For tests: reset state."""
        self._active.clear()
