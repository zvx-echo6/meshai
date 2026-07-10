"""Part 3 tests -- wzdx per-region DAILY work-zone count summary.

WZDxSummaryScheduler.fire_once() must, for each coverage region with >=1
active in-coverage work zone, broadcast ONE <=140-char line routed through
the region_routes "roads" family cells -- exactly like the fire scheduled
path routes through "fire" cells (test_fire_reminder_region_routing.py is
the pattern this mirrors). Regions with 0 zones must be skipped. The fire
must be on a DAILY clock tick, not change-based (no throttle/dedup table).
"""
from __future__ import annotations

import asyncio
import time

import pytest

from meshai.config import Config, RegionRouteMatrix
from meshai.notifications.pipeline.dispatcher import Dispatcher
from meshai.notifications.scheduled.wzdx_summary import WZDxSummaryScheduler
from meshai.persistence import get_db


# --------------------------------------------------------------------- recorder


class RecChannel:
    """Records each delivery's transport + channel value + message."""

    def __init__(self, rec: list, succeed: bool = True):
        self.rec = rec
        self.succeed = succeed

    async def deliver(self, payload, rule):
        self.rec.append({
            "delivery_type": rule.delivery_type,
            "broadcast_channel": getattr(rule, "broadcast_channel", None),
            "meshcore_channel": getattr(rule, "meshcore_channel", None),
            "message": payload.message if payload else None,
        })
        return self.succeed


# --------------------------------------------------------------------- fixtures


# Same region layout as test_fire_reminder_region_routing.py (SW/SC/East),
# but the matrix cells here are keyed under the "roads" family.
_COVERAGE_AREAS = [
    {"name": "SW",   "west": -117.0, "south": 43.0, "east": -115.5, "north": 44.2},
    {"name": "SC",   "west": -115.2, "south": 42.0, "east": -113.8, "north": 43.0},
    {"name": "East", "west": -112.8, "south": 43.0, "east": -111.2, "north": 44.2},
]

_PT = {
    "SW":   (43.6, -116.2),
    "SC":   (42.5, -114.5),
    "East": (43.5, -112.0),
}


def _roads_cfg(*, with_matrix=True, mt_enabled=True, mc_enabled=True,
                cold_start_grace=0):
    cfg = Config()
    cfg.notifications.rules = []
    cfg.notifications.cold_start_grace_seconds = cold_start_grace

    roads = cfg.notifications.toggles["roads"]
    roads.enabled = True
    roads.min_severity = "routine"
    roads.regions = []
    roads.freshness_seconds = 0
    roads.cooldown_seconds = 0
    roads.broadcast_channel = 9
    roads.meshcore_channel = "#aida"
    roads.severity_channels = {
        "routine": ["mesh_broadcast", "meshcore_broadcast"],
        "priority": ["mesh_broadcast", "meshcore_broadcast"],
        "immediate": ["mesh_broadcast", "meshcore_broadcast"],
    }

    cfg.coverage.enabled = True
    cfg.coverage.areas = list(_COVERAGE_AREAS)

    if with_matrix:
        cfg.notifications.region_routes = RegionRouteMatrix(
            mt_enabled=mt_enabled, mc_enabled=mc_enabled,
            cells={
                "roads": {
                    "SW":   {"mt": 3, "mc": "#sw-id-aida",
                             "min_severity": "routine", "enabled": True},
                    "SC":   {"mt": 2, "mc": "#sc-id-aida",
                             "min_severity": "routine", "enabled": True},
                    "East": {"mt": 5, "mc": "#e-id-aida",
                             "min_severity": "routine", "enabled": True},
                },
            },
        )
    return cfg


def _dispatcher(cfg, succeed=True):
    rec: list = []
    d = Dispatcher(cfg, lambda rule, conn: RecChannel(rec, succeed), connector=None)
    return d, rec


def _seed_wzdx_row(conn, *, external_id, lat, lon, road="US-95",
                    end_at=None, sub_type="lanes reduced, surface work"):
    now = int(time.time())
    conn.execute(
        "INSERT OR REPLACE INTO traffic_events(source, external_id, road, "
        "direction, sub_type, impact, lat, lon, first_seen_at, last_seen_at, "
        "last_broadcast_at, start_at, end_at) "
        "VALUES ('wzdx', ?, ?, 'southbound', ?, 'partial', ?, ?, ?, ?, NULL, ?, ?)",
        (external_id, road, sub_type, lat, lon, now, now, now - 3600, end_at),
    )


