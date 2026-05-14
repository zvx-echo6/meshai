"""Tests for DigestScheduler (Phase 2.3b).

Uses asyncio.run() since pytest-asyncio is not available in the container.
"""

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional
from unittest.mock import MagicMock, call

import pytest

from meshai.notifications.events import make_event
from meshai.notifications.pipeline.digest import DigestAccumulator
from meshai.notifications.pipeline.scheduler import DigestScheduler


# ---- Test Fixtures ----

@dataclass
class MockRule:
    """Mock notification rule for testing."""
    name: str = "test-rule"
    enabled: bool = True
    trigger_type: str = "schedule"
    schedule_match: str = "digest"
    delivery_type: str = "mesh_broadcast"
    broadcast_channel: int = 0


@dataclass
class MockDigestConfig:
    """Mock digest config."""
    schedule: str = "07:00"
    include: list = field(default_factory=list)


@dataclass
class MockNotificationsConfig:
    """Mock notifications config."""
    enabled: bool = True
    digest: MockDigestConfig = field(default_factory=MockDigestConfig)
    rules: list = field(default_factory=list)


@dataclass
class MockConfig:
    """Mock config for scheduler tests."""
    notifications: MockNotificationsConfig = field(default_factory=MockNotificationsConfig)


class MockChannel:
    """Mock channel that records deliveries."""

    def __init__(self):
        self.deliveries = []

    def deliver(self, payload: dict):
        self.deliveries.append(payload)


def make_scheduler(
    schedule: str = "07:00",
    rules: Optional[list] = None,
    clock: Optional[callable] = None,
    sleep: Optional[callable] = None,
    accumulator: Optional[DigestAccumulator] = None,
) -> tuple[DigestScheduler, MockConfig, dict]:
    """Factory for creating test schedulers.

    Returns (scheduler, config, channels_by_rule_name).
    """
    if rules is None:
        rules = [MockRule()]

    config = MockConfig(
        notifications=MockNotificationsConfig(
            digest=MockDigestConfig(schedule=schedule),
            rules=rules,
        )
    )

    channels = {}

    def channel_factory(rule):
        ch = MockChannel()
        channels[rule.name] = ch
        return ch

    if accumulator is None:
        accumulator = DigestAccumulator()

    scheduler = DigestScheduler(
        accumulator=accumulator,
        config=config,
        channel_factory=channel_factory,
        clock=clock,
        sleep=sleep,
    )

    return scheduler, config, channels


# ---- Schedule Computation Tests ----

class TestScheduleComputation:
    """Tests for _next_fire_at and _parse_schedule."""

    def test_parse_schedule_valid(self):
        """Valid HH:MM parses correctly."""
        scheduler, _, _ = make_scheduler()
        assert scheduler._parse_schedule("07:00") == (7, 0)
        assert scheduler._parse_schedule("23:59") == (23, 59)
        assert scheduler._parse_schedule("00:00") == (0, 0)
        assert scheduler._parse_schedule("12:30") == (12, 30)

    def test_parse_schedule_with_whitespace(self):
        """Whitespace is stripped."""
        scheduler, _, _ = make_scheduler()
        assert scheduler._parse_schedule("  07:00  ") == (7, 0)

    def test_parse_schedule_invalid_falls_back(self):
        """Invalid schedules fall back to 07:00."""
        scheduler, _, _ = make_scheduler()
        # Bad format
        assert scheduler._parse_schedule("7:00:00") == (7, 0)
        assert scheduler._parse_schedule("invalid") == (7, 0)
        assert scheduler._parse_schedule("") == (7, 0)
        # Out of range
        assert scheduler._parse_schedule("25:00") == (7, 0)
        assert scheduler._parse_schedule("12:60") == (7, 0)

    def test_next_fire_at_future_today(self):
        """If schedule time is later today, returns today's timestamp."""
        # Set clock to 06:00 on a known date
        base_dt = datetime(2024, 6, 15, 6, 0, 0)
        base_ts = base_dt.timestamp()

        scheduler, _, _ = make_scheduler(schedule="07:00", clock=lambda: base_ts)
        next_fire = scheduler._next_fire_at(base_ts)

        # Should be 07:00 same day
        expected_dt = datetime(2024, 6, 15, 7, 0, 0)
        assert abs(next_fire - expected_dt.timestamp()) < 1

    def test_next_fire_at_past_today_schedules_tomorrow(self):
        """If schedule time has passed today, returns tomorrow's timestamp."""
        # Set clock to 08:00 on a known date
        base_dt = datetime(2024, 6, 15, 8, 0, 0)
        base_ts = base_dt.timestamp()

        scheduler, _, _ = make_scheduler(schedule="07:00", clock=lambda: base_ts)
        next_fire = scheduler._next_fire_at(base_ts)

        # Should be 07:00 next day
        expected_dt = datetime(2024, 6, 16, 7, 0, 0)
        assert abs(next_fire - expected_dt.timestamp()) < 1

    def test_next_fire_at_exact_time_schedules_tomorrow(self):
        """If clock is exactly at schedule time, schedules tomorrow."""
        base_dt = datetime(2024, 6, 15, 7, 0, 0)
        base_ts = base_dt.timestamp()

        scheduler, _, _ = make_scheduler(schedule="07:00", clock=lambda: base_ts)
        next_fire = scheduler._next_fire_at(base_ts)

        # Should be 07:00 next day
        expected_dt = datetime(2024, 6, 16, 7, 0, 0)
        assert abs(next_fire - expected_dt.timestamp()) < 1

    def test_schedule_str_reads_from_config(self):
        """_schedule_str reads from config.notifications.digest.schedule."""
        scheduler, _, _ = make_scheduler(schedule="19:30")
        assert scheduler._schedule_str() == "19:30"

    def test_schedule_str_defaults_to_0700(self):
        """Missing digest config defaults to 07:00."""
        config = MockConfig()
        config.notifications.digest = None

        scheduler = DigestScheduler(
            accumulator=DigestAccumulator(),
            config=config,
            channel_factory=lambda r: MockChannel(),
        )
        assert scheduler._schedule_str() == "07:00"


