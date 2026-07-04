"""Phase-0b: self-test the harness helpers.

Verifies:
  1. pinned_time freezes clock.now() to the given epoch.
  2. pinned_time restores the original after the block.
  3. pinned_tz sets and restores TZ (opt-in; tested in isolation).
  4. assert_byte_identical passes on equal strings, fails on differing ones.
  5. run_gate_sequence diffs correctly with two simple fake deciders.
  6. load_fixtures returns empty list for a non-existent hazard.
"""
from __future__ import annotations

import os
import time

import pytest

import meshai.notifications.clock as clock_mod
from tests.harness import (
    assert_byte_identical,
    load_fixtures,
    pinned_time,
    pinned_tz,
    run_gate_sequence,
)


# ---------------------------------------------------------------------------
# GateResult import for fake deciders
# ---------------------------------------------------------------------------

from meshai.notifications.gating.base import GateResult


# ---------------------------------------------------------------------------
# 1-2. pinned_time
# ---------------------------------------------------------------------------

_FROZEN = 1_700_000_000.0


class TestPinnedTime:
    def test_clock_now_is_frozen_inside_block(self):
        with pinned_time(_FROZEN):
            result = clock_mod.now()
        assert result == _FROZEN

    def test_clock_now_dt_is_frozen_inside_block(self):
        from datetime import timezone
        with pinned_time(_FROZEN):
            dt = clock_mod.now_dt(tz=timezone.utc)
        assert dt.timestamp() == pytest.approx(_FROZEN)

    def test_clock_now_restored_after_block(self):
        before = clock_mod.now
        with pinned_time(_FROZEN):
            pass
        # The function object is restored to the original callable.
        assert clock_mod.now is before

    def test_clock_now_restored_on_exception(self):
        original_now = clock_mod.now
        try:
            with pinned_time(_FROZEN):
                raise RuntimeError("test exception")
        except RuntimeError:
            pass
        # Must be restored even after exception.
        assert clock_mod.now is original_now

    def test_nested_pinned_time_restores_correctly(self):
        outer_epoch = 1_600_000_000.0
        inner_epoch = 1_700_000_000.0
        with pinned_time(outer_epoch):
            assert clock_mod.now() == outer_epoch
            with pinned_time(inner_epoch):
                assert clock_mod.now() == inner_epoch
            # outer is restored
            assert clock_mod.now() == outer_epoch

    def test_pinned_time_yields_epoch(self):
        with pinned_time(_FROZEN) as yielded:
            assert yielded == _FROZEN


# ---------------------------------------------------------------------------
# 3. pinned_tz
# ---------------------------------------------------------------------------

class TestPinnedTz:
    def test_sets_tz_inside_block(self):
        with pinned_tz("UTC"):
            assert os.environ.get("TZ") == "UTC"

    def test_restores_tz_after_block(self):
        orig = os.environ.get("TZ")
        with pinned_tz("UTC"):
            pass
        restored = os.environ.get("TZ")
        assert restored == orig  # both None, or both the original string

    def test_restores_tz_on_exception(self):
        orig = os.environ.get("TZ")
        try:
            with pinned_tz("UTC"):
                raise RuntimeError("test")
        except RuntimeError:
            pass
        assert os.environ.get("TZ") == orig

    def test_yields_name(self):
        with pinned_tz("America/Boise") as name:
            assert name == "America/Boise"


# ---------------------------------------------------------------------------
# 4. assert_byte_identical
# ---------------------------------------------------------------------------

class TestAssertByteIdentical:
    def test_passes_on_equal_strings(self):
        assert_byte_identical("hello world", "hello world")

    def test_passes_on_emoji_equal(self):
        s = "🔥 FIRE: Test Creek NE 1500ac 35% routine"
        assert_byte_identical(s, s)

    def test_fails_on_differing_content(self):
        with pytest.raises(AssertionError) as exc_info:
            assert_byte_identical("hello world!", "hello world")
        msg = str(exc_info.value)
        assert "golden" in msg or "new" in msg  # diff headers present

    def test_fails_on_byte_length_difference(self):
        # Same visible chars but different byte lengths via Unicode.
        a = "café"    # café (NFC, 2-byte é)
        b = "café"   # cafe + combining accent (decomposed, also visually café)
        # These have different UTF-8 byte lengths.
        if a.encode("utf-8") != b.encode("utf-8"):
            with pytest.raises(AssertionError):
                assert_byte_identical(a, b)
        else:
            # If the runtime normalized them, both forms happen to be equal.
            assert_byte_identical(a, b)

    def test_diff_message_is_readable(self):
        with pytest.raises(AssertionError) as exc_info:
            assert_byte_identical("new string", "golden string")
        msg = str(exc_info.value)
        # Should mention byte counts.
        assert "bytes" in msg

    def test_passes_on_empty_strings(self):
        assert_byte_identical("", "")


