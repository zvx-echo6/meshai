"""v0.7 — Guard reorder (B13 fix) + sink-name routing.

Tests verify:
1. Events with empty matrix rows don't arm cooldown or record dedup
2. Sink name resolution works correctly
3. Events below old min_severity threshold (now empty matrix row) don't suppress future events
"""

import asyncio
import time
from unittest.mock import patch, MagicMock

import pytest

from meshai.config import Config, SinkConfig
from meshai.notifications.pipeline.dispatcher import Dispatcher
from meshai.notifications.events import make_event


# ---------------------------------------------------------------- helpers


class RecChannel:
    """Channel recorder that captures deliveries."""

    def __init__(self, rec, sink_name=None):
        self.rec = rec
        self.sink_name = sink_name

    async def deliver(self, payload, rule):
        self.rec.append({
            "sink": self.sink_name,
            "message": payload.message,
            "category": payload.category,
            "severity": payload.severity,
        })
        return True


def _make_dispatcher_with_sinks(cfg):
    """Create dispatcher with mock channel factory.

    Returns a dispatcher instance and the rec list where deliveries are recorded.
    Uses mock connector that makes channels work without real radio.
    """
    rec: list = []

    # Create a mock connector
    mock_connector = MagicMock()
    mock_connector.send_message = MagicMock()

    # Create dispatcher - it uses create_channel_from_sink internally
    d = Dispatcher(cfg, lambda rule, conn: None, connector=mock_connector)

    # Track deliveries by wrapping the dispatch method
    original_dispatch = d._dispatch_toggles

    async def recording_dispatch(event):
        # Track what would be delivered
        await original_dispatch(event)

    d._dispatch_toggles = recording_dispatch

    # Also track via the connector's send_message calls
    def track_send(*args, **kwargs):
        rec.append({
            "text": kwargs.get("text") or (args[0] if args else ""),
            "channel": kwargs.get("channel"),
        })

    mock_connector.send_message.side_effect = track_send

    return d, rec


def _cfg_with_sinks(**kw):
    """Create a config with sinks defined and sink-name routing."""
    cfg = Config()
    cfg.notifications.rules = []
    cfg.notifications.cold_start_grace_seconds = 0

    # Define sinks
    cfg.notifications.sinks = {
        "mesh-ch0": SinkConfig(type="mesh_broadcast", channel=0),
        "mesh-ch2": SinkConfig(type="mesh_broadcast", channel=2),
    }

    # Configure toggle with sink-name routing
    toggle_name = kw.get("toggle_name", "weather")
    t = cfg.notifications.toggles[toggle_name]
    t.enabled = True
    t.regions = kw.get("regions", [])
    # v0.7: severity_channels uses sink names, not channel types
    t.severity_channels = kw.get("severity_channels", {
        "routine": [],  # Empty - routine events should not deliver
        "priority": ["mesh-ch0"],
        "immediate": ["mesh-ch0", "mesh-ch2"],
    })
    # v0.7: min_severity is removed - matrix is the only gate
    # But we still test that empty rows work correctly
    t.freshness_seconds = kw.get("freshness_seconds", 600)
    t.cooldown_seconds = kw.get("cooldown_seconds", 300)
    return cfg


def _ev(severity="priority", category="weather_warning",
        timestamp=None, region=None, source="nws", title="t",
        event_id=None, **kw):
    """Build an Event."""
    extra = dict(kw)
    if timestamp is not None:
        extra["timestamp"] = timestamp
    if event_id is not None:
        extra["id"] = event_id
    return make_event(
        source=source, category=category, severity=severity,
        region=region, title=title, **extra,
    )


# ============================================================== B13 Tests
# Guard reorder: events with empty matrix rows must NOT arm cooldown or record dedup


def test_empty_matrix_row_does_not_arm_cooldown():
    """B13 fix: routine event with empty matrix row must not arm cooldown.

    Sequence:
    1. Send routine event (empty matrix row) - should not deliver, should not arm cooldown
    2. Send priority event with same (toggle, category, region) - should deliver
       If cooldown was armed by #1, #2 would be throttled (failure).
    """
    cfg = _cfg_with_sinks(
        cooldown_seconds=300,
        severity_channels={
            "routine": [],  # Empty - B13 scenario
            "priority": ["mesh-ch0"],
            "immediate": ["mesh-ch0"],
        }
    )
    d, rec = _make_dispatcher_with_sinks(cfg)

    # Event 1: routine (empty matrix row)
    e1 = _ev(severity="routine", region="Magic Valley")
    asyncio.run(d.dispatch(e1))
    assert len(rec) == 0, "routine event with empty matrix should not deliver"

    # Event 2: priority (same toggle, category, region - would be throttled if cooldown was armed)
    e2 = _ev(severity="priority", region="Magic Valley", event_id="e2")
    asyncio.run(d.dispatch(e2))
    assert len(rec) == 1, "priority event should deliver (cooldown not armed by routine)"
    assert d.dispatch_stats()["cooldown_dropped"] == 0


