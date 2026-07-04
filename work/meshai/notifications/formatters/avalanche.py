"""Avalanche advisory formatter — Phase-1 implementation.

Reads canonical event.data schema:
    danger_level    int   NAADS 1–5 (1=Low, 2=Moderate, 3=Considerable,
                          4=High, 5=Extreme)
    danger_name     str   human-readable level name (e.g. "Considerable")
    zone_name       str   advisory zone (e.g. "Sawtooth Mountains")
    center_id       str   forecast center ID (e.g. "SNFAC")
    travel_advice   str   full advisory text; line-2 uses first sentence only
    lat, lon        float centroid coordinates
    expires?        float epoch seconds (optional — accepted but not rendered;
                          seam available when now-relative display is needed)
    (+ decider-injected: is_update, _severity_override)

Wire format (mirrors avy_handler._render, adds is_update modifier):
    Line 1: ⛷ AVY {prefix}: {zone_name} — {danger_name} ({danger_level})
            prefix = "Update"   when is_update=True
                   = "WARNING"  when danger_level >= 4 (High/Extreme)
                   = "Watch"    when danger_level == 3 (Considerable)
    Line 2: {first sentence of travel_advice}  (if present)
    Line 3: {center_id} · valid today           (else "valid today")

Tier-b wire changes vs avy_handler._render():
    1. is_update=True produces "AVY Update:" prefix (handler had no is_update path).
    2. All other formatting is identical (emoji/prefix/zone/name/level/travel/center/·).

For non-update events the output is byte-identical to _render() so parity
tests pass without adjustment.

Time contract: `now` is accepted as a structural seam but not used for
rendering (expires display would use it in future).  All time reads MUST
go through meshai.notifications.clock (clock.now / clock.now_dt) — never
the stdlib equivalents — so golden-file tests can freeze the clock via
monkeypatch.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from meshai.notifications.formatters._budget import fit_to_budget

if TYPE_CHECKING:
    from meshai.notifications.events import Event


def format(event: "Event", *, now: float, budget: int) -> str:
    """Render the avalanche advisory wire string from canonical event.data.

    Args:
        event:  Pipeline Event — reads from event.data (canonical schema).
        now:    Frozen-clock epoch (seam; available for future expires display).
        budget: Mesh-packet character budget (from budget_for("avalanche")).

    Returns:
        UTF-8 string fitting within *budget* characters.
    """
    d = event.data or {}

    # ── Danger level ─────────────────────────────────────────────────────────
    danger_level = d.get("danger_level")
    if isinstance(danger_level, float):
        danger_level = int(danger_level)
    elif not isinstance(danger_level, int):
        danger_level = 0

    danger_name = d.get("danger_name") or str(danger_level)
    zone_name = d.get("zone_name") or "Unknown Zone"
    center_id = (d.get("center_id") or "").strip()
    travel = (d.get("travel_advice") or "").strip()

    # ── Prefix: Update vs WARNING vs Watch ───────────────────────────────────
    # is_update is injected by decide() data_patch (native: from EnvironmentalStore
    # change-detection; Central: defaults False until trend detection lands).
    is_update = bool(d.get("is_update", False))
    if is_update:
        prefix = "Update"
    elif danger_level >= 4:
        prefix = "WARNING"
    else:
        prefix = "Watch"

    # ── Build lines ──────────────────────────────────────────────────────────
    emoji = "⛷"  # ⛷

    # Line 1: emoji + AVY + prefix + zone + danger level (always present)
    line1 = (
        f"{emoji} AVY {prefix}: {zone_name} — {danger_name} ({danger_level})"
    )

    # Line 2: travel advice — first sentence only (identical to _render)
    line2: str | None = None
    if travel:
        m = re.search(r"[.!?]", travel)
        line2 = travel[: m.end()] if m else travel

    # Line 3: center ID + validity (identical to _render)
    line3 = f"{center_id} · valid today" if center_id else "valid today"

    msg = "\n".join(ln for ln in [line1, line2, line3] if ln)
    return fit_to_budget(msg, budget)
