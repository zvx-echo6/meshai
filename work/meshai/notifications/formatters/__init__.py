"""Formatter registry for the Phase-1+ mesh-message dispatch path.

FORMATTERS is intentionally empty at this phase (Phase 0 scaffold).
No category is migrated yet — all events fall through to the legacy
compose_mesh_message Mode-B path.  Zero behavior change.

Usage (future phases):
    from meshai.notifications.formatters import register

    @register("earthquake_event")
    def _fmt_quake(event, *, now: float, budget: int) -> str:
        ...
"""

from typing import Callable, Optional

# Empty registry — populated by per-category formatter modules (Phase 1+).
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
