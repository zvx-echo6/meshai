"""Event grouper.

Coalesces events sharing a group_key inside a time window. The most
recent event for a group_key wins; older versions are replaced.
Events without a group_key pass through immediately.

The grouper holds events in a window. tick() flushes events whose
window has expired. For Phase 2.2, tick() is called explicitly by
tests; Phase 2.3+ will integrate with the digest scheduler for live
operation.
"""

import logging
import time
from typing import Callable

from meshai.notifications.events import Event


class Grouper:
    """Coalesce same-group_key events inside a window."""

    def __init__(
        self,
        next_handler: Callable[[Event], None],
        window_seconds: float = 60.0,
    ):
        """Initialize.

        Args:
            next_handler: Callable that receives events when they
                exit the grouper (either immediately if no group_key,
                or after the window expires).
            window_seconds: How long to hold a group_key before
                emitting downstream (default 60 seconds).
        """
        self._next = next_handler
        self._window = window_seconds
        # {group_key: (event, hold_until_ts)}
        self._held: dict[str, tuple[Event, float]] = {}
        self._logger = logging.getLogger("meshai.pipeline.grouper")

    def _now(self) -> float:
        return time.time()

    def handle(self, event: Event) -> None:
        """Process an event.

        Events without group_key pass through immediately.
        Events with group_key are held, replacing any prior held
        event with the same group_key. The held event is emitted
        later via tick().
        """
        if not event.group_key:
            self._next(event)
            return

        now = self._now()
        hold_until = now + self._window
        prior = self._held.get(event.group_key)
        if prior is not None:
            self._logger.info(
                f"COALESCED event {event.id} into group {event.group_key!r}, "
                f"replacing prior event {prior[0].id}"
            )
        self._held[event.group_key] = (event, hold_until)

    def tick(self) -> int:
        """Flush events whose window has expired.

        Returns the number of events emitted.
        """
        now = self._now()
        to_emit = [
            (gk, ev) for gk, (ev, hu) in self._held.items() if hu <= now
        ]
        for gk, _ in to_emit:
            del self._held[gk]
        for _, ev in to_emit:
            self._next(ev)
        return len(to_emit)

    def flush_all(self) -> int:
        """Immediately emit every held event, regardless of window.

        Used at shutdown and by tests. Returns count emitted.
        """
        events = [ev for ev, _ in self._held.values()]
        self._held.clear()
        for ev in events:
            self._next(ev)
        return len(events)

    def held_count(self) -> int:
        """For tests: number of events currently held."""
        return len(self._held)
