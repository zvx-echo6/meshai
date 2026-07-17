"""Regression tests for the FIRMS FirePacer contract (issue #119).

Originally three sections (A/B/C) guarding issues #117-#119 in the path
from firms_handler's growth/spotting/halt/cluster fusion decisions to the
actual meshai Event that reaches the dispatcher + pacer. Sections A and B,
plus one test in section C, drove that path exclusively through
`meshai.central.consumer.CentralConsumer._normalize()`/`._handle()` -- the
Central NATS-consumer bridge, which has been deleted (production runs the
native env/firms.py -> firms_handler.ingest_hotspot_pixel fusion path
exclusively; see meshai.env.firms.FirmsAdapter._make_fusion_event, which
independently applies the same `_severity_override`-over-`severity`
resolution). Deleting CentralConsumer makes those tests uncollectable, and
since the mechanism they guarded (issues #117/#118) lived entirely inside
the now-dead consumer, they can no longer exist as tests of live behavior
-- git history preserves them.

The #117/#118 category+severity contract they exercised at the data_patch
level is independently covered against the LIVE native gating path in
tests/test_firms_refactor.py (asserts `data_patch["category"]` /
`data_patch["_severity_override"]` for growth/spotting/halt/cluster) and at
the Event/category level in tests/test_firms_native_fusion.py (drives the
real adapter tick() -> to_event() chain and asserts `ev.category`).

What remains here (issue #119, section C) is two tests that exercise the
FirePacer class directly with no dependency on CentralConsumer -- these are
native, standalone FirePacer unit tests (head-of-line ordering, no-drop
guarantee) and survive unchanged. The third section-C test (routing a real
FIRMS growth broadcast into a mocked pacer via CentralConsumer._handle) is
deleted along with A/B for the same reason; the equivalent native-path
routing guarantee (store._emit_event() -> FirePacer, for source="firms")
is already covered end-to-end in tests/test_native_fire_pacer.py.
"""
from __future__ import annotations

import asyncio

from meshai.notifications.events import make_event
from meshai.notifications.pipeline.pacer import FirePacer


# ═════════════════════════════════════════════════════════════════════════════
# FirePacer covers FIRMS; immediate jumps the queue; nothing is dropped
# (issue #119)
# ═════════════════════════════════════════════════════════════════════════════

class TestPacerCoversFirms:
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
