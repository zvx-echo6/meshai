"""Friendly mesh-broadcast string composer (v0.5.2).

Replaces the bare `event.summary or event.title or event.category` fallback
that, when summary/title were empty (e.g. central-sourced events whose
category arrives as `central.<thing>`), produced debug-format broadcasts
like `[Weather] central.weather_warning`.

Composition order, highest priority first:
  1. category emoji + short uppercase label    (always — e.g. "🔥 FIRE:")
  2. primary identifier (title|summary|name)   (always)
  3. region (state/county or named region)     (optional, drop order 5)
  4. quantitative field (ac / M / mph / ft)    (optional, drop order 4)
  5. distance + bearing                        (optional, drop order 3)
  6. severity word                             (always — "priority")
  7. context (containment, cause, expires)     (optional, dropped FIRST)

Hard cap: 150 bytes UTF-8 (`len(s.encode('utf-8'))`). Segments are dropped
whole; never mid-codepoint truncation. If the required segments alone
exceed the budget, the primary identifier is shrunk by codepoints and
suffixed with `…` so the byte budget always holds.
"""

import logging
import re
from html import unescape
from typing import Optional

from meshai.notifications.events import Event

logger = logging.getLogger(__name__)


# Hard byte budget for a single mesh broadcast line (Matt-approved cap).
_BYTE_BUDGET = 150


# Per-category emoji. Falls back to severity-based when category unknown.
# Glyphs mirror the registry example_messages in categories.py so what we
# emit at runtime matches the documented user-facing format.
_CATEGORY_EMOJI: dict[str, str] = {
    # Weather
    "weather_warning": "⚠",
    "weather_watch": "⏳",
    "weather_advisory": "ℹ",
    "weather_statement": "📋",
    # Space weather / RF
    "hf_blackout": "⚠",
    "geomagnetic_storm": "🌐",
    "tropospheric_ducting": "📡",
    # v0.5.7-rf additions:
    "rf_anomalous_propagation": "📡",
    "rf_ducting_enhancement": "📡",
    "rf_propagation_alert": "⚠",
    "solar_radiation_storm": "🌐",
    # Fire (v0.5.7-fire: fire_proximity/wildfire_proximity removed; aligned
    # to the new registry entries wildfire_hotspot + wildfire_incident).
    "wildfire_hotspot": "🔥",
    "wildfire_incident": "🔥",
    "new_ignition": "🛰",
    # Hydro (now under seismic family per v0.5.2 §5)
    "stream_flood_warning": "🌊",
    "stream_high_water": "🌊",
    # Roads
    "road_closure": "🚧",
    "traffic_congestion": "🚗",
    "work_zone": "🚧",
    "road_incident": "🚨",
    # Avalanche
    "avalanche_warning": "⛷",
    "avalanche_watch": "⛷",            # v0.5.7-avalanche
    "avalanche_considerable": "⛷",     # legacy / forward-compat
    # Mesh health
    "infra_offline": "⚠",
    "critical_node_down": "🚨",
    "infra_recovery": "✅",
    "new_router": "📡",
    "battery_warning": "🔋",
    "battery_critical": "🔋",
    "battery_emergency": "🚨",
    "battery_trend": "🔋",
    "power_source_change": "⚡",
    "solar_not_charging": "☀",
    "high_utilization": "📊",
    "sustained_high_util": "📊",
    "packet_flood": "📻",
    "infra_single_gateway": "📶",
    "feeder_offline": "📡",
    "region_total_blackout": "🚨",
    "mesh_score_low": "📉",
    "region_score_low": "📉",
    # Seismic
    "earthquake_event": "🌐",
    "earthquake": "🌐",
}

_SEVERITY_EMOJI: dict[str, str] = {
    "immediate": "🚨",
    "priority": "⚠",
    "routine": "ℹ",
}

