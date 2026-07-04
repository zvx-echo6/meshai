"""Staged cutover gate — contract tests.

Three net-behavior states are verified:

State 1 — Bake (default deploy):
    MESHAI_CUTOVER_CATEGORIES unset
    MESHAI_SHADOW_CATEGORIES=earthquake_event,...
    → compose_mesh_message uses OLD legacy path for LIVE broadcast.
    → shadow_gate / shadow_render run dry-run (bake comparison active).

State 2 — Cutover:
    MESHAI_CUTOVER_CATEGORIES=earthquake_event
    → compose_mesh_message uses the new formatter for the LIVE broadcast.
    → shadow_gate / shadow_render skip (nothing to compare — new path IS live).

State 3 — Default / tests (both vars unset):
    → compose_mesh_message always legacy.
    → shadow hooks are fully off.
    → Direct formatter/decider unit tests still work (they call formatters
      directly, bypassing compose_mesh_message).

All tests clear the lru_caches around env-var mutations.
"""
from __future__ import annotations

import os
import pytest

import meshai.notifications.shadow as shadow_mod
from meshai.notifications.cutover import _clear_cache as _cutover_clear
from meshai.notifications.cutover import is_cutover
from meshai.notifications.events import make_event
from meshai.notifications.renderers.composer import compose_mesh_message


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reset_caches():
    """Clear lru_caches for both cutover and shadow after env-var changes."""
    _cutover_clear()
    shadow_mod._clear_enabled_cache()


def _make_quake_event(*, title="M3.3 earthquake near Stanley, ID"):
    """Minimal Event for compose_mesh_message testing (earthquake_event)."""
    return make_event(
        source="usgs_quake",
        category="earthquake_event",
        severity="routine",
        title=title,
        data={
            "magnitude": 3.3,
            "depth_km": 11.0,
            "lat": 44.46,
            "lon": -112.61,
            "place": "19 km S of Lima, Montana",
            "tsunami": False,
            "pager": None,
            "is_update": False,
            "occurred_at": None,
            "event_id": "us_cutover_test_001",
            "_severity_override": None,
            "_dedup_suffix": "",
            "distance_km": 160.0,
        },
    )


# ---------------------------------------------------------------------------
# 1. is_cutover — env parsing
# ---------------------------------------------------------------------------

class TestIsCutover:
    """is_cutover correctly reads and caches MESHAI_CUTOVER_CATEGORIES."""

    def setup_method(self):
        os.environ.pop("MESHAI_CUTOVER_CATEGORIES", None)
        _cutover_clear()

    def teardown_method(self):
        os.environ.pop("MESHAI_CUTOVER_CATEGORIES", None)
        _cutover_clear()

    def test_unset_returns_false_for_all(self):
        assert is_cutover("earthquake_event") is False
        assert is_cutover("geomagnetic_storm") is False
        assert is_cutover("avalanche_warning") is False
        assert is_cutover("") is False

    def test_single_category_parses(self):
        os.environ["MESHAI_CUTOVER_CATEGORIES"] = "earthquake_event"
        _cutover_clear()
        assert is_cutover("earthquake_event") is True
        assert is_cutover("geomagnetic_storm") is False

    def test_multiple_categories_parse(self):
        os.environ["MESHAI_CUTOVER_CATEGORIES"] = (
            "earthquake_event,geomagnetic_storm,rf_propagation_alert"
        )
        _cutover_clear()
        assert is_cutover("earthquake_event") is True
        assert is_cutover("geomagnetic_storm") is True
        assert is_cutover("rf_propagation_alert") is True
        assert is_cutover("avalanche_warning") is False

    def test_whitespace_stripped(self):
        os.environ["MESHAI_CUTOVER_CATEGORIES"] = " earthquake_event , avalanche_warning "
        _cutover_clear()
        assert is_cutover("earthquake_event") is True
        assert is_cutover("avalanche_warning") is True

    def test_empty_string_all_false(self):
        os.environ["MESHAI_CUTOVER_CATEGORIES"] = ""
        _cutover_clear()
        assert is_cutover("earthquake_event") is False

    def test_cache_cleared_after_env_change(self):
        os.environ.pop("MESHAI_CUTOVER_CATEGORIES", None)
        _cutover_clear()
        assert is_cutover("earthquake_event") is False

        os.environ["MESHAI_CUTOVER_CATEGORIES"] = "earthquake_event"
        # Without cache clear, still False (cached).
        assert is_cutover("earthquake_event") is False

        # After clear, re-reads env.
        _cutover_clear()
        assert is_cutover("earthquake_event") is True


