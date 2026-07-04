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
    """MESHAI_SHADOW_CATEGORIES set but DECIDERS empty → still no-op."""

    def setup_method(self):
        os.environ["MESHAI_SHADOW_CATEGORIES"] = "earthquake_event"
        _reset_shadow_cache()

    def teardown_method(self):
        os.environ.pop("MESHAI_SHADOW_CATEGORIES", None)
        _reset_shadow_cache()

    def test_enabled_for_returns_true(self):
        assert shadow_mod.enabled_for("earthquake_event") is True

    def test_shadow_gate_no_filesystem_when_no_decider(self, tmp_path, monkeypatch):
        """get_decider returns None → shadow_gate exits before any file write."""
        monkeypatch.setattr(shadow_mod, "_SHADOW_DIR", str(tmp_path / "shadow"))
        # DECIDERS is empty (Phase 0 scaffold), so get_decider("earthquake_event")
        # returns None and shadow_gate returns early without writing anything.
        shadow_mod.shadow_gate(
            "earthquake_event",
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
                "earthquake_event",
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
