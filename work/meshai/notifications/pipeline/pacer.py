"""FIFO output pacer for fire broadcasts.

Rate-limits fire events to at most one broadcast per interval (default 60s).
Used both during drain catch-up (post-reconnect decision pass) and during
normal operation to prevent burst-delivery of multiple fire events in rapid
succession.

The queue is in-memory only — no persistence. On restart, the drain mode in
consumer.py re-evaluates from DB state, so queued events lost on shutdown
are re-derived naturally.

Issue #119 (head-of-line priority): an "immediate"-severity event (e.g. a
FIRMS wildfire_spotting broadcast) is enqueued at the HEAD of the FIFO
instead of the tail, so it is never stuck waiting behind lower-urgency
"priority" events already queued. "priority" events still append at the
tail (plain FIFO among themselves). This is a pure ordering change --
the queue remains unbounded and nothing is ever dropped.
"""

from __future__ import annotations

import asyncio
import collections
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class FirePacer:
    """Unbounded FIFO (with immediate-severity head-of-line) queue that
    drains events to the bus at a fixed rate."""

    def __init__(self, bus, interval_seconds: float = 60.0):
        """Args:
            bus: the EventBus whose .emit() method delivers events downstream
            interval_seconds: minimum seconds between consecutive deliveries
        """
        self._bus = bus
        self._interval = interval_seconds
        # A plain deque + asyncio.Event, rather than asyncio.Queue, so an
        # "immediate" event can jump the line via appendleft() -- asyncio.Queue
        # only exposes tail-append (put_nowait). Never bounded, never drops.
        self._queue: collections.deque = collections.deque()
        self._not_empty = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    def enqueue(self, event) -> None:
        """Non-blocking enqueue. Safe to call from sync code in the same loop.

        Issue #119: "immediate"-severity events go to the HEAD of the queue
        so they broadcast before any already-queued "priority" event; every
        other severity appends at the tail as before.
        """
        if getattr(event, "severity", None) == "immediate":
            self._queue.appendleft(event)
            logger.debug(
                "pacer: enqueued (head-of-line, immediate) event source=%s "
                "category=%s (pending=%d)",
                event.source, event.category, len(self._queue))
        else:
            self._queue.append(event)
            logger.debug("pacer: enqueued event source=%s category=%s (pending=%d)",
                          event.source, event.category, len(self._queue))
        self._not_empty.set()

    async def _drain_loop(self) -> None:
        """Pop one event, emit it, sleep interval, repeat."""
        while True:
            while not self._queue:
                self._not_empty.clear()
                try:
                    await self._not_empty.wait()
                except asyncio.CancelledError:
                    return
            event = self._queue.popleft()
            try:
                self._bus.emit(event)
                logger.info("pacer: emitted event source=%s category=%s (remaining=%d)",
                            event.source, event.category, len(self._queue))
            except Exception:
                logger.exception("pacer: bus.emit() failed for event source=%s",
                                 event.source)
            if not self._queue:
                # No point sleeping when nothing is queued — wait for next put
                continue
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                return

    async def start(self) -> None:
        """Spawn the drain loop as a background task."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._drain_loop())
        logger.info("pacer: started (interval=%.0fs)", self._interval)

    async def stop(self) -> None:
        """Cancel the drain loop. Queued events are discarded."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            remaining = len(self._queue)
            if remaining:
                logger.warning("pacer: stopped with %d events still queued", remaining)
            else:
                logger.info("pacer: stopped (queue empty)")

    def pending_count(self) -> int:
        """Number of events waiting in the queue."""
        return len(self._queue)
