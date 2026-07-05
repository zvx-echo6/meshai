"""USGS NWIS stream-gauge (hydro) formatter — Phase-3 migration.

Mirrors central.nwis_handler._render EXACTLY (tier-a, byte-identical).  Reads
the canonical schema the Central path writes into event.data on broadcast:

    site_id, gauge_name, stage_ft, flow_cfs, unit, threshold_state,
    reading_time, lat, lon

Wire format (single line, MEDIUM):
    🌊 New: {gauge_name}: {label} {stage_ft:.1f} ft, flow {flow_cfs:,} cfs, @ lat,lon

Where {label} maps threshold_state via _LABEL:
    action          -> "action stage"
    flood_minor     -> "minor flooding"
    flood_moderate  -> "moderate flooding"
    flood_major     -> "major flooding"

Segments:
  * stage segment: "{label} {stage_ft:.1f} ft" when stage_ft is numeric, else
    the bare label.  The "ft" unit is hard-coded (matches _render, which
    ignores its `unit` parameter for the stage segment).
  * flow segment: ", flow {int(round(flow_cfs)):,} cfs" only when flow_cfs is
    numeric (companion 00060 discharge reading present).
  * coords segment: ", @ {lat:.3f},{lon:.3f}" only when both coords numeric.

Time contract: `now` is accepted but NOT used for rendering (structural seam;
never read the wall clock here).  `budget` is injected — the caller supplies
budget_for("usgs_nwis").  fit_to_budget is applied last; for the short hydro
wire it is a no-op under a 140-char budget, so output stays byte-identical to
_render (which did not budget-fit).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

# Import directly from the canonical implementation to avoid the circular
# import chain: central.budget → formatters.__init__ → formatters.hydro.
from meshai.notifications.formatters._budget import fit_to_budget

if TYPE_CHECKING:
    from meshai.notifications.events import Event


# Human-readable label per threshold_state — mirrors nwis_handler._LABEL.
_LABEL = {
    "action":         "action stage",
    "flood_minor":    "minor flooding",
    "flood_moderate": "moderate flooding",
    "flood_major":    "major flooding",
}


def format(event: "Event", *, now: float, budget: int) -> str:
    """Render the hydro wire string from canonical event.data.

    Args:
        event:  Pipeline Event — reads from event.data (canonical schema).
        now:    Frozen-clock epoch (seam; not used in current rendering).
        budget: Mesh-packet character budget (from budget_for("usgs_nwis")).

    Returns:
        UTF-8 string fitting within *budget* characters.
    """
    d = event.data or {}

    gauge_name = d.get("gauge_name")
    threshold_state = d.get("threshold_state")
    stage_ft = d.get("stage_ft")
    flow_cfs = d.get("flow_cfs")
    lat = d.get("lat")
    lon = d.get("lon")
    # `unit` is present in the canonical dict but intentionally unused here —
    # _render ignores its unit parameter and hard-codes "ft" in the stage
    # segment; mirror that exactly for byte-identity.

    label = _LABEL.get(threshold_state, threshold_state)

    # Stage segment.
    if isinstance(stage_ft, (int, float)):
        stage_seg = f"{label} {stage_ft:.1f} ft"
    else:
        stage_seg = label

    # Optional flow segment (companion 00060 discharge).
    flow_seg = ""
    if isinstance(flow_cfs, (int, float)):
        flow_seg = f", flow {int(round(flow_cfs)):,} cfs"

    # Optional coords segment.
    coords = ""
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        coords = f", @ {lat:.3f},{lon:.3f}"

    msg = f"🌊 New: {gauge_name}: {stage_seg}{flow_seg}{coords}"
    return fit_to_budget(msg, budget)
