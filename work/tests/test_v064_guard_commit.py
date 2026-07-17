"""v0.6-4 — B13 guard-commit ordering + dedup suffix.

Regression tests for two silent-suppression defects:

1. B13 ordering: cooldown arming and dedup recording used to happen
   BEFORE the region filter / severity floor / matrix resolution and
   BEFORE delivery. A non-deliverable or failed event therefore burned
   guard state and suppressed later deliverable events for up to the
   dedup retention window. Now both commit only after >=1 successful
   delivery.

2. Dedup suffix: feeds like WFIGS publish the SAME upstream id
   (IrwinID) on every sweep, so the bare (source, id) dedup key
   permanently swallowed every post-"New" lifecycle broadcast. The
   handler now stamps the broadcast-justifying state into
   event.data["_dedup_suffix"]; unchanged repeats dedup, updates pass.
"""

import asyncio

import pytest

from meshai.config import Config
from meshai.notifications.pipeline.dispatcher import Dispatcher
from meshai.notifications.events import make_event


class RecChannel:
    def __init__(self, rec, succeed=True):
        self.rec = rec
        self.succeed = succeed

    async def deliver(self, payload, rule):
        self.rec.append({
            "delivery_type": rule.delivery_type,
            "message": payload.message,
        })
        return self.succeed


def _cfg(**kw):
    cfg = Config()
    cfg.notifications.rules = []
    cfg.notifications.cold_start_grace_seconds = 0
    t = cfg.notifications.toggles["weather"]
    t.enabled = True
    t.min_severity = kw.get("min_severity", "routine")
    t.regions = kw.get("regions", [])
    t.severity_channels = kw.get("severity_channels", {
        "routine": ["mesh_broadcast"],
        "priority": ["mesh_broadcast"],
        "immediate": ["mesh_broadcast"],
    })
    t.freshness_seconds = 600
    t.cooldown_seconds = kw.get("cooldown_seconds", 300)
    t.broadcast_channel = 1
    return cfg


def _ev(eid="ev-1", severity="priority", **data):
    ev = make_event(source="nws", category="weather.alert.severe",
                    severity=severity, title="t")
    ev.id = eid
    if data:
        ev.data.update(data)
    return ev


def _disp(cfg, succeed=True):
    rec: list = []
    d = Dispatcher(cfg, lambda rule, conn: RecChannel(rec, succeed), connector=None)
    return d, rec


# ---------------------------------------------------------------- B13


def test_empty_matrix_row_burns_no_guard_state():
    """Event whose severity row is empty must leave no cooldown/dedup."""
    cfg = _cfg(severity_channels={"routine": [], "priority": [],
                                   "immediate": ["mesh_broadcast"]})
    d, rec = _disp(cfg)
    asyncio.run(d.dispatch(_ev(eid="fire-x", severity="priority")))
    assert rec == []
    assert len(d._toggle_cooldown) == 0
    assert len(d._dedup_lru) == 0
    # Operator fixes the matrix -> the SAME event id must now deliver.
    cfg.notifications.toggles["weather"].severity_channels["priority"] = \
        ["mesh_broadcast"]
    asyncio.run(d.dispatch(_ev(eid="fire-x", severity="priority")))
    assert len(rec) == 1


def test_below_severity_floor_burns_no_guard_state():
    cfg = _cfg(min_severity="immediate")
    d, rec = _disp(cfg)
    asyncio.run(d.dispatch(_ev(eid="ev-floor", severity="priority")))
    assert rec == [] and not d._toggle_cooldown and not d._dedup_lru


def test_wrong_region_burns_no_guard_state():
    cfg = _cfg(regions=["US-ID"])
    ev = _ev(eid="ev-region", severity="priority")
    ev.region = "US-MT"
    d, rec = _disp(cfg)
    asyncio.run(d.dispatch(ev))
    assert rec == [] and not d._toggle_cooldown and not d._dedup_lru


def test_failed_delivery_leaves_no_dedup_and_retries():
    """All-channels-failed delivery must not dedup-record: the next
    sweep's redelivery of the same id must reach the channel again."""
    cfg = _cfg()
    d, rec = _disp(cfg, succeed=False)
    asyncio.run(d.dispatch(_ev(eid="retry-me", severity="priority")))
    assert len(rec) == 1            # attempted
    assert not d._dedup_lru         # but not recorded
    assert not d._toggle_cooldown   # and no cooldown armed
    # Channel recovers -> same event id delivers.
    d._channel_factory = lambda rule, conn: RecChannel(rec, succeed=True)
    asyncio.run(d.dispatch(_ev(eid="retry-me", severity="priority")))
    assert len(rec) == 2
    assert d._dedup_lru             # recorded only on success


def test_successful_delivery_commits_cooldown_and_dedup():
    cfg = _cfg()
    d, rec = _disp(cfg)
    asyncio.run(d.dispatch(_ev(eid="ok-1", severity="priority")))
    assert len(rec) == 1
    assert len(d._toggle_cooldown) == 1
    assert ("nws", "ok-1") in d._dedup_lru
    # Exact repeat is throttled (cooldown fires first at this severity).
    asyncio.run(d.dispatch(_ev(eid="ok-1", severity="priority")))
    assert len(rec) == 1
    assert d._cooldown_dropped == 1
    # With cooldown out of the way, the repeat is dedup-dropped.
    cfg.notifications.toggles["weather"].cooldown_seconds = 0
    asyncio.run(d.dispatch(_ev(eid="ok-1", severity="priority")))
    assert len(rec) == 1
    assert d._dedup_dropped == 1


# ---------------------------------------------------------------- suffix


def test_dedup_suffix_lets_updates_pass_and_repeats_dedup():
    """Same upstream id: identical suffix dedups, changed suffix passes —
    the WFIGS lifecycle-update fix."""
    cfg = _cfg(cooldown_seconds=0)
    d, rec = _disp(cfg)
    # "New" broadcast: 0.1 acres.
    asyncio.run(d.dispatch(
        _ev(eid="{IRWIN-1}", severity="immediate", _dedup_suffix="0.1|0")))
    assert len(rec) == 1
    # Feed re-sweeps, nothing changed: same id + same suffix -> dedup.
    asyncio.run(d.dispatch(
        _ev(eid="{IRWIN-1}", severity="immediate", _dedup_suffix="0.1|0")))
    assert len(rec) == 1
    assert d._dedup_dropped == 1
    # The fire blows up: same id, NEW suffix -> must broadcast.
    asyncio.run(d.dispatch(
        _ev(eid="{IRWIN-1}", severity="immediate", _dedup_suffix="9400.0|10")))
    assert len(rec) == 2
    # And that update's repeat dedups too.
    asyncio.run(d.dispatch(
        _ev(eid="{IRWIN-1}", severity="immediate", _dedup_suffix="9400.0|10")))
    assert len(rec) == 2


def test_wfigs_handler_stamps_dedup_suffix():
    from meshai.env.fire_render import _attach_commit_handles
    data = {}
    _attach_commit_handles(data, irwin_id="{X}", acres=42.0,
                            contained_pct=15, event_log_row_id=None)
    assert data["_dedup_suffix"] == "42.0|15"
    assert data["_cooldown_suffix"] == "{X}"