def _fire(cfg, succeed=True, now=None):
    d, rec = _dispatcher(cfg, succeed=succeed)
    sch = WZDxSummaryScheduler(cfg, d, clock=(lambda: now) if now else time.time)
    dispatched = asyncio.run(sch.fire_once(now=now))
    return dispatched, rec


# ============================================================ SW / SC / East


def test_sw_region_summary_routes_ch3_and_sw_mc():
    """SW region with 2 active zones -> ONE line, MT ch3 + MC #sw-id-aida."""
    cfg = _roads_cfg()
    conn = get_db()
    lat, lon = _PT["SW"]
    _seed_wzdx_row(conn, external_id="wz-sw-1", lat=lat, lon=lon, road="US-95")
    _seed_wzdx_row(conn, external_id="wz-sw-2", lat=lat, lon=lon, road="I-84")

    dispatched, rec = _fire(cfg)
    assert dispatched == 1

    mt = [r for r in rec if r["delivery_type"] == "mesh_broadcast"]
    mc = [r for r in rec if r["delivery_type"] == "meshcore_broadcast"]
    assert len(mt) == 1 and mt[0]["broadcast_channel"] == 3
    assert len(mc) == 1 and mc[0]["meshcore_channel"] == "#sw-id-aida"

    msg = mt[0]["message"]
    assert msg.startswith("🚧 SW: 2 active work zones")
    assert "DM AIDA for details" in msg
    assert len(msg.encode("utf-8")) <= 140
    # Must NOT hit the toggle default / rf_propagation-style channels.
    assert all(r["broadcast_channel"] != 9 for r in mt)


def test_sc_region_summary_routes_ch2_and_sc_mc():
    cfg = _roads_cfg()
    conn = get_db()
    lat, lon = _PT["SC"]
    _seed_wzdx_row(conn, external_id="wz-sc-1", lat=lat, lon=lon, road="US-93")

    dispatched, rec = _fire(cfg)
    assert dispatched == 1

    mt = [r for r in rec if r["delivery_type"] == "mesh_broadcast"]
    mc = [r for r in rec if r["delivery_type"] == "meshcore_broadcast"]
    assert len(mt) == 1 and mt[0]["broadcast_channel"] == 2
    assert len(mc) == 1 and mc[0]["meshcore_channel"] == "#sc-id-aida"
    assert mt[0]["message"].startswith("🚧 SC: 1 active work zones")


def test_east_region_summary_routes_ch5_and_east_mc():
    cfg = _roads_cfg()
    conn = get_db()
    lat, lon = _PT["East"]
    _seed_wzdx_row(conn, external_id="wz-e-1", lat=lat, lon=lon, road="US-20")

    dispatched, rec = _fire(cfg)
    assert dispatched == 1

    mt = [r for r in rec if r["delivery_type"] == "mesh_broadcast"]
    mc = [r for r in rec if r["delivery_type"] == "meshcore_broadcast"]
    assert len(mt) == 1 and mt[0]["broadcast_channel"] == 5
    assert len(mc) == 1 and mc[0]["meshcore_channel"] == "#e-id-aida"


# ============================================================ multi-region + skip


def test_multiple_regions_each_get_own_line_zero_zone_regions_skipped():
    """SW and East each have zones; SC has none -> exactly 2 dispatched
    lines (SC skipped, no line emitted for it)."""
    cfg = _roads_cfg()
    conn = get_db()
    sw_lat, sw_lon = _PT["SW"]
    e_lat, e_lon = _PT["East"]
    _seed_wzdx_row(conn, external_id="wz-sw-x", lat=sw_lat, lon=sw_lon, road="US-95")
    _seed_wzdx_row(conn, external_id="wz-e-x", lat=e_lat, lon=e_lon, road="US-20")

    dispatched, rec = _fire(cfg)
    assert dispatched == 2

    mt = [r for r in rec if r["delivery_type"] == "mesh_broadcast"]
    channels = {r["broadcast_channel"] for r in mt}
    assert channels == {3, 5}   # SW ch3, East ch5 -- SC (ch2) never fired
    messages = " ".join(r["message"] for r in mt)
    assert "SC" not in messages


def test_no_active_zones_dispatches_nothing():
    cfg = _roads_cfg()
    dispatched, rec = _fire(cfg)
    assert dispatched == 0
    assert rec == []


