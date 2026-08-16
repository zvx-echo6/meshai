"""Regression tests for the usgs_quake dispatcher-freshness + region-tagging fix.

Problem (see meshai/notifications/pipeline/dispatcher.py:359-374 and
meshai/env/usgs_quake.py): the usgs_quake adapter polls a rolling PAST-DAY
USGS feed (2.5_day.geojson), so a genuinely first-seen quake is routinely
already 10min-20h old by the time it's detected. The generic per-toggle
freshness_seconds (600s) silently dropped essentially every native quake
before broadcast -- confirmed against production: quake_events had 12
first-sighting rows (decide() ran, magnitude/region gate passed, DB row
inserted) with last_broadcast_at=NULL on every single one (commit() never
reached because the staleness filter dropped the event downstream first).

Fix 1 (dispatcher.py): earthquake_event gets its own adapter_config-backed
freshness override (adapter_config.usgs_quake.freshness_seconds, default
3600s), mirroring the existing wfigs/"fire" override -- but scoped to the
CATEGORY, not the "seismic" family/toggle, because stream_flood_warning /
stream_high_water (hydro) also live under toggle="seismic" and must keep
using the generic per-toggle freshness unchanged.

Fix 2 (env/usgs_quake.py): to_event() no longer passes the adapter's fixed
config region ("magic_valley", which never matches a region_routes cell
key) onto the Event. Event.region is left unset so CoverageFilter's
geometry-based tagger (lat/lon vs named coverage areas) can stamp the real
region name.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import pytest

from meshai.config import Config
from meshai.coverage_area import MonitoringArea
from meshai.env.usgs_quake import USGSQuakeAdapter
from meshai.notifications.events import make_event
from meshai.notifications.pipeline.coverage_filter import CoverageFilter
from meshai.notifications.pipeline.dispatcher import Dispatcher


# --------------------------------------------------------------------------
# Shared dispatcher-test helpers (mirror tests/test_v052_dispatcher.py)
# --------------------------------------------------------------------------

class RecChannel:
    def __init__(self, rec):
        self.rec = rec

    async def deliver(self, payload, rule):
        self.rec.append({"name": rule.name, "message": payload.message})
        return True


def _make_dispatcher(cfg):
    rec: list = []
    d = Dispatcher(cfg, lambda rule, conn: RecChannel(rec), connector=None)
    return d, rec


def _dispatch_one(cfg, event):
    d, rec = _make_dispatcher(cfg)
    asyncio.run(d.dispatch(event))
    return d, rec


def _quake_cfg():
    """Config with the seismic toggle enabled, cold-start grace disabled."""
    cfg = Config()
    cfg.notifications.rules = []
    cfg.notifications.cold_start_grace_seconds = 0
    t = cfg.notifications.toggles["seismic"]
    t.enabled = True
    t.min_severity = "routine"
    t.regions = []
    t.severity_channels = {
        "routine": ["mesh_broadcast"],
        "priority": ["mesh_broadcast"],
        "immediate": ["mesh_broadcast"],
    }
    t.cooldown_seconds = 0
    # Generic per-toggle freshness -- deliberately tight (600s, the old
    # default) so these tests prove the earthquake_event category is NOT
    # using this value anymore.
    t.freshness_seconds = 600
    return cfg


def _quake_event(age_seconds: float, event_id="us6000abcd", lat=42.6, lon=-114.5):
    return make_event(
        source="usgs_quake",
        category="earthquake_event",
        severity="routine",
        title=f"M3.0 -- {event_id}",
        timestamp=time.time() - age_seconds,
        lat=lat,
        lon=lon,
        group_key=event_id,
        inhibit_keys=[event_id],
    )


# ============================================================
# (a) fresh quake passes under the new per-adapter override
# ============================================================

def test_fresh_quake_passes_staleness_gate_with_override():
    """A 30-minute-old quake (1800s) exceeds the generic 600s toggle window
    but must pass under the adapter_config.usgs_quake.freshness_seconds
    override (default 3600s)."""
    cfg = _quake_cfg()
    event = _quake_event(age_seconds=1800)
    d, rec = _dispatch_one(cfg, event)
    assert len(rec) == 1, "30-min-old quake must broadcast under the 3600s override"
    assert d.dispatch_stats()["stale_dropped"] == 0


# ============================================================
# (b) old quake (12h) is still dropped
# ============================================================

def test_old_quake_still_dropped_by_override():
    """A 12-hour-old quake (43200s) must still be dropped -- the override
    widens the window, it does not disable the staleness check."""
    cfg = _quake_cfg()
    event = _quake_event(age_seconds=12 * 3600)
    d, rec = _dispatch_one(cfg, event)
    assert rec == [], "12h-old quake must still be dropped as stale"
    assert d.dispatch_stats()["stale_dropped"] == 1


def test_hydro_seismic_sibling_unaffected_by_quake_override():
    """Guardrail: stream_flood_warning shares toggle='seismic' with
    earthquake_event but must keep using the GENERIC per-toggle freshness
    (600s here), not the quake-specific 3600s override. A 1800s-old hydro
    event must still be dropped."""
    cfg = _quake_cfg()
    event = make_event(
        source="usgs", category="stream_flood_warning", severity="priority",
        title="Snake River nr Twin Falls 12.8 ft",
        timestamp=time.time() - 1800,
    )
    d, rec = _dispatch_one(cfg, event)
    assert rec == [], "hydro sibling must NOT inherit the quake freshness override"
    assert d.dispatch_stats()["stale_dropped"] == 1


# ============================================================
# (c) region-tagging from real lat/lon
# ============================================================

SW_IDAHO = MonitoringArea(name="SW Idaho", west=-117.993408, south=42.05,
                           east=-115.389404, north=44.331707)
SC_IDAHO = MonitoringArea(name="SC Idaho", west=-115.389404, south=42.05,
                           east=-112.8, north=44.331707)
EAST_IDAHO = MonitoringArea(name="East Idaho", west=-112.8, south=41.9,
                             east=-110.9, north=45.331707)
PROD_AREAS = [SW_IDAHO, SC_IDAHO, EAST_IDAHO]


def _quake_adapter():
    cfg = MagicMock()
    cfg.feed_url = "https://example.test/feed.geojson"
    cfg.min_magnitude = 2.5
    cfg.bbox = [-115.5, 42.0, -110.0, 45.2]
    cfg.region = "magic_valley"
    cfg.tick_seconds = 300
    return USGSQuakeAdapter(cfg)


def test_quake_event_region_tags_to_matching_coverage_area():
    """A quake with real Idaho lat/lon, run through the actual adapter's
    to_event() and then the real CoverageFilter (named exactly like prod's
    SW/SC/East Idaho areas), must end up tagged with a region name that
    matches a region_routes cell key -- NOT the adapter's stale
    "magic_valley" default."""
    adapter = _quake_adapter()
    raw = {
        "source": "usgs_quake",
        "event_id": "us7000example",
        "event_type": "Earthquake",
        "severity": "routine",
        "headline": "M2.7 -- 10 km N of Twin Falls, ID",
        "magnitude": 2.7,
        "place": "10 km N of Twin Falls, ID",
        "depth_km": 8.0,
        "sig": 80,
        "url": "https://earthquake.usgs.gov/x",
        "region": "magic_valley",
        "lat": 42.61,     # Twin Falls -- falls inside SC Idaho
        "lon": -114.48,
        "quake_time": time.time(),
        "expires": time.time() + 86400,
        "fetched_at": time.time(),
    }
    event = adapter.to_event(raw)
    assert event is not None
    assert event.region is None  # adapter no longer pre-stamps a region

    received: list = []
    flt = CoverageFilter(next_handler=received.append, areas=PROD_AREAS, enabled=True)
    flt.handle(event)

    assert len(received) == 1, "in-region quake must pass the coverage gate"
    tagged = received[0]
    assert tagged.region == "SC Idaho", (
        f"expected geometry tagging to set 'SC Idaho', got {tagged.region!r} "
        "(pre-fix this would have stayed 'magic_valley' and never matched a "
        "region_routes cell key)"
    )
    assert tagged.regions == ["SC Idaho"]