# Short uppercase labels (≤6 chars). Per-category where the family-default
# is wrong (e.g. hydro lives under seismic toggle but reads better as FLOOD).
_CATEGORY_LABEL: dict[str, str] = {
    "stream_flood_warning": "FLOOD",
    "stream_high_water": "HYDRO",
    "wildfire_hotspot": "FIRE",
    "wildfire_incident": "FIRE",
    "new_ignition": "FIRE",
    "weather_warning": "WX",
    "weather_watch": "WX",
    "weather_advisory": "WX",
    "weather_statement": "WX",
    "hf_blackout": "RF",
    "geomagnetic_storm": "RF",
    "tropospheric_ducting": "RF",
    # v0.5.7-rf additions:
    "rf_anomalous_propagation": "RF",
    "rf_ducting_enhancement": "RF",
    "rf_propagation_alert": "RF",
    "solar_radiation_storm": "RF",
    "road_closure": "ROADS",
    "traffic_congestion": "ROADS",
    "avalanche_warning": "AVY",
    "avalanche_watch": "AVY",            # v0.5.7-avalanche
    "avalanche_considerable": "AVY",     # legacy / forward-compat
    "earthquake_event": "QUAKE",
    "earthquake": "QUAKE",
    "critical_node_down": "MESH",
    "infra_offline": "MESH",
    "feeder_offline": "MESH",
    "region_total_blackout": "MESH",
}

_FAMILY_LABELS: dict[str, str] = {
    "weather": "WX",
    "fire": "FIRE",
    "rf_propagation": "RF",
    "roads": "ROADS",
    "avalanche": "AVY",
    "seismic": "GEO",
    "mesh_health": "MESH",
    "tracking": "TRK",
}


def _byte_len(s: str) -> int:
    """Length in UTF-8 bytes (mesh wire-byte reality)."""
    return len(s.encode("utf-8"))


# v0.5.7-weather: NWS data.description / data.instruction arrive as raw HTML
# (Central guide §"Surprise 3"). Adapters that reuse those fields for title /
# summary / region currently leak literal <p>/<br>/</p> tags to LoRa. Strip
# tags + decode entities BEFORE byte-budget truncation so the 150 B cap counts
# real glyphs, not markup. Applied universally — safe (no-op) on plain text.
_HTML_TAG_RE = re.compile(r"<[^>]+>")
# <br> and block-closers become spaces so adjacent paragraphs don't fuse.
_HTML_BREAK_RE = re.compile(r"</?(?:br|p|div|li|tr|h[1-6])\b[^>]*>", re.IGNORECASE)


def strip_html_tags(text: str) -> str:
    """Remove HTML tags and decode entities, collapsing whitespace.

    Block-level tags (<br>, <p>, etc.) become a single space so sentences
    from adjacent paragraphs don't fuse. All other tags are removed outright.
    HTML entities (&amp;, &nbsp;, &mdash;, …) are decoded via html.unescape.
    Result is whitespace-collapsed and stripped.
    """
    if not text:
        return ""
    s = _HTML_BREAK_RE.sub(" ", text)
    s = _HTML_TAG_RE.sub("", s)
    s = unescape(s)
    # Collapse runs of whitespace (incl. newlines from the original markup).
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _category_emoji(event: Event) -> str:
    e = _CATEGORY_EMOJI.get(event.category)
    if e:
        return e
    return _SEVERITY_EMOJI.get(event.severity, "•")


def _category_label(event: Event) -> str:
    """Short uppercase prefix label. Category > toggle family > stripped category."""
    lbl = _CATEGORY_LABEL.get(event.category)
    if lbl:
        return lbl
    try:
        from meshai.notifications.categories import get_toggle
        tog = get_toggle(event.category)
        if tog and tog in _FAMILY_LABELS:
            return _FAMILY_LABELS[tog]
    except Exception:
        pass
    # Strip the `central.` debug prefix so even unknown categories render clean.
    cat = event.category.removeprefix("central.") if event.category else ""
    if not cat:
        return "ALERT"
    return cat.upper().replace("_", " ").split(" ", 1)[0][:8]


