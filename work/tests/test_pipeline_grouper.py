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


def test_wildfire_spotting_bypasses_the_grouper_even_with_group_key():
    """wildfire_spotting is a CATEGORY-scoped bypass (owner-approved): it
    skips the coalescing window entirely, even though it carries a
    group_key that would otherwise hold it. This is the one narrow
    exemption -- see _NEVER_COALESCE_CATEGORIES in grouper.py for why
    it's safe (source-side 1h per-fire cooldown) and why it must stay
    category-scoped, not severity-scoped."""
    rec = Recorder()
    g = Grouper(next_handler=rec.handle, window_seconds=60.0)
    ev = make_event(
        source="firms",
        category="wildfire_spotting",
        severity="immediate",
        title="test spotting",
        lat=42.6,
        lon=-114.5,
        group_key="fire-irwin-123",
    )
    g.handle(ev)
    # Passed straight through -- NOT held for the coalescing window.
    assert len(rec.received) == 1
    assert rec.received[0].category == "wildfire_spotting"
    assert g.held_count() == 0


def test_wildfire_growth_immediate_is_still_held_no_severity_bypass_crept_in():
    """A same-severity, same-fire-family event of a DIFFERENT category
    (wildfire_growth, not wildfire_spotting) must still be coalesced.
    This proves the new bypass is scoped to category and did not
    accidentally reintroduce a severity-based bypass (the exact bug
    commit 85d48ce3 removed)."""
    rec = Recorder()
    g = Grouper(next_handler=rec.handle, window_seconds=60.0)
    ev = make_event(
        source="wfigs",
        category="wildfire_growth",
        severity="immediate",
        title="test growth",
        lat=42.6,
        lon=-114.5,
        group_key="fire-irwin-123",
    )
    g.handle(ev)
    assert rec.received == []
    assert g.held_count() == 1


def test_wildfire_incident_immediate_is_still_held_no_severity_bypass_crept_in():
    """Same as above for wildfire_incident: only wildfire_spotting bypasses,
    every other fire category (even at immediate severity) is coalesced."""
    rec = Recorder()
    g = Grouper(next_handler=rec.handle, window_seconds=60.0)
    ev = make_event(
        source="wfigs",
        category="wildfire_incident",
        severity="immediate",
        title="test incident",
        lat=42.6,
        lon=-114.5,
        group_key="fire-irwin-123",
    )
    g.handle(ev)
    assert rec.received == []
    assert g.held_count() == 1


def test_wildfire_spotting_with_no_group_key_still_passes_through():
    """Existing no-group_key behavior is preserved for spotting too --
    the bypass condition is an `or`, not a replacement."""
    rec = Recorder()
    g = Grouper(next_handler=rec.handle, window_seconds=60.0)
    ev = make_event(
        source="firms",
        category="wildfire_spotting",
        severity="immediate",
        title="test spotting no key",
        lat=42.6,
        lon=-114.5,
        group_key=None,
    )
    g.handle(ev)
    assert len(rec.received) == 1
    assert g.held_count() == 0
