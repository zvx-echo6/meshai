"""FIRMS fire-fusion formatter — Phase-3c migration.

Reproduces the two FIRMS broadcast wire shapes that are NOT the WFIGS incident
render, reading the render hints the decider stamps into event.data:

  wildfire_spotting  ->  🔥 Possible spotting {dist:.1f} mi {dir} of {name} perimeter
  wildfire_halted    ->  🔥 {name} no growth in {hours}h

The third migrated FIRMS category, ``wildfire_growth``, reuses
``formatters/fire.py`` (its wire is the WFIGS incident render with a movement
dict) and is registered there — NOT here.

Neither legacy wire applied ``fit_to_budget`` (they were terse raw f-strings),
so this formatter deliberately does NOT budget-fit an unmatched fire — it
returns the raw wire to stay byte-identical. When the fire (``data["irwin_id"]``
-- present in both event.data shapes, stamped by the gating deciders in
notifications/gating/firms.py) is Watch Duty-matched, a trailing incident-link
line IS budget-fit via ``fit_to_budget_with_suffix`` (using the same budget
source formatters/fire.py uses, ``budget_for("wfigs")``) so the link is never
cut. ``now`` is accepted for signature parity and unused (this formatter is
clock-free per the Phase-0 purity guard); ``budget`` is likewise accepted but
unused -- the wildfire_growth wire (formatters/fire.py) is the only FIRMS
shape with a live budget contract to preserve, and the caller's ``budget`` for
this formatter is not ``budget_for("wfigs")``.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from meshai.env.watchduty import incident_url
from meshai.notifications.formatters._budget import budget_for, fit_to_budget_with_suffix
from meshai.notifications.formatters.fire import watchduty_info

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

    wd = watchduty_info(d.get("irwin_id"))

    if category == "wildfire_halted":
        name = (wd.get("name") if wd else None) or d.get("incident_name") or "(unnamed fire)"
        hours = d.get("hours")
        wire = f"🔥 {name} no growth in {hours}h"
    else:
        # Default: wildfire_spotting.
        dist_mi = d.get("dist_mi")
        direction = d.get("direction")
        incident_name = (wd.get("name") if wd else None) or d.get("incident_name")
        wire = (
            f"🔥 Possible spotting {dist_mi:.1f} mi {direction} of "
            f"{incident_name} perimeter"
        )

    if wd:
        return fit_to_budget_with_suffix(wire, incident_url(wd["id"]), budget_for("wfigs"))
    return wire