# ---- Fire Behavior Tests ----

class TestFireBehavior:
    """Tests for _fire() digest delivery."""

    def test_fire_delivers_to_matching_rule(self):
        """_fire() delivers digest to rules with schedule_match='digest'."""
        accumulator = DigestAccumulator()
        # Add an event so digest has content
        accumulator.enqueue(make_event(
            source="test",
            category="weather_warning",
            severity="priority",
            title="Test Alert",
            summary="Test alert summary",
        ))

        scheduler, _, channels = make_scheduler(
            rules=[MockRule(name="digest-mesh")],
            accumulator=accumulator,
        )

        now = time.time()

        async def run_fire():
            await scheduler._fire(now)

        asyncio.run(run_fire())

        assert "digest-mesh" in channels
        ch = channels["digest-mesh"]
        assert len(ch.deliveries) == 1
        payload = ch.deliveries[0]
        assert payload["category"] == "digest"
        assert payload["severity"] == "routine"
        assert "Test alert" in payload["message"] or "Weather" in payload["message"]

    def test_fire_skips_disabled_rules(self):
        """Disabled rules are not delivered to."""
        scheduler, _, channels = make_scheduler(
            rules=[MockRule(name="disabled", enabled=False)],
        )

        async def run_fire():
            await scheduler._fire(time.time())

        asyncio.run(run_fire())

        # Channel should not be created for disabled rule
        assert "disabled" not in channels

    def test_fire_skips_non_schedule_rules(self):
        """Rules with trigger_type != 'schedule' are skipped."""
        rule = MockRule(name="condition-rule", trigger_type="condition")
        scheduler, _, channels = make_scheduler(rules=[rule])

        async def run_fire():
            await scheduler._fire(time.time())

        asyncio.run(run_fire())

        assert "condition-rule" not in channels

    def test_fire_skips_non_digest_schedule_rules(self):
        """Schedule rules with schedule_match != 'digest' are skipped."""
        rule = MockRule(name="other-schedule", schedule_match="daily_report")
        scheduler, _, channels = make_scheduler(rules=[rule])

        async def run_fire():
            await scheduler._fire(time.time())

        asyncio.run(run_fire())

        assert "other-schedule" not in channels

    def test_fire_mesh_delivery_chunks(self):
        """Mesh delivery types get per-chunk delivery."""
        accumulator = DigestAccumulator(mesh_char_limit=100)
        # Add multiple events to force chunking
        for i in range(5):
            accumulator.enqueue(make_event(
                source="test",
                category="weather_warning",
                severity="priority",
                title=f"Alert {i}",
                summary=f"Weather alert number {i} with enough text to use space",
            ))

        scheduler, _, channels = make_scheduler(
            rules=[MockRule(name="mesh", delivery_type="mesh_broadcast")],
            accumulator=accumulator,
        )

        now = time.time()

        async def run_fire():
            await scheduler._fire(now)

        asyncio.run(run_fire())

        ch = channels["mesh"]
        # Should have multiple deliveries (one per chunk)
        assert len(ch.deliveries) >= 1
        # Check chunk metadata
        for payload in ch.deliveries:
            assert "chunk_index" in payload
            assert "chunk_total" in payload

    def test_fire_email_delivery_full_text(self):
        """Email delivery type gets single full-text delivery."""
        accumulator = DigestAccumulator()
        accumulator.enqueue(make_event(
            source="test",
            category="weather_warning",
            severity="priority",
            title="Test Alert",
            summary="Test alert summary",
        ))

        scheduler, _, channels = make_scheduler(
            rules=[MockRule(name="email", delivery_type="email")],
            accumulator=accumulator,
        )

        async def run_fire():
            await scheduler._fire(time.time())

        asyncio.run(run_fire())

        ch = channels["email"]
        assert len(ch.deliveries) == 1
        payload = ch.deliveries[0]
        assert "chunk_index" not in payload
        assert "--- " in payload["message"]  # Full format has header

    def test_fire_updates_last_fire_at(self):
        """_fire() updates last_fire_at timestamp."""
        scheduler, _, _ = make_scheduler()
        assert scheduler.last_fire_at() == 0.0

        now = time.time()

        async def run_fire():
            await scheduler._fire(now)

        asyncio.run(run_fire())

        assert scheduler.last_fire_at() == now

    def test_fire_empty_digest_still_delivers(self):
        """Empty digest is still delivered (with 'no alerts' message)."""
        scheduler, _, channels = make_scheduler(
            rules=[MockRule(name="mesh")],
        )

        async def run_fire():
            await scheduler._fire(time.time())

        asyncio.run(run_fire())

        ch = channels["mesh"]
        assert len(ch.deliveries) == 1
        assert "No alerts" in ch.deliveries[0]["message"]


