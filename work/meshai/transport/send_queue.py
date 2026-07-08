"""Per-radio serialized, paced send queue.

Each radio (Meshtastic, MeshCore) gets its own RadioSendQueue instance.
The drain task pops one job at a time, executes the async send, resolves the
caller's Future with the actual bool result, then sleeps pacing_seconds before
popping the next job — enforcing strict FIFO serialization with configurable
inter-packet spacing.

Design notes
------------
* Jobs are ``(async_fn, concurrent.futures.Future | None)`` tuples where
  ``async_fn`` is a zero-argument coroutine factory (a lambda/closure that
  captures all send parameters).
* ``concurrent.futures.Future`` is used rather than ``asyncio.Future`` so the
  same future can be set from within ANY event loop (the MC drain runs on the
  MC loop; callers on the main loop wrap it via ``asyncio.wrap_future``).
* The queue is UNBOUNDED — no messages are ever dropped.
* Pacing is read live from the supplied callable on every drain cycle so a
  config-GUI change takes effect on the next send without restart.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

_PACING_FLOOR = 0.25  # seconds — absolute minimum between sends


class RadioSendQueue:
    """Serialized, paced outbound queue for a single radio transport.

    Usage::

        q = RadioSendQueue(pacing_fn=lambda: cfg.meshtastic_send_pacing_seconds)
        # On the event loop where the drain should run:
        q.start(loop)
        ...
        # To enqueue from the SAME loop (awaitable):
        result = await q.enqueue_async(my_send_coro_factory)
        # To enqueue from a DIFFERENT thread/loop (non-blocking):
        cfut = q.enqueue_threadsafe(my_send_coro_factory, target_loop)
        result = await asyncio.wrap_future(cfut)
        # On shutdown:
        await q.stop()
    """

    def __init__(self, pacing_fn: Callable[[], float]) -> None:
        """
        Args:
            pacing_fn: Zero-argument callable that returns the current pacing
                       gap in seconds.  Called on every drain iteration so
                       config-GUI changes take effect immediately.
        """
        self._pacing_fn = pacing_fn
        self._queue: Optional[asyncio.Queue] = None
        self._drain_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Create the asyncio.Queue and launch the drain task on *loop*.

        Must be called from WITHIN *loop* (i.e. via ``loop.call_soon_threadsafe``
        or from a coroutine running on *loop*).
        """
        self._loop = loop
        self._queue = asyncio.Queue()
        self._drain_task = loop.create_task(self._drain(), name="radio-send-queue-drain")
        logger.debug("RadioSendQueue: drain task started on loop %r", loop)

    async def stop(self) -> None:
        """Cancel the drain task and wait for it to finish.

        Any futures still in the queue are left unresolved; callers will see
        CancelledError if they are awaiting them.
        """
        if self._drain_task is None:
            return
        self._drain_task.cancel()
        try:
            await self._drain_task
        except asyncio.CancelledError:
            pass
        self._drain_task = None
        logger.debug("RadioSendQueue: drain task stopped")

    @property
    def running(self) -> bool:
        return self._drain_task is not None and not self._drain_task.done()

    # ------------------------------------------------------------------
    # Enqueue helpers
    # ------------------------------------------------------------------

    def _make_cfut(self) -> concurrent.futures.Future:
        return concurrent.futures.Future()

    async def enqueue_async(self, async_fn: Callable[[], Awaitable[bool]]) -> bool:
        """Enqueue *async_fn* and await its result on the current (same) loop.

        ``async_fn`` must be a zero-argument callable returning a coroutine
        that resolves to bool.  Raises if the queue is not started.
        """
        if self._queue is None or self._loop is None:
            raise RuntimeError("RadioSendQueue.start() not called")
        cfut: concurrent.futures.Future = self._make_cfut()
        await self._queue.put((async_fn, cfut))
        return await asyncio.wrap_future(cfut, loop=self._loop)

    def enqueue_threadsafe(
        self,
        async_fn: Callable[[], Awaitable[bool]],
        caller_loop: asyncio.AbstractEventLoop,
    ) -> concurrent.futures.Future:
        """Enqueue *async_fn* from a DIFFERENT loop or thread (non-blocking).

        Returns a ``concurrent.futures.Future``; wrap with
        ``asyncio.wrap_future(cfut, loop=caller_loop)`` to await on the
        caller's loop.  Raises if the queue is not started.
        """
        if self._queue is None or self._loop is None:
            raise RuntimeError("RadioSendQueue.start() not called")
        cfut: concurrent.futures.Future = self._make_cfut()
        asyncio.run_coroutine_threadsafe(self._queue.put((async_fn, cfut)), self._loop)
        return cfut

    def enqueue_fire_and_forget(self, async_fn: Callable[[], Awaitable[bool]]) -> None:
        """Enqueue *async_fn* with no result tracking (fire-and-forget).

        Safe to call from within the queue's own loop.
        """
        if self._queue is None:
            logger.warning("RadioSendQueue: fire-and-forget dropped (queue not started)")
            return
        self._queue.put_nowait((async_fn, None))

    # ------------------------------------------------------------------
    # Drain
    # ------------------------------------------------------------------

    async def _drain(self) -> None:
        """Pop items one at a time, execute, resolve future, pace."""
        while True:
            item = await self._queue.get()
            async_fn, cfut = item
            result = False
            try:
                result = await async_fn()
            except asyncio.CancelledError:
                if cfut is not None and not cfut.done():
                    cfut.cancel()
                raise
            except Exception as exc:
                logger.error("RadioSendQueue: send job raised: %s", exc, exc_info=True)
                if cfut is not None and not cfut.done():
                    cfut.set_exception(exc)
            else:
                if cfut is not None and not cfut.done():
                    cfut.set_result(result)

            # Pace: read live so config-GUI changes take effect next send.
            pacing = max(_PACING_FLOOR, self._pacing_fn())
            await asyncio.sleep(pacing)