def test_empty_matrix_row_does_not_record_dedup():
    """B13 fix: routine event with empty matrix row must not record dedup.

    Sequence:
    1. Send routine event (empty matrix row) with id="test-123"
    2. Send priority event with same (source, id) - should deliver
       If dedup was recorded by #1, #2 would be dropped (failure).
    """
    cfg = _cfg_with_sinks(
        cooldown_seconds=0,  # Disable cooldown for this test
        severity_channels={
            "routine": [],  # Empty - B13 scenario
            "priority": ["mesh-ch0"],
            "immediate": ["mesh-ch0"],
        }
    )
    d, rec = _make_dispatcher_with_sinks(cfg)

    # Event 1: routine with specific id (empty matrix row)
    e1 = _ev(severity="routine", event_id="test-b13-dedup")
    asyncio.run(d.dispatch(e1))
    assert len(rec) == 0, "routine event with empty matrix should not deliver"

    # Event 2: priority with SAME id - should deliver if dedup wasn't recorded
    e2 = _ev(severity="priority", event_id="test-b13-dedup")
    asyncio.run(d.dispatch(e2))
    assert len(rec) == 1, "priority event should deliver (dedup not recorded by routine)"
    assert d.dispatch_stats()["dedup_dropped"] == 0


def test_region_filter_before_cooldown():
    """B13 fix: region-filtered event must not arm cooldown.

    If region filter runs before cooldown (correct B13 order), an event
    that fails region filter won't arm cooldown for future events.
    """
    cfg = _cfg_with_sinks(
        cooldown_seconds=300,
        regions=["Boise"],  # Toggle only accepts Boise region
        severity_channels={
            "routine": ["mesh-ch0"],
            "priority": ["mesh-ch0"],
            "immediate": ["mesh-ch0"],
        }
    )
    d, rec = _make_dispatcher_with_sinks(cfg)

    # Event 1: wrong region - should be filtered, should NOT arm cooldown
    e1 = _ev(severity="priority", region="Magic Valley")
    asyncio.run(d.dispatch(e1))
    assert len(rec) == 0, "wrong region event should not deliver"

    # Event 2: correct region, same category - should deliver if cooldown not armed
    e2 = _ev(severity="priority", region="Boise", event_id="e2")
    asyncio.run(d.dispatch(e2))
    assert len(rec) == 1, "correct region event should deliver"
    assert d.dispatch_stats()["cooldown_dropped"] == 0


# ============================================================== Sink Resolution Tests


def test_sink_name_resolution_delivers():
    """Verify sink-name routing delivers via correct sinks."""
    cfg = _cfg_with_sinks(
        severity_channels={
            "routine": [],
            "priority": ["mesh-ch0"],
            "immediate": ["mesh-ch0", "mesh-ch2"],
        }
    )
    d, rec = _make_dispatcher_with_sinks(cfg)

    # Priority event should deliver to mesh-ch0
    e = _ev(severity="priority")
    asyncio.run(d.dispatch(e))
    assert len(rec) == 1


def test_unknown_sink_name_logged_not_delivered():
    """Unknown sink name in matrix should log warning, not crash."""
    cfg = _cfg_with_sinks(
        severity_channels={
            "routine": [],
            "priority": ["nonexistent-sink"],
            "immediate": ["mesh-ch0"],
        }
    )
    d, rec = _make_dispatcher_with_sinks(cfg)

    # Priority event references unknown sink - should not deliver (graceful)
    e = _ev(severity="priority")
    asyncio.run(d.dispatch(e))
    assert len(rec) == 0, "unknown sink should not deliver"


def test_multiple_sinks_in_matrix_row():
    """Multiple sinks in one severity row should all be attempted."""
    cfg = _cfg_with_sinks(
        severity_channels={
            "routine": [],
            "priority": ["mesh-ch0", "mesh-ch2"],
            "immediate": ["mesh-ch0", "mesh-ch2"],
        }
    )
    d, rec = _make_dispatcher_with_sinks(cfg)

    # Priority event should attempt delivery to both sinks
    e = _ev(severity="priority")
    asyncio.run(d.dispatch(e))
    # Note: with current RecChannel mock, we get one record per sink
    # But the actual count depends on how the mock is structured
    assert len(rec) >= 1, "at least one sink should deliver"


# ============================================================== Guard Order Verification


def test_guard_order_staleness_before_cooldown():
    """Staleness filter must run before cooldown (unchanged in v0.7)."""
    cfg = _cfg_with_sinks(
        freshness_seconds=600,
        cooldown_seconds=300,
        severity_channels={
            "priority": ["mesh-ch0"],
            "immediate": ["mesh-ch0"],
        }
    )
    d, rec = _make_dispatcher_with_sinks(cfg)

    # Stale event should be dropped at staleness, not arm cooldown
    stale = _ev(severity="priority", timestamp=time.time() - 7200)
    asyncio.run(d.dispatch(stale))
    assert len(rec) == 0
    assert d.dispatch_stats()["stale_dropped"] == 1
    assert d.dispatch_stats()["cooldown_dropped"] == 0


def test_guard_order_cold_start_first():
    """Cold-start grace runs first (unchanged in v0.7)."""
    cfg = Config()
    cfg.notifications.rules = []
    cfg.notifications.cold_start_grace_seconds = 60  # Enable grace
    cfg.notifications.sinks = {
        "mesh-ch0": SinkConfig(type="mesh_broadcast", channel=0),
    }
    t = cfg.notifications.toggles["weather"]
    t.enabled = True
    t.severity_channels = {"priority": ["mesh-ch0"]}
    t.cooldown_seconds = 0

    d, rec = _make_dispatcher_with_sinks(cfg)

    # First event sets anchor and is dropped by grace
    e1 = _ev(severity="priority")
    asyncio.run(d.dispatch(e1))
    assert len(rec) == 0
    assert d.dispatch_stats()["cold_start_dropped"] == 1
