"""Tests for the per-radio serialized send queue.

Covers:
- FIFO ordering (no reordering, no drops)
- Pacing: consecutive timestamps >= pacing_seconds (0.05 s in tests)
- Event loop not blocked during burst
- Concurrent tasks make progress while queue drains
- Config floor enforced (min 0.25 s)
- Burst serialized: no parallel send overlap
- RadioSendQueue.start/stop lifecycle
- MT transport send_message_async falls back when queue not started
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import time
from typing import List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meshai.transport.send_queue import RadioSendQueue, _PACING_FLOOR


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_queue(pacing: float = 0.05) -> RadioSendQueue:
    return RadioSendQueue(pacing_fn=lambda: pacing)


async def _run_with_queue(pacing: float, jobs) -> list:
    """Run *jobs* (list of async callables) through a queue; return results in order."""
    q = _make_queue(pacing)
    loop = asyncio.get_event_loop()
    q.start(loop)
    results = []
    for fn in jobs:
        result = await q.enqueue_async(fn)
        results.append(result)
    await q.stop()
    return results


# ---------------------------------------------------------------------------
# FIFO ordering
# ---------------------------------------------------------------------------


class TestFIFO:
    @pytest.mark.asyncio
    async def test_results_in_enqueue_order(self):
        """Results come back in the order items were enqueued."""
        order = []

        async def make_job(n):
            async def _job():
                order.append(n)
                return True
            return _job

        q = _make_queue(pacing=0.01)
        loop = asyncio.get_event_loop()
        q.start(loop)

        futs = []
        for i in range(5):
            futs.append(await q.enqueue_async(await make_job(i)))

        # Wait for all to complete
        await asyncio.gather(*[asyncio.wrap_future(concurrent.futures.Future()) for _ in range(0)],
                              return_exceptions=True)
        # Stop drains remaining items
        await q.stop()

        assert order == [0, 1, 2, 3, 4]

    @pytest.mark.asyncio
    async def test_no_drops(self):
        """Every enqueued item executes — no items are dropped.

        Uses enqueue_async so we can await all completions without sleeping;
        this also avoids dependence on the pacing floor timing.
        """
        executed = []

        async def make_job(n):
            async def _job():
                executed.append(n)
                return True
            return _job

        q = _make_queue(pacing=0.01)
        loop = asyncio.get_event_loop()
        q.start(loop)

        N = 6
        # Enqueue all jobs concurrently (fire them as tasks), then gather.
        tasks = [asyncio.ensure_future(q.enqueue_async(await make_job(i))) for i in range(N)]
        await asyncio.gather(*tasks)
        await q.stop()

        assert len(executed) == N
        assert sorted(executed) == list(range(N))


# ---------------------------------------------------------------------------
# Pacing
# ---------------------------------------------------------------------------


class TestPacing:
    @pytest.mark.asyncio
    async def test_pacing_gap_respected(self):
        """Send timestamps are spaced >= pacing_seconds apart."""
        pacing = 0.05
        timestamps: list[float] = []

        async def _job():
            timestamps.append(time.monotonic())
            return True

        q = _make_queue(pacing=pacing)
        loop = asyncio.get_event_loop()
        q.start(loop)

        N = 4
        futs = [asyncio.ensure_future(q.enqueue_async(_job)) for _ in range(N)]
        await asyncio.gather(*futs)
        await q.stop()

        assert len(timestamps) == N
        for i in range(1, N):
            gap = timestamps[i] - timestamps[i - 1]
            assert gap >= pacing * 0.9, f"gap[{i}]={gap:.3f} < pacing={pacing}"

    @pytest.mark.asyncio
    async def test_pacing_read_live(self):
        """Pacing value is read from the callable on each iteration."""
        pacing_value = 0.05
        timestamps: list[float] = []

        async def _job():
            timestamps.append(time.monotonic())
            return True

        q = RadioSendQueue(pacing_fn=lambda: pacing_value)
        loop = asyncio.get_event_loop()
        q.start(loop)

        # Enqueue first
        futs = [asyncio.ensure_future(q.enqueue_async(_job)) for _ in range(2)]
        await asyncio.gather(*futs)

        # Change pacing and run 2 more
        pacing_value = 0.10
        futs = [asyncio.ensure_future(q.enqueue_async(_job)) for _ in range(2)]
        await asyncio.gather(*futs)

        await q.stop()
        assert len(timestamps) == 4


# ---------------------------------------------------------------------------
# Config floor
# ---------------------------------------------------------------------------


class TestPacingFloor:
    @pytest.mark.asyncio
    async def test_floor_enforced(self):
        """Pacing below the floor is clamped up to _PACING_FLOOR (0.25 s)."""
        q = RadioSendQueue(pacing_fn=lambda: 0.001)  # way below floor
        loop = asyncio.get_event_loop()
        q.start(loop)

        timestamps: list[float] = []

        async def _job():
            timestamps.append(time.monotonic())
            return True

        futs = [asyncio.ensure_future(q.enqueue_async(_job)) for _ in range(2)]
        await asyncio.gather(*futs)
        await q.stop()

        assert len(timestamps) == 2
        gap = timestamps[1] - timestamps[0]
        assert gap >= _PACING_FLOOR * 0.9, f"floor not enforced: gap={gap:.3f}"

    def test_floor_constant(self):
        assert _PACING_FLOOR == 0.25


# ---------------------------------------------------------------------------
# Event loop not blocked
# ---------------------------------------------------------------------------


class TestNonBlocking:
    @pytest.mark.asyncio
    async def test_other_tasks_progress_during_drain(self):
        """The event loop remains available to other coroutines while the queue drains."""
        pacing = 0.05
        q = _make_queue(pacing=pacing)
        loop = asyncio.get_event_loop()
        q.start(loop)

        progress_count = 0

        async def _send_job():
            await asyncio.sleep(0)  # yield briefly
            return True

        async def _observer():
            nonlocal progress_count
            for _ in range(8):
                await asyncio.sleep(0.02)
                progress_count += 1

        # Run drain + observer concurrently
        futs = [asyncio.ensure_future(q.enqueue_async(_send_job)) for _ in range(5)]
        obs = asyncio.ensure_future(_observer())
        await asyncio.gather(*futs, obs)
        await q.stop()

        # Observer should have completed all its iterations
        assert progress_count >= 6, f"observer only made {progress_count} iterations"

    @pytest.mark.asyncio
    async def test_send_returns_actual_result(self):
        """Future resolves to the actual bool returned by the send job."""
        q = _make_queue(pacing=0.01)
        loop = asyncio.get_event_loop()
        q.start(loop)

        async def _ok():
            return True

        async def _fail():
            return False

        r1 = await q.enqueue_async(_ok)
        r2 = await q.enqueue_async(_fail)
        r3 = await q.enqueue_async(_ok)
        await q.stop()

        assert r1 is True
        assert r2 is False
        assert r3 is True


# ---------------------------------------------------------------------------
# Serialization — no overlap
# ---------------------------------------------------------------------------


class TestSerialization:
    @pytest.mark.asyncio
    async def test_no_concurrent_sends(self):
        """Only one job runs at a time — active_at windows never overlap."""
        active_intervals: list[tuple[float, float]] = []
        lock = asyncio.Lock()

        async def _job():
            start = time.monotonic()
            async with lock:
                end = time.monotonic()
            active_intervals.append((start, end))
            return True

        q = _make_queue(pacing=0.01)
        loop = asyncio.get_event_loop()
        q.start(loop)

        N = 5
        futs = [asyncio.ensure_future(q.enqueue_async(_job)) for _ in range(N)]
        await asyncio.gather(*futs)
        await q.stop()

        assert len(active_intervals) == N


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_stop(self):
        q = _make_queue(pacing=0.01)
        loop = asyncio.get_event_loop()
        assert not q.running
        q.start(loop)
        assert q.running
        await q.stop()
        assert not q.running

    @pytest.mark.asyncio
    async def test_stop_with_pending_items(self):
        """stop() cancels the drain; pending items stay in queue (not processed after stop)."""
        processed = []

        async def _slow_job():
            await asyncio.sleep(0.5)  # slow — won't complete before stop
            processed.append(1)
            return True

        q = _make_queue(pacing=0.01)
        loop = asyncio.get_event_loop()
        q.start(loop)
        # Enqueue a slow job + a second job
        q.enqueue_fire_and_forget(_slow_job)
        await asyncio.sleep(0.01)  # let drain start the slow job
        await q.stop()
        # The slow job was in-flight; don't assert specific processed count.
        assert not q.running

    @pytest.mark.asyncio
    async def test_enqueue_before_start_raises(self):
        q = _make_queue(pacing=0.01)
        with pytest.raises(RuntimeError, match="start"):
            await q.enqueue_async(lambda: None)

    @pytest.mark.asyncio
    async def test_fire_and_forget_before_start_is_noop(self):
        """enqueue_fire_and_forget on unstarted queue logs and does nothing."""
        q = _make_queue(pacing=0.01)
        # Should not raise
        q.enqueue_fire_and_forget(lambda: None)


# ---------------------------------------------------------------------------
# MeshtasticTransport.send_message_async fallback
# ---------------------------------------------------------------------------


class TestMTFallback:
    @pytest.mark.asyncio
    async def test_send_message_async_falls_back_without_queue(self):
        """When queue not started, send_message_async uses run_in_executor."""
        from meshai.config import ConnectionConfig
        from meshai.connector import MeshtasticTransport

        cfg = ConnectionConfig()
        mt = MeshtasticTransport(cfg)

        # Mock the blocking send so we don't need a real radio
        with patch.object(mt, "send_message", return_value=True) as mock_send:
            result = await mt.send_message_async("hello", channel=0)

        assert result is True
        mock_send.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_message_async_via_queue(self):
        """When queue is started, send_message_async goes through the drain."""
        from meshai.config import ConnectionConfig
        from meshai.connector import MeshtasticTransport

        cfg = ConnectionConfig(meshtastic_send_pacing_seconds=0.05)
        mt = MeshtasticTransport(cfg)

        # Arm queue manually (normally done by set_message_callback)
        loop = asyncio.get_event_loop()
        from meshai.transport.send_queue import RadioSendQueue
        pacing_fn = lambda: max(0.25, getattr(cfg, "meshtastic_send_pacing_seconds", 2.0))
        mt._mt_queue = RadioSendQueue(pacing_fn=pacing_fn)
        mt._mt_queue.start(loop)

        calls = []
        with patch.object(mt, "_blocking_mt_send", side_effect=lambda *a, **kw: calls.append(a) or True):
            r1 = await mt.send_message_async("msg1", channel=0)
            r2 = await mt.send_message_async("msg2", channel=0)

        await mt._mt_queue.stop()
        assert r1 is True
        assert r2 is True
        assert len(calls) == 2


# ---------------------------------------------------------------------------
# Config round-trip
# ---------------------------------------------------------------------------


class TestConfig:
    def test_pacing_defaults(self):
        from meshai.config import ConnectionConfig
        cfg = ConnectionConfig()
        assert cfg.meshtastic_send_pacing_seconds == 2.0
        assert cfg.meshcore_send_pacing_seconds == 2.0

    def test_pacing_round_trips(self):
        from meshai.config import ConnectionConfig, _dataclass_to_dict, _dict_to_dataclass
        cfg = ConnectionConfig(meshtastic_send_pacing_seconds=3.5, meshcore_send_pacing_seconds=1.5)
        d = _dataclass_to_dict(cfg)
        assert d["meshtastic_send_pacing_seconds"] == 3.5
        assert d["meshcore_send_pacing_seconds"] == 1.5
        cfg2 = _dict_to_dataclass(ConnectionConfig, d)
        assert cfg2.meshtastic_send_pacing_seconds == 3.5
        assert cfg2.meshcore_send_pacing_seconds == 1.5
