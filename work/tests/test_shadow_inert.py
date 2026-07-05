"""Phase-0b: verify shadow hooks are inert by default and never mutate state.

Test cases:
  1. MESHAI_SHADOW_CATEGORIES unset → shadow_gate / shadow_render are pure no-ops
     (no filesystem, no DB, no exception).
  2. MESHAI_SHADOW_CATEGORIES set to a category with no decider registered →
     still no-op (get_decider returns None, early return).
  3. MESHAI_SHADOW_CATEGORIES set to a category with no formatter registered →
     still no-op (get_formatter returns None, early return).
  4. shadow_gate / shadow_render never raise regardless of input.
"""
from __future__ import annotations

import os
import pytest

import meshai.notifications.shadow as shadow_mod


def _reset_shadow_cache():
    """Clear lru_cache so env-var changes take effect."""
    shadow_mod._clear_enabled_cache()


# ---------------------------------------------------------------------------
# Helper: a minimal fake Event-like object for shadow_render
# ---------------------------------------------------------------------------

class _FakeEvent:
    id = "test-event-001"
    category = "earthquake_event"
    source = "usgs_quake"
    data: dict = {}


# ---------------------------------------------------------------------------
# Case 1: env var unset — must be completely off
# ---------------------------------------------------------------------------

class TestShadowInertWhenEnvUnset:
    """With MESHAI_SHADOW_CATEGORIES unset, all shadow functions are no-ops."""

    def setup_method(self):
        os.environ.pop("MESHAI_SHADOW_CATEGORIES", None)
        _reset_shadow_cache()

    def test_enabled_for_returns_false(self):
        assert shadow_mod.enabled_for("earthquake_event") is False
        assert shadow_mod.enabled_for("nws") is False
        assert shadow_mod.enabled_for("") is False

    def test_shadow_gate_no_filesystem(self, tmp_path, monkeypatch):
        """shadow_gate must not touch the filesystem when the env var is unset."""
        monkeypatch.setattr(shadow_mod, "_SHADOW_DIR", str(tmp_path / "shadow"))
        shadow_mod.shadow_gate(
            "earthquake_event",
            {"_severity_override": "immediate"},
            source="usgs_quake",
            now=1_700_000_000.0,
            old_broadcast=True,
        )
        # No shadow dir should have been created.
        assert not (tmp_path / "shadow").exists()

    def test_shadow_render_no_filesystem(self, tmp_path, monkeypatch):
        """shadow_render must not touch the filesystem when the env var is unset."""
        monkeypatch.setattr(shadow_mod, "_SHADOW_DIR", str(tmp_path / "shadow"))
        shadow_mod.shadow_render(
            "earthquake_event",
            _FakeEvent(),
            old_wire="🌍 EQ M4.2: Test, ID 30mi NE routine",
        )
        assert not (tmp_path / "shadow").exists()

    def test_shadow_gate_returns_none(self):
        result = shadow_mod.shadow_gate(
            "earthquake_event", {}, source="usgs", now=0.0, old_broadcast=False
        )
        assert result is None

    def test_shadow_render_returns_none(self):
        result = shadow_mod.shadow_render(
            "earthquake_event", _FakeEvent(), old_wire="something"
        )
        assert result is None


# ---------------------------------------------------------------------------
# Case 2: env var set but no decider registered for that category
# ---------------------------------------------------------------------------

class TestShadowInertWhenNoDecider:
    """MESHAI_SHADOW_CATEGORIES set but no decider registered → still no-op."""

    # A category name with NO decider and NO formatter registered, so the
    # "no decider" premise is genuinely true regardless of migration phase.
    _CATEGORY = "__no_such_category__"

    def setup_method(self):
        os.environ["MESHAI_SHADOW_CATEGORIES"] = self._CATEGORY
        _reset_shadow_cache()

    def teardown_method(self):
        os.environ.pop("MESHAI_SHADOW_CATEGORIES", None)
        _reset_shadow_cache()

    def test_enabled_for_returns_true(self):
        assert shadow_mod.enabled_for(self._CATEGORY) is True

    def test_shadow_gate_no_filesystem_when_no_decider(self, tmp_path, monkeypatch):
        """get_decider returns None → shadow_gate exits before any file write."""
        monkeypatch.setattr(shadow_mod, "_SHADOW_DIR", str(tmp_path / "shadow"))
        # No decider is registered for this category, so get_decider(...) returns
        # None and shadow_gate returns early without writing anything.
        shadow_mod.shadow_gate(
            self._CATEGORY,
            {"_dedup_suffix": "M4.2"},
            source="usgs_quake",
            now=1_700_000_000.0,
            old_broadcast=True,
        )
        assert not (tmp_path / "shadow").exists()

    def test_shadow_gate_does_not_raise(self):
        """shadow_gate must not propagate any exception."""
        try:
            shadow_mod.shadow_gate(
                self._CATEGORY,
                None,  # intentionally bad input — must not raise
                source="usgs_quake",
                now=1_700_000_000.0,
                old_broadcast=False,
            )
        except Exception as exc:
            pytest.fail(f"shadow_gate raised unexpectedly: {exc!r}")


# ---------------------------------------------------------------------------
# Case 3: env var set but no formatter registered for that category
# ---------------------------------------------------------------------------

