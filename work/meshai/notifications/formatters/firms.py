"""FIRMS fire-fusion formatter — Phase-3c migration.

Reproduces the two FIRMS broadcast wire shapes that are NOT the WFIGS incident
render byte-identically, reading the render hints the decider stamps into
event.data:

  wildfire_spotting  ->  🔥 Possible spotting {dist:.1f} mi {dir} of {name} perimeter
  wildfire_halted    ->  🔥 {name} no growth in {hours}h

The third migrated FIRMS category, ``wildfire_growth``, reuses
``formatters/fire.py`` (its wire is the WFIGS incident render with a movement
dict) and is registered there — NOT here.

Neither legacy wire applied ``fit_to_budget`` (they were terse raw f-strings),
so this formatter deliberately does NOT budget-fit — it returns the raw wire to
stay byte-identical.  ``now`` / ``budget`` are accepted for signature parity and
unused (this formatter is clock-free per the Phase-0 purity guard).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from meshai.notifications.events import Event


def format(event: "Event", *, now: float, budget: int) -> str:
    """Render the FIRMS spotting/halt wire from canonical event.data.

    Branch selection is on event.category (falling back to data["category"]).
    """
    d = event.data or {}
    category = None
    try:
        category = event.category
    except Exception:
        category = None
    if category is None:
        category = d.get("category")

    if category == "wildfire_halted":
        name = d.get("incident_name") or "(unnamed fire)"
        hours = d.get("hours")
        return f"🔥 {name} no growth in {hours}h"

    # Default: wildfire_spotting.
    dist_mi = d.get("dist_mi")
    direction = d.get("direction")
    incident_name = d.get("incident_name")
    return (
        f"🔥 Possible spotting {dist_mi:.1f} mi {direction} of "
        f"{incident_name} perimeter"
    )
