"""Gating/budgeting decision registry for the Phase-1+ dispatch path.

DECIDERS is intentionally empty at this phase (Phase 0 scaffold).
No category is migrated yet — all gating logic remains in the existing
handler modules (wfigs_handler, nws_handler, etc.).  Zero behavior change.

Usage (future phases):
    from meshai.notifications.gating import register
    from meshai.notifications.gating.base import GateResult

    @register("earthquake_event")
    def _gate_quake(event, *, now: float) -> GateResult:
        ...
"""

from typing import Callable, Optional

# Empty registry — populated by per-category gating modules (Phase 1+).
DECIDERS: dict = {}


def register(category: str, fn: Callable) -> Callable:
    """Register a gating callable for a category (or family toggle name)."""
    DECIDERS[category] = fn
    return fn


def get_decider(category: str) -> Optional[Callable]:
    """Return the gating decider for *category*, or None if none is registered.

    Resolution order:
      1. Direct category match in DECIDERS.
      2. Family/toggle fallback: look up the category's toggle name via
         get_toggle(), then check DECIDERS for that toggle key.
      3. None — caller falls through to legacy per-handler gating logic.
    """
    fn = DECIDERS.get(category)
    if fn is not None:
        return fn
    # Family fallback — mirrors the toggle lookup pattern in composer.py.
    try:
        from meshai.notifications.categories import get_toggle
        tog = get_toggle(category)
        if tog:
            fn = DECIDERS.get(tog)
            if fn is not None:
                return fn
    except Exception:
        pass
    return None
