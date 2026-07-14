"""Grouper tests: coalescing (all severities) + periodic flush.

Note: there is deliberately NO immediate-severity bypass -- commit 85d48ce3
removed it so fire broadcasts obey rate control. The only pass-through is
"event has no group_key".
"""

from meshai.notifications.pipeline.grouper import Grouper
from meshai.notifications.events import make_event


class Recorder:
    def __init__(self):
        self.received = []

    def handle(self, event):
        self.received.append(event)


def _ev(severity, group_key="gk1"):
    return make_event(
        source="usgs_quake",
        category="earthquake_event",
        severity=severity,
        title=f"test {severity}",
        lat=42.6,
        lon=-114.5,
        group_key=group_key,
        inhibit_keys=[group_key],
    )


def test_immediate_severity_is_also_coalesced_no_bypass():
    """An immediate event WITH a group_key is held, like every other severity.

    The grouper used to exempt severity == "immediate" from the coalescing
    window. Commit 85d48ce3 ("fix(fire): remove immediate-severity exemption
    from grouper + cooldown") DELETED that bypass on purpose: fire events
    carry _severity_override="immediate", and the exemption meant they
    skipped the coalescer and zeroed the dispatcher cooldown, leaving fire
    with no rate control at all in normal live operation. Rate control now
    applies to ALL severities; the drain-mode pacer covers reconnect bursts.

    This test previously asserted the OLD bypass contract and had been red
    ever since. Re-adding the bypass to make it pass would re-open the fire
    broadcast-spam hole on a public-safety mesh -- so the test is what moves,
    not the source.
    """
    rec = Recorder()
    g = Grouper(next_handler=rec.handle, window_seconds=60.0)
    g.handle(_ev("immediate"))
    # Held for coalescing, NOT delivered straight through.
    assert rec.received == []
    assert g.held_count() == 1
    # The periodic flush (start_pipeline's _grouper_flush_loop) is what
    # eventually delivers it, once the window expires.
    g2 = Grouper(next_handler=rec.handle, window_seconds=0.0)
    g2.handle(_ev("immediate", group_key="gk2"))
    assert g2.tick() == 1
    assert len(rec.received) == 1
    assert rec.received[0].severity == "immediate"


def test_no_group_key_still_passes_through_immediately():
    """The ONE remaining bypass: an event with no group_key isn't coalesced
    (there's nothing to coalesce it against)."""
    rec = Recorder()
    g = Grouper(next_handler=rec.handle, window_seconds=60.0)
    g.handle(_ev("immediate", group_key=None))
    assert len(rec.received) == 1
    assert g.held_count() == 0


def test_periodic_flush_drains_routine():
    """A routine event is held, then released by tick() once its window passes."""
    rec = Recorder()
    g = Grouper(next_handler=rec.handle, window_seconds=0.0)  # 0s window -> tick drains now
    g.handle(_ev("routine"))
    # Held on arrival, not yet delivered.
    assert g.held_count() == 1
    assert rec.received == []
    # The periodic flush task calls tick(); simulate one tick.
    drained = g.tick()
    assert drained == 1
    assert len(rec.received) == 1
    assert rec.received[0].severity == "routine"
    assert g.held_count() == 0


def test_priority_is_also_coalesced_not_bypassed():
    """Priority events still buffer (only immediate bypasses)."""
    rec = Recorder()
    g = Grouper(next_handler=rec.handle, window_seconds=60.0)
    g.handle(_ev("priority"))
    assert rec.received == []
    assert g.held_count() == 1
