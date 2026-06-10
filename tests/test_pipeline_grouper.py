"""Phase 2.16.1 grouper tests: immediate bypass + periodic flush of routine."""

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


def test_immediate_severity_bypasses_grouper():
    """An immediate event with a group_key is delivered at once, not buffered."""
    rec = Recorder()
    g = Grouper(next_handler=rec.handle, window_seconds=60.0)
    g.handle(_ev("immediate"))
    # Delivered immediately, nothing held.
    assert len(rec.received) == 1
    assert rec.received[0].severity == "immediate"
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
