"""IPAWS civil-alert formatter.

Renders NON-weather civil emergency alerts (evacuation, Civil Emergency
Message, AMBER, 911 outage, law enforcement, HazMat) from the canonical CAP
``event.data`` dict the IPAWS adapter emits (same shape as the NWS path).

Civil alerts carry their signal in the CAP ``headline`` (a plain human
sentence), so — unlike the weather formatter, which parses structured
HAZARD.../IMPACT... blocks — this formatter is headline-forward. Real CAP
data has no machine-readable Idaho READY/SET/GO evacuation phase (there is
no ``<responseType>`` in the wild); ``evac_phase.detect_phase`` scans the
headline/CMAMtext/description free text for the phrases agencies actually
use ("Level 3 GO NOW", "Evacuation Warning", ...) so the wire message can
lead with the phase instead of raw CAP jargon:

    Line 1 (phase detected):  {emoji} {prefix}{PHASE} — {action}
        e.g. "🚨 GO — Leave now"
    Line 1 (no phase found — unchanged fallback):
        {emoji} {prefix}{event}      e.g. "🚨 Evacuation Immediate"
    Line 2: {area}[ · Until {t} {tz}]    areaDesc (first area) + expiry
    Line 3: {CMAMtext or headline}       the agency's own public alert text

Reuses ``event.data`` (canonical) + ``_budget.fit_to_budget``; does NOT touch
the NWS formatter. ``now`` is a structural seam (not used — expiry is absolute).
"""
from __future__ import annotations

import zoneinfo
from datetime import datetime
from typing import TYPE_CHECKING

from meshai.notifications.evac_phase import detect_phase
from meshai.notifications.formatters._budget import fit_to_budget

if TYPE_CHECKING:
    from meshai.notifications.events import Event


# Category → leading emoji. Immediate-severity civil hazards get 🚨; the rest ⚠️.
_CATEGORY_EMOJI = {
    "emergency_evacuation": "🚨",
    "emergency_amber": "🚨",
    "emergency_hazmat": "🚨",
    "emergency_911_outage": "⚠️",
    "emergency_law": "⚠️",
    "emergency_civil": "⚠️",
}

# Idaho READY/SET/GO — the one-line action tied to each detected phase.
_PHASE_ACTION = {
    "READY": "Get prepared",
    "SET": "Be ready to leave",
    "GO": "Leave now",
}


def format(event: "Event", *, now: float, budget: int) -> str:
    """Render the IPAWS civil-alert wire string from canonical event.data.

    Args:
        event:  Pipeline Event — reads event.data (canonical CAP schema).
        now:    Frozen-clock epoch (structural seam; expiry is absolute).
        budget: Mesh-packet character budget.

    Returns:
        UTF-8 string fitting within *budget* characters.
    """
    d = event.data or {}

    event_type = d.get("event") or "Emergency Alert"
    area_desc = d.get("area_desc") or ""
    headline = (d.get("headline") or "").strip()
    description = d.get("description") or ""
    parameters = d.get("parameters") or {}
    cmam_text = parameters.get("CMAMtext") or ""
    if isinstance(cmam_text, list):  # defensive: a repeated valueName collapses to a list
        cmam_text = cmam_text[0] if cmam_text else ""
    expires_epoch = d.get("expires_at")
    prefix = d.get("_ipaws_prefix") or ""
    category = d.get("category") or event.category or "emergency_civil"

    emoji = _CATEGORY_EMOJI.get(category, "⚠️")
    prefix_seg = f"{prefix}: " if prefix else ""

    # Line 1: lead with the Idaho READY/SET/GO phase when the free text
    # actually names one; otherwise keep the raw CAP event string (safe
    # fallback — never invent a phase that isn't there).
    phase = detect_phase(headline, cmam_text, description)
    if phase:
        # A detected GO always gets the alarm emoji, regardless of CAP
        # category — a confirmed "leave now" evacuation must never render
        # with the softer category-based ⚠️ (e.g. emergency_civil).
        if phase == "GO":
            emoji = "🚨"
        action = _PHASE_ACTION[phase]
        line1 = f"{emoji} {prefix_seg}{phase} — {action}"
    else:
        line1 = f"{emoji} {prefix_seg}{event_type}"

    # Line 2: first area + optional expiry ("Until 4:54 PM MDT")
    area = (area_desc or "").split(";")[0].strip()
    if len(area) > 60:
        cut = area[:60].rsplit(" ", 1)[0] or area[:60]
        area = cut + "…"
    time_seg = ""
    if expires_epoch:
        tz = zoneinfo.ZoneInfo("America/Boise")
        exp_local = datetime.fromtimestamp(expires_epoch, tz=tz)
        time_seg = f"Until {exp_local.strftime('%-I:%M %p %Z')}"
    if area and time_seg:
        line2 = f"{area} · {time_seg}"
    else:
        line2 = area or time_seg

    # Line 3: the agency's own public alert text (CMAMtext — purpose-written
    # for WEA, ~90 chars) when present; else fall back to the headline as before.
    line3 = cmam_text.strip() or headline

    msg = "\n".join(ln for ln in (line1, line2, line3) if ln)
    return fit_to_budget(msg, budget)
