"""Region-routes matrix dispatcher tests (P3 — authoritative-on-match).

Covers the matrix branch inserted between Section 1 (staleness) and Section 2
(region-scope/toggle-min_severity) in _dispatch_toggles.

NOTE: the test harness does NOT build a CoverageFilter — event.region /
event.regions are set manually on each event object. That is intentional: the
matrix branch resolves against event.region/regions independently of how those
fields were populated.

Test inventory
--------------
1. cell_match_routes_mt_and_mc        — a cell with both mt+mc sends two deliveries
2. routine_event_bypasses_toggle_floor — matrix cell floor=routine; toggle.min_severity=priority
3. no_cell_for_region_falls_through   — event region not in cells → normal toggle path
4. matrix_disabled_falls_through      — enabled=False → byte-identical toggle path
5. per_region_cooldown_independence   — cooldown on region A does not block region B
6. per_channel_dedup_independence     — failed channel leaves no dedup; retries on next call
7. authoritative_no_double_send       — matrix match does NOT also fire toggle default
8. empty_chans_authoritative_no_send  — every cell below floor → return, no toggle fallback
9. dedup_suppresses_repeat_same_channel — same event id + same channel → dedup on second call
10. cell_disabled_flag_skipped         — cell with enabled=False is ignored
"""

import asyncio
import time

import pytest

from meshai.config import Config, RegionRouteMatrix
from meshai.notifications.pipeline.dispatcher import Dispatcher
from meshai.notifications.events import make_event


# ------------------------------------------------------------------ helpers


class RecChannel:
    """Channel recorder capturing delivery type, channel values, and success."""

    def __init__(self, rec: list, succeed: bool = True):
        self.rec = rec
        self.succeed = succeed

    async def deliver(self, payload, rule):
        self.rec.append({
            "delivery_type": rule.delivery_type,
            "broadcast_channel": getattr(rule, "broadcast_channel", None),
            "meshcore_channel": getattr(rule, "meshcore_channel", None),
            "name": rule.name,
            "message": payload.message if payload else None,
        })
        return self.succeed


def _make_dispatcher(cfg, succeed=True):
    rec: list = []
    d = Dispatcher(cfg, lambda rule, conn: RecChannel(rec, succeed), connector=None)
    return d, rec


def _dispatch(cfg, event, succeed=True):
    d, rec = _make_dispatcher(cfg, succeed=succeed)
    asyncio.run(d.dispatch(event))
    return d, rec


def _base_cfg(fam="fire", cooldown_s=0, min_severity="priority"):
    """Minimal config: one toggle enabled, no cold-start grace."""
    cfg = Config()
    cfg.notifications.rules = []
    cfg.notifications.cold_start_grace_seconds = 0
    t = cfg.notifications.toggles[fam]
    t.enabled = True
    t.min_severity = min_severity
    t.regions = []           # no toggle-level region filter
    t.severity_channels = {
        "routine": ["mesh_broadcast"],
        "priority": ["mesh_broadcast"],
        "immediate": ["mesh_broadcast"],
    }
    t.freshness_seconds = 0  # never stale in tests
    t.cooldown_seconds = cooldown_s
    t.broadcast_channel = 0  # toggle default channel (different from matrix cells)
    return cfg


def _rr(cells: dict, enabled: bool = True) -> RegionRouteMatrix:
    return RegionRouteMatrix(enabled=enabled, cells=cells)


def _ev(fam="fire", region=None, regions=None, severity="immediate",
        eid=None, suffix=None):
    ev = make_event(
        source="test", category="wildfire_incident" if fam == "fire" else "weather_warning",
        severity=severity, title="Test event",
    )
    if eid:
        ev.id = eid
    if region:
        ev.region = region
    if regions:
        ev.regions = regions
    if suffix:
        ev.data["_dedup_suffix"] = suffix
    return ev


# ============================================================ Test 1
# Cell match → both mt and mc channels deliver

