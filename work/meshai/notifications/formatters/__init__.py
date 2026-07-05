"""Formatter registry for the Phase-1+ mesh-message dispatch path.

Phase-1: earthquake_event registered (quake.py).
All other categories still fall through to the legacy compose_mesh_message
Mode-B path.

Usage (future phases):
    from meshai.notifications.formatters import register

    @register("earthquake_event")
    def _fmt_quake(event, *, now: float, budget: int) -> str:
        ...
"""

from typing import Callable, Optional

# Populated by per-category formatter modules imported below.
FORMATTERS: dict[str, Callable] = {}


def register(category: str, fn: Callable) -> Callable:
    """Register a formatter callable for a category (or family toggle name).

    Decorates or calls directly:
        register("earthquake_event", my_fn)
        @register("earthquake_event")
        def my_fn(...): ...
    """
    FORMATTERS[category] = fn
    return fn


def get_formatter(category: str) -> Optional[Callable]:
    """Return the formatter for *category*, or None if none is registered.

    Resolution order:
      1. Direct category match in FORMATTERS.
      2. Family/toggle fallback: look up the category's toggle name via
         get_toggle(), then check FORMATTERS for that toggle key.
      3. None — caller falls through to legacy Mode-B composition.
    """
    fn = FORMATTERS.get(category)
    if fn is not None:
        return fn
    # Family fallback — mirrors _category_label()'s toggle lookup in composer.py.
    try:
        from meshai.notifications.categories import get_toggle
        tog = get_toggle(category)
        if tog:
            fn = FORMATTERS.get(tog)
            if fn is not None:
                return fn
    except Exception:
        pass
    return None


# ── Phase-1 registrations ────────────────────────────────────────────────────
# Import triggers the @register decorator (or explicit register() call) in
# each formatter module.  Add one import per migrated category.

from meshai.notifications.formatters import quake as _quake_fmt_mod  # noqa: E402,F401
register("earthquake_event", _quake_fmt_mod.format)

from meshai.notifications.formatters import avalanche as _avy_fmt_mod  # noqa: E402,F401
register("avalanche_warning", _avy_fmt_mod.format)
register("avalanche_watch", _avy_fmt_mod.format)

# SWPC: geomagnetic_storm (swpc_kindex / native G-scale) and rf_propagation_alert
# (swpc_alerts flare / native R-scale).  solar_radiation_storm (proton) stays on
# the legacy Mode-B path — NOT registered here.
from meshai.notifications.formatters import swpc as _swpc_fmt_mod  # noqa: E402,F401
register("geomagnetic_storm", _swpc_fmt_mod.format)
register("rf_propagation_alert", _swpc_fmt_mod.format)

# Phase-2: NWS weather alerts (weather_warning + weather_statement).
# weather_watch and weather_advisory are not yet migrated (Phase-2 scope).
from meshai.notifications.formatters import nws as _nws_fmt_mod  # noqa: E402,F401
register("weather_warning", _nws_fmt_mod.format)
register("weather_statement", _nws_fmt_mod.format)

# Phase-2: incident / roads categories.
# One formatter handles all four; event.category drives the render path.
from meshai.notifications.formatters import incident as _incident_fmt_mod  # noqa: E402,F401
register("work_zone",          _incident_fmt_mod.format)
register("road_incident",      _incident_fmt_mod.format)
register("road_closure",       _incident_fmt_mod.format)
register("traffic_congestion", _incident_fmt_mod.format)
