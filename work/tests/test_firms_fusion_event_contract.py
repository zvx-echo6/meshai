"""Regression tests for the FIRMS fire-fusion Event contract (issues #117-#119).

Three independent bugs in the path from firms_handler's growth / spotting /
halt / cluster fusion decisions to the actual meshai Event that reaches the
dispatcher + pacer:

  #117 category overrides dead: consumer._normalize() computed `category`
       from the raw Central category BEFORE the per-adapter handler ran, and
       the final make_event() call never re-read data["category"], so every
       firms_handler category stamp (wildfire_growth / wildfire_halted /
       wildfire_spotting / unattributed_hotspot_cluster) was a silent no-op.

  #118 severity overrides dead for 3 of 4 fusion kinds: consumer.py only ever
       honored data["_severity_override"], but firms_handler's halt / spotting
       / cluster sites stamped the plain data["severity"] key instead (only
       growth used the correct key), so those events fell back to whatever
       map_severity(inner.get("severity")) produced from the raw envelope.

  #119 FirePacer didn't cover FIRMS: the pacer gate only matched
       source in ("fires", "wfigs") and severity == "priority", but FIRMS
       fusion broadcasts carry source="firms" and growth/spotting are
       severity="immediate" -- so none of them were ever paced. The fix
       broadens the gate to source="firms" + {"priority","immediate"}, and
       adds head-of-line insertion so an "immediate" event is never stuck
       behind already-queued "priority" events.

Cluster detection itself is a deliberate, always-on feature of main (PR #73:
curated new-fire cluster broadcasts, cold-start silent-seeded). Nothing here
enables or disables it -- the cluster case below only asserts that its
severity override reaches the Event (the #118 fix).

Sections:
  A. Category overrides survive to the emitted Event (growth/halt/spotting).
  B. Severity overrides survive to the emitted Event, using the shared
     `_severity_override` contract (spotting=immediate, halt=routine,
     cluster=priority).
  C. FirePacer routes FIRMS broadcasts; immediate jumps the queue; nothing
     is ever dropped.
"""
from __future__ import annotations

import asyncio
import math
import time
import uuid

import pytest

from meshai.config import Config
from meshai.central.consumer import CentralConsumer
from meshai.notifications.events import make_event
from meshai.notifications.pipeline.pacer import FirePacer
from meshai.persistence import close_thread_connection, init_db
from meshai.persistence import db as persistence_db

_MI_PER_DEG_LAT = 69.0
_SUBJECT = "central.fire.hotspot.N20.high.us.id"


# ── isolation ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / f"meshai-{uuid.uuid4().hex}.sqlite")
    monkeypatch.setenv("MESHAI_DB_PATH", db_path)
    persistence_db._initialised.clear()
    close_thread_connection()
    init_db()
    try:
        from meshai.adapter_config import adapter_config as _ac
        _ac.invalidate()
    except Exception:
        pass
    yield db_path
    close_thread_connection()
    persistence_db._initialised.discard(db_path)


@pytest.fixture(autouse=True)
def _no_cutover(monkeypatch):
    """Default deploy state: nothing cut over -> firms_handler's legacy
    stamps are what actually reach `data`."""
    monkeypatch.delenv("MESHAI_CUTOVER_CATEGORIES", raising=False)
    from meshai.notifications.cutover import _clear_cache
    _clear_cache()
    yield
    _clear_cache()


# firms_handler.handle_firms defaults `now` to real wall-clock time
# (int(time.time())) whenever it is called without an explicit `now`
# kwarg -- which is exactly how consumer._normalize()/_handle() call it (no
# `now` is threaded through from the envelope). The growth/spotting/halt
# fixtures below use fixed 2026-06-06 acq_date/acq_time values (matching the
# rest of the FIRMS test suite), so pin the wall clock to a fixed reference
# in the same window; otherwise the halt detector opportunistically fires on
# every pixel once the real host clock is weeks past the canned acq_times.
_FIXED_NOW = 1780768800.0  # 2026-06-06 18:00 UTC


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    monkeypatch.setattr("time.time", lambda: _FIXED_NOW)
    yield


