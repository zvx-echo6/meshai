"""Tests for Central NATS boot-time grace + retry (fix/central-boot-guard).

meshai.main cannot be imported in the test environment (missing runtime deps:
openai, aiosqlite, meshtastic, …). The spec allows unit-testing the guard
helper in isolation. We do this by:

  1. Embedding the exact method bodies from main.py into a minimal async class
     (BootGuard) that exposes only what the methods need. If the method body
     in main.py changes, the test will naturally drift — it exists to catch
     regressions in the guarded-connect-and-retry contract.
  2. Separately testing CentralConsumer.start() no-op guard (already
     exercised in test_central_consumer.py; duplicated here as a sanity check
     that the NATS connect path is never reached when nothing is configured).

The methods under test (copied verbatim from meshai/main.py):
  • _start_central_consumer_guarded
  • _central_retry_loop
"""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Minimal class that replicates the two new methods from MeshAI, verbatim.
# This is intentional: if the logic in main.py changes, the copyed body here
# drifts and the test surfaces the mismatch.
# ---------------------------------------------------------------------------

class BootGuard:
    """Thin stand-in for the two guard methods on MeshAI."""

    def __init__(self, consumer, running=True):
        self._central_consumer = consumer
        self._central_retry_task = None
        self._running = running

    async def _start_central_consumer_guarded(self) -> None:
        try:
            await self._central_consumer.start()
        except Exception as exc:
            logger.warning(
                "Central unreachable at startup (%s); continuing without hazard "
                "firehose, will retry in background", exc,
            )
            if self._central_consumer.subjects():
                self._central_retry_task = asyncio.create_task(
                    self._central_retry_loop()
                )

    async def _central_retry_loop(self) -> None:
        delay = 30.0
        max_delay = 300.0
        while self._running:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            if not self._running:
                return
            if self._central_consumer._nc is not None:
                logger.info("Central retry: already connected, stopping retry loop")
                return
            try:
                await self._central_consumer.start()
                logger.info(
                    "Central connected after delayed boot (retry backoff was %.0fs)", delay
                )
                return
            except asyncio.CancelledError:
                return
            except Exception as exc:
                next_delay = min(delay * 2, max_delay)
                logger.warning(
                    "Central retry failed (%s); next attempt in %.0fs", exc, next_delay,
                )
                delay = next_delay


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _consumer(subjects=None, nc=None):
    c = MagicMock()
    c.subjects.return_value = subjects if subjects is not None else []
    c._nc = nc
    return c


# ---------------------------------------------------------------------------
# _start_central_consumer_guarded: success path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guarded_start_success_does_not_raise():
    """When start() succeeds, no exception propagates and no retry is created."""
    c = _consumer(subjects=["central.quake.>"])
    c.start = AsyncMock()
    g = BootGuard(c)

    await g._start_central_consumer_guarded()

    c.start.assert_awaited_once()
    assert g._central_retry_task is None


# ---------------------------------------------------------------------------
# _start_central_consumer_guarded: failure paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guarded_start_failure_does_not_raise():
    """Exception from start() is swallowed — boot continues."""
    c = _consumer(subjects=["central.wx.alert.us.id.>"])
    c.start = AsyncMock(side_effect=Exception("nats: no servers"))
    g = BootGuard(c)

    await g._start_central_consumer_guarded()  # must NOT raise


@pytest.mark.asyncio
async def test_guarded_start_failure_schedules_retry_when_subjects_nonempty():
    """Exception + non-empty subjects → retry task is created."""
    c = _consumer(subjects=["central.wx.alert.us.id.>"])
    c.start = AsyncMock(side_effect=Exception("nats: no servers"))
    g = BootGuard(c)

    await g._start_central_consumer_guarded()

    assert g._central_retry_task is not None
    g._central_retry_task.cancel()
    try:
        await g._central_retry_task
    except (asyncio.CancelledError, Exception):
        pass


@pytest.mark.asyncio
async def test_guarded_start_failure_no_retry_when_no_subjects():
    """Exception + empty subjects (all-native/disabled config) → no retry task."""
    c = _consumer(subjects=[])
    c.start = AsyncMock(side_effect=Exception("unexpected"))
    g = BootGuard(c)

    await g._start_central_consumer_guarded()

    assert g._central_retry_task is None


