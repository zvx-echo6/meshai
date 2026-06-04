"""v0.5.2 — staleness filter, cooldown, dedup, friendly renderer, hydro family.

Spec: docs/v0.5.2-spec-cooldown-and-staleness.md (Sections 1–5).

Eight tests per spec Verification §C plus a couple of guards on
counter increments / stats exposure. We intentionally exercise both the
unit (`compose_mesh_message`) and the integration (dispatcher hands the
composed string into the channel payload).
"""

import asyncio
import time

import pytest

from meshai.config import Config, NotificationRuleConfig
from meshai.notifications.pipeline.dispatcher import Dispatcher
from meshai.notifications.events import make_event
from meshai.notifications.renderers.composer import (
    compose_mesh_message,
    _BYTE_BUDGET,
)


# ---------------------------------------------------------------- helpers


class RecChannel:
    """Channel recorder that captures rule + full payload (including .message)."""

    def __init__(self, rec):
        self.rec = rec

    async def deliver(self, payload, rule):
        self.rec.append({
            "delivery_type": rule.delivery_type,
            "name": rule.name,
            "message": payload.message,
            "category": payload.category,
            "severity": payload.severity,
        })
        return True


def _make_dispatcher(cfg):
    rec: list = []
    d = Dispatcher(cfg, lambda rule, conn: RecChannel(rec), connector=None)
    return d, rec


def _dispatch_one(cfg, event):
    d, rec = _make_dispatcher(cfg)
    asyncio.run(d.dispatch(event))
    return d, rec


def _cfg(toggle_name="weather", **kw):
    """Default config: one toggle enabled with mesh_broadcast on priority."""
    cfg = Config()
    cfg.notifications.rules = []
    t = cfg.notifications.toggles[toggle_name]
    t.enabled = True
    t.min_severity = kw.get("min_severity", "routine")
    t.regions = kw.get("regions", [])
    t.severity_channels = kw.get("severity_channels", {
        "routine": ["mesh_broadcast"],
        "priority": ["mesh_broadcast"],
        "immediate": ["mesh_broadcast"],
    })
    # v0.5.2 fields — tests override per-case as needed
    t.freshness_seconds = kw.get("freshness_seconds", 600)
    t.cooldown_seconds = kw.get("cooldown_seconds", 300)
    return cfg


def _ev(severity="priority", category="weather_warning",
        timestamp=None, region=None, source="nws", title="t", **kw):
    """Build an Event. timestamp=None means "now" via make_event auto-set."""
    extra = dict(kw)
    if timestamp is not None:
        extra["timestamp"] = timestamp
    return make_event(
        source=source, category=category, severity=severity,
        region=region, title=title, **extra,
    )


# ============================================================== Section 1
# Staleness filter

def test_staleness_drops_old_central_event():
    """Spec §1: event with timestamp = now - 7200s must be dropped at entrance."""
    cfg = _cfg(freshness_seconds=600)
    stale = _ev(timestamp=time.time() - 7200)
    d, rec = _dispatch_one(cfg, stale)
    assert rec == [], "stale event must not be dispatched"
    assert d.dispatch_stats()["stale_dropped"] == 1


def test_staleness_passes_fresh_event():
    """Spec §1: a fresh event (now) flows through normally."""
    cfg = _cfg(freshness_seconds=600)
    fresh = _ev(timestamp=time.time())  # 0s old
    d, rec = _dispatch_one(cfg, fresh)
    assert len(rec) == 1
    assert d.dispatch_stats()["stale_dropped"] == 0


def test_staleness_applies_to_immediate_severity():
    """Spec §1 note: stale-immediate also drops — recipient saw it elsewhere."""
    cfg = _cfg(freshness_seconds=600)
    stale_imm = _ev(severity="immediate", timestamp=time.time() - 3600)
    d, rec = _dispatch_one(cfg, stale_imm)
    assert rec == []
    assert d.dispatch_stats()["stale_dropped"] == 1


# ============================================================== Section 2
# Per-toggle cooldown

def test_cooldown_throttles_same_category_region():
    """Spec §2: two events with same (toggle, category, region) within window
    → only the first fires; second is silently throttled."""
    cfg = _cfg(cooldown_seconds=300)
    d, rec = _make_dispatcher(cfg)
    e1 = _ev(region="Magic Valley")
    e2 = _ev(region="Magic Valley")
    asyncio.run(d.dispatch(e1))
    asyncio.run(d.dispatch(e2))
    assert len(rec) == 1, "second event in cooldown window must be dropped"
    assert d.dispatch_stats()["cooldown_dropped"] == 1


