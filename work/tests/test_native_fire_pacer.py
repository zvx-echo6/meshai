"""Native fire-family events must be paced too, not just Central ones.

FirePacer (notifications/pipeline/pacer.py) was wired ONLY into the Central
NATS consumer path (central/consumer.py._handle routes fire-family events to
`self._pacer.enqueue(event)` instead of `self._bus.emit(event)` -- see issue
#119). In an all-native deployment (central.enabled=False, the actual
production configuration on CT 108) that consumer never receives any NATS
message, so the pacer sat completely unused: every native fire adapter
(env/fires.py, source="nifc"; env/firms.py, source="firms") emitted straight
to the EventBus from EnvironmentalStore._emit_event with no rate limiting at
all. A FIRMS poll (or a WFIGS poll) that produces several distinct
fires/clusters at once -- a lightning outbreak forming multiple new-fire
clusters, or several already-tracked fires crossing a satellite-pass boundary
in the same fetch -- would dump all of them onto the mesh essentially
back-to-back instead of at the intended <=1/60s cadence.

These tests exercise the fix: store._emit_event() now routes fire-family
Events (source in {"nifc", "firms"}, severity in {"priority", "immediate"})
through an attached FirePacer, mirroring the exact gate CentralConsumer._handle
applies, and leaves everything else (other native adapters, "routine"-severity
fire events, and the case where no pacer is attached at all) unchanged.
"""
from __future__ import annotations

import asyncio

import pytest

from meshai.config import EnvironmentalConfig
from meshai.env.store import EnvironmentalStore
from meshai.notifications.cutover import _clear_cache
from meshai.notifications.events import make_event
from meshai.notifications.pipeline.bus import EventBus
from meshai.notifications.pipeline.pacer import FirePacer


@pytest.fixture(autouse=True)
def _no_cutover(monkeypatch):
    """None of the categories used below are meant to hit the real gating
    deciders (this file is only exercising the pacer-routing gate); clearing
    cutover keeps store._emit_event's decider hook a no-op regardless of
    what earlier tests left in the environment / lru_cache."""
    monkeypatch.delenv("MESHAI_CUTOVER_CATEGORIES", raising=False)
    _clear_cache()
    yield
    _clear_cache()


class _StubAdapter:
    """Minimal adapter stand-in: to_event() always returns the same
    caller-controlled Event, independent of the raw dict passed in."""

    def __init__(self, source: str, severity: str, category: str):
        self._source = source
        self._severity = severity
        self._category = category

    def to_event(self, raw_evt: dict):
        eid = raw_evt["event_id"]
        return make_event(
            source=self._source,
            category=self._category,
            severity=self._severity,
            title=eid,
            summary=eid,
            group_key=eid,
        )


def _make_store():
    bus = EventBus()
    captured: list = []
    bus.subscribe(lambda e: captured.append(e))
    store = EnvironmentalStore(EnvironmentalConfig(), event_bus=bus)
    return store, bus, captured


class _FakePacer:
    """Records enqueue() calls without any real draining."""

    def __init__(self):
        self.calls: list = []

    def enqueue(self, event) -> None:
        self.calls.append(event)


def test_native_firms_immediate_event_routes_through_pacer():
    """source=firms, severity=immediate (a FIRMS growth/spotting broadcast)
    with a pacer attached must be enqueued on the pacer, and must NOT also
    reach the bus directly (no double-delivery)."""
    store, bus, captured = _make_store()
    pacer = _FakePacer()
    store._fire_pacer = pacer
    adapter = _StubAdapter(source="firms", severity="immediate",
                            category="wildfire_growth")

    store._emit_event(adapter, {"event_id": "g1"})

    assert len(pacer.calls) == 1
    assert pacer.calls[0].source == "firms"
    assert pacer.calls[0].severity == "immediate"
    assert captured == [], "must not ALSO be emitted straight to the bus"


def test_native_nifc_priority_event_routes_through_pacer():
    """source=nifc (WFIGS incident), severity=priority is also paced."""
    store, bus, captured = _make_store()
    pacer = _FakePacer()
    store._fire_pacer = pacer
    adapter = _StubAdapter(source="nifc", severity="priority",
                            category="unattributed_hotspot_cluster")

    store._emit_event(adapter, {"event_id": "n1"})

    assert len(pacer.calls) == 1
    assert pacer.calls[0].source == "nifc"
    assert captured == []


def test_native_fire_routine_severity_not_paced():
    """A fire-family event at "routine" severity (e.g. a FIRMS halt) is
    excluded from pacing -- exactly like the Central-path gate, which only
    covers priority/immediate."""
    store, bus, captured = _make_store()
    pacer = _FakePacer()
    store._fire_pacer = pacer
    adapter = _StubAdapter(source="firms", severity="routine",
                            category="wildfire_halted")

    store._emit_event(adapter, {"event_id": "h1"})

    assert pacer.calls == []
    assert len(captured) == 1
    assert captured[0].severity == "routine"


def test_non_fire_native_event_never_paced():
    """A non-fire native adapter (e.g. roads511) at immediate severity must
    go straight to the bus even with a pacer attached -- pacing is
    fire-family-only, keyed on event.source."""
    store, bus, captured = _make_store()
    pacer = _FakePacer()
    store._fire_pacer = pacer
    adapter = _StubAdapter(source="roads511", severity="immediate",
                            category="road_closure")

    store._emit_event(adapter, {"event_id": "r1"})

    assert pacer.calls == []
    assert len(captured) == 1
    assert captured[0].source == "roads511"


def test_no_pacer_attached_falls_back_to_direct_emit():
    """Pre-existing behavior is unaffected when no pacer is attached (e.g.
    notifications disabled, or the brief startup window in main.py before
    the pacer is constructed and wired in)."""
    store, bus, captured = _make_store()
    assert store._fire_pacer is None
    adapter = _StubAdapter(source="firms", severity="immediate",
                            category="wildfire_growth")

    store._emit_event(adapter, {"event_id": "g1"})

    assert len(captured) == 1
    assert captured[0].source == "firms"


def test_native_immediate_event_jumps_ahead_of_already_queued_priority_events():
    """End-to-end through a REAL FirePacer: two native "priority" fires
    queued first must not block a later native "immediate" one (head-of-line,
    issue #119's fix -- now reachable from the native ingest path), and
    nothing is ever dropped."""
    store, bus, emitted = _make_store()
    pacer = FirePacer(bus=bus, interval_seconds=0.01)
    store._fire_pacer = pacer

    p1 = _StubAdapter(source="nifc", severity="priority",
                       category="unattributed_hotspot_cluster")
    p2 = _StubAdapter(source="nifc", severity="priority",
                       category="unattributed_hotspot_cluster")
    imm = _StubAdapter(source="firms", severity="immediate",
                        category="wildfire_spotting")

    store._emit_event(p1, {"event_id": "priority-1"})
    store._emit_event(p2, {"event_id": "priority-2"})
    store._emit_event(imm, {"event_id": "immediate-1"})

    assert pacer.pending_count() == 3
    assert emitted == []

    async def _drive():
        await pacer.start()
        await asyncio.sleep(0.2)
        await pacer.stop()

    asyncio.run(_drive())

    assert [e.title for e in emitted] == [
        "immediate-1", "priority-1", "priority-2"]
