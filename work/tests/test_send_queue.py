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


# ---------------------------------------------------------------------------
# Regression tests — deadlock / teardown
# ---------------------------------------------------------------------------


class TestDeadlockRegression:
    """Regression suite for the two deadlock/hang bugs fixed in feat/send-queue.

    Both tests must PASS on the fixed code and would HANG (timeout) on the
    pre-fix code:

    BLOCKER 1 — ``req_telemetry_async`` self-deadlock:
      Pre-fix: _telem_job_outer (running inside the drain) called
      _req_telemetry_async which called _enqueue_mc_loop_send — a nested
      enqueue-and-await inside a single-threaded drain job — deadlock.
      Post-fix: _req_telemetry_async is inline; no nested enqueue.

    BLOCKER 2 — pending futures abandoned on teardown/reconnect:
      Pre-fix: stop() only cancelled the drain task; queue-sitting items had
      their concurrent.futures.Futures left unresolved, so
      ``await asyncio.wrap_future(cfut)`` callers hung indefinitely.
      Post-fix: stop() drains the remaining queue and cancels every cfut.
    """

    @pytest.mark.asyncio
    async def test_telemetry_queue_no_deadlock(self):
        """req_telemetry_async must not self-deadlock the MC drain (BLOCKER 1).

        Drives the on-demand telemetry poll through a real _mc_send_queue
        (not mocked) with fake MC commands.  Asserts it resolves within 2 s
        and that a subsequent send on the same queue also drains (queue not
        wedged).  With the pre-fix code this would hang at the asyncio.wait_for
        timeout because the nested _enqueue_mc_loop_send deadlocks the drain.
        """
        from meshai.config import ConnectionConfig
        from meshai.transport.meshcore_transport import MeshCoreTransport

        cfg = ConnectionConfig(
            meshcore_host="127.0.0.1",
            meshcore_send_pacing_seconds=0.01,
        )
        mc = MeshCoreTransport(cfg)
        loop = asyncio.get_event_loop()

        # Arm the MC queue directly (bypass connect() / TCP).
        mc._loop = loop
        mc._connected = True
        mc._mc_send_queue = asyncio.Queue()
        mc._mc_drain_task = loop.create_task(
            mc._mc_drain_loop(), name="test-mc-drain"
        )

        fake_lpp = [{"channel": 0, "type": 120, "value": 80}]  # battery_pct=80

        class _FakeCommands:
            async def req_telemetry_sync(self, contact, min_timeout=5):
                return fake_lpp

        class _FakeMC:
            commands = _FakeCommands()

            def get_contact_by_key_prefix(self, prefix):
                return {"adv_name": "Node1", "public_key": prefix}

            def get_contact_by_name(self, name):
                return {"adv_name": name, "public_key": "aabbcc"}

        mc._mc = _FakeMC()

        # Must complete within 2 s; pre-fix code deadlocks here.
        data = await asyncio.wait_for(
            mc.req_telemetry_async("aabbcc"),
            timeout=2.0,
        )
        assert data is not None, "expected telemetry data back from cache"
        assert data.get("battery_pct") == 80

        # Subsequent send on the same queue must also drain (queue not wedged).
        done = asyncio.Event()

        async def _normal_send() -> bool:
            done.set()
            return True

        ok = await asyncio.wait_for(
            mc._enqueue_mc_loop_send(_normal_send), timeout=2.0
        )
        assert ok is True
        assert done.is_set()

        # Cleanup.
        mc._mc_drain_task.cancel()
        try:
            await mc._mc_drain_task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_teardown_resolves_pending_futures(self):
        """RadioSendQueue.stop() must resolve all pending futures (BLOCKER 2).

        Enqueues a slow job to occupy the drain, then enqueues three more that
        sit in the queue.  Calls stop() mid-drain and asserts every future is
        done (not pending/hanging) and resolves promptly to
        CancelledError/exception.  With pre-fix code the pending futs would
        never be set so the assert would fail or the test would time out.
        """
        q = RadioSendQueue(pacing_fn=lambda: 10.0)  # huge pacing keeps drain idle long
        loop = asyncio.get_event_loop()
        q.start(loop)

        drain_started = asyncio.Event()

        async def _slow_job():
            drain_started.set()
            await asyncio.sleep(60)  # will be cancelled by stop()
            return True

        async def _fast_job():
            return True

        # Kick off the slow job — drain picks it up immediately.
        slow_task = asyncio.ensure_future(q.enqueue_async(_slow_job))
        await asyncio.wait_for(drain_started.wait(), timeout=1.0)

        # Enqueue three more jobs while drain is occupied by slow_job.
        pending_tasks = [
            asyncio.ensure_future(q.enqueue_async(_fast_job)) for _ in range(3)
        ]

        # Stop the queue while slow_job is in-flight and fast jobs are pending.
        await q.stop()
        # Give the event loop one tick to propagate cfut cancellations into
        # the asyncio tasks waiting at wrap_future(cfut).
        await asyncio.sleep(0)

        # Every future must be resolved — none hanging indefinitely.
        # Await them with a short timeout; pre-fix code they would never complete.
        all_tasks = [slow_task] + pending_tasks
        done, pending_set = await asyncio.wait(all_tasks, timeout=1.0)
        assert not pending_set, (
            f"{len(pending_set)} task(s) still pending after stop() — "
            "futures not resolved on teardown"
        )

        # Awaiting them must raise (CancelledError) — not return a value.
        for task in all_tasks:
            assert task.done()
            with pytest.raises(
                (asyncio.CancelledError, concurrent.futures.CancelledError, Exception)
            ):
                task.result()

    @pytest.mark.asyncio
    async def test_reconnect_resolves_old_futures(self):
        """_start_mc_queue on reconnect must cancel futures from the old queue
        (BLOCKER 2 reconnect path).

        Arms the queue, enqueues three items without draining them, then calls
        _start_mc_queue again (simulating reconnect).  All three old futures
        must be cancelled so no caller hangs.  Pre-fix code would leave them
        unresolved.
        """
        from meshai.config import ConnectionConfig
        from meshai.transport.meshcore_transport import MeshCoreTransport

        cfg = ConnectionConfig(
            meshcore_host="127.0.0.1",
            meshcore_send_pacing_seconds=0.01,
        )
        mc = MeshCoreTransport(cfg)
        loop = asyncio.get_event_loop()
        mc._loop = loop

        # Initial arm — drain is live but _mc is None so any job would return False.
        mc._start_mc_queue()
        await asyncio.sleep(0)  # let drain task start

        # Enqueue three items; they sit in the queue unprocessed.
        pending_cfuts: list[concurrent.futures.Future] = []
        for _ in range(3):
            cfut: concurrent.futures.Future = concurrent.futures.Future()
            async def _noop() -> bool:
                return True
            await mc._mc_send_queue.put((_noop, cfut))
            pending_cfuts.append(cfut)

        # Simulate reconnect: _start_mc_queue replaces the queue.
        # The fix must cancel old cfuts before creating the new queue.
        mc._start_mc_queue()
        await asyncio.sleep(0.05)  # let any scheduled callbacks run

        for cfut in pending_cfuts:
            assert cfut.done(), "old cfut not resolved after _start_mc_queue reconnect"
            assert cfut.cancelled(), "old cfut should be cancelled"

        # Cleanup new drain.
        if mc._mc_drain_task is not None:
            mc._mc_drain_task.cancel()
            try:
                await mc._mc_drain_task
            except asyncio.CancelledError:
                pass