def test_cell_match_routes_mt_and_mc():
    """A cell with mt=3 and mc='sci-fires' must produce two deliveries —
    mesh_broadcast on channel 3 and meshcore_broadcast on channel 'sci-fires'."""
    cfg = _base_cfg(fam="fire")
    cfg.notifications.region_routes = _rr(cells={
        "fire": {
            "SCI": {"mt": 3, "mc": "sci-fires", "min_severity": "routine", "enabled": True},
        }
    })
    ev = _ev(fam="fire", region="SCI")
    _, rec = _dispatch(cfg, ev)

    types = {r["delivery_type"] for r in rec}
    assert "mesh_broadcast" in types, "must deliver via mesh_broadcast"
    assert "meshcore_broadcast" in types, "must deliver via meshcore_broadcast"
    assert len(rec) == 2, "exactly two deliveries (mt + mc)"

    mb = next(r for r in rec if r["delivery_type"] == "mesh_broadcast")
    mcb = next(r for r in rec if r["delivery_type"] == "meshcore_broadcast")
    assert mb["broadcast_channel"] == 3
    assert mcb["meshcore_channel"] == "sci-fires"


# ============================================================ Test 2
# Routine severity event routes via matrix even when toggle.min_severity=priority

def test_routine_event_bypasses_toggle_floor():
    """Matrix cell min_severity=routine must route a routine-severity event even
    when the toggle's own min_severity=priority (Section 2 bypass)."""
    cfg = _base_cfg(fam="fire", min_severity="priority")
    cfg.notifications.region_routes = _rr(cells={
        "fire": {
            "SWI": {"mt": 1, "mc": None, "min_severity": "routine", "enabled": True},
        }
    })
    ev = _ev(fam="fire", region="SWI", severity="routine")
    _, rec = _dispatch(cfg, ev)

    # Matrix delivered at mt=1
    assert len(rec) == 1
    assert rec[0]["delivery_type"] == "mesh_broadcast"
    assert rec[0]["broadcast_channel"] == 1


# ============================================================ Test 3
# Event region not in cells → fall through to normal toggle path

def test_no_cell_for_region_falls_through():
    """When the event's region is not present in fam_cells, _matrix_matched is
    None and the dispatcher falls through to the toggle default path."""
    cfg = _base_cfg(fam="fire", min_severity="routine")
    cfg.notifications.region_routes = _rr(cells={
        "fire": {
            "SCI": {"mt": 3, "mc": None, "min_severity": "routine", "enabled": True},
        }
    })
    # Region 'SWI' has no cell
    ev = _ev(fam="fire", region="SWI")
    _, rec = _dispatch(cfg, ev)

    # Toggle default path delivers via broadcast_channel=0 (toggle's default)
    assert len(rec) == 1
    assert rec[0]["delivery_type"] == "mesh_broadcast"
    assert rec[0]["broadcast_channel"] == 0   # toggle default, not matrix cell


# ============================================================ Test 4
# matrix disabled → byte-identical to no-matrix behaviour

def test_matrix_disabled_falls_through():
    """region_routes.enabled=False must leave the toggle path completely unchanged."""
    cfg = _base_cfg(fam="fire", min_severity="routine")
    cfg.notifications.region_routes = _rr(
        cells={"fire": {"SCI": {"mt": 7, "mc": "x", "min_severity": "routine", "enabled": True}}},
        enabled=False,
    )
    ev = _ev(fam="fire", region="SCI")
    _, rec = _dispatch(cfg, ev)

    # Toggle default still fires with broadcast_channel=0
    assert len(rec) == 1
    assert rec[0]["broadcast_channel"] == 0


# ============================================================ Test 5
# Per-region cooldown independence

def test_per_region_cooldown_independence():
    """Cooldown armed for region A must not block region B."""
    cfg = _base_cfg(fam="fire", cooldown_s=300, min_severity="routine")
    cfg.notifications.region_routes = _rr(cells={
        "fire": {
            "SCI": {"mt": 3, "mc": None, "min_severity": "routine", "enabled": True},
            "SWI": {"mt": 1, "mc": None, "min_severity": "routine", "enabled": True},
        }
    })
    d, rec = _make_dispatcher(cfg)

    ev_sci = _ev(fam="fire", region="SCI", eid="ev-sci")
    asyncio.run(d.dispatch(ev_sci))
    # SCI ch=3 now has cooldown armed
    assert len(rec) == 1
    assert rec[0]["broadcast_channel"] == 3

    ev_swi = _ev(fam="fire", region="SWI", eid="ev-swi")
    asyncio.run(d.dispatch(ev_swi))
    # SWI ch=1 must still deliver despite SCI cooldown
    assert len(rec) == 2
    assert rec[1]["broadcast_channel"] == 1
    assert d.dispatch_stats()["cooldown_dropped"] == 0


# ============================================================ Test 6
# Per-channel dedup independence: failed channel leaves no dedup trace