@pytest.fixture
def consumer():
    """CentralConsumer with a mocked bus, mirroring
    test_consumer_default_deny.py's `consumer` fixture."""
    from unittest.mock import MagicMock
    cfg = Config()
    cfg.notifications.cold_start_grace_seconds = 0
    bus = MagicMock()
    c = CentralConsumer(cfg.environmental, bus)
    return c, bus


# ── envelope + fire-seeding helpers (mirror test_firms_refactor.py) ─────────

def _seed_fire(*, irwin_id, lat, lon, name="Stub Fire", **cols):
    from meshai.persistence import get_db
    conn = get_db()
    base = {"irwin_id": irwin_id, "incident_name": name, "lat": lat, "lon": lon,
            "last_event_at": int(time.time())}
    base.update(cols)
    keys = ",".join(base)
    ph = ",".join("?" * len(base))
    conn.execute(f"INSERT INTO fires({keys}) VALUES ({ph})", tuple(base.values()))


def _envelope(*, lat, lon, acq_date="2026-06-06", acq_time="1200",
              frp=20.0, satellite="N20", eid=None):
    eid = eid or f"firms-{lat}-{lon}-{acq_time}"
    return {
        "id": f"env_{eid}",
        "data": {
            "id": eid,
            "adapter": "firms",
            "category": "wildfire_hotspot",
            "severity": "routine",
            "geo": {"primary_region": "US-ID"},
            "data": {
                "latitude": lat, "longitude": lon, "frp": frp,
                "bright_ti4": 320.0, "satellite": satellite,
                "instrument": "VIIRS", "confidence": "high",
                "acq_date": acq_date, "acq_time": acq_time,
                "daynight": "D", "version": "2.0NRT",
            },
        },
    }


def _offset_mi(lat, lon, north_mi, east_mi):
    dlat = north_mi / _MI_PER_DEG_LAT
    dlon = east_mi / (_MI_PER_DEG_LAT * math.cos(math.radians(lat)))
    return lat + dlat, lon + dlon


# ═════════════════════════════════════════════════════════════════════════════
# A. Category overrides reach the emitted Event (issue #117)
# ═════════════════════════════════════════════════════════════════════════════

class TestCategoryOverrideReachesEvent:
    def test_growth_event_category_is_wildfire_growth(self, consumer):
        c, _bus = consumer
        center_lat, center_lon = 42.0, -114.0
        _seed_fire(irwin_id="ID-CAT-G", lat=center_lat, lon=center_lon,
                   name="Pine Gulch")
        # Pass A: 5 pixels build the baseline (no broadcast yet).
        for i in range(5):
            evt = c._normalize(_SUBJECT, _envelope(
                lat=center_lat + 0.0001 * i, lon=center_lon + 0.0001 * (i - 2),
                acq_time=f"12{i:02d}", frp=20.0 + i, eid=f"ga{i}"))
            assert evt is None, "pass-A pixels must not broadcast"
        # Pass B: 1 mi N, later pass bucket -> growth boundary.
        pass_b_lat = center_lat + (1.0 / _MI_PER_DEG_LAT)
        evt = c._normalize(_SUBJECT, _envelope(
            lat=pass_b_lat, lon=center_lon, acq_time="1800", frp=22.0, eid="gb"))
        assert evt is not None
        assert evt.category == "wildfire_growth", (
            "category override must survive to the Event, not the generic "
            "wildfire_hotspot/wildfire_incident fallback")
        assert evt.source == "firms"

    def test_halt_event_category_is_wildfire_halted(self, consumer):
        c, _bus = consumer
        now = 1780768800
        idle_at = now - 14 * 3600
        from meshai.persistence import get_db
        get_db().execute(
            "INSERT INTO fires(irwin_id, incident_name, lat, lon, "
            "last_event_at, last_pass_id, last_pass_at) VALUES (?,?,?,?,?,?,?)",
            ("ID-CAT-H", "Cold Fire", 42.5, -114.5, int(idle_at),
             "N20-329627", float(idle_at)))
        # A fresh unattributed pixel far away triggers the opportunistic
        # halt detector for the idle fire.
        evt = c._normalize(_SUBJECT, _envelope(
            lat=45.0, lon=-118.0, acq_time="1800", eid="halt1"))
        assert evt is not None
        assert evt.category == "wildfire_halted"

    def test_spotting_event_category_is_wildfire_spotting(self, consumer):
        c, _bus = consumer
        center_lat, center_lon = 43.0, -115.0
        _seed_fire(irwin_id="ID-CAT-S", lat=center_lat, lon=center_lon,
                   name="Spot Fire")
        for i in range(6):
            angle = i * math.pi / 3
            la = center_lat + (0.5 / _MI_PER_DEG_LAT) * math.sin(angle)
            cos_lat = math.cos(math.radians(center_lat))
            lo = center_lon + (0.5 / (_MI_PER_DEG_LAT * cos_lat)) * math.cos(angle)
            evt = c._normalize(_SUBJECT, _envelope(
                lat=la, lon=lo, acq_time=f"12{i * 2:02d}", eid=f"sa{i}"))
            assert evt is None
        sp_lat, sp_lon = _offset_mi(center_lat, center_lon,
                                    north_mi=2.0 / math.sqrt(2),
                                    east_mi=2.0 / math.sqrt(2))
        evt = c._normalize(_SUBJECT, _envelope(
            lat=sp_lat, lon=sp_lon, acq_time="1800", eid="sb"))
        assert evt is not None
        assert evt.category == "wildfire_spotting"