# ---- Lifecycle Tests ----

class TestLifecycle:
    """Tests for start/stop lifecycle."""

    def test_start_creates_task(self):
        """start() creates and runs an asyncio task."""
        scheduler, _, _ = make_scheduler()

        async def run_start():
            await scheduler.start()
            assert scheduler._task is not None
            assert not scheduler._task.done()
            await scheduler.stop()

        asyncio.run(run_start())

    def test_start_twice_raises(self):
        """Starting twice raises RuntimeError."""
        scheduler, _, _ = make_scheduler()

        async def run_double_start():
            await scheduler.start()
            try:
                with pytest.raises(RuntimeError, match="already running"):
                    await scheduler.start()
            finally:
                await scheduler.stop()

        asyncio.run(run_double_start())

    def test_stop_cancels_task(self):
        """stop() cancels the running task."""
        scheduler, _, _ = make_scheduler()

        async def run_stop():
            await scheduler.start()
            task = scheduler._task
            await scheduler.stop()
            assert scheduler._task is None
            assert task.done()

        asyncio.run(run_stop())

    def test_stop_idempotent(self):
        """stop() on non-running scheduler is safe."""
        scheduler, _, _ = make_scheduler()

        async def run_stop():
            # Never started
            await scheduler.stop()
            # Should not raise

        asyncio.run(run_stop())

    def test_stop_event_interrupts_sleep(self):
        """stop() interrupts the sleep and exits cleanly."""
        sleep_calls = []

        async def fake_sleep(duration):
            sleep_calls.append(duration)
            # Actually sleep briefly so we can cancel
            await asyncio.sleep(0.01)

        # Set clock far from schedule time to get long sleep
        base_dt = datetime(2024, 6, 15, 8, 0, 0)
        scheduler, _, _ = make_scheduler(
            schedule="07:00",
            clock=lambda: base_dt.timestamp(),
            sleep=fake_sleep,
        )

        async def run_test():
            await scheduler.start()
            # Give task time to enter sleep
            await asyncio.sleep(0.05)
            await scheduler.stop()

        asyncio.run(run_test())

        # Task should have exited cleanly


# ---- Integration Tests ----

