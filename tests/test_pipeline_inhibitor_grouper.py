"""Tests for Phase 2.2 inhibitor and grouper."""

import pytest
from unittest.mock import Mock

from meshai.notifications.events import Event
from meshai.notifications.pipeline.inhibitor import Inhibitor
from meshai.notifications.pipeline.grouper import Grouper


def make_event(id, severity, inhibit_keys=None, group_key=None):
    return Event(
        id=id,
        source="test",
        category="test_cat",
        severity=severity,
        title=f"Event {id}",
        inhibit_keys=inhibit_keys or [],
        group_key=group_key,
    )


# ===================== INHIBITOR TESTS =====================

class TestInhibitor:

    def test_event_without_inhibit_keys_passes_through(self):
        next_handler = Mock()
        inhibitor = Inhibitor(next_handler)
        event = make_event("e1", "immediate", inhibit_keys=[])
        inhibitor.handle(event)
        next_handler.assert_called_once_with(event)

    def test_lower_severity_after_higher_is_suppressed(self):
        next_handler = Mock()
        inhibitor = Inhibitor(next_handler)
        ev1 = make_event("e1", "immediate", inhibit_keys=["battery:NODE1"])
        ev2 = make_event("e2", "priority", inhibit_keys=["battery:NODE1"])
        inhibitor.handle(ev1)
        inhibitor.handle(ev2)
        assert next_handler.call_count == 1
        next_handler.assert_called_once_with(ev1)

    def test_equal_severity_is_suppressed(self):
        next_handler = Mock()
        inhibitor = Inhibitor(next_handler)
        ev1 = make_event("e1", "priority", inhibit_keys=["key1"])
        ev2 = make_event("e2", "priority", inhibit_keys=["key1"])
        inhibitor.handle(ev1)
        inhibitor.handle(ev2)
        assert next_handler.call_count == 1

    def test_higher_severity_after_lower_passes_and_upgrades(self):
        next_handler = Mock()
        inhibitor = Inhibitor(next_handler)
        ev1 = make_event("e1", "priority", inhibit_keys=["key1"])
        ev2 = make_event("e2", "immediate", inhibit_keys=["key1"])
        inhibitor.handle(ev1)
        inhibitor.handle(ev2)
        assert next_handler.call_count == 2
        keys = inhibitor.active_keys()
        assert keys["key1"][0] == 2  # immediate rank

    def test_inhibit_key_expires_after_ttl(self):
        next_handler = Mock()
        inhibitor = Inhibitor(next_handler, ttl_seconds=10)
        current_time = [0.0]
        inhibitor._now = lambda: current_time[0]

        ev1 = make_event("e1", "immediate", inhibit_keys=["key1"])
        current_time[0] = 0.0
        inhibitor.handle(ev1)

        current_time[0] = 15.0
        ev2 = make_event("e2", "routine", inhibit_keys=["key1"])
        inhibitor.handle(ev2)

        assert next_handler.call_count == 2

    def test_multiple_keys_any_active_suppresses(self):
        next_handler = Mock()
        inhibitor = Inhibitor(next_handler)

        ev1 = make_event("e1", "immediate", inhibit_keys=["a", "b"])
        inhibitor.handle(ev1)
        assert next_handler.call_count == 1

        ev2 = make_event("e2", "routine", inhibit_keys=["b", "c"])
        inhibitor.handle(ev2)
        assert next_handler.call_count == 1  # suppressed by "b"

        ev3 = make_event("e3", "routine", inhibit_keys=["c", "d"])
        inhibitor.handle(ev3)
        assert next_handler.call_count == 2  # passes, no active key


# ===================== GROUPER TESTS =====================

class TestGrouper:

    def test_event_without_group_key_emits_immediately(self):
        next_handler = Mock()
        grouper = Grouper(next_handler)
        event = make_event("e1", "immediate", group_key=None)
        grouper.handle(event)
        next_handler.assert_called_once_with(event)

    def test_event_with_group_key_is_held_not_emitted(self):
        next_handler = Mock()
        grouper = Grouper(next_handler)
        event = make_event("e1", "immediate", group_key="fire:42")
        grouper.handle(event)
        next_handler.assert_not_called()
        assert grouper.held_count() == 1

    def test_second_same_group_key_replaces_first(self):
        next_handler = Mock()
        grouper = Grouper(next_handler)
        ev1 = make_event("e1", "immediate", group_key="fire:42")
        ev2 = make_event("e2", "immediate", group_key="fire:42")
        grouper.handle(ev1)
        grouper.handle(ev2)
        next_handler.assert_not_called()
        assert grouper.held_count() == 1
        grouper.flush_all()
        assert next_handler.call_count == 1
        emitted_event = next_handler.call_args[0][0]
        assert emitted_event.id == "e2"

    def test_tick_emits_when_window_expired(self):
        next_handler = Mock()
        grouper = Grouper(next_handler, window_seconds=5)
        current_time = [0.0]
        grouper._now = lambda: current_time[0]

        current_time[0] = 0.0
        event = make_event("e1", "immediate", group_key="g")
        grouper.handle(event)
        assert grouper.held_count() == 1

        current_time[0] = 3.0
        grouper.tick()
        next_handler.assert_not_called()
        assert grouper.held_count() == 1

        current_time[0] = 10.0
        grouper.tick()
        next_handler.assert_called_once()
        assert grouper.held_count() == 0

    def test_flush_all_emits_everything_immediately(self):
        next_handler = Mock()
        grouper = Grouper(next_handler)
        ev1 = make_event("e1", "immediate", group_key="g1")
        ev2 = make_event("e2", "immediate", group_key="g2")
        ev3 = make_event("e3", "immediate", group_key="g3")
        grouper.handle(ev1)
        grouper.handle(ev2)
        grouper.handle(ev3)
        assert grouper.held_count() == 3
        grouper.flush_all()
        assert next_handler.call_count == 3
        assert grouper.held_count() == 0


# ===================== INTEGRATION TEST =====================

class TestInhibitorGrouperChain:

    def test_inhibitor_then_grouper_chain(self):
        terminal = Mock()
        grouper = Grouper(next_handler=terminal)
        inhibitor = Inhibitor(next_handler=grouper.handle)

        # Send immediate event with group_key and inhibit_keys
        ev1 = make_event("e1", "immediate", group_key="g1", inhibit_keys=["k1"])
        inhibitor.handle(ev1)
        # After inhibitor: passed (no prior key)
        # After grouper: held (group_key present)
        terminal.assert_not_called()
        assert grouper.held_count() == 1

        # Send routine event with same group_key and inhibit_keys
        ev2 = make_event("e2", "routine", group_key="g1", inhibit_keys=["k1"])
        inhibitor.handle(ev2)
        # After inhibitor: SUPPRESSED (k1 active at higher rank)
        terminal.assert_not_called()
        assert grouper.held_count() == 1  # still 1, not 2

        # Flush grouper
        grouper.flush_all()
        terminal.assert_called_once()
        emitted = terminal.call_args[0][0]
        assert emitted.id == "e1"  # the immediate, not suppressed routine