class TestShadowRenderInertWhenNoFormatter:
    """MESHAI_SHADOW_CATEGORIES set but no formatter registered → still no-op.

    Note: earthquake_event has a formatter in Phase 1+; this class uses
    wildfire_incident which remains un-migrated and has no formatter entry.
    """

    _CATEGORY = "wildfire_incident"

    def setup_method(self):
        os.environ["MESHAI_SHADOW_CATEGORIES"] = self._CATEGORY
        _reset_shadow_cache()

    def teardown_method(self):
        os.environ.pop("MESHAI_SHADOW_CATEGORIES", None)
        _reset_shadow_cache()

    def test_shadow_render_no_filesystem_when_no_formatter(self, tmp_path, monkeypatch):
        """get_formatter returns None → shadow_render exits before any file write."""
        monkeypatch.setattr(shadow_mod, "_SHADOW_DIR", str(tmp_path / "shadow"))
        shadow_mod.shadow_render(
            self._CATEGORY,
            _FakeEvent(),
            old_wire="old wire string",
        )
        assert not (tmp_path / "shadow").exists()

    def test_shadow_render_does_not_raise(self):
        """shadow_render must not propagate any exception."""
        try:
            shadow_mod.shadow_render(
                self._CATEGORY,
                None,  # intentionally bad input — must not raise
                old_wire="anything",
            )
        except Exception as exc:
            pytest.fail(f"shadow_render raised unexpectedly: {exc!r}")


# ---------------------------------------------------------------------------
# Case 4: multiple categories, partial enable
# ---------------------------------------------------------------------------

class TestShadowPartialEnable:
    """Only listed categories are enabled; others stay off."""

    def setup_method(self):
        os.environ["MESHAI_SHADOW_CATEGORIES"] = "nws,earthquake_event"
        _reset_shadow_cache()

    def teardown_method(self):
        os.environ.pop("MESHAI_SHADOW_CATEGORIES", None)
        _reset_shadow_cache()

    def test_listed_category_is_enabled(self):
        assert shadow_mod.enabled_for("nws") is True
        assert shadow_mod.enabled_for("earthquake_event") is True

    def test_unlisted_category_is_disabled(self):
        assert shadow_mod.enabled_for("fire") is False
        assert shadow_mod.enabled_for("geomagnetic_storm") is False

    def test_shadow_gate_off_for_unlisted(self, tmp_path, monkeypatch):
        monkeypatch.setattr(shadow_mod, "_SHADOW_DIR", str(tmp_path / "shadow"))
        shadow_mod.shadow_gate(
            "fire", {}, source="wfigs", now=0.0, old_broadcast=True
        )
        assert not (tmp_path / "shadow").exists()


# ---------------------------------------------------------------------------
# Case 5: shadow_gate forwards `source` to the decider (regression guard)
# ---------------------------------------------------------------------------

class TestShadowGateForwardsSource:
    """shadow_gate must call the decider with the keyword-only `source` arg.

    The decider contract is `decide(data, *, source, now) -> GateResult`.
    A prior bug called `decider(data, now=now)`, omitting `source`, so every
    decider raised TypeError (swallowed by shadow_gate) and zero mismatch
    records were ever written.  This test registers a fake decider that
    asserts `source` was forwarded and returns broadcast=True; with
    old_broadcast=False that is a mismatch, so exactly one record must be
    captured.
    """

    _CATEGORY = "__shadow_test_cat__"

    def setup_method(self):
        # Enable shadow for the test-only category; ensure it is NOT cut over
        # (so is_cutover is False and shadow_gate does not early-return).
        os.environ["MESHAI_SHADOW_CATEGORIES"] = self._CATEGORY
        os.environ.pop("MESHAI_CUTOVER_CATEGORIES", None)
        _reset_shadow_cache()
        from meshai.notifications.cutover import _clear_cache as _clear_cutover_cache
        _clear_cutover_cache()

        # Register a fake decider that asserts the forwarded source.
        from meshai.notifications.gating import DECIDERS, register
        from meshai.notifications.gating.base import GateResult

        def _fake_decider(data, *, source, now):
            assert source == "test-src", f"source not forwarded: {source!r}"
            return GateResult(broadcast=True)

        self._DECIDERS = DECIDERS
        register(self._CATEGORY, _fake_decider)

    def teardown_method(self):
        # Pop the fake decider so it never leaks into other tests.
        self._DECIDERS.pop(self._CATEGORY, None)
        os.environ.pop("MESHAI_SHADOW_CATEGORIES", None)
        os.environ.pop("MESHAI_CUTOVER_CATEGORIES", None)
        _reset_shadow_cache()
        from meshai.notifications.cutover import _clear_cache as _clear_cutover_cache
        _clear_cutover_cache()

    def test_source_forwarded_and_mismatch_recorded(self, monkeypatch):
        # Capture records locally instead of touching the real shadow dir.
        captured = []
        monkeypatch.setattr(
            shadow_mod, "_append_jsonl",
            lambda category, record: captured.append(record),
        )

        result = shadow_mod.shadow_gate(
            self._CATEGORY, {}, source="test-src", now=0.0, old_broadcast=False
        )

        # Contract: returns None, does not raise.
        assert result is None
        # Exactly one mismatch record captured (new=True vs old=False).
        assert len(captured) == 1
        record = captured[0]
        assert record["new_broadcast"] is True
        assert record["old_broadcast"] is False
        assert record["source"] == "test-src"