def test_expired_zone_not_counted():
    """A wzdx row whose end_at is in the past is NOT an active zone."""
    cfg = _roads_cfg()
    conn = get_db()
    lat, lon = _PT["SW"]
    past = int(time.time()) - 86400
    _seed_wzdx_row(conn, external_id="wz-sw-expired", lat=lat, lon=lon,
                    road="US-95", end_at=past)

    dispatched, rec = _fire(cfg)
    assert dispatched == 0
    assert rec == []


def test_open_ended_zone_counted_as_active():
    """A wzdx row with end_at=NULL (open-ended) IS an active zone."""
    cfg = _roads_cfg()
    conn = get_db()
    lat, lon = _PT["SW"]
    _seed_wzdx_row(conn, external_id="wz-sw-open", lat=lat, lon=lon,
                    road="US-95", end_at=None)

    dispatched, rec = _fire(cfg)
    assert dispatched == 1


# ============================================================ unresolved region


def test_unresolvable_region_falls_back_to_roads_toggle_default():
    """A zone whose lat/lon lands in NO named coverage area is simply not
    counted toward any region (no line emitted for it) -- there IS no
    'unresolved region' broadcast for a summary line (unlike the fire
    reminder path, a count needs a NAMED region to attach to)."""
    cfg = _roads_cfg()
    conn = get_db()
    _seed_wzdx_row(conn, external_id="wz-none", lat=0.0, lon=0.0, road="US-1")

    dispatched, rec = _fire(cfg)
    assert dispatched == 0
    assert rec == []


def test_region_matched_but_not_in_matrix_falls_back_to_toggle_default():
    """A zone in a coverage region (SW) with NO matrix cell for 'roads' in
    that region falls back to the roads toggle default channels."""
    cfg = _roads_cfg()
    del cfg.notifications.region_routes.cells["roads"]["SW"]
    conn = get_db()
    lat, lon = _PT["SW"]
    _seed_wzdx_row(conn, external_id="wz-sw-nomatrix", lat=lat, lon=lon, road="US-95")

    dispatched, rec = _fire(cfg)
    assert dispatched == 1
    mt = [r for r in rec if r["delivery_type"] == "mesh_broadcast"]
    mc = [r for r in rec if r["delivery_type"] == "meshcore_broadcast"]
    assert len(mt) == 1 and mt[0]["broadcast_channel"] == 9   # roads toggle default
    assert len(mc) == 1 and mc[0]["meshcore_channel"] == "#aida"


def test_matrix_disabled_falls_back_to_roads_toggle_default():
    cfg = _roads_cfg(mt_enabled=False, mc_enabled=False)
    conn = get_db()
    lat, lon = _PT["SW"]
    _seed_wzdx_row(conn, external_id="wz-sw-matrixoff", lat=lat, lon=lon, road="US-95")

    dispatched, rec = _fire(cfg)
    assert dispatched == 1
    mt = [r for r in rec if r["delivery_type"] == "mesh_broadcast"]
    mc = [r for r in rec if r["delivery_type"] == "meshcore_broadcast"]
    assert len(mt) == 1 and mt[0]["broadcast_channel"] == 9
    assert len(mc) == 1 and mc[0]["meshcore_channel"] == "#aida"


# ============================================================ daily-tick (not change-based)


def test_fires_on_daily_tick_not_change_based():
    """fire_once() with the SAME unchanged set of active zones fires again
    on a second call -- there is deliberately NO change-detection / dedup
    table gating the daily summary (per spec)."""
    cfg = _roads_cfg()
    conn = get_db()
    lat, lon = _PT["SW"]
    _seed_wzdx_row(conn, external_id="wz-sw-daily", lat=lat, lon=lon, road="US-95")

    dispatched1, rec1 = _fire(cfg)
    assert dispatched1 == 1
    dispatched2, rec2 = _fire(cfg)
    assert dispatched2 == 1   # fires again -- daily clock tick, not a diff


def test_cold_start_grace_suppresses_first_fire():
    cfg = _roads_cfg(cold_start_grace=3600)
    conn = get_db()
    lat, lon = _PT["SW"]
    _seed_wzdx_row(conn, external_id="wz-sw-grace", lat=lat, lon=lon, road="US-95")

    dispatched, rec = _fire(cfg)
    assert dispatched == 0
    assert rec == []