# ---------------------------------------------------------------------------
# 5. run_gate_sequence with fake deciders
# ---------------------------------------------------------------------------

class TestRunGateSequence:
    """Use two simple fake deciders to verify the diff logic."""

    # Fake old handler: broadcasts if the fixture has "broadcast": true.
    @staticmethod
    def _old_handler(fixture: dict, *, now: float) -> bool:
        return bool(fixture.get("broadcast", True))

    # Fake new decider: broadcasts if "new_broadcast" key is present and true.
    @staticmethod
    def _new_decider_agree(fixture: dict, *, now: float) -> GateResult:
        # Mirrors the old handler.
        broadcast = bool(fixture.get("broadcast", True))
        return GateResult(broadcast=broadcast, lifecycle="test", reason="agree")

    @staticmethod
    def _new_decider_disagree(fixture: dict, *, now: float) -> GateResult:
        # Always suppresses — will disagree when old handler broadcasts.
        return GateResult(broadcast=False, lifecycle="test", reason="disagree")

    def _make_fixtures(self, specs):
        """Build minimal fixture dicts."""
        return [{"broadcast": b} for b in specs]

    def _make_timeline(self, n):
        return [1_700_000_000.0 + i * 300 for i in range(n)]

    def test_all_agree(self):
        specs = [True, False, True]
        fixtures = self._make_fixtures(specs)
        timeline = self._make_timeline(len(fixtures))
        results = run_gate_sequence(
            self._old_handler,
            self._new_decider_agree,
            fixtures,
            timeline=timeline,
        )
        assert len(results) == 3
        for r in results:
            assert r["match"] is True, f"Expected match at fixture {r['fixture_n']}: {r}"
            assert r["diffs"] == {}

    def test_disagree_is_flagged(self):
        fixtures = self._make_fixtures([True, True, False])
        timeline = self._make_timeline(len(fixtures))
        results = run_gate_sequence(
            self._old_handler,
            self._new_decider_disagree,
            fixtures,
            timeline=timeline,
        )
        # fixture 0 and 1: old=True, new=False → mismatch
        assert results[0]["match"] is False
        assert results[0]["diffs"]["broadcast"] == {"old": True, "new": False}
        # fixture 2: old=False, new=False → match
        assert results[2]["match"] is True

    def test_result_keys_present(self):
        fixtures = self._make_fixtures([True])
        results = run_gate_sequence(
            self._old_handler,
            self._new_decider_agree,
            fixtures,
            timeline=self._make_timeline(1),
        )
        r = results[0]
        assert "fixture_n" in r
        assert "now" in r
        assert "old_broadcast" in r
        assert "new_broadcast" in r
        assert "match" in r
        assert "diffs" in r

    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError, match="timeline"):
            run_gate_sequence(
                self._old_handler,
                self._new_decider_agree,
                [{"broadcast": True}, {"broadcast": False}],
                timeline=[1_700_000_000.0],  # length mismatch
            )

    def test_decider_exception_treated_as_suppress(self):
        def _crashing_decider(fixture, *, now):
            raise RuntimeError("boom")

        fixtures = self._make_fixtures([True])
        results = run_gate_sequence(
            self._old_handler,
            _crashing_decider,
            fixtures,
            timeline=self._make_timeline(1),
        )
        # old=True, new=False (crashing decider → suppress) → mismatch
        assert results[0]["old_broadcast"] is True
        assert results[0]["new_broadcast"] is False
        assert results[0]["match"] is False

    def test_now_is_passed_to_handlers(self):
        received = []

        def _handler_capturing_now(fixture, *, now):
            received.append(("old", now))
            return True

        def _decider_capturing_now(fixture, *, now):
            received.append(("new", now))
            return GateResult(broadcast=True)

        timeline = [1_700_000_000.0, 1_700_000_300.0]
        run_gate_sequence(
            _handler_capturing_now,
            _decider_capturing_now,
            [{"x": 1}, {"x": 2}],
            timeline=timeline,
        )
        assert ("old", 1_700_000_000.0) in received
        assert ("old", 1_700_000_300.0) in received
        assert ("new", 1_700_000_000.0) in received

    def test_empty_fixtures(self):
        results = run_gate_sequence(
            self._old_handler,
            self._new_decider_agree,
            [],
            timeline=[],
        )
        assert results == []


# ---------------------------------------------------------------------------
# 6. load_fixtures for non-existent hazard
# ---------------------------------------------------------------------------

class TestLoadFixtures:
    def test_missing_hazard_returns_empty(self):
        result = load_fixtures("__nonexistent_hazard_xyzzy__")
        assert result == []