def test_per_channel_dedup_failed_channel_retries():
    """A channel that fails delivery must leave no dedup entry, so it retries
    next sweep.  A distinct (different ch_type/chan) channel that succeeded is
    still deduped on the second dispatch."""
    cfg = _base_cfg(fam="fire", min_severity="routine")
    cfg.notifications.region_routes = _rr(cells={
        "fire": {
            "SCI": {
                "mt": 3, "mc": "sci-fires",
                "min_severity": "routine", "enabled": True,
            },
        }
    })
    d = Dispatcher.__new__(Dispatcher)

    # Track which channels have succeeded to control per-channel return value.
    _succeeded: set = set()
    _rec: list = []

    def _factory(rule, conn):
        ch = rule.delivery_type
        chan = rule.broadcast_channel if ch == "mesh_broadcast" else rule.meshcore_channel

        class _Ch:
            async def deliver(self_, payload, rule_):
                _rec.append({
                    "delivery_type": ch,
                    "chan": chan,
                })
                # mesh_broadcast ch=3 fails first time only
                if ch == "mesh_broadcast" and ("mb", chan) not in _succeeded:
                    return False
                _succeeded.add((ch, chan))
                return True

        return _Ch()

    from meshai.persistence import init_db
    import os, tempfile
    # Use a fresh DB (conftest already set MESHAI_DB_PATH via monkeypatch)
    Dispatcher.__init__(d, cfg, _factory, connector=None)

    ev = _ev(fam="fire", region="SCI", eid="dedup-ch-test")

    # First dispatch: mesh_broadcast fails, meshcore_broadcast succeeds.
    asyncio.run(d.dispatch(ev))
    assert len(_rec) == 2  # both attempted

    # Dedup keys are 2-tuples (source, "id#suffix|ch_type|chan") — the channel
    # is baked into the second element (this shape survives the boot restore).
    # meshcore_broadcast succeeded → recorded; mesh_broadcast failed → NOT.
    assert any("meshcore_broadcast" in k[1] for k in d._dedup_lru), \
        "meshcore_broadcast (success) must be in dedup_lru"
    assert not any("|mesh_broadcast|" in k[1] for k in d._dedup_lru), \
        "mesh_broadcast (failed) must NOT be in dedup_lru"

    # Second dispatch same event: meshcore_broadcast deduped; mesh_broadcast retries.
    before = len(_rec)
    asyncio.run(d.dispatch(ev))
    new_deliveries = _rec[before:]
    # Only mesh_broadcast attempted again (meshcore was deduped)
    assert any(r["delivery_type"] == "mesh_broadcast" for r in new_deliveries), \
        "mesh_broadcast must retry on second dispatch"
    assert not any(r["delivery_type"] == "meshcore_broadcast" for r in new_deliveries), \
        "meshcore_broadcast must be deduped on second dispatch"
    assert d.dispatch_stats()["dedup_dropped"] == 1


# ============================================================ Test 7
# Authoritative-on-match: matrix match must NOT also fire toggle default

def test_authoritative_no_double_send():
    """When the matrix matches, the toggle default (Sections 2–6) must NOT run.
    We verify by checking the rule name: matrix branch uses toggle:<fam> but
    the channel is the matrix cell's channel, not the toggle's broadcast_channel."""
    cfg = _base_cfg(fam="fire", min_severity="routine")
    cfg.notifications.toggles["fire"].broadcast_channel = 0  # toggle default
    cfg.notifications.region_routes = _rr(cells={
        "fire": {
            "SCI": {"mt": 5, "mc": None, "min_severity": "routine", "enabled": True},
        }
    })
    ev = _ev(fam="fire", region="SCI")
    _, rec = _dispatch(cfg, ev)

    # Exactly one delivery, via matrix cell channel 5 (not toggle default 0)
    assert len(rec) == 1
    assert rec[0]["broadcast_channel"] == 5, \
        "matrix channel (5) must be used, not toggle default (0)"


# ============================================================ Test 8
# Empty chans (every cell below floor) → authoritative no-send, not toggle fallback

def test_empty_chans_authoritative_no_send():
    """When every matched cell is below its min_severity floor the matrix
    still consumes the event (authoritative) and returns without sending.
    The toggle default must NOT run as a fallback."""
    cfg = _base_cfg(fam="fire", min_severity="routine")
    cfg.notifications.region_routes = _rr(cells={
        "fire": {
            "SCI": {
                "mt": 3, "mc": None,
                "min_severity": "immediate",   # floor = immediate
                "enabled": True,
            },
        }
    })
    # routine event — below the cell floor
    ev = _ev(fam="fire", region="SCI", severity="routine")
    _, rec = _dispatch(cfg, ev)

    assert rec == [], "below floor → authoritative no-send, toggle default does NOT fire"


