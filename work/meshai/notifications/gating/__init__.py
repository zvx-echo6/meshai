"""Gating/budgeting decision registry for the Phase-1+ dispatch path.

Phase-1: earthquake_event registered (quake.py).
All other categories retain their existing handler-module gating.

Usage (future phases):
    from meshai.notifications.gating import register
    from meshai.notifications.gating.base import GateResult

    @register("earthquake_event")
    def _gate_quake(event, *, now: float) -> GateResult:
        ...
"""

from typing import Callable, Optional

# Populated by per-category gating modules imported below.
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


# ── Phase-1 registrations ────────────────────────────────────────────────────
from meshai.notifications.gating import quake as _quake_gate_mod  # noqa: E402,F401
register("earthquake_event", _quake_gate_mod.decide)

from meshai.notifications.gating import avalanche as _avy_gate_mod  # noqa: E402,F401
register("avalanche_warning", _avy_gate_mod.decide)
register("avalanche_watch", _avy_gate_mod.decide)

# SWPC: geomagnetic_storm (swpc_kindex / native G-scale) and rf_propagation_alert
# (swpc_alerts flare / native R-scale).  solar_radiation_storm (proton) stays on
# the legacy path — NOT registered here.
from meshai.notifications.gating import swpc as _swpc_gate_mod  # noqa: E402,F401
register("geomagnetic_storm", _swpc_gate_mod.decide)
register("rf_propagation_alert", _swpc_gate_mod.decide)
