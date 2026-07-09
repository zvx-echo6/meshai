"""Fire-reminder region routing tests.

A wfigs "Active:" reminder must land on the SAME channels a live New/Update
fire alert for that fire would — routed through the `fire` toggle +
region_routes matrix, with the fire's region derived from its lat/lon exactly
the way the event path derives it (coverage areas -> region names). Before this
change, fire reminders went through dispatch_scheduled_broadcast(), which
hardcodes the rf_propagation toggle -> Meshtastic ch4 / MeshCore #aida
(band-conditions), ignoring the fire's region entirely.

These tests drive the FULL path: ReminderScheduler.tick_once() reads the seeded
`fires` row, the wfigs branch calls dispatch_scheduled_fire_broadcast(), which
derives the region from the fire's lat/lon over the configured coverage areas
and routes via the matrix. Delivery channels are asserted against a real
Dispatcher + a recording channel (no MagicMock) so the exact channel is checked.

Region layout (coverage areas named to match the matrix cell keys):
    SW   Idaho  ~ Boise         (43.6, -116.2)  -> MT ch3 / MC #sw-id-aida
    SC   Idaho  ~ Twin Falls    (42.5, -114.5)  -> MT ch2 / MC #sc-id-aida
    East Idaho  ~ Idaho Falls   (43.5, -112.0)  -> MT ch5 / MC #e-id-aida
    Toggle default (fire)       ~ MT ch9 / MC #aida  (never rf_propagation/ch4)
"""
from __future__ import annotations

import asyncio
import time

import pytest

from meshai.config import Config, RegionRouteMatrix
from meshai.notifications.pipeline.dispatcher import Dispatcher
from meshai.notifications.reminders import ReminderScheduler
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


# Coverage areas (name -> small bbox around the reference point). The name is
# what event_region_names() returns AND what the matrix cell is keyed on.
_COVERAGE_AREAS = [
    {"name": "SW",   "west": -117.0, "south": 43.0, "east": -115.5, "north": 44.2},
    {"name": "SC",   "west": -115.2, "south": 42.0, "east": -113.8, "north": 43.0},
    {"name": "East", "west": -112.8, "south": 43.0, "east": -111.2, "north": 44.2},
]

# Reference points that fall inside exactly one named area above.
_PT = {
    "SW":   (43.6, -116.2),
    "SC":   (42.5, -114.5),
    "East": (43.5, -112.0),
}