def _primary_identifier(event: Event) -> str:
    """Title > summary > registry friendly name > scrubbed category.

    HTML is stripped first so the byte budget counts real glyphs.
    """
    t = strip_html_tags((event.title or "").strip())
    if t:
        return t
    s = strip_html_tags((event.summary or "").strip())
    if s:
        return s
    try:
        from meshai.notifications.categories import get_category
        info = get_category(event.category)
        name = info.get("name")
        if name:
            return str(name)
    except Exception:
        pass
    cat = (event.category or "").removeprefix("central.")
    if cat:
        return cat.replace("_", " ").title()
    return "Alert"


def _region_segment(event: Event) -> Optional[str]:
    region = event.region or (event.regions[0] if event.regions else None)
    if region is None:
        return None
    cleaned = strip_html_tags(str(region))
    return cleaned or None


def _safe(callable_):
    """Run a segment-extractor; swallow exceptions (renderer must not crash)."""
    try:
        return callable_()
    except Exception:
        return None


def _quant_segment(event: Event) -> Optional[str]:
    """Most informative quantitative field from event.data, if present."""
    data = event.data or {}
    if "acres" in data:
        return _safe(lambda: f"{int(float(data['acres'])):,} ac")
    if "magnitude" in data:
        return _safe(lambda: f"M{float(data['magnitude']):.1f}")
    if "mph_gust" in data:
        return _safe(lambda: f"gust {int(float(data['mph_gust']))} mph")
    if "depth_ft" in data:
        return _safe(lambda: f"{float(data['depth_ft'])} ft")
    if "stage_ft" in data:
        return _safe(lambda: f"{float(data['stage_ft'])} ft")
    if "kp" in data:
        return _safe(lambda: f"Kp={data['kp']}")
    return None


def _distance_segment(event: Event) -> Optional[str]:
    data = event.data or {}
    dist = data.get("distance_km")
    if dist is None:
        return None
    bearing = data.get("bearing")
    anchor = data.get("anchor")
    try:
        head = f"{int(round(float(dist)))} km"
    except Exception:
        return None
    parts = [head]
    if bearing:
        parts.append(str(bearing))
    if anchor:
        parts.append(f"of {anchor}")
    return " ".join(parts)


def _context_segment(event: Event) -> Optional[str]:
    """Optional context (dropped FIRST when over budget)."""
    data = event.data or {}
    bits: list[str] = []
    if data.get("containment_pct") is not None:
        try:
            bits.append(f"{int(float(data['containment_pct']))}% contained")
        except Exception:
            pass
    if data.get("cause"):
        bits.append(str(data["cause"]))
    if data.get("expires_at"):
        bits.append(f"exp {data['expires_at']}")
    return ", ".join(bits) if bits else None


def _resolve_budget(event: Event) -> int:
    """Pick a per-adapter packet budget for the new formatter path.

    Mirrors how the existing precomposed handlers select their budget:
    wfigs uses budget_for("wfigs"), quake uses budget_for("usgs_quake"),
    etc.  Here we use event.source (the adapter name) as the budget key.
    Falls back to 140 (the universal LoRa default) when source is absent
    or budget_for raises.
    """
    src = (getattr(event, "source", "") or "").strip()
    if src:
        try:
            from meshai.notifications.formatters._budget import budget_for
            return budget_for(src)
        except Exception:
            pass
    return 140