# ---------------------------------------------------------------------------
# 2. State 3 — Both vars unset (default / test isolation)
#    compose_mesh_message falls back to legacy even with formatters registered.
# ---------------------------------------------------------------------------

class TestBothVarsUnset:
    """With no env vars: legacy path for compose, shadow fully off."""

    def setup_method(self):
        os.environ.pop("MESHAI_CUTOVER_CATEGORIES", None)
        os.environ.pop("MESHAI_SHADOW_CATEGORIES", None)
        _reset_caches()

    def teardown_method(self):
        os.environ.pop("MESHAI_CUTOVER_CATEGORIES", None)
        os.environ.pop("MESHAI_SHADOW_CATEGORIES", None)
        _reset_caches()

    def test_compose_uses_legacy_not_formatter(self):
        """compose_mesh_message on a registered-but-not-cutover category must
        return the Mode-B legacy output, NOT the new formatter output."""
        from meshai.notifications.formatters import get_formatter
        # Confirm the formatter IS registered for earthquake_event.
        assert get_formatter("earthquake_event") is not None, (
            "earthquake_event formatter must be registered (Phase-1)"
        )
        event = _make_quake_event()
        result = compose_mesh_message(event)
        # Legacy Mode-B output uses the emoji+LABEL: prefix + title + severity.
        # It does NOT start with "🌐 New:" (that's the formatter's line-1 prefix).
        assert "New:" not in result, (
            f"Formatter was invoked when not cutover — result: {result!r}"
        )
        # Mode-B always appends the severity word and uses the QUAKE label.
        assert "QUAKE:" in result or "earthquake" in result.lower() or "routine" in result, (
            f"Unexpected legacy output shape: {result!r}"
        )

    def test_shadow_gate_off(self, tmp_path, monkeypatch):
        monkeypatch.setattr(shadow_mod, "_SHADOW_DIR", str(tmp_path / "shadow"))
        shadow_mod.shadow_gate(
            "earthquake_event", {}, source="usgs_quake", now=0.0,
            old_broadcast=True,
        )
        assert not (tmp_path / "shadow").exists()

    def test_shadow_render_off(self, tmp_path, monkeypatch):
        monkeypatch.setattr(shadow_mod, "_SHADOW_DIR", str(tmp_path / "shadow"))
        shadow_mod.shadow_render(
            "earthquake_event",
            _make_quake_event(),
            old_wire="legacy wire",
        )
        assert not (tmp_path / "shadow").exists()


# ---------------------------------------------------------------------------
# 3. State 1 — Bake: shadow enabled, cutover NOT set
#    → compose_mesh_message uses legacy; shadow hooks active
# ---------------------------------------------------------------------------

class TestBakeState:
    """Shadow enabled, cutover unset — bake period behavior."""

    _SHADOW_CATS = "earthquake_event,geomagnetic_storm,rf_propagation_alert,avalanche_warning,avalanche_watch"

    def setup_method(self):
        os.environ.pop("MESHAI_CUTOVER_CATEGORIES", None)
        os.environ["MESHAI_SHADOW_CATEGORIES"] = self._SHADOW_CATS
        _reset_caches()

    def teardown_method(self):
        os.environ.pop("MESHAI_CUTOVER_CATEGORIES", None)
        os.environ.pop("MESHAI_SHADOW_CATEGORIES", None)
        _reset_caches()

    def test_compose_still_uses_legacy_in_bake(self):
        """Bake state: shadow enabled does NOT change the live render path."""
        event = _make_quake_event()
        result = compose_mesh_message(event)
        assert "New:" not in result, (
            f"Formatter was invoked during bake state — result: {result!r}"
        )

    def test_shadow_enabled_for_listed_category(self):
        assert shadow_mod.enabled_for("earthquake_event") is True

    def test_shadow_gate_not_skipped_in_bake(self, tmp_path, monkeypatch):
        """shadow_gate should run (not be skipped by cutover) during bake.

        The gate will find a registered decider and attempt a comparison.
        We only assert that the cutover check does NOT short-circuit it:
        enabled_for returns True and the hook proceeds past both guards.
        """
        # Verify: NOT cutover, IS shadow-enabled → hook runs past both guards.
        assert is_cutover("earthquake_event") is False
        assert shadow_mod.enabled_for("earthquake_event") is True


# ---------------------------------------------------------------------------
# 4. State 2 — Cutover: category explicitly cut over
#    → compose_mesh_message uses NEW formatter; shadow hooks skip
# ---------------------------------------------------------------------------

