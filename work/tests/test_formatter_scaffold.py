"""Phase-0 scaffold tests: formatter registry empty + dispatch correctness.

(a) With an empty registry, get_formatter returns None for real categories.
(b) A registered dummy formatter is called verbatim — multi-line output is
    returned as-is (no Mode-B single-line cap applied).
"""

import pytest

from meshai.notifications.formatters import FORMATTERS, get_formatter, register
from meshai.notifications.events import make_event
from meshai.notifications.renderers.composer import compose_mesh_message


# ── (a) empty registry returns None for known categories ──────────────────────

@pytest.mark.parametrize("category", [
    # earthquake_event removed: Phase-1 registers formatters.quake for it.
    # weather_warning/weather_statement removed: Phase-2 registers formatters.nws.
    # road_closure/work_zone/road_incident/traffic_congestion removed: Phase-2 registers formatters.incident.
    "wildfire_incident",
    "battery_critical",
])
def test_get_formatter_returns_none_while_registry_empty(category):
    """Un-migrated categories must still return None from get_formatter."""
    # Only categories not yet migrated to the formatter+gater architecture.
    assert category not in FORMATTERS, (
        f"Category {category!r} should not be in FORMATTERS yet (not migrated)"
    )
    assert get_formatter(category) is None


# ── (b) registered dummy formatter is dispatched verbatim ────────────────────

_SYNTHETIC_CATEGORY = "_test_scaffold_dummy_category_phase0"
_MULTILINE_OUTPUT = "Line one\nLine two\nLine three"


def _dummy_formatter(event, *, now: float, budget: int) -> str:
    """Returns a fixed multi-line string to prove verbatim passthrough."""
    return _MULTILINE_OUTPUT


def test_registered_formatter_returns_verbatim_multiline(monkeypatch):
    """Register a dummy for a synthetic category; compose_mesh_message must
    return the dummy's multi-line output verbatim — newlines preserved — not
    re-processed through Mode-B's single-line budget loop.

    Cutover gate: the formatter is only dispatched when the category appears in
    MESHAI_CUTOVER_CATEGORIES.  This test sets the var to verify the formatter
    IS invoked once cut over (the complementary not-cutover case is covered by
    test_cutover_gate.py).
    """
    from meshai.notifications.cutover import _clear_cache as _cutover_clear
    # Mark synthetic category as cut over for this test.
    monkeypatch.setenv("MESHAI_CUTOVER_CATEGORIES", _SYNTHETIC_CATEGORY)
    _cutover_clear()
    # Register the dummy (clean up afterwards to avoid cross-test pollution).
    register(_SYNTHETIC_CATEGORY, _dummy_formatter)
    try:
        event = make_event(
            source="test",
            category=_SYNTHETIC_CATEGORY,
            severity="routine",
            title="First line\nSecond line",  # multi-line title
        )
        result = compose_mesh_message(event)
        assert result == _MULTILINE_OUTPUT, (
            f"Expected verbatim multi-line output, got: {result!r}"
        )
        # Verify newlines are preserved (Mode-B would strip them).
        assert "\n" in result, "Newlines must survive the formatter dispatch path"
    finally:
        FORMATTERS.pop(_SYNTHETIC_CATEGORY, None)
        _cutover_clear()  # restore cache for subsequent tests