def compose_mesh_message(event: Event) -> str:
    """Compose a friendly mesh-broadcast string with 150-byte UTF-8 hard cap.

    Single line, no newlines. Drops segments wholesale (lowest priority first)
    to fit the budget; never mid-codepoint truncation.

    OPTION A bypass: if `event.data["_meshai_precomposed"]` is truthy, the
    title is already a fully formatted mesh string from the per-adapter
    normalizer (meshai/central_normalizer.py + the work_zone renderer).
    Return it verbatim -- no family-label prefix, no region tail, no
    severity word append.
    """
    # Phase-1+ formatter dispatch — gated on MESHAI_CUTOVER_CATEGORIES.
    # A formatter registered for an event's category is only called in the
    # LIVE path once that category has been explicitly cut over.  Until then
    # the formatter is used only by shadow_render (dry-run comparison) and by
    # direct unit tests — compose_mesh_message falls through to the legacy path.
    from meshai.notifications.formatters import get_formatter
    from meshai.notifications.cutover import is_cutover, NATIVE_ALWAYS_DECIDE
    from meshai.notifications import clock
    fmt = get_formatter(event.category)
    # Native WFIGS fire categories always render via the shared fire formatter
    # (same single render path a cut-over category takes), independent of the
    # shadow-bake env var. Central is off in this deployment, so these categories
    # are emitted only by the native fire adapter — no Central-render impact.
    if fmt is not None and (
            is_cutover(event.category)
            or event.category in NATIVE_ALWAYS_DECIDE):
        try:
            return fmt(event, now=clock.now(), budget=_resolve_budget(event))
        except Exception:
            logger.exception(
                "formatter failed for %s; falling back to legacy", event.category
            )
    # ---- existing passthrough + Mode-B unchanged below ----

    if event.data and event.data.get("_meshai_precomposed") and event.title:
        return event.title

    emoji = _category_emoji(event)
    label = _category_label(event)
    head = f"{emoji} {label}:"
    primary = _primary_identifier(event)
    severity = event.severity or "routine"

    # Build segments. drop_order is the order they're shed when over budget
    # (higher = shed first). Required segments have drop_order = -1.
    # Tuple: (drop_order, separator_before, text, is_required)
    segments: list[tuple[int, str, str, bool]] = []
    segments.append((-1, "", head, True))           # 1. always
    segments.append((-1, " ", primary, True))        # 2. always
    region = _region_segment(event)
    if region:
        segments.append((1, ", ", region, False))   # 3.
    quant = _quant_segment(event)
    if quant:
        segments.append((2, " — ", quant, False))   # 4.
    distance = _distance_segment(event)
    if distance:
        segments.append((3, ", ", distance, False)) # 5.
    segments.append((-1, ". ", severity, True))      # 6. always
    context = _context_segment(event)
    if context:
        segments.append((4, " — ", context, False)) # 7. FIRST to drop

    kept_idx = list(range(len(segments)))

    while True:
        line = ""
        for i in kept_idx:
            _, sep, text, _ = segments[i]
            line = line + (sep if line else "") + text
        if _byte_len(line) <= _BYTE_BUDGET:
            return line
        # Drop the optional segment with the highest drop_order.
        candidate = None
        for i in kept_idx:
            if not segments[i][3]:                    # not required
                if candidate is None or segments[i][0] > segments[candidate][0]:
                    candidate = i
        if candidate is None:
            # All remaining are required; have to shrink primary identifier.
            return _hard_truncate(segments, kept_idx, _BYTE_BUDGET)
        kept_idx.remove(candidate)


def _hard_truncate(segments, kept_idx, budget: int) -> str:
    """Required segments alone exceed budget; shrink primary by codepoints.

    Never mid-codepoint: Python str slicing is codepoint-safe, and we
    re-check UTF-8 byte length after each shrink.
    """
    ellipsis = "…"
    # Identify required pieces excluding the primary (index 1).
    head_text = segments[0][2]
    primary_text = segments[1][2]
    fixed_after = ""
    for i in kept_idx:
        if i in (0, 1):
            continue
        _, sep, text, _ = segments[i]
        fixed_after = fixed_after + sep + text
    # Reserve bytes for head + " " + ellipsis + fixed_after.
    fixed_bytes = _byte_len(head_text) + _byte_len(" ") + _byte_len(ellipsis) + _byte_len(fixed_after)
    primary_budget = budget - fixed_bytes
    if primary_budget <= 0:
        # Required-only fit attempt without the primary at all.
        bare = head_text + fixed_after
        if _byte_len(bare) <= budget:
            return bare
        # Nuclear: just the head emoji+label, drop everything else.
        return head_text if _byte_len(head_text) <= budget else "•"
    # Shrink primary by codepoints from the right.
    cut = primary_text
    while cut and _byte_len(cut) > primary_budget:
        cut = cut[:-1]
    return f"{head_text} {cut}{ellipsis}{fixed_after}"