def test_cooldown_releases_after_window():
    """Spec §2: cooldown_seconds=0 disables throttling → both fire."""
    cfg = _cfg(cooldown_seconds=0)
    d, rec = _make_dispatcher(cfg)
    # Different event IDs (so dedup doesn't catch us) — vary group_key.
    asyncio.run(d.dispatch(_ev(group_key="a")))
    asyncio.run(d.dispatch(_ev(group_key="b")))
    assert len(rec) == 2, "cooldown_seconds=0 must allow both"


def test_cooldown_different_region_not_throttled():
    """Spec §2: cooldown is keyed on region — different regions don't share."""
    cfg = _cfg(cooldown_seconds=300)
    d, rec = _make_dispatcher(cfg)
    asyncio.run(d.dispatch(_ev(region="Magic Valley", group_key="mv")))
    asyncio.run(d.dispatch(_ev(region="Wood River", group_key="wr")))
    assert len(rec) == 2
    assert d.dispatch_stats()["cooldown_dropped"] == 0


# ============================================================== Section 3
# (source, event.id) dedup

def test_dedup_catches_identical_source_event_id():
    """Spec §3: same (source, id) on consecutive deliveries — second dropped.
    Uses two events constructed with the same identity (no group_key)."""
    cfg = _cfg(cooldown_seconds=0)  # disable cooldown so only dedup can drop
    d, rec = _make_dispatcher(cfg)
    e1 = _ev()
    e2 = _ev()
    # make_event auto-computes the same id for identical source+category+geo
    assert e1.id == e2.id, "preflight: ids must match for this test"
    asyncio.run(d.dispatch(e1))
    asyncio.run(d.dispatch(e2))
    assert len(rec) == 1
    assert d.dispatch_stats()["dedup_dropped"] == 1


def test_dedup_lru_eviction_under_load():
    """Spec §3: bounded LRU at 10k entries — distinct ids don't crash, and
    after 10k+ entries the size stabilizes. We assert just the cap behavior
    using a private constant so we don't churn 10k events in the test."""
    from meshai.notifications.pipeline import dispatcher as disp_mod
    cfg = _cfg(cooldown_seconds=0, freshness_seconds=0)  # disable both guards
    d, rec = _make_dispatcher(cfg)
    cap = disp_mod._DEDUP_LRU_MAX
    # Fire cap + 5 distinct events; the LRU should hold exactly cap.
    for i in range(cap + 5):
        asyncio.run(d.dispatch(_ev(group_key=f"k{i}")))
    assert d.dispatch_stats()["dedup_lru_size"] == cap


# ============================================================== Section 4
# Friendly renderer

def test_renderer_produces_friendly_string():
    """Spec §4: compose_mesh_message yields a string with severity emoji +
    UPPERCASE label + primary identifier + severity word; ≤150 bytes UTF-8."""
    e = make_event(
        source="nws", category="weather_warning", severity="priority",
        title="Red Flag Warning", region="Twin Falls", timestamp=time.time(),
    )
    s = compose_mesh_message(e)
    assert "⚠" in s and "WX" in s
    assert "Red Flag Warning" in s
    assert "priority" in s
    assert len(s.encode("utf-8")) <= _BYTE_BUDGET


def test_renderer_byte_budget_drops_optional_segments():
    """Spec §4: when over budget, optional segments drop FIRST (context, then
    distance, then quant, then region). Required segments (head + primary +
    severity) always survive."""
    big_title = "A" * 200
    e = make_event(
        source="nws", category="fire_proximity", severity="immediate",
        title=big_title, region="Wood River Valley",
        timestamp=time.time(),
        data={
            "acres": 1500,
            "containment_pct": 25,
            "cause": "lightning",
            "distance_km": 8,
            "bearing": "W",
            "anchor": "Hailey",
        },
    )
    s = compose_mesh_message(e)
    assert len(s.encode("utf-8")) <= _BYTE_BUDGET
    # Head + severity word still present:
    assert s.startswith("🔥 FIRE:")
    assert "immediate" in s
    # Lowest-priority optional (context) must have been dropped:
    assert "% contained" not in s
    assert "lightning" not in s