class TestIntegration:
    """Integration tests with real timing (short intervals)."""

    def test_scheduler_fires_on_schedule(self):
        """Scheduler fires when schedule time arrives."""
        fire_times = []
        accumulator = DigestAccumulator()

        # Start at 06:59:59.95 (50ms before 07:00), delay will be ~50ms
        clock_time = [datetime(2024, 6, 15, 6, 59, 59, 950000).timestamp()]

        def fake_clock():
            return clock_time[0]

        scheduler, _, channels = make_scheduler(
            schedule="07:00",
            clock=fake_clock,
            accumulator=accumulator,
        )

        # Track when fire happens
        original_fire = scheduler._fire

        async def tracking_fire(now):
            fire_times.append(now)
            await original_fire(now)
            # After first fire, advance clock so next cycle has long delay
            clock_time[0] = datetime(2024, 6, 15, 8, 0, 0).timestamp()

        scheduler._fire = tracking_fire

        async def run_test():
            await scheduler.start()
            # Wait for the ~50ms delay plus some buffer
            await asyncio.sleep(0.2)
            await scheduler.stop()

        asyncio.run(run_test())

        # Should have fired once
        assert len(fire_times) >= 1

    def test_scheduler_multiple_rules(self):
        """Scheduler delivers to multiple matching rules."""
        accumulator = DigestAccumulator()
        accumulator.enqueue(make_event(
            source="test",
            category="weather_warning",
            severity="priority",
            title="Test",
            summary="Test summary",
        ))

        rules = [
            MockRule(name="mesh1", delivery_type="mesh_broadcast"),
            MockRule(name="mesh2", delivery_type="mesh_dm"),
            MockRule(name="email", delivery_type="email"),
        ]

        scheduler, _, channels = make_scheduler(
            rules=rules,
            accumulator=accumulator,
        )

        async def run_fire():
            await scheduler._fire(time.time())

        asyncio.run(run_fire())

        # All three should have received deliveries
        assert "mesh1" in channels
        assert "mesh2" in channels
        assert "email" in channels
        assert len(channels["mesh1"].deliveries) >= 1
        assert len(channels["mesh2"].deliveries) >= 1
        assert len(channels["email"].deliveries) == 1

    def test_scheduler_handles_delivery_error(self):
        """Scheduler continues after delivery error."""
        accumulator = DigestAccumulator()
        accumulator.enqueue(make_event(
            source="test",
            category="weather_warning",
            severity="priority",
            title="Test",
            summary="Test",
        ))

        rules = [
            MockRule(name="bad"),
            MockRule(name="good"),
        ]

        call_order = []

        def bad_channel_factory(rule):
            call_order.append(rule.name)
            if rule.name == "bad":
                ch = MagicMock()
                ch.deliver.side_effect = RuntimeError("delivery failed")
                return ch
            return MockChannel()

        scheduler = DigestScheduler(
            accumulator=accumulator,
            config=MockConfig(
                notifications=MockNotificationsConfig(rules=rules)
            ),
            channel_factory=bad_channel_factory,
        )

        async def run_fire():
            await scheduler._fire(time.time())

        asyncio.run(run_fire())

        # Both rules should have been attempted
        assert "bad" in call_order
        assert "good" in call_order


# ---- Matching Rules Tests ----

class TestMatchingRules:
    """Tests for _matching_rules() filter logic."""

    def test_matching_rules_filters_correctly(self):
        """Only enabled schedule rules with schedule_match='digest' match."""
        rules = [
            MockRule(name="good", enabled=True, trigger_type="schedule", schedule_match="digest"),
            MockRule(name="disabled", enabled=False, trigger_type="schedule", schedule_match="digest"),
            MockRule(name="condition", enabled=True, trigger_type="condition", schedule_match="digest"),
            MockRule(name="other-match", enabled=True, trigger_type="schedule", schedule_match="daily"),
            MockRule(name="no-match", enabled=True, trigger_type="schedule", schedule_match=None),
        ]

        scheduler, _, _ = make_scheduler(rules=rules)
        matches = scheduler._matching_rules()

        assert len(matches) == 1
        assert matches[0].name == "good"

    def test_matching_rules_empty_when_no_rules(self):
        """Returns empty list when no rules configured."""
        scheduler, _, _ = make_scheduler(rules=[])
        matches = scheduler._matching_rules()
        assert matches == []
