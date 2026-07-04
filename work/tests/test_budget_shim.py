"""Phase-0: verify the budget.py re-export shim is identity-equal to the impl.

Both import paths must resolve to the SAME function objects so any runtime
patching (e.g. in existing tests) affects both paths simultaneously.
"""

import meshai.central.budget as _shim
import meshai.notifications.formatters._budget as _impl


def test_budget_for_is_same_object():
    assert _shim.budget_for is _impl.budget_for, (
        "meshai.central.budget.budget_for must be the same object as "
        "meshai.notifications.formatters._budget.budget_for"
    )


def test_fit_to_budget_is_same_object():
    assert _shim.fit_to_budget is _impl.fit_to_budget, (
        "meshai.central.budget.fit_to_budget must be the same object as "
        "meshai.notifications.formatters._budget.fit_to_budget"
    )


def test_shim_imports_work():
    """Smoke: the public names are importable from the legacy path."""
    from meshai.central.budget import budget_for, fit_to_budget  # noqa: F401
    assert callable(budget_for)
    assert callable(fit_to_budget)


def test_budget_for_returns_default_without_adapter_config(monkeypatch):
    """budget_for falls back to 140 when the adapter key is absent."""
    val = _shim.budget_for("__no_such_adapter__")
    assert val == 140