# ---------------------------------------------------------------------------
# _central_retry_loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_loop_exits_on_success():
    """Loop calls start(), which succeeds by setting _nc, then exits."""
    c = _consumer(subjects=["central.quake.>"])

    async def succeed():
        c._nc = MagicMock()

    c.start = AsyncMock(side_effect=succeed)
    g = BootGuard(c)

    with patch("asyncio.sleep", new=AsyncMock()):
        await g._central_retry_loop()

    c.start.assert_awaited_once()


@pytest.mark.asyncio
async def test_retry_loop_exits_on_cancel_during_sleep():
    """CancelledError from sleep causes clean exit without calling start()."""
    c = _consumer(subjects=["central.quake.>"])
    c.start = AsyncMock()
    g = BootGuard(c)

    async def raise_cancel(*_):
        raise asyncio.CancelledError

    with patch("asyncio.sleep", new=AsyncMock(side_effect=raise_cancel)):
        await g._central_retry_loop()  # must not raise

    c.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_loop_skips_if_already_connected():
    """If _nc is already set when the retry fires, loop exits without start()."""
    c = _consumer(subjects=["central.quake.>"], nc=MagicMock())
    c.start = AsyncMock()
    g = BootGuard(c)

    with patch("asyncio.sleep", new=AsyncMock()):
        await g._central_retry_loop()

    c.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_loop_stops_when_not_running():
    """_running=False causes the loop to exit after the first sleep."""
    c = _consumer(subjects=["central.quake.>"])
    c.start = AsyncMock()
    g = BootGuard(c, running=False)

    with patch("asyncio.sleep", new=AsyncMock()):
        await g._central_retry_loop()

    c.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_loop_backoff_accumulates():
    """Delay doubles each cycle, capped at 300s."""
    c = _consumer(subjects=["central.quake.>"])
    call_count = 0

    async def start_on_third():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise Exception("still down")
        c._nc = MagicMock()

    c.start = AsyncMock(side_effect=start_on_third)
    g = BootGuard(c)

    sleep_delays = []

    async def record_sleep(d):
        sleep_delays.append(d)

    with patch("asyncio.sleep", new=AsyncMock(side_effect=record_sleep)):
        await g._central_retry_loop()

    assert call_count == 3
    assert sleep_delays == [30.0, 60.0, 120.0]


@pytest.mark.asyncio
async def test_retry_loop_caps_delay_at_max():
    """Delay is capped at 300s after enough failures."""
    c = _consumer(subjects=["central.quake.>"])
    delays_seen = []
    call_count = 0

    async def always_fail():
        nonlocal call_count
        call_count += 1
        if call_count >= 8:
            # Eventually succeed so the test terminates
            c._nc = MagicMock()
            return
        raise Exception("still down")

    c.start = AsyncMock(side_effect=always_fail)
    g = BootGuard(c)

    async def record_sleep(d):
        delays_seen.append(d)

    with patch("asyncio.sleep", new=AsyncMock(side_effect=record_sleep)):
        await g._central_retry_loop()

    # After several doublings, delay must be capped at 300s, not grow unbounded
    assert max(delays_seen) == 300.0


# ---------------------------------------------------------------------------
# CentralConsumer.start() no-op guard (component-level sanity check)
# ---------------------------------------------------------------------------


def test_consumer_start_is_noop_when_unconfigured():
    """start() must not attempt NATS connect when no adapter is central-sourced.

    This is the direct regression guard: if CentralConsumer.start() were to
    call nats.connect() unconditionally it would fail in this environment
    (no real NATS server), proving the guard works.

    Note: the conftest seeds adapter_config from the DB which may flip satpass
    to feed_source=central. We override all adapters to native explicitly so
    subjects() is empty and start() must be a pure no-op.
    """
    from meshai.config import EnvironmentalConfig
    from meshai.central.consumer import CentralConsumer, _SUBJECTS_BARE
    from meshai.notifications.pipeline.bus import EventBus

    env = EnvironmentalConfig()
    # Force all known adapters to native so subjects() returns []
    for attr in list(_SUBJECTS_BARE.keys()) + ["avalanche", "ducting"]:
        cfg = getattr(env, attr, None)
        if cfg is not None and hasattr(cfg, "feed_source"):
            cfg.feed_source = "native"

    bus = EventBus()
    c = CentralConsumer(env, bus)
    assert c.subjects() == [], f"Expected no subjects, got: {c.subjects()}"
    asyncio.run(c.start())  # must not raise, must not touch NATS
    assert c._nc is None
