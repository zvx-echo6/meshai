"""NWS weather-alert formatter — Phase-2 implementation.

Reads canonical event.data schema:
    event, same_code, cap_severity, certainty, expires_at,
    area_desc, geocoder{city,county,state},
    description (FULL), parameters (raw CAP dict),
    msgType, references, cap_id
    (+ decider-injected: _nws_prefix, _severity_override)

Tier-A wire contract:
    Byte-identical to nws_handler._render() for the same input values.
    The relative expiry uses the injected `now` (stdlib clock calls forbidden);
    fit to injected `budget`.

Time contract: `now` is accepted but not used for rendering (structural seam
for future relative-time annotations).  All time reads MUST go through
meshai.notifications.clock (clock.now / clock.now_dt) — never stdlib
equivalents — so golden-file tests can freeze the clock via monkeypatch.
"""
from __future__ import annotations

import re
import zoneinfo
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from meshai.notifications.formatters._budget import fit_to_budget

if TYPE_CHECKING:
    from meshai.notifications.events import Event

# ── Event-emoji tables (verbatim from nws_handler) ───────────────────────────

_EVENT_EMOJI = [
    ("tornado",            "🌪️"),
    ("severe thunderstorm", "🌩️"),
    ("thunderstorm",       "🌩️"),
    ("flash flood",        "🌊"),
    ("flood",              "🌊"),
    ("winter storm",       "❄️"),
    ("blizzard",           "❄️"),
    ("ice storm",          "❄️"),
    ("ice",                "❄️"),
    ("excessive heat",     "🌡️"),
    ("heat",               "🌡️"),
    ("high wind",          "🌬️"),
    ("wind",               "🌬️"),
    ("fire weather",       "🔥"),
    ("red flag",           "🔥"),
    ("air quality",        "😷"),
    ("freeze",             "🥶"),
    ("frost",              "🥶"),
]

_SAME_EMOJI = {
    "TOR": "🌪️", "SVR": "⛈️", "FFW": "🌊", "FLW": "🌊",
    "WSW": "❄️", "BZW": "❄️", "WCY": "❄️", "EWW": "💨",
    "HWW": "💨", "FRW": "🔥", "SPS": "🌬️", "SMW": "⛈️",
    "MAW": "🌊", "ADR": "⚠️",
}

# ── Hail descriptor table (verbatim from nws_handler) ────────────────────────

_HAIL_DESCRIPTORS = {
    "pea": 0.25, "half inch": 0.50, "penny": 0.75, "nickel": 0.88,
    "quarter": 1.00, "half dollar": 1.25, "ping pong": 1.50, "ping-pong": 1.50,
    "golf ball": 1.75, "golf": 1.75, "hen egg": 2.00, "tennis ball": 2.50,
    "baseball": 2.75, "softball": 4.00,
}


# ── Parsing helpers (verbatim from nws_handler) ───────────────────────────────

def _parse_nws_description(description: str) -> dict:
    result = {}
    patterns = {
        "hazard":         r"HAZARD\.\.\.(.*?)(?=\n\n|\nSOURCE|\nIMPACT|\nLocations|$)",
        "impact":         r"IMPACT\.\.\.(.*?)(?=\n\n|\nLocations|$)",
        "tornado":        r"TORNADO\.\.\.(.*?)(?=\n\n|\n[A-Z]+\.\.\.|$)",
        "tornado_threat": r"TORNADO DAMAGE THREAT\.\.\.(.*?)(?=\n\n|\n[A-Z]+\.\.\.|$)",
        "locations":      r"Locations impacted include[.…]*\s*(.*?)(?=\n\n|$)",
    }
    for key, pattern in patterns.items():
        m = re.search(pattern, description or "", re.DOTALL | re.IGNORECASE)
        if m:
            text = m.group(1).replace("\n", " ").strip()
            if text:
                # Preserve the FULL town list for path-sampling;
                # all other fields keep the 80-char cap.
                result[key] = text[:400] if key == "locations" else text[:80]
    return result


def _parse_motion(params: dict) -> tuple:
    """Parse eventMotionDescription into (compass, speed_mph).
    Format: '...DEG...KT' e.g. '254DEG...35KT'
    Returns (compass_str, speed_mph_int) or (None, None)."""
    raw = (params.get("eventMotionDescription") or [""])[0]
    if not raw:
        return None, None
    m = re.search(r"(\d+)DEG\.+(\d+)KT", raw)
    if not m:
        return None, None
    deg = float(m.group(1))
    knots = int(m.group(2))
    mph = round(knots * 1.15)
    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    compass = dirs[int((deg + 22.5) / 45) % 8]
    return compass, mph


def _emoji_for_event(event_type: Optional[str]) -> str:
    if not event_type:
        return "⚠️"
    s = event_type.lower()
    for substr, emoji in _EVENT_EMOJI:
        if substr in s:
            return emoji
    return "⚠️"


def _tighten_wind(wind: str) -> str:
    """'60 MPH' -> '60mph winds' (no space before mph, no 'wind gusts' filler)."""
    w = (wind or "").strip().lower().replace(" mph", "mph")
    if not w:
        return ""
    if not w.endswith("mph"):
        w = f"{w}mph"
    return f"{w} winds"