# ═════════════════════════════════════════════════════════════════════════════
# B. Severity overrides reach the emitted Event via `_severity_override`
#    (issue #118)
# ═════════════════════════════════════════════════════════════════════════════

class TestSeverityOverrideReachesEvent:
    def test_spotting_event_severity_is_immediate(self, consumer):
        c, _bus = consumer
        center_lat, center_lon = 44.0, -116.0
        _seed_fire(irwin_id="ID-SEV-S", lat=center_lat, lon=center_lon)
        for i in range(6):
            angle = i * math.pi / 3
            la = center_lat + (0.5 / _MI_PER_DEG_LAT) * math.sin(angle)
            cos_lat = math.cos(math.radians(center_lat))
            lo = center_lon + (0.5 / (_MI_PER_DEG_LAT * cos_lat)) * math.cos(angle)
            c._normalize(_SUBJECT, _envelope(
                lat=la, lon=lo, acq_time=f"12{i * 2:02d}", eid=f"ssa{i}"))
        sp_lat, sp_lon = _offset_mi(center_lat, center_lon,
                                    north_mi=2.0 / math.sqrt(2),
                                    east_mi=2.0 / math.sqrt(2))
        evt = c._normalize(_SUBJECT, _envelope(
            lat=sp_lat, lon=sp_lon, acq_time="1800", eid="ssb"))
        assert evt is not None
        assert evt.severity == "immediate", (
            "spotting must reach the pacer/dispatcher as immediate severity, "
            "not fall back to map_severity() of the raw envelope")

    def test_halt_event_severity_is_routine(self, consumer):
        c, _bus = consumer
        now = 1780768800
        idle_at = now - 14 * 3600
        from meshai.persistence import get_db
        get_db().execute(
            "INSERT INTO fires(irwin_id, incident_name, lat, lon, "
            "last_event_at, last_pass_id, last_pass_at) VALUES (?,?,?,?,?,?,?)",
            ("ID-SEV-H", "Cold Fire", 42.5, -114.5, int(idle_at),
             "N20-329627", float(idle_at)))
        evt = c._normalize(_SUBJECT, _envelope(
            lat=45.0, lon=-118.0, acq_time="1800", eid="sevhalt"))
        assert evt is not None
        assert evt.severity == "routine"

    def test_cluster_event_severity_is_priority(self, consumer):
        c, _bus = consumer
        base_lat, base_lon = 43.500, -114.500
        pixels = [
            (base_lat, base_lon, "1200"),
            (base_lat + 0.001, base_lon + 0.001, "1210"),
            (base_lat - 0.001, base_lon - 0.002, "1220"),
        ]
        events = []
        for i, (la, lo, t) in enumerate(pixels):
            evt = c._normalize("central.fire.hotspot.N20.high.unknown", _envelope(
                lat=la, lon=lo, acq_time=t, eid=f"clu{i}"))
            if evt is not None:
                events.append(evt)
        assert len(events) == 1, f"expected exactly one cluster event: {events}"
        assert events[0].category == "unattributed_hotspot_cluster"
        assert events[0].severity == "priority"


