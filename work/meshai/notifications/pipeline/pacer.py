"""FIFO output pacer for fire broadcasts.

Rate-limits fire events to at most one broadcast per interval (default 60s).
Used both during drain catch-up (post-reconnect decision pass) and during
normal operation to prevent burst-delivery of multiple fire events in rapid
succession.

The queue is in-memory only — no persistence. On restart, the drain mode in
consumer.py re-evaluates from DB state, so queued events lost on shutdown
are re-derived naturally.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class FirePacer:
    """Unbounded FIFO queue that drains events to the bus at a fixed rate."""

    def __init__(self, bus, interval_seconds: float = 60.0):
        """Args:
            bus: the EventBus whose .emit() method delivers events downstream
            interval_seconds: minimum seconds between consecutive deliveries
        """
        self._bus = bus
        self._interval = interval_seconds
        self._queue: asyncio.Queue = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None

    def enqueue(self, event) -> None:
        """Non-blocking enqueue. Safe to call from sync code in the same loop."""
        self._queue.put_nowait(event)
        logger.debug("pacer: enqueued event source=%s category=%s (pending=%d)",
                      event.source, event.category, self._queue.qsize())

    async def _drain_loop(self) -> None:
        """Pop one event, emit it, sleep interval, repeat."""
        while True:
            try:
                event = await self._queue.get()
            except asyncio.CancelledError:
                return
            try:
                self._bus.emit(event)
                logger.info("pacer: emitted event source=%s category=%s (remaining=%d)",
                            event.source, event.category, self._queue.qsize())
            except Exception:
                logger.exception("pacer: bus.emit() failed for event source=%s",
                                 event.source)
            if self._queue.empty():
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
            remaining = self._queue.qsize()
            if remaining:
                logger.warning("pacer: stopped with %d events still queued", remaining)
            else:
                logger.info("pacer: stopped (queue empty)")

    def pending_count(self) -> int:
        """Number of events waiting in the queue."""
        return self._queue.qsize()