# ============================================================ Test 9
# Dedup suppresses repeat on same channel

def test_dedup_suppresses_repeat_same_channel():
    """Same event id + same channel → first send succeeds, second is deduped."""
    cfg = _base_cfg(fam="fire", min_severity="routine")
    cfg.notifications.region_routes = _rr(cells={
        "fire": {
            "SCI": {"mt": 2, "mc": None, "min_severity": "routine", "enabled": True},
        }
    })
    d, rec = _make_dispatcher(cfg)

    ev = _ev(fam="fire", region="SCI", eid="dedup-repeat")
    asyncio.run(d.dispatch(ev))
    assert len(rec) == 1

    asyncio.run(d.dispatch(ev))
    assert len(rec) == 1   # second suppressed
    assert d.dispatch_stats()["dedup_dropped"] == 1


# ============================================================ Test 10
# cell enabled=False is skipped

def test_cell_disabled_flag_skipped():
    """A cell with enabled=False must be ignored even when its region matches."""
    cfg = _base_cfg(fam="fire", min_severity="routine")
    cfg.notifications.region_routes = _rr(cells={
        "fire": {
            "SCI": {"mt": 3, "mc": None, "min_severity": "routine", "enabled": False},
        }
    })
    ev = _ev(fam="fire", region="SCI")
    _, rec = _dispatch(cfg, ev)

    # Cell disabled → chans empty → authoritative no-send
    assert rec == []


# ============================================================ Bonus: existing tests
# Smoke-check that non-matrix toggle path is unaffected when region_routes.enabled=False

def test_existing_toggle_path_unaffected_by_inert_matrix():
    """Sanity: a toggle with no region_routes config behaves exactly as before
    (sends via the toggle's default broadcast_channel=1)."""
    cfg = _base_cfg(fam="weather", min_severity="routine")
    cfg.notifications.toggles["weather"].broadcast_channel = 1
    # No region_routes set → default RegionRouteMatrix(enabled=False)
    ev = make_event(
        source="nws", category="weather_warning", severity="priority",
        title="Red Flag",
    )
    _, rec = _dispatch(cfg, ev)
    assert len(rec) == 1
    assert rec[0]["broadcast_channel"] == 1


# ============================================================ Bonus: regions list

def test_matrix_resolves_from_event_regions_list():
    """event.regions=[...] (multi-region) must also resolve cells, not just
    event.region.  Both matching regions send to their respective channels."""
    cfg = _base_cfg(fam="fire", min_severity="routine")
    cfg.notifications.region_routes = _rr(cells={
        "fire": {
            "SCI": {"mt": 3, "mc": None, "min_severity": "routine", "enabled": True},
            "SWI": {"mt": 1, "mc": None, "min_severity": "routine", "enabled": True},
        }
    })
    # event.region=None, event.regions=["SCI","SWI"]
    ev = _ev(fam="fire", regions=["SCI", "SWI"])
    _, rec = _dispatch(cfg, ev)

    # Each region maps to a distinct mt index → two sends
    channels = {r["broadcast_channel"] for r in rec}
    assert channels == {1, 3}


def test_restart_dedup_key_matches_boot_restore_form():
    """Regression (restart-flood): the matrix dedup key MUST equal the boot-
    restore shape — a 2-tuple (source, "id|ch_type|chan"). Dispatcher
    construction rehydrates _dedup_lru as 2-tuples; a 4-tuple check key would
    miss after every restart and re-broadcast the whole matrix backlog."""
    cfg = _base_cfg(fam="fire", cooldown_s=0)
    cfg.notifications.region_routes = _rr(
        {"fire": {"SCI": {"mt": 3, "mc": None,
                          "min_severity": "routine", "enabled": True}}}
    )
    d, rec = _make_dispatcher(cfg)
    ev = _ev(fam="fire", region="SCI", eid="restart-test")
    # Exactly what dispatcher.__init__ restores from dispatcher_dedup:
    d._dedup_lru[("test", "restart-test|mesh_broadcast|3")] = True
    asyncio.run(d.dispatch(ev))
    mesh_sends = [r for r in rec if r["delivery_type"] == "mesh_broadcast"]
    assert mesh_sends == [], (
        "matrix must dedup against the restored 2-tuple key; a re-broadcast "
        "here is the restart-flood bug"
    )