def test_renderer_never_mid_character_truncation():
    """The composer must never emit a UTF-8 byte sequence that splits a
    codepoint. Even with required-only over budget, we drop wholesale or
    shrink by codepoints + ellipsis."""
    # All four-byte emoji glyphs in a row, primary forced super long.
    e = make_event(
        source="nws", category="wildfire_proximity", severity="priority",
        title="🔥" * 200,  # 800 bytes of emoji
        timestamp=time.time(),
    )
    s = compose_mesh_message(e)
    # Must be valid UTF-8 (no UnicodeDecodeError on round-trip).
    s.encode("utf-8").decode("utf-8")
    assert len(s.encode("utf-8")) <= _BYTE_BUDGET


def test_renderer_no_debug_fallback_for_central_prefixed_category():
    """Regression — the prod incident: central.<category> event with empty
    title/summary must NOT yield `[Family] central.category` debug format."""
    e = make_event(
        source="central", category="central.weather_warning",
        severity="priority",
        title="",        # explicitly empty
        timestamp=time.time(),
    )
    s = compose_mesh_message(e)
    assert "central.weather_warning" not in s
    # Must still carry a meaningful label even though category is unrecognized.
    assert any(c.isupper() for c in s)


def test_renderer_message_lands_in_toggle_payload():
    """Integration: composer output must reach the channel as payload.message."""
    cfg = _cfg(cooldown_seconds=0, freshness_seconds=0)
    e = _ev(title="Red Flag Warning", region="Twin Falls")
    _, rec = _dispatch_one(cfg, e)
    assert len(rec) == 1
    msg = rec[0]["message"]
    assert "Red Flag Warning" in msg
    assert "⚠" in msg  # weather_warning emoji
    assert len(msg.encode("utf-8")) <= _BYTE_BUDGET


# ============================================================== Section 5
# Hydro family routing

def test_hydro_event_maps_to_geohazards_toggle():
    """Spec §5: stream_flood_warning + stream_high_water route to the
    canonical Geohazards toggle (`seismic` in VALID_TOGGLES). Weather
    toggle alone must NOT fire on them anymore."""
    cfg = Config()
    cfg.notifications.rules = []
    # Enable BOTH weather and seismic toggles so we can prove routing.
    cfg.notifications.toggles["weather"].enabled = True
    cfg.notifications.toggles["weather"].min_severity = "routine"
    cfg.notifications.toggles["weather"].severity_channels = {
        "routine": ["mesh_broadcast"], "priority": ["mesh_broadcast"],
    }
    cfg.notifications.toggles["weather"].cooldown_seconds = 0
    cfg.notifications.toggles["seismic"].enabled = True
    cfg.notifications.toggles["seismic"].min_severity = "routine"
    cfg.notifications.toggles["seismic"].severity_channels = {
        "routine": ["mesh_broadcast"], "priority": ["mesh_broadcast"],
    }
    cfg.notifications.toggles["seismic"].cooldown_seconds = 0

    e = make_event(
        source="usgs", category="stream_flood_warning", severity="priority",
        title="Snake River nr Twin Falls 12.8 ft", timestamp=time.time(),
    )
    _, rec = _dispatch_one(cfg, e)
    names = {r["name"] for r in rec}
    assert "toggle:seismic" in names, "hydro must route to seismic family"
    assert "toggle:weather" not in names, "hydro must NOT route to weather"


def test_hydro_high_water_also_seismic():
    """Same as above for stream_high_water (the lower-severity sibling)."""
    cfg = Config()
    cfg.notifications.rules = []
    cfg.notifications.toggles["seismic"].enabled = True
    cfg.notifications.toggles["seismic"].min_severity = "routine"
    cfg.notifications.toggles["seismic"].severity_channels = {
        "routine": ["mesh_broadcast"],
    }
    cfg.notifications.toggles["seismic"].cooldown_seconds = 0
    e = make_event(
        source="usgs", category="stream_high_water", severity="routine",
        title="Snake River 9.8 ft", timestamp=time.time(),
    )
    _, rec = _dispatch_one(cfg, e)
    assert len(rec) == 1 and rec[0]["name"] == "toggle:seismic"


# ============================================================== misc

def test_dispatch_stats_exposes_all_counters():
    """Stats dict shape is part of the v0.5.2 contract for /api/health."""
    cfg = _cfg()
    d, _ = _make_dispatcher(cfg)
    stats = d.dispatch_stats()
    assert set(stats.keys()) == {
        "stale_dropped", "cooldown_dropped", "dedup_dropped",
        "cooldown_keys", "dedup_lru_size",
    }