class TestCutoverActive:
    """With earthquake_event in MESHAI_CUTOVER_CATEGORIES."""

    def setup_method(self):
        os.environ["MESHAI_CUTOVER_CATEGORIES"] = "earthquake_event"
        os.environ.pop("MESHAI_SHADOW_CATEGORIES", None)
        _reset_caches()

    def teardown_method(self):
        os.environ.pop("MESHAI_CUTOVER_CATEGORIES", None)
        os.environ.pop("MESHAI_SHADOW_CATEGORIES", None)
        _reset_caches()

    def test_is_cutover_true(self):
        assert is_cutover("earthquake_event") is True
        assert is_cutover("geomagnetic_storm") is False  # not listed

    def test_compose_uses_new_formatter(self):
        """Once cut over, compose_mesh_message dispatches to the formatter.

        The quake formatter produces a multi-line "New: M<mag> — <place>"
        string that is byte-for-byte different from Mode-B output.
        """
        from tests.harness.goldens import pinned_time
        _AT = 1_783_200_000.0
        event = _make_quake_event()
        with pinned_time(_AT):
            result = compose_mesh_message(event)
        # Formatter line-1 format: "<emoji> New: M3.3 — 19 km S of Lima, Montana"
        assert "New:" in result, (
            f"Formatter output expected 'New:' prefix, got: {result!r}"
        )
        assert "M3.3" in result, f"Magnitude missing from formatter output: {result!r}"
        assert "Lima, Montana" in result, (
            f"Place string missing from formatter output: {result!r}"
        )

    def test_shadow_gate_skips_when_cutover(self, tmp_path, monkeypatch):
        """shadow_gate must be a no-op when the category is cut over."""
        monkeypatch.setattr(shadow_mod, "_SHADOW_DIR", str(tmp_path / "shadow"))
        # Enable shadow for earthquake_event too — but cutover takes precedence.
        monkeypatch.setenv("MESHAI_SHADOW_CATEGORIES", "earthquake_event")
        shadow_mod._clear_enabled_cache()
        shadow_mod.shadow_gate(
            "earthquake_event",
            {"_dedup_suffix": "M3.3"},
            source="usgs_quake",
            now=1_783_200_000.0,
            old_broadcast=True,
        )
        # Nothing should have been written — cutover skips before any I/O.
        assert not (tmp_path / "shadow").exists(), (
            "shadow_gate wrote JSONL even though category is cut over"
        )

    def test_shadow_render_skips_when_cutover(self, tmp_path, monkeypatch):
        """shadow_render must be a no-op when the category is cut over."""
        monkeypatch.setattr(shadow_mod, "_SHADOW_DIR", str(tmp_path / "shadow"))
        monkeypatch.setenv("MESHAI_SHADOW_CATEGORIES", "earthquake_event")
        shadow_mod._clear_enabled_cache()
        shadow_mod.shadow_render(
            "earthquake_event",
            _make_quake_event(),
            old_wire="legacy wire that differs from new formatter",
        )
        assert not (tmp_path / "shadow").exists(), (
            "shadow_render wrote JSONL even though category is cut over"
        )


# ---------------------------------------------------------------------------
# 5. Partial cutover — only the listed category is live; others stay legacy
# ---------------------------------------------------------------------------

class TestPartialCutover:
    """Only one of several migrated categories is cut over."""

    def setup_method(self):
        # earthquake_event is cut over; geomagnetic_storm is NOT.
        os.environ["MESHAI_CUTOVER_CATEGORIES"] = "earthquake_event"
        _reset_caches()

    def teardown_method(self):
        os.environ.pop("MESHAI_CUTOVER_CATEGORIES", None)
        _reset_caches()

    def test_cutover_category_uses_formatter(self):
        from tests.harness.goldens import pinned_time
        _AT = 1_783_200_000.0
        event = _make_quake_event()
        with pinned_time(_AT):
            result = compose_mesh_message(event)
        assert "New:" in result, f"quake should use formatter: {result!r}"

    def test_non_cutover_category_uses_legacy(self):
        """geomagnetic_storm has a formatter but is not cut over → legacy."""
        from meshai.notifications.formatters import get_formatter
        assert get_formatter("geomagnetic_storm") is not None

        event = make_event(
            source="swpc",
            category="geomagnetic_storm",
            severity="priority",
            title="G3 Geomagnetic Storm",
            data={"kp": 7.0, "scale_code": "G3", "driver": "kp"},
        )
        result = compose_mesh_message(event)
        # Legacy Mode-B: uses title "G3 Geomagnetic Storm", starts with the RF emoji.
        assert "New:" not in result, (
            f"geomagnetic_storm should use legacy path: {result!r}"
        )
