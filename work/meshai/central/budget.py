"""Re-export shim — implementation has moved to meshai.notifications.formatters._budget.

Every existing ``from meshai.central.budget import budget_for, fit_to_budget``
import continues to work unchanged.  This shim is the sole consumer of the
canonical implementation; update the implementation there, not here.
"""
from meshai.notifications.formatters._budget import budget_for, fit_to_budget

__all__ = ["budget_for", "fit_to_budget"]