def _fire_cfg(*, with_matrix=True, mt_enabled=True, mc_enabled=True,
              cold_start_grace=0):
    """Config: fire toggle enabled, coverage areas for region tagging, and the
    per-region fire matrix (SW/SC/East -> MT/MC channels)."""
    cfg = Config()
    cfg.notifications.rules = []
    cfg.notifications.cold_start_grace_seconds = cold_start_grace

    # Fire toggle: default channels DISTINCT from every matrix cell + from
    # rf_propagation (ch4) so a mis-route is unambiguous. MT default = ch9,
    # MC default = #aida (the same MC default the legacy path used — proving
    # the fallback is the FIRE toggle default, not rf_propagation's).
    fire = cfg.notifications.toggles["fire"]
    fire.enabled = True
    fire.min_severity = "routine"
    fire.regions = []
    fire.freshness_seconds = 0
    fire.cooldown_seconds = 0
    fire.broadcast_channel = 9
    fire.meshcore_channel = "#aida"
    fire.severity_channels = {
        "routine": ["mesh_broadcast", "meshcore_broadcast"],
        "priority": ["mesh_broadcast", "meshcore_broadcast"],
        "immediate": ["mesh_broadcast", "meshcore_broadcast"],
    }

    # rf_propagation toggle configured too, so a mis-route to the OLD path
    # would land on ch4/#aida-band and be caught by the assertions.
    rf = cfg.notifications.toggles["rf_propagation"]
    rf.enabled = True
    rf.broadcast_channel = 4
    rf.meshcore_channel = "#aida-band"
    rf.severity_channels = {
        "priority": ["mesh_broadcast", "meshcore_broadcast"],
    }

    cfg.coverage.enabled = True
    cfg.coverage.areas = list(_COVERAGE_AREAS)

    if with_matrix:
        cfg.notifications.region_routes = RegionRouteMatrix(
            mt_enabled=mt_enabled, mc_enabled=mc_enabled,
            cells={
                "fire": {
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


def _seed_fire(conn, *, irwin_id, lat, lon, last_broadcast_at,
               current_contained_pct=10, name="Test Fire",
               county="Ada", state="ID"):
    now = int(time.time())
    conn.execute(
        "INSERT OR REPLACE INTO fires(irwin_id, incident_name, incident_type, "
        "current_acres, current_contained_pct, lat, lon, county, state, "
        "declared_at, last_event_at, first_broadcast_at, last_broadcast_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (irwin_id, name, "WF", 500, current_contained_pct, lat, lon,
         county, state, last_broadcast_at, now,
         last_broadcast_at, last_broadcast_at),
    )


def _enable_wfigs_reminders():
    """Flip the reminders_wfigs.enabled kill switch on IN THE TEST DB ONLY.

    This mutates the per-test isolated sqlite adapter_config seed (conftest
    points MESHAI_DB_PATH at a tmp file). It does NOT touch /data/config or
    the container's adapter_config — the production kill switch stays False.
    """
    conn = get_db()
    conn.execute(
        "UPDATE adapter_config SET default_json='true' "
        "WHERE adapter='reminders_wfigs' AND key='enabled'"
    )
    conn.execute(
        "UPDATE adapter_config SET value_json='true' "
        "WHERE adapter='reminders_wfigs' AND key='enabled'"
    )
    from meshai.adapter_config import adapter_config as _ac
    _ac.invalidate()


def _tick(cfg, *, irwin_id, region, succeed=True, **fire_kw):
    """Seed one fire in `region`, run one reminder tick, return (fired, rec)."""
    conn = get_db()
    _enable_wfigs_reminders()
    lat, lon = _PT[region]
    _seed_fire(conn, irwin_id=irwin_id, lat=lat, lon=lon,
               last_broadcast_at=int(time.time()) - 9 * 3600, **fire_kw)
    d, rec = _dispatcher(cfg, succeed=succeed)
    sch = ReminderScheduler(d, clock=time.time)
    fired = asyncio.run(sch.tick_once())
    return fired, rec


# ============================================================ SW / SC / East


def test_sw_idaho_fire_reminder_routes_ch3_and_sw_mc():
    """SW Idaho fire reminder -> MT ch3 + MC #sw-id-aida (NOT ch4, NOT #aida)."""
    cfg = _fire_cfg()
    fired, rec = _tick(cfg, irwin_id="F-SW", region="SW")
    assert fired == 1

    mt = [r for r in rec if r["delivery_type"] == "mesh_broadcast"]
    mc = [r for r in rec if r["delivery_type"] == "meshcore_broadcast"]
    assert len(mt) == 1 and mt[0]["broadcast_channel"] == 3, \
        "SW fire reminder must go to MT ch3, got %r" % (mt,)
    assert len(mc) == 1 and mc[0]["meshcore_channel"] == "#sw-id-aida", \
        "SW fire reminder must go to MC #sw-id-aida, got %r" % (mc,)

    # Explicitly rule out the OLD hardcoded rf_propagation route.
    assert all(r["broadcast_channel"] != 4 for r in mt), "must NOT hit ch4"
    assert all(r["meshcore_channel"] not in ("#aida", "#aida-band") for r in mc)
    # And it must be an "Active:" reminder.
    assert any("Active" in (r["message"] or "") for r in rec)


def test_sc_idaho_fire_reminder_routes_ch2_and_sc_mc():
    """SC Idaho fire reminder -> MT ch2 + MC #sc-id-aida."""
    cfg = _fire_cfg()
    fired, rec = _tick(cfg, irwin_id="F-SC", region="SC")
    assert fired == 1

    mt = [r for r in rec if r["delivery_type"] == "mesh_broadcast"]
    mc = [r for r in rec if r["delivery_type"] == "meshcore_broadcast"]
    assert len(mt) == 1 and mt[0]["broadcast_channel"] == 2
    assert len(mc) == 1 and mc[0]["meshcore_channel"] == "#sc-id-aida"


def test_east_idaho_fire_reminder_routes_ch5_and_east_mc():
    """East Idaho fire reminder -> MT ch5 + MC #e-id-aida."""
    cfg = _fire_cfg()
    fired, rec = _tick(cfg, irwin_id="F-E", region="East")
    assert fired == 1

    mt = [r for r in rec if r["delivery_type"] == "mesh_broadcast"]
    mc = [r for r in rec if r["delivery_type"] == "meshcore_broadcast"]
    assert len(mt) == 1 and mt[0]["broadcast_channel"] == 5
    assert len(mc) == 1 and mc[0]["meshcore_channel"] == "#e-id-aida"


# ============================================================ unresolved region


def test_unresolvable_region_falls_back_to_fire_toggle_default():
    """A fire whose lat/lon lands in NO named coverage area falls back to the
    FIRE toggle default (MT ch9 / MC #aida) — never rf_propagation (ch4)."""
    cfg = _fire_cfg()
    conn = get_db()
    _enable_wfigs_reminders()
    # Point in the middle of the ocean — inside no named area.
    _seed_fire(conn, irwin_id="F-NONE", lat=0.0, lon=0.0,
               last_broadcast_at=int(time.time()) - 9 * 3600)
    d, rec = _dispatcher(cfg)
    fired = asyncio.run(ReminderScheduler(d, clock=time.time).tick_once())
    assert fired == 1

    mt = [r for r in rec if r["delivery_type"] == "mesh_broadcast"]
    mc = [r for r in rec if r["delivery_type"] == "meshcore_broadcast"]
    assert len(mt) == 1 and mt[0]["broadcast_channel"] == 9, \
        "unresolved fire must fall back to fire toggle default MT ch9, got %r" % (mt,)
    assert len(mc) == 1 and mc[0]["meshcore_channel"] == "#aida", \
        "unresolved fire must fall back to fire toggle default MC #aida, got %r" % (mc,)
    # NOT the rf_propagation band-conditions channels.
    assert all(r["broadcast_channel"] != 4 for r in mt)
    assert all(r["meshcore_channel"] != "#aida-band" for r in mc)


def test_region_matched_but_not_in_matrix_falls_back_to_default():
    """A fire whose region IS derived (SW) but has NO cell in the matrix falls
    back to the fire toggle default, still NOT rf_propagation."""
    cfg = _fire_cfg()
    # Drop the SW cell so the derived 'SW' region has no matrix entry.
    del cfg.notifications.region_routes.cells["fire"]["SW"]
    fired, rec = _tick(cfg, irwin_id="F-SW2", region="SW")
    assert fired == 1

    mt = [r for r in rec if r["delivery_type"] == "mesh_broadcast"]
    mc = [r for r in rec if r["delivery_type"] == "meshcore_broadcast"]
    assert len(mt) == 1 and mt[0]["broadcast_channel"] == 9
    assert len(mc) == 1 and mc[0]["meshcore_channel"] == "#aida"


def test_matrix_disabled_falls_back_to_fire_toggle_default():
    """region_routes disabled for both transports -> fire toggle default
    channels (ch9/#aida), never rf_propagation."""
    cfg = _fire_cfg(mt_enabled=False, mc_enabled=False)
    fired, rec = _tick(cfg, irwin_id="F-SW3", region="SW")
    assert fired == 1

    mt = [r for r in rec if r["delivery_type"] == "mesh_broadcast"]
    mc = [r for r in rec if r["delivery_type"] == "meshcore_broadcast"]
    assert len(mt) == 1 and mt[0]["broadcast_channel"] == 9
    assert len(mc) == 1 and mc[0]["meshcore_channel"] == "#aida"


# ============================================================ regressions


def test_rf_propagation_reminder_still_routes_ch4_and_aida():
    """REGRESSION: a band-conditions (rf_propagation) reminder still uses the
    UNCHANGED dispatch_scheduled_broadcast path -> MT ch4 / MC #aida.

    Driven via the same _dispatch entry the scheduler uses, with adapter=swpc
    standing in for a scheduled non-fire broadcaster: it must NOT touch the new
    fire path and must land on rf_propagation's configured channels."""
    cfg = _fire_cfg()
    d, rec = _dispatcher(cfg)
    sch = ReminderScheduler(d, clock=time.time)

    # A minimal fake row is fine — _dispatch(adapter="swpc", ...) only needs
    # _row_pk to resolve, which reads row["event_id"].
    row = {"event_id": "S-RF"}
    ok = asyncio.run(sch._dispatch("swpc", row, "🌌 Active: ongoing space weather"))
    assert ok is True

    # rf_propagation is priority-class: severity_channels['priority'] =
    # [mesh_broadcast, meshcore_broadcast] -> ch4 + #aida-band (its config).
    mt = [r for r in rec if r["delivery_type"] == "mesh_broadcast"]
    mc = [r for r in rec if r["delivery_type"] == "meshcore_broadcast"]
    assert len(mt) == 1 and mt[0]["broadcast_channel"] == 4, \
        "rf_propagation reminder must still use ch4, got %r" % (mt,)
    assert len(mc) == 1 and mc[0]["meshcore_channel"] == "#aida-band"
    # It must NOT have used any fire matrix / fire-default channel.
    assert all(r["broadcast_channel"] not in (2, 3, 5, 9) for r in mt)


def test_fire_reminder_does_not_touch_rf_propagation_path():
    """The wfigs branch must call dispatch_scheduled_fire_broadcast and NOT
    dispatch_scheduled_broadcast (the rf_propagation path). Verified by spying
    on both dispatcher methods with a matched-region fire."""
    from unittest.mock import AsyncMock

    cfg = _fire_cfg()
    conn = get_db()
    _enable_wfigs_reminders()
    lat, lon = _PT["SW"]
    _seed_fire(conn, irwin_id="F-SPY", lat=lat, lon=lon,
               last_broadcast_at=int(time.time()) - 9 * 3600)

    d, _rec = _dispatcher(cfg)
    d.dispatch_scheduled_broadcast = AsyncMock(return_value=True)
    fire_spy = AsyncMock(return_value=True)
    d.dispatch_scheduled_fire_broadcast = fire_spy

    fired = asyncio.run(ReminderScheduler(d, clock=time.time).tick_once())
    assert fired == 1
    fire_spy.assert_called_once()
    kwargs = fire_spy.call_args.kwargs
    assert kwargs["source_event_pk"] == "F-SPY"
    assert kwargs["lat"] == pytest.approx(lat)
    assert kwargs["lon"] == pytest.approx(lon)
    d.dispatch_scheduled_broadcast.assert_not_called()


def test_disabled_wfigs_reminders_send_nothing():
    """Sanity: with the kill switch OFF (default), no fire reminder fires even
    when a stale fire exists."""
    cfg = _fire_cfg()
    conn = get_db()
    # NOTE: intentionally do NOT call _enable_wfigs_reminders().
    lat, lon = _PT["SW"]
    _seed_fire(conn, irwin_id="F-OFF", lat=lat, lon=lon,
               last_broadcast_at=int(time.time()) - 9 * 3600)
    d, rec = _dispatcher(cfg)
    fired = asyncio.run(ReminderScheduler(d, clock=time.time).tick_once())
    assert fired == 0
    assert rec == []