def _fmt_hail(hail: str) -> str:
    """Numeric or descriptor hail size -> '1\" hail'. Descriptor maps per NWS."""
    s = (hail or "").strip()
    if not s:
        return ""
    val = None
    low = s.lower()
    for k, v in _HAIL_DESCRIPTORS.items():
        if k in low:
            val = v
            break
    if val is None:
        try:
            val = float(re.sub(r"[^0-9.]", "", s))
        except (ValueError, TypeError):
            return ""
    txt = f"{val:.2f}".rstrip("0").rstrip(".")
    return f'{txt}" hail'


def _collapse_certainty(text: str) -> str:
    """'Radar confirmed'/'Radar indicated' -> 'radar'; 'Observed' -> 'observed'."""
    low = (text or "").strip().lower()
    if low in ("radar confirmed", "radar indicated"):
        return "radar"
    if low == "observed":
        return "observed"
    if low == "likely":
        return "likely"
    if low == "on ground":
        return "on ground"
    return (text or "").strip()


def _tighten_hazard(text: str) -> str:
    """Compact a free-form NWS hazard sentence into the terse mesh idiom."""
    if not text:
        return ""
    t = text.strip().rstrip(".")
    low = t.lower()
    for k in sorted(_HAIL_DESCRIPTORS, key=len, reverse=True):
        for variant in (f"{k} size hail", f"{k}-size hail", f"{k} sized hail"):
            idx = low.find(variant)
            if idx != -1:
                repl = _fmt_hail(k)
                if repl:
                    t = t[:idx] + repl + t[idx + len(variant):]
                    low = t.lower()
                break
    t = re.sub(r"(\d+(?:\.\d+)?)[\- ]inch(?:es)?\s+hail",
               lambda m: _fmt_hail(m.group(1)) or m.group(0), t,
               flags=re.IGNORECASE)
    t = re.sub(r"wind gusts?\s+(?:in excess of|up to|to|of|reaching|near|around)"
               r"\s+(\d+)\s*mph", r"\1mph gusts", t, flags=re.IGNORECASE)
    t = re.sub(r"winds?\s+gusting\s+(?:up\s+)?to\s+(\d+)\s*mph",
               r"\1mph gusts", t, flags=re.IGNORECASE)
    t = re.sub(r"(?:damaging\s+)?winds?\s+(?:in excess of|up to|to|of)\s+"
               r"(\d+)\s*mph", r"\1mph winds", t, flags=re.IGNORECASE)
    t = re.sub(r"\bin excess of\b", ">", t, flags=re.IGNORECASE)
    t = re.sub(r"(\d+)\s*mph", r"\1mph", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+", " ", t).strip()
    return t


# ── Public API ────────────────────────────────────────────────────────────────

def format(event: "Event", *, now: float, budget: int) -> str:
    """Render the NWS weather-alert wire string from canonical event.data.

    Args:
        event:  Pipeline Event — reads from event.data (canonical schema).
        now:    Frozen-clock epoch (structural seam; not used in current
                rendering — expiry is absolute, not relative).
        budget: Mesh-packet character budget (from budget_for("nws")).

    Returns:
        UTF-8 string fitting within *budget* characters.
        Byte-identical to nws_handler._render() for equivalent inputs (Tier-A).

    Canonical fields read from event.data:
        event           str  — NWS event type ("Severe Thunderstorm Warning")
        same_code       str  — SAME/eventCode value ("SVR", "SPS", etc.)
        area_desc       str  — areaDesc from CAP
        expires_at      int  — unix epoch of expiry (or None)
        description     str  — FULL CAP description (not truncated)
        parameters      dict — raw CAP parameters dict
        certainty       str  — CAP certainty ("Observed", "Likely", ...)
        _nws_prefix     str  — decider-injected prefix ("", "Update", "Active")
    """
    d = event.data or {}

    # ── Extract canonical fields ──────────────────────────────────────────────
    event_type = d.get("event") or "Weather Alert"
    area_desc = d.get("area_desc") or ""
    expires_epoch = d.get("expires_at")
    description_full = d.get("description") or ""
    parameters = d.get("parameters") or {}
    same_code = d.get("same_code") or ""
    certainty = (d.get("certainty") or "").strip()
    prefix = d.get("_nws_prefix") or ""

    # ── Adapter config ────────────────────────────────────────────────────────
    from meshai.adapter_config import adapter_config

    # ── Parse description ─────────────────────────────────────────────────────
    desc = _parse_nws_description(description_full)

    # ── Emoji (SAME code → explicit map, else event-type substring) ───────────
    emoji = _SAME_EMOJI.get(same_code) or _emoji_for_event(event_type)
    prefix_seg = f"{prefix}: " if prefix else ""

    # Line 1: emoji + event type
    line1 = f"{emoji} {prefix_seg}{event_type or 'Weather Alert'}"

    # Line 2: "Until {time} {tz} — {area}"
    tz = zoneinfo.ZoneInfo("America/Boise")
    if expires_epoch:
        exp_local = datetime.fromtimestamp(expires_epoch, tz=tz)
        exp_str = exp_local.strftime("%-I:%M %p %Z")
        time_seg = f"Until {exp_str}"
    else:
        time_seg = ""
    area = (area_desc or "").split(";")[0].strip()
    _area_limit = int(adapter_config.nws.area_max_chars)
    if len(area) > _area_limit:
        cut = area[:_area_limit].rsplit(" ", 1)[0]
        if not cut:
            cut = area[:_area_limit]
        area = cut + "…"
    if time_seg and area:
        line2 = f"{time_seg} — {area}"
    elif time_seg:
        line2 = time_seg
    elif area:
        line2 = area
    else:
        line2 = ""

    # Line 3: hazard + certainty/threat (SAME-code branched)
    line3 = ""
    if same_code == "TOR":
        detection = (parameters.get("tornadoDetection") or [""])[0]
        status = "on ground" if detection == "OBSERVED" else "radar"
        threat = (parameters.get("tornadoDamageThreat") or [""])[0]
        threat_seg = f" · {threat.lower()} damage" if threat else ""
        line3 = f"tornado {status}{threat_seg}"
    elif same_code == "SVR":
        wind = (parameters.get("maxWindGust") or [""])[0]
        hail = (parameters.get("maxHailSize") or [""])[0]
        bits = []
        if wind and wind not in ("0 MPH", ""):
            w = _tighten_wind(wind)
            if w:
                bits.append(w)
        if hail and hail not in ("0.00", "0", ""):
            h = _fmt_hail(hail)
            if h:
                bits.append(h)
        hazard = ", ".join(bits)
        # SVR is radar-based: "Observed" certainty => "Radar confirmed" => "radar".
        confirm = _collapse_certainty(
            "Radar confirmed" if certainty == "Observed" else "Radar indicated")
        line3 = f"{hazard} · {confirm}" if hazard else confirm
    elif same_code in ("FFW", "FLW"):
        hazard_text = desc.get("hazard") or ""
        if ". " in hazard_text:
            hazard_text = hazard_text.split(". ")[0]
        hazard_text = _tighten_hazard(hazard_text)
        desc_lower = description_full.lower()
        flood_cause = ""
        for keyword, label in [("thunderstorm", "thunderstorms"),
                                ("dam", "dam failure"),
                                ("snowmelt", "snowmelt"),
                                ("ice jam", "ice jam")]:
            if keyword in desc_lower:
                flood_cause = label
                break
        cause_seg = f" · {flood_cause}" if flood_cause else ""
        line3 = f"{hazard_text}{cause_seg}" if hazard_text else flood_cause
    else:
        # SPS, WSW, etc.: first hazard sentence (tightened) + certainty if
        # Observed/Likely.
        hazard_text = desc.get("hazard") or ""
        if ". " in hazard_text:
            hazard_text = hazard_text.split(". ")[0]
        hazard_text = _tighten_hazard(hazard_text)
        cert_seg = ""
        if certainty in ("Observed", "Likely"):
            cert_seg = f" · {_collapse_certainty(certainty)}"
        line3 = f"{hazard_text}{cert_seg}" if hazard_text else ""

    # Line 4: motion + locations (path-sampled if the full town list won't fit).
    compass, speed_mph = _parse_motion(parameters)
    motion = f"Moving {compass} {speed_mph} mph" if compass and speed_mph else ""

    # Parse the (now-full) locations string into an ordered town list.
    raw_locs = (desc.get("locations") or "").rstrip("., ")
    towns = [t.strip() for t in raw_locs.split(",") if t.strip()]
    if towns:
        towns[-1] = re.sub(r"^and\s+", "", towns[-1], flags=re.IGNORECASE).strip()
        towns = [t for t in towns if t]

    def _dedup(seq):
        """Drop consecutive repeats."""
        out = []
        for t in seq:
            if not out or out[-1] != t:
                out.append(t)
        return out

    def _line4(locs: str) -> str:
        if motion and locs:
            return f"{motion} — {locs}"
        if motion:
            return motion
        return locs or ""

    PACKET_LIMIT = budget

    # Location representations from richest to poorest; first form that fits wins.
    loc_options = [", ".join(towns)]
    if len(towns) >= 3:
        loc_options.append(" → ".join(
            _dedup([towns[0], towns[len(towns) // 2], towns[-1]])))
    if len(towns) >= 2:
        loc_options.append(" → ".join(_dedup([towns[0], towns[-1]])))
    if towns:
        loc_options.append(towns[0])
    loc_options.append("")  # motion only / empty

    base_lines = [l for l in (line1, line2, line3) if l]
    msg = None
    for locs in loc_options:
        cand4 = _line4(locs)
        lines = base_lines + ([cand4] if cand4 else [])
        candidate = "\n".join(lines)
        if len(candidate) <= PACKET_LIMIT:
            msg = candidate
            break
    if msg is None:
        # Even motion-only line 4 overflows: drop line 4 entirely.
        msg = "\n".join(base_lines)

    # Final hard-cap safety net for the pathological case where lines 1-3 alone
    # overflow.
    return fit_to_budget(msg, PACKET_LIMIT)
