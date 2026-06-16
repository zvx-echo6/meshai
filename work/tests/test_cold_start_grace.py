"""v0.5.8b — cold-start grace + post-broadcast commit hook in Dispatcher.

The grace window suppresses mesh broadcasts for N seconds after the FIRST
event the dispatcher sees through an enabled toggle. The persistence layer
(handler-side) has already run by then, so fires/event_log rows exist;
only the broadcast (and the mesh_broadcasts_out audit + last_broadcast_*
callback) is gated.
"""

import asyncio
import time
from unittest.mock import patch

import pytest

from meshai.config import Config
from meshai.notifications.events import make_event
from meshai.notifications.pipeline.dispatcher import Dispatcher


# ---------- helpers -------------------------------------------------------


class RecChannel:
    def __init__(self, rec):
        self.rec = rec

    async def deliver(self, payload, rule):
        self.rec.append({
            "name": rule.name,
            "message": payload.message,
            "delivery_type": rule.delivery_type,
        })
        return True


def _cfg(*, cold_start_grace_seconds=60, toggle_name="weather"):
    cfg = Config()
    cfg.notifications.rules = []
    cfg.notifications.cold_start_grace_seconds = cold_start_grace_seconds
    t = cfg.notifications.toggles[toggle_name]
    t.enabled = True
    t.min_severity = "routine"
    t.severity_channels = {"routine": ["mesh_broadcast"]}
    # Wide-open v0.5.2 gates so the cold-start gate is the only thing
    # that can drop these events.
    t.freshness_seconds = 0
    t.cooldown_seconds = 0
    return cfg


def _ev(*, source="nws", category="weather_warning",
        severity="routine", title="t", **kw):
    return make_event(
        source=source, category=category, severity=severity,
        title=title, timestamp=time.time(), **kw,
    )


def _make(cfg):
    rec: list = []
    d = Dispatcher(cfg, lambda r, c: RecChannel(rec), connector=None)
    return d, rec


# ---------- (a) first event during grace ----------------------------------


def test_cold_start_grace_drops_first_event_inside_window():
    cfg = _cfg(cold_start_grace_seconds=60)
    d, rec = _make(cfg)

    fake_now = 1_000_000.0
    with patch("meshai.notifications.pipeline.dispatcher.time.time",
                return_value=fake_now):
        asyncio.run(d.dispatch(_ev()))

    assert rec == [], "broadcast must be suppressed inside grace window"
    stats = d.dispatch_stats()
    assert stats["cold_start_dropped"] == 1
    assert stats["cold_start_anchor_at"] == fake_now


# ---------- (b) event arriving 30s into grace -- still dropped -----------


def test_cold_start_grace_drops_event_partway_through_window():
    cfg = _cfg(cold_start_grace_seconds=60)
    d, rec = _make(cfg)

    t0 = 2_000_000.0
    # First event anchors the window.
    with patch("meshai.notifications.pipeline.dispatcher.time.time",
                return_value=t0):
        asyncio.run(d.dispatch(_ev()))
    # 30s in, still inside the 60s window.
    with patch("meshai.notifications.pipeline.dispatcher.time.time",
                return_value=t0 + 30):
        asyncio.run(d.dispatch(_ev()))

    assert rec == []
    assert d.dispatch_stats()["cold_start_dropped"] == 2


# ---------- (c) event 61s after first -- broadcasts ----------------------


def test_cold_start_grace_passes_event_after_window():
    cfg = _cfg(cold_start_grace_seconds=60)
    d, rec = _make(cfg)

    t0 = 3_000_000.0
    with patch("meshai.notifications.pipeline.dispatcher.time.time",
                return_value=t0):
        asyncio.run(d.dispatch(_ev()))      # dropped
    with patch("meshai.notifications.pipeline.dispatcher.time.time",
                return_value=t0 + 61):
        asyncio.run(d.dispatch(_ev()))      # broadcasts

    assert len(rec) == 1
    stats = d.dispatch_stats()
    assert stats["cold_start_dropped"] == 1


# ---------- (d) grace = 0 disables the feature ----------------------------


def test_cold_start_grace_zero_disables_feature():
    cfg = _cfg(cold_start_grace_seconds=0)
    d, rec = _make(cfg)
    asyncio.run(d.dispatch(_ev()))
    assert len(rec) == 1
    stats = d.dispatch_stats()
    assert stats["cold_start_dropped"] == 0
    # Anchor not set when grace disabled (no gate ran).
    assert stats["cold_start_anchor_at"] is None
