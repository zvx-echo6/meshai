"""Earthquake event formatter — Phase-1 reference implementation.

Reads canonical event.data schema:
    magnitude, depth_km, lat, lon, place, tsunami, pager,
    occurred_at, event_id
    (+ decider-injected: distance_km, is_update, _severity_override,
    _dedup_suffix)

Tier-b wire changes vs v0.5.10 _render():
    1. PAGER alert is now rendered (previously only gated, not displayed).
    2. Update-prefix is live: "Update:" when data["is_update"] is True
       (handler hard-coded False; formatter is ready for when it fires).
    3. All other formatting is identical (emoji/magnitude/place/depth/coords/
       tsunami line — unchanged from _render()).

Wire format (multi-line, fits mesh packet budget):
    Line 1:  {emoji} {prefix} M{mag:.1f} — {place}
    Line 2:  Depth: {depth} km · @ {lat:.3f}, {lon:.3f}
    Line 3:  🚨 TSUNAMI WARNING   (only when tsunami flag is set)
    Line 4:  ⚠️ PAGER: {level}    (only when PAGER orange/red)

Emoji selection (unchanged from _render()):
    tsunami         → 🚨
    M >= 5.0        → ⚠️
    routine         → 🌐

Time contract: `now` is accepted but not used for rendering (structural seam
for future relative-time annotations).  All time reads MUST go through
meshai.notifications.clock (clock.now / clock.now_dt) — never the stdlib
equivalents — so golden-file tests can freeze the clock via monkeypatch.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from meshai.adapter_config import adapter_config
# Import directly from the canonical implementation to avoid the circular
# import chain: central.budget → formatters.__init__ → formatters.quake → central.budget
from meshai.notifications.formatters._budget import fit_to_budget

if TYPE_CHECKING:
    from meshai.notifications.events import Event


def _emoji_for(mag, tsunami: bool) -> str:
    """Emoji selection mirrors quake_handler._emoji_for exactly."""
    if tsunami:
        return "\U0001f6a8"  # 🚨
    try:
        floor = float(adapter_config.usgs_quake.escalate_mag_floor)
    except Exception:
        floor = 5.0
    if isinstance(mag, (int, float)) and mag >= floor:
        return "⚠️"  # ⚠️
    return "\U0001f310"  # 🌐


def format(event: "Event", *, now: float, budget: int) -> str:
    """Render the quake wire string from canonical event.data.

    Args:
        event:  Pipeline Event — reads from event.data (canonical schema).
        now:    Frozen-clock epoch (seam; not used in current rendering).
        budget: Mesh-packet character budget (from budget_for("usgs_quake")).

    Returns:
        UTF-8 string fitting within *budget* characters.
    """
    d = event.data or {}

    # ── Magnitude ──────────────────────────────────────────────────────────
    mag = d.get("magnitude") or d.get("mag")
    if isinstance(mag, str):
        try:
            mag = float(mag)
        except ValueError:
            mag = None
    elif isinstance(mag, (int, float)):
        mag = float(mag)
    mag_str = f"{mag:.1f}" if isinstance(mag, (int, float)) else "?"

    # ── Place / location ────────────────────────────────────────────────────
    place = d.get("place")
    place_str = place if place else "unknown location"

    # ── Depth + coords ──────────────────────────────────────────────────────
    # Accept canonical depth_km and raw USGS "depth" key as fallback.
    depth_km = d.get("depth_km") if d.get("depth_km") is not None else d.get("depth")
    # Accept canonical lat/lon and raw USGS latitude/longitude as fallback.
    lat = d.get("lat") if d.get("lat") is not None else d.get("latitude")
    lon = d.get("lon") if d.get("lon") is not None else d.get("longitude")

    # ── Flags ───────────────────────────────────────────────────────────────
    tsunami = bool(d.get("tsunami") or d.get("tsunami_warning"))

    # Tier-b ①: render PAGER alert (was only used for gating, never shown)
    pager = d.get("pager") if d.get("pager") is not None else d.get("alert")
    try:
        pager_levels = {s.lower() for s in adapter_config.usgs_quake.broadcast_pager_alerts}
    except Exception:
        pager_levels = {"orange", "red"}

    # Tier-b ②: update-prefix live (was hard-coded False in _render)
    is_update = bool(d.get("is_update", False))

    # ── Build lines ─────────────────────────────────────────────────────────
    emoji = _emoji_for(mag, tsunami)
    prefix = "Update:" if is_update else "New:"

    # Line 1: emoji + prefix + magnitude + place (always present)
    line1 = f"{emoji} {prefix} M{mag_str} — {place_str}"

    # Line 2: depth + coords (present when values available)
    parts: list[str] = []
    if isinstance(depth_km, (int, float)):
        parts.append(f"Depth: {int(round(depth_km))} km")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        parts.append(f"@ {lat:.3f}, {lon:.3f}")
    line2 = " · ".join(parts) if parts else None

    # Line 3: tsunami (identical to _render)
    line3 = "\U0001f6a8 TSUNAMI WARNING" if tsunami else None

    # Line 4: PAGER alert — tier-b NEW (was never in _render)
    line4: str | None = None
    if pager and isinstance(pager, str) and pager.lower() in pager_levels:
        line4 = f"⚠️ PAGER: {pager.lower()}"

    msg = "\n".join(l for l in [line1, line2, line3, line4] if l)
    return fit_to_budget(msg, budget)
