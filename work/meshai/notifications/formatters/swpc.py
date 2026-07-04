"""SWPC space-weather event formatter — Phase-1 refactor.

Reads canonical event.data schema:
    driver      : "kp" | "flare"  — what drove the event
    scalar      : float (Kp value) | str (flare class) | None
    scale_code  : "G3" | "R3" | etc.  — NOAA scale code
    message     : str  — raw SWPC alert message body (or "")
    issued_at   : str | None  — ISO timestamp for the time tag line

Wire format (multi-line, identical structure to swpc_handler._render()):
    Geomag:   🧲 New: G3 Geomagnetic Storm — Kp7
              HF degraded, aurora possible
              SWPC · 2026-07-04 05:09

    Flare:    ☀️ New: X1.0 Solar Flare — R3
              HF radio fading, GPS may glitch
              SWPC · 2026-06-03 11:59

    (when scalar is None the dash-separated tail is omitted)

Tier-b fix note: `_severity_override` is set by the decider (gating/swpc.py)
so geomag/flare events dispatch at priority/immediate severity instead of the
"routine" default the old swpc_handler always produced.

All other formatting is identical to swpc_handler._render().

Time contract: `now` is accepted but not used for rendering (structural seam
for future relative-time annotations).  All time reads MUST go through
meshai.notifications.clock — never the stdlib equivalents — so golden-file
tests can freeze the clock via monkeypatch.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from meshai.notifications.formatters._budget import fit_to_budget

if TYPE_CHECKING:
    from meshai.notifications.events import Event


def _trunc(s: str, limit: int = 120) -> str:
    """Truncate *s* at the last word boundary at or before *limit* chars."""
    if len(s) <= limit:
        return s
    cut = s[:limit].rsplit(" ", 1)[0]
    if not cut:
        cut = s[:limit]
    return cut + "…"


def format(event: "Event", *, now: float, budget: int) -> str:
    """Render SWPC wire string from canonical event.data.

    Args:
        event:  Pipeline Event — reads from event.data (canonical schema).
        now:    Frozen-clock epoch (structural seam; not used in rendering).
        budget: Mesh-packet character budget.

    Returns:
        UTF-8 string fitting within *budget* characters.
    """
    d = event.data or {}

    driver = d.get("driver")          # "kp" | "flare"
    scalar = d.get("scalar")          # float (Kp) | str (flare class) | None
    scale_code = d.get("scale_code") or ""   # "G3", "R3", etc.

    # ── Detail line (line 2) ─────────────────────────────────────────────────
    message = d.get("message") or ""
    if isinstance(message, str):
        message = _trunc(message.strip())
    else:
        message = ""

    # ── Time tag (line 3) ────────────────────────────────────────────────────
    time_tag = ""
    issued_at = d.get("issued_at") or d.get("time_tag") or ""
    if isinstance(issued_at, str) and issued_at:
        time_tag = issued_at[:16].replace("T", " ")

    prefix = "New:"   # SWPC events are point-in-time; "Update:" not used

    if driver == "kp":
        # ── Geomagnetic storm ────────────────────────────────────────────────
        if isinstance(scalar, (int, float)):
            scalar_str: str | None = f"Kp{int(round(scalar))}"
        else:
            scalar_str = None

        if scalar_str:
            line1 = f"🧲 {prefix} {scale_code} Geomagnetic Storm — {scalar_str}"
        else:
            line1 = f"🧲 {prefix} {scale_code} Geomagnetic Storm"

        line2 = message if message else "HF degraded, aurora possible"
        line3 = f"SWPC · {time_tag}" if time_tag else "SWPC"

    elif driver == "flare":
        # ── Solar flare ──────────────────────────────────────────────────────
        if isinstance(scalar, str) and scalar:
            line1 = f"☀️ {prefix} {scalar} Solar Flare — {scale_code}"
        else:
            line1 = f"☀️ {prefix} {scale_code} Solar Flare"

        line2 = message if message else "HF radio fading, GPS may glitch"
        line3 = f"SWPC · {time_tag}" if time_tag else "SWPC"

    else:
        # ── Unknown driver (fallback — should not occur in normal operation) ─
        line1 = f"⚠️ {prefix} Space Weather Event — {scale_code or '?'}"
        line2 = message if message else None
        line3 = f"SWPC · {time_tag}" if time_tag else "SWPC"

    msg = "\n".join(l for l in [line1, line2, line3] if l)
    return fit_to_budget(msg, budget)
