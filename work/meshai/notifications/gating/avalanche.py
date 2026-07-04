"""Avalanche advisory gating decider — Phase-1 implementation.

Mirrors avy_handler danger-level gate EXACTLY.
All thresholds are read from adapter_config.avalanche (same source as the
handler) so GUI edits take effect on the next event without restart.

decide(data, *, source, now) -> GateResult:
    Canonical data schema consumed:
        danger_level    int   NAADS 1–5 (1=Low … 5=Extreme)
        danger_name     str
        zone_name       str
        center_id       str
        travel_advice   str
        lat, lon        float (optional)
        expires?        float (optional)
        is_update?      bool  (native path: set by EnvironmentalStore change-
                              detection; Central path: defaults False)

    Emitted data_patch keys:
        is_update           bool     — propagated from incoming data or False
        _severity_override  str|None — "priority" for High/Extreme (4-5);
                                       None for Considerable (3) and below
                                       (envelope severity governs those)

    commit(now: float) -> None:
        None — there is no avalanche-specific DB table; event_log tracking
        is managed by handle_avy() for the Central path.  Native path events
        do not persist broadcast timestamps (they re-gate on every poll).

Gate logic (mirrors avy_handler danger-level gate verbatim):
    danger_level >= min_danger_level  → broadcast
    else                              → suppress

    min_danger_level defaults to 3 (Considerable) from adapter_config.

centralseverity → NAADS 1–5 mapping (tier-b, documented here; remap
lives in handle_avy() — by the time decide() is called, danger_level
is already NAADS):

    ┌─────────────────────┬───────────────────────┬───────────────────┐
    │ centralseverity int │ NAADS danger_level int │ danger_name       │
    ├─────────────────────┼───────────────────────┼───────────────────┤
    │ 0                   │ 1                      │ Low               │
    │ 1                   │ 2                      │ Moderate          │
    │ 2                   │ 3                      │ Considerable      │  ← documented
    │ 3                   │ 4                      │ High              │  ← documented
    │ 4                   │ 5                      │ Extreme           │  ← documented
    │ other               │ 0                      │ No Rating         │
    └─────────────────────┴───────────────────────┴───────────────────┘

    Values 0 and 1 are INFERRED (avy_handler.py comments document only
    2=Considerable, 3=High, 4=Extreme).  The remap formula is:
        naads = centralseverity + 1  for 0 ≤ centralseverity ≤ 4
        naads = 0                    otherwise

    ⚠ NEEDS LIVE VALIDATION IN-SEASON (October+): read a live Central
    avalanche envelope for a zone known to be rated Considerable and
    confirm centralseverity == 2.  If it's a different value, update
    the mapping table in handle_avy._remap_centralseverity() and add
    a comment here.

First-sighting / trend:
    Avalanche does NOT suppress re-broadcasts the way quake uses the
    no-Update rule.  The danger-level gate is the sole broadcast gate.
    is_update is propagated from the incoming data dict for display only
    (the formatter renders "AVY Update:" vs "AVY WARNING:").
"""
from __future__ import annotations

import logging
from typing import Optional

from meshai.adapter_config import adapter_config
from meshai.notifications.gating.base import GateResult

logger = logging.getLogger(__name__)


def _severity_override_for(danger_level: int) -> Optional[str]:
    """Derive _severity_override from NAADS danger level.

    High (4) / Extreme (5) → "priority"  (avoid terrain; widespread activity)
    Considerable (3)        → None        (let envelope severity govern)
    Low / Moderate / other  → None        (suppressed by gate; not reached)
    """
    if danger_level >= 4:
        return "priority"
    return None


# ── Public API ────────────────────────────────────────────────────────────────

def decide(data: dict, *, source: str, now: float) -> GateResult:
    """Gate + trend decision for avalanche_warning / avalanche_watch.

    Parameters
    ----------
    data:
        Canonical Event.data dict. danger_level must be NAADS 1–5 (handle_avy
        performs the centralseverity→NAADS remap before calling here).
    source:
        Adapter source name, e.g. "avalanche".
    now:
        Current epoch (clock.now()) — determinism seam, not used internally.

    Returns
    -------
    GateResult with:
        broadcast=True   lifecycle="new" or "update"  data_patch set
        broadcast=False  lifecycle="suppress"          data_patch={}
    """
    danger_level = data.get("danger_level")
    if isinstance(danger_level, float):
        danger_level = int(danger_level)
    if not isinstance(danger_level, int):
        return GateResult(
            broadcast=False, lifecycle="suppress",
            reason="danger_level missing or non-numeric in canonical data",
        )

    # ── Danger-level gate (mirrors avy_handler verbatim) ─────────────────────
    try:
        min_level = int(adapter_config.avalanche.min_danger_level)
    except Exception:
        min_level = 3  # default: Considerable and above

    if danger_level < min_level:
        return GateResult(
            broadcast=False, lifecycle="suppress",
            reason=f"danger_level={danger_level} below min_danger_level={min_level}",
        )

    # ── Trend / is_update ─────────────────────────────────────────────────────
    # Native path: EnvironmentalStore sets _is_update when danger_level rises.
    # Central path: defaults False (no trend detection yet).
    is_update = bool(data.get("is_update") or data.get("_is_update", False))
    lifecycle = "update" if is_update else "new"

    # ── Severity override ─────────────────────────────────────────────────────
    sev_override = _severity_override_for(danger_level)

    zone_name = data.get("zone_name", "?")
    center_id = data.get("center_id", "?")

    patch: dict = {
        "is_update": is_update,
        "_severity_override": sev_override,
    }

    # commit=None: no avalanche-specific DB table.  event_log tracking lives
    # in handle_avy() (Central path).  Native path re-gates on every poll.
    return GateResult(
        broadcast=True,
        lifecycle=lifecycle,
        reason=(
            f"danger_level={danger_level} >= min={min_level} "
            f"zone={zone_name} center={center_id} is_update={is_update}"
        ),
        data_patch=patch,
        commit=None,
    )