# ═════════════════════════════════════════════════════════════════════════════
# C. FirePacer covers FIRMS; immediate jumps the queue; nothing is dropped
#    (issue #119)
# ═════════════════════════════════════════════════════════════════════════════

class TestPacerCoversFirms:
    def test_firms_growth_broadcast_routes_through_pacer(self, consumer):
        """A real FIRMS growth broadcast (source=firms, severity=immediate)
        must be handed to the pacer, not emitted straight to the bus."""
        from unittest.mock import MagicMock
        c, bus = consumer
        pacer = MagicMock()
        c._pacer = pacer

        center_lat, center_lon = 42.0, -114.0
        _seed_fire(irwin_id="ID-PACE-G", lat=center_lat, lon=center_lon,
                   name="Pine Gulch")
        for i in range(5):
            c._handle(_SUBJECT, _raw(_envelope(
                lat=center_lat + 0.0001 * i, lon=center_lon + 0.0001 * (i - 2),
                acq_time=f"12{i:02d}", frp=20.0 + i, eid=f"pga{i}")))
        pass_b_lat = center_lat + (1.0 / _MI_PER_DEG_LAT)
        event = c._handle(_SUBJECT, _raw(_envelope(
            lat=pass_b_lat, lon=center_lon, acq_time="1800", frp=22.0,
            eid="pgb")))

        assert event is not None
        assert event.category == "wildfire_growth"
        pacer.enqueue.assert_called_once_with(event)
        bus.emit.assert_not_called()

    def test_immediate_event_emitted_before_already_queued_priority_events(self):
        """Two 'priority' events are queued first; a later 'immediate' event
        must still be emitted BEFORE them (head-of-line), not after."""
        emitted = []

        class _FakeBus:
            def emit(self, event):
                emitted.append(event)

        pacer = FirePacer(_FakeBus(), interval_seconds=0.01)

        p1 = make_event(source="fires", category="wildfire_incident",
                         severity="priority", title="priority-1")
        p2 = make_event(source="fires", category="wildfire_incident",
                         severity="priority", title="priority-2")
        imm = make_event(source="firms", category="wildfire_spotting",
                          severity="immediate", title="immediate-1")

        pacer.enqueue(p1)
        pacer.enqueue(p2)
        pacer.enqueue(imm)  # must jump ahead of p1/p2

        async def _drive():
            await pacer.start()
            await asyncio.sleep(0.2)
            await pacer.stop()

        asyncio.run(_drive())

        assert [e.title for e in emitted] == [
            "immediate-1", "priority-1", "priority-2"]

    def test_pacer_never_drops_events(self):
        """Rapid-fire enqueue of many events (mixed severities) -- the
        unbounded FIFO must eventually deliver every single one."""
        emitted = []

        class _FakeBus:
            def emit(self, event):
                emitted.append(event)

        pacer = FirePacer(_FakeBus(), interval_seconds=0.001)

        total = 25
        for i in range(total):
            sev = "immediate" if i % 5 == 0 else "priority"
            pacer.enqueue(make_event(
                source="firms", category="wildfire_growth",
                severity=sev, title=f"evt-{i}"))
        assert pacer.pending_count() == total

        async def _drive():
            await pacer.start()
            # Generous wait: interval is 1ms, 25 events, allow real margin.
            await asyncio.sleep(1.0)
            await pacer.stop()

        asyncio.run(_drive())

        assert len(emitted) == total, (
            f"pacer must never drop events: expected {total}, got {len(emitted)}")
        assert pacer.pending_count() == 0


def _raw(envelope: dict) -> bytes:
    import json
    return json.dumps(envelope).encode()
