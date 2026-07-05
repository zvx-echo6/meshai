"""WFIGS wildfire formatter — Phase-3b migration.

Reproduces BOTH legacy WFIGS wire shapes byte-identically, reading the canonical
schema the Central path writes into event.data on broadcast:

  (a) Active incident / growth — mirrors ``wfigs_handler._render`` exactly:
        Line 1: 🔥 {name} — {New|Update}
        Line 2: {acres} ac{ (+delta)} · containment {pct}%
        Line 3: {movement line}  OR  {anchor}
        Line 4: Cause: {cause} · Discovered {Mon D}   (either/both/neither)

  (b) All-clear ("contained & closed") — mirrors the tombstone branch exactly:
        Line 1: ✅ {name} — contained & closed
        Line 2: {acres} ac | {pct}% contained | {anchor}   (only present parts)

Branch selection (event.data):
    category == "wildfire_closed"  OR  _kind == "wfigs_tombstone"  -> all-clear
    otherwise                                                      -> incident

Canonical schema consumed (active incident):
    incident_name, acres, contained_pct, fire_cause, declared_at_epoch,
    unique_fire_id, lat, lon, county, state, landclass, geocoder_city,
    movement (FIRMS-injected {direction, speed_mph}, else absent/None),
    is_update, last_bcast_acres, last_bcast_contained  (decider render hints)

Canonical schema consumed (all-clear):
    incident_name, acres, contained_pct, lat, lon, county, state

Anchor resolution (``_fire_anchor``) reproduces ``wfigs_handler._location_anchor``
tier-for-tier: geocoder_city → curated town_anchors / Photon nearest_town (via
the shared ``resolve_anchor`` helper, re-formatted to the legacy string) →
landclass → "{county} Co {state}" → state → "(location unknown)".  The extra
fallback tiers around ``resolve_anchor`` are required for byte-identity because
``resolve_anchor`` covers only the town step.

Time contract: ``now`` is accepted but unused (the discovery date is rendered
from ``declared_at_epoch`` in a fixed UTC-6 offset, exactly as ``_render``).
``budget`` is injected — the caller supplies ``budget_for("wfigs")``.
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import TYPE_CHECKING, Optional

from meshai.adapter_config import adapter_config
from meshai.notifications.formatters._anchor import resolve_anchor
from meshai.notifications.formatters._budget import fit_to_budget

if TYPE_CHECKING:
    from meshai.notifications.events import Event

logger = logging.getLogger(__name__)


def _fire_anchor(d: dict) -> str:
    """Byte-identical replica of ``wfigs_handler._location_anchor``.

    geocoder.city > nearest town (curated town_anchors, then Photon) >
    landclass > "{county} Co {state}" > state > "(location unknown)".
    """
    city = d.get("geocoder_city")
    if city:
        return str(city)

    lat = d.get("lat")
    lon = d.get("lon")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        try:
            max_mi = float(adapter_config.wfigs.anchor_max_mi)
        except Exception:
            max_mi = 100.0
        try:
            res = resolve_anchor(lat, lon, max_mi=max_mi)
        except Exception:
            logger.debug("fire anchor: resolve_anchor failed; falling through")
            res = None
        if res and res.get("town"):
            # Legacy applies .title() in BOTH the town_anchors and nearest_town
            # arms; resolve_anchor titles only the town_anchors arm, so title
            # here unconditionally (idempotent for already-titled names).
            town = str(res["town"]).title()
            dist = res.get("distance_mi")
            bearing = res.get("bearing")
            if isinstance(dist, (int, float)):
                if dist < 1:
                    return f"near {town}"
                return f"{int(round(dist))} mi {bearing or ''} of {town}".strip()
            return f"near {town}"

    landclass = d.get("landclass")
    if landclass:
        return str(landclass)

    county = d.get("county")
    state = d.get("state")
    if county and state:
        return f"{county} Co {state}"
    if state:
        return str(state)
    return "(location unknown)"


def _render_incident(d: dict, budget: int) -> str:
    """Byte-identical replica of ``wfigs_handler._render`` (active incident)."""
    name = d.get("incident_name") or "(unnamed)"
    acres = d.get("acres")
    contained_pct = d.get("contained_pct")
    cause = d.get("fire_cause")
    declared_at_epoch = d.get("declared_at_epoch")
    movement = d.get("movement")
    is_update = bool(d.get("is_update"))
    last_bcast_acres = d.get("last_bcast_acres")
    prefix = "Update" if is_update else "New"
    anchor = _fire_anchor(d)

    lines: list[str] = []

    # Line 1: header
    lines.append(f"🔥 {name} — {prefix}")

    # Line 2: size / containment with delta (plain text — no bold markdown).
    acres_str = f"{int(acres):,} ac" if acres is not None else "size unknown"
    delta_str = ""
    if (prefix == "Update" and last_bcast_acres is not None
            and acres is not None and acres > last_bcast_acres):
        delta_str = f" (+{int(acres - last_bcast_acres):,})"
    contained_str = (f"containment {int(contained_pct)}%"
                     if contained_pct is not None else "containment unknown")
    lines.append(f"{acres_str}{delta_str} · {contained_str}")

    # Line 3: movement or plain anchor.
    if (isinstance(movement, dict)
            and movement.get("direction") and movement.get("speed_mph") is not None):
        lines.append(f"Moving {movement['direction']} {movement['speed_mph']:.1f} mi/h · {anchor}")
    else:
        lines.append(f"{anchor}")

    # Line 4: cause / discovered (DATE ONLY — no time-of-day).
    cause_part = cause if cause else None
    disc_part = None
    if declared_at_epoch is not None:
        try:
            dt = _dt.datetime.fromtimestamp(
                declared_at_epoch,
                tz=_dt.timezone(_dt.timedelta(hours=-6)))
            disc_part = dt.strftime("%b %-d")
        except Exception:
            pass
    if cause_part and disc_part:
        lines.append(f"Cause: {cause_part} · Discovered {disc_part}")
    elif cause_part:
        lines.append(f"Cause: {cause_part}")
    elif disc_part:
        lines.append(f"Discovered {disc_part}")

    return fit_to_budget("\n".join(lines), budget)


def _render_allclear(d: dict, budget: int) -> str:
    """Byte-identical replica of the ``wfigs_handler`` tombstone all-clear wire."""
    name = d.get("incident_name") or "(unnamed fire)"
    parts: list[str] = []
    acres = d.get("acres")
    contained_pct = d.get("contained_pct")
    if acres is not None:
        parts.append(f"{int(acres):,} ac")
    if contained_pct is not None:
        parts.append(f"{int(contained_pct)}% contained")
    # Location via the same anchor chain, but the tombstone branch feeds ONLY
    # {lat, lon, county, state} (no geocoder_city / landclass).
    loc_dict = {
        "lat": d.get("lat"), "lon": d.get("lon"),
        "county": d.get("county"), "state": d.get("state"),
    }
    anchor = _fire_anchor(loc_dict)
    if anchor and anchor != "(location unknown)":
        parts.append(anchor)
    lines = [f"✅ {name} — contained & closed"]
    if parts:
        lines.append(" | ".join(parts))
    return fit_to_budget("\n".join(lines), budget)


def format(event: "Event", *, now: float, budget: int) -> str:
    """Render the WFIGS wire string from canonical event.data.

    Args:
        event:  Pipeline Event — reads from event.data (canonical schema).
        now:    Frozen-clock epoch (seam; not used in current rendering).
        budget: Mesh-packet character budget (from budget_for("wfigs")).

    Returns:
        UTF-8 string fitting within *budget* characters.
    """
    d = event.data or {}
    category = None
    try:
        category = event.category
    except Exception:
        category = None
    if category is None:
        category = d.get("category")

    if category == "wildfire_closed" or d.get("_allclear") or d.get("_kind") == "wfigs_tombstone":
        return _render_allclear(d, budget)
    return _render_incident(d, budget)
