"""Watch Duty evacuation-alert formatter — Group B.

Renders the wire string for a "wildfire_evac" Event, reading the canonical
schema ``gating/watchduty.py::decide_evac`` writes into ``event.data``
(the reading dict, plus the decider's ``kind`` stamp):

    name, level, zone_text, wd_event_id, kind

Header line depends on ``kind``:
    order       -> "EVACUATION ORDER: {name}"
    warning     -> "EVACUATION WARNING: {name}"
    downgraded  -> "Evacuation order downgraded to warning: {name}"
    lifted      -> "Evacuations lifted: {name}"
    updated     -> "EVACUATION {ORDER|WARNING} UPDATED: {name}" (level picks
                   ORDER vs WARNING)

Body: header, plus a second line with ``zone_text`` when it is non-empty and
``kind`` is not "lifted" (an all-clear has nothing left to describe). Fit via
``fit_to_budget_with_suffix`` so Watch Duty's own incident link always
survives intact on its own trailing line, exactly like the WFIGS fire
formatter's Watch Duty link handling.

Time contract: ``now`` is accepted but unused. ``budget`` is injected -- the
caller supplies ``budget_for("watchduty")`` (main.py overrides
``adapter_config.watchduty.single_packet_max_chars`` to the live transport's
``max_chars``, same as every other broadcast adapter).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from meshai.env.watchduty import incident_url
from meshai.notifications.formatters._budget import fit_to_budget_with_suffix

if TYPE_CHECKING:
    from meshai.notifications.events import Event

logger = logging.getLogger(__name__)

_HEADERS = {
    "order": "EVACUATION ORDER: {name}",
    "warning": "EVACUATION WARNING: {name}",
    "downgraded": "Evacuation order downgraded to warning: {name}",
    "lifted": "Evacuations lifted: {name}",
}


def _header(kind: str, level: str, name: str) -> str:
    if kind == "updated":
        level_word = "ORDER" if level == "order" else "WARNING"
        return f"EVACUATION {level_word} UPDATED: {name}"
    tmpl = _HEADERS.get(kind)
    if tmpl:
        return tmpl.format(name=name)
    # Defensive fallback for an unknown kind -- never crash rendering.
    return f"Evacuation update: {name}"


def format_evac(event: "Event", *, now: float, budget: int) -> str:
    """Render the Watch Duty evacuation wire string from canonical
    event.data.

    Args:
        event:  Pipeline Event -- reads from event.data.
        now:    Frozen-clock epoch (seam; not used in current rendering).
        budget: Mesh-packet character budget (from budget_for("watchduty")).

    Returns:
        UTF-8 string fitting within *budget* characters.
    """
    d = event.data or {}
    kind = d.get("kind") or "updated"
    level = d.get("level") or ""
    name = d.get("name") or "(unnamed fire)"
    zone_text = d.get("zone_text") or ""
    wd_event_id = d.get("wd_event_id")

    header = _header(kind, level, name)
    body = f"{header}\n{zone_text}" if (zone_text and kind != "lifted") else header

    suffix = incident_url(wd_event_id) if wd_event_id else ""
    if not suffix:
        from meshai.notifications.formatters._budget import fit_to_budget
        return fit_to_budget(body, budget)
    return fit_to_budget_with_suffix(body, suffix, budget)
