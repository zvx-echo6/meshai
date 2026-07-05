"""v0.5.10 NWS weather-alerts handler.

Severity floor: broadcast only when CAP severity in {Extreme, Severe}. Watch /
Advisory / Statement (Moderate, Minor, Unknown) get logged to event_log
handled=0 and silently skipped.

Tombstone handling: msgType in {Cancel, Expire} -> log handled=0, no
broadcast.

Per-CAP-id dedup: nws_alerts table keyed on CAP `event_id` (the urn-style
identifier). First sighting fires `New:`; re-issues UPSERT current_* but
don't re-broadcast (v0.5.9-incident no-Update rule).

Wire format (MEDIUM, ~80-90 B):
    {emoji} {event_type}: {area_desc}, until {expires_short}, @ {lat:.3f},{lon:.3f}

Emoji by event_type prefix (substring match, case-insensitive):
    Tornado Warning           -> 🌪️
    Severe Thunderstorm War.. -> 🌩️
    Flash Flood / Flood       -> 🌊
    Winter Storm / Blizzard / Ice -> ❄️
    Heat / Excessive Heat     -> 🌡️
    High Wind / Wind          -> 🌬️
    Fire Weather / Red Flag   -> 🔥
    Air Quality               -> 😷
    Frost / Freeze            -> 🥶
    default                   -> ⚠️
"""
from __future__ import annotations
from meshai.adapter_config import adapter_config
from meshai.central.budget import budget_for, fit_to_budget

import logging
import re
import time
import zoneinfo
from datetime import datetime, timezone
from typing import Any, Optional

from meshai.notifications import clock

from meshai.persistence import get_db

logger = logging.getLogger(__name__)


# v0.6-3b: severity gate + tombstone msgTypes live in adapter_config.nws
# (broadcast_severities, tombstone_msgtypes). Read at handler call time.

# Ordered (substring, emoji) checks; first match wins.
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

_NWS_OFFICE_SHORT = {
    "KBOI": "Boise", "KPIH": "Pocatello", "KMSO": "Missoula",
    "KOTX": "Spokane", "KSLC": "Salt Lake City", "KMFR": "Medford",
    "KPDT": "Pendleton", "KSEW": "Seattle",
}


def _nws_office(params: dict) -> str:
    try:
        wmo = (params.get("WMOidentifier") or [""])[0]
        code = wmo.split()[1]
        return _NWS_OFFICE_SHORT.get(code, code[1:])
    except Exception:
        return ""


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
                # Preserve the FULL town list for path-sampling in _render();
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
    # Bearing is the direction the storm is moving TOWARD
    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    compass = dirs[int((deg + 22.5) / 45) % 8]
    return compass, mph


def _now() -> int: return int(clock.now())


def _is_update(conn, d: dict) -> bool:
    """Return True if any CAP id in `references` was previously broadcast."""
    refs = d.get("references") or []
    if not refs:
        return False
    ref_ids = [r["identifier"] for r in refs
              if isinstance(r, dict) and r.get("identifier")]
    if not ref_ids:
        return False
    placeholders = ",".join("?" * len(ref_ids))
    row = conn.execute(
        f"SELECT 1 FROM nws_alerts WHERE event_id IN ({placeholders}) "
        "AND last_broadcast_at IS NOT NULL LIMIT 1",
        ref_ids,
    ).fetchone()
    return row is not None


def _parse_iso(s: Optional[str]) -> Optional[int]:
    if not s: return None
    try: return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception: return None


def _emoji_for_event(event_type: Optional[str]) -> str:
    if not event_type: return "⚠️"
    s = event_type.lower()
    for substr, emoji in _EVENT_EMOJI:
        if substr in s:
            return emoji
    return "⚠️"


def _format_expires_short(epoch: Optional[int], now: Optional[int] = None) -> str:
    """Renders 'until 8:15pm' / 'until Mon 3am' / 'until 6/12 8pm' depending on
    how far away the expiry is. now defaults to current time so the relative
    rendering is correct in tests too."""
    if not epoch: return "expires unknown"
    now = now or _now()
    diff = epoch - now
    try:
        dt = datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone()
    except Exception:
        return "expires unknown"

    hour = dt.strftime("%-I").lstrip("0") or "0"
    minute = dt.minute
    ampm = "am" if dt.hour < 12 else "pm"
    if minute:
        time_str = f"{hour}:{minute:02d}{ampm}"
    else:
        time_str = f"{hour}{ampm}"

    if diff < 6 * 3600:
        return f"until {time_str}"
    if diff < 7 * 86400:
        return f"until {dt.strftime('%a')} {time_str}"
    return f"until {dt.strftime('%-m/%-d')} {time_str}"


def _location_anchor(area_desc: Optional[str], geocoder_city: Optional[str],
                       county: Optional[str], state: Optional[str]) -> str:
    """Priority: geocoder.city > areaDesc (first 30 chars) > county+state > state."""
    if geocoder_city:
        return str(geocoder_city)
    if area_desc:
        # NWS areaDesc is often semicolon-delimited list of zones; trim to first.
        head = area_desc.split(";")[0].strip()
        if len(head) > 30: head = head[:27] + "..."
        return head
    if county and state:
        return f"{county} Co {state}"
    if state:
        return str(state)
    return "(location unknown)"


def handle_nws(envelope: dict, subject: str,
                data: Optional[dict] = None,
                now: Optional[int] = None) -> Optional[str]:
    """Central path handler for NWS weather-alert envelopes.

    Phase-2 refactor: when the category is cut over (MESHAI_CUTOVER_CATEGORIES
    contains weather_warning or weather_statement), gating is delegated to
    meshai.notifications.gating.nws.decide() and canonical data is written
    into the shared `data` dict for the formatter.

    On NOT-cutover: exact legacy code path, byte-for-byte unchanged.
    """
    if not isinstance(envelope, dict): return None
    inner = envelope.get("data") or {}
    if (inner.get("adapter") or "") != "nws": return None

    d = inner.get("data") or {}
    geo = inner.get("geo") or {}
    ge = (d.get("_enriched") or {}).get("geocoder") or {}
    now = now if now is not None else _now()
    category_raw = inner.get("category") or ""
    severity_word = _coerce_severity(inner.get("severity"))

    cap_id = d.get("id") or inner.get("id")
    if not cap_id:
        return None

    # ── Field extraction (shared by both paths) ───────────────────────────────
    msg_type = d.get("msgType")
    event_type = d.get("event") or _category_to_event_type(category_raw)
    area_desc = d.get("areaDesc")
    headline = d.get("headline")
    description = d.get("description")
    cap_severity = d.get("severity")
    county = d.get("areaDesc") or ge.get("county")
    state = ge.get("state") or d.get("state")
    expires_epoch = _parse_iso(d.get("expires"))
    same_code = ((d.get("eventCode") or {}).get("SAME") or [""])[0]
    certainty = d.get("certainty") or ""
    references = d.get("references") or []
    parameters = d.get("parameters") or {}

    lat = lon = None
    cent = geo.get("centroid") or []
    if isinstance(cent, list) and len(cent) >= 2:
        lon, lat = cent[0], cent[1]

    # ── Cutover gate ──────────────────────────────────────────────────────────
    from meshai.notifications.cutover import is_cutover
    _cutover = is_cutover("weather_warning") or is_cutover("weather_statement")

    if _cutover:
        # ── NEW PATH: delegate to gating/nws.py decide() ─────────────────────
        try:
            conn = get_db()
        except Exception:
            logger.exception("nws_handler: persistence unavailable")
            return None

        # Tombstone: log handled=0, return None (before canonical/decide).
        if msg_type in set(adapter_config.nws.tombstone_msgtypes):
            _log_event(conn, now=now, source="nws", category=category_raw,
                        severity_word=severity_word, event_id_external=cap_id,
                        subject=subject, handled=0,
                        table_name="nws_alerts", table_pk=cap_id)
            return None

        # Always log the event (both broadcast and suppress paths).
        log_id = _log_event_returning_id(
            conn, now=now, source="nws", category=category_raw,
            severity_word=severity_word, event_id_external=cap_id,
            subject=subject, handled=0,
            table_name="nws_alerts", table_pk=cap_id)

        # Build canonical data dict for decide() and the formatter.
        canonical: dict = {
            "cap_id": cap_id,
            "event": event_type,
            "same_code": same_code,
            "cap_severity": cap_severity,
            "certainty": certainty,
            "expires_at": expires_epoch,
            "area_desc": area_desc,
            "geocoder": {
                "city": ge.get("city"),
                "county": county,
                "state": state,
            },
            "description": description,
            "parameters": parameters,
            "msgType": msg_type,
            "references": references,
            "category": category_raw,
            "headline": headline,
        }

        from meshai.notifications.gating.nws import decide as _gate_decide
        gate = _gate_decide(canonical, source="nws", now=float(now))

        if not gate.broadcast:
            return None

        # Write canonical fields + gate data_patch into shared data dict.
        if isinstance(data, dict):
            data.update(canonical)
            data.update(gate.data_patch)
            data["_broadcast_audit"] = {"table": "nws_alerts", "pk": cap_id}

            _raw_commit = gate.commit
            _log_row_id = log_id

            def _on_commit(committed_at: float) -> None:
                if _raw_commit is not None:
                    _raw_commit(committed_at)
                if _log_row_id is not None:
                    try:
                        c = get_db()
                        c.execute("UPDATE event_log SET handled=1 WHERE id=?",
                                  (int(_log_row_id),))
                    except Exception:
                        logger.exception("nws commit: event_log update failed")

            data["_on_broadcast_committed"] = _on_commit

        # Return _render() wire for backward compat with existing call-sites.
        # At dispatch time compose_mesh_message() will use the registered
        # formatter (formatters/nws.py) which re-renders from event.data
        # producing byte-identical output (tier-a).
        _prefix = gate.data_patch.get("_nws_prefix", "")
        return _render(event_type=event_type, area_desc=area_desc,
                        geocoder_city=ge.get("city"), county=county, state=state,
                        expires_epoch=expires_epoch, lat=lat, lon=lon, now=now,
                        prefix=_prefix, d=d)

    # ── NOT cutover: exact legacy path (byte-for-byte unchanged) ─────────────
    try:
        conn = get_db()
    except Exception:
        logger.exception("nws_handler: persistence unavailable")
        return None

    # Tombstone: msgType in {Cancel, Expire} -> log handled=0, no broadcast.
    if msg_type in set(adapter_config.nws.tombstone_msgtypes):
        _log_event(conn, now=now, source="nws", category=category_raw,
                    severity_word=severity_word, event_id_external=cap_id,
                    subject=subject, handled=0,
                    table_name="nws_alerts", table_pk=cap_id)
        return None

    # CAP-severity pre-filter (GATE A) removed. All NWS alerts now flow into
    # the notification pipeline; breadth is governed solely by the per-toggle
    # dispatcher severity threshold.

    # Warning → immediate promotion: deterministically sets _severity_override
    # so a wrong or missing CAP severity field cannot under-rank a real warning.
    # Mirrors the wfigs_handler pattern. Applied before all broadcast-return
    # paths so it covers new alert, cold-start race, and dedup-window re-bcast.
    if isinstance(data, dict) and (
            category_raw.endswith("_warning") or category_raw.endswith(".warning")):
        data["_severity_override"] = "immediate"

    # Per-CAP-id dedup.
    log_id = _log_event_returning_id(
        conn, now=now, source="nws", category=category_raw,
        severity_word=severity_word, event_id_external=cap_id,
        subject=subject, handled=0,
        table_name="nws_alerts", table_pk=cap_id)

    row = conn.execute(
        "SELECT last_broadcast_at FROM nws_alerts WHERE event_id=?",
        (cap_id,)).fetchone()

    if row is None:
        conn.execute(
            "INSERT INTO nws_alerts(event_id, alert_type, severity, county, "
            "state, headline, description, expires_at, first_seen_at, "
            "last_broadcast_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (cap_id, event_type, cap_severity, county, state,
             headline, description, expires_epoch, now, None),
        )
        _prefix = "Update" if _is_update(conn, d) else ""
        wire = _render(event_type=event_type, area_desc=area_desc,
                        geocoder_city=ge.get("city"), county=county, state=state,
                        expires_epoch=expires_epoch, lat=lat, lon=lon, now=now,
                        prefix=_prefix, d=d)
        _attach_commit(data, cap_id=cap_id, event_log_row_id=log_id)
        return wire

    if row["last_broadcast_at"] is None:
        # Cold-start race: row exists but broadcast was previously dropped.
        _prefix = "Update" if _is_update(conn, d) else ""
        wire = _render(event_type=event_type, area_desc=area_desc,
                        geocoder_city=ge.get("city"), county=county, state=state,
                        expires_epoch=expires_epoch, lat=lat, lon=lon, now=now,
                        prefix=_prefix, d=d)
        _attach_commit(data, cap_id=cap_id, event_log_row_id=log_id)
        return wire

    # v0.6-phase3: dedup-window relaxation. If the CAP id was last
    # broadcast more than `nws.duplicate_allowed_after_seconds` ago, allow
    # the re-broadcast with an "Active:" prefix; otherwise suppress.
    last_bcast = float(row["last_broadcast_at"])
    window_s = int(adapter_config.nws.duplicate_allowed_after_seconds)
    if window_s > 0 and (now - last_bcast) >= window_s:
        wire = _render(event_type=event_type, area_desc=area_desc,
                        geocoder_city=ge.get("city"), county=county, state=state,
                        expires_epoch=expires_epoch, lat=lat, lon=lon, now=now,
                        prefix="Active", d=d)
        _attach_commit(data, cap_id=cap_id, event_log_row_id=log_id)
        return wire
    return None


# Hail descriptor -> diameter in inches (NWS convention).
_HAIL_DESCRIPTORS = {
    "pea": 0.25, "half inch": 0.50, "penny": 0.75, "nickel": 0.88,
    "quarter": 1.00, "half dollar": 1.25, "ping pong": 1.50, "ping-pong": 1.50,
    "golf ball": 1.75, "golf": 1.75, "hen egg": 2.00, "tennis ball": 2.50,
    "baseball": 2.75, "softball": 4.00,
}


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
    """Compact a free-form NWS hazard sentence into the terse mesh idiom the
    SVR branch already uses, so no product type (SPS/WSW/FFW/FLW/else) carries a
    bloated line 3. Drops filler ('in excess of' -> '>'), collapses '45 mph' ->
    '45mph', rewrites wind-gust phrasings to 'Nmph gusts', and converts hail
    descriptors ('pea size hail') to numeric inches ('0.25" hail')."""
    if not text:
        return ""
    t = text.strip().rstrip(".")
    # Hail: '<descriptor> size hail' -> 'N" hail' (keep any leading connector).
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
    # Numeric hail: 'N inch hail' / 'N-inch hail' -> 'N" hail'.
    t = re.sub(r"(\d+(?:\.\d+)?)[\- ]inch(?:es)?\s+hail",
               lambda m: _fmt_hail(m.group(1)) or m.group(0), t,
               flags=re.IGNORECASE)
    # Wind-gust phrasings -> 'Nmph gusts'.
    t = re.sub(r"wind gusts?\s+(?:in excess of|up to|to|of|reaching|near|around)"
               r"\s+(\d+)\s*mph", r"\1mph gusts", t, flags=re.IGNORECASE)
    t = re.sub(r"winds?\s+gusting\s+(?:up\s+)?to\s+(\d+)\s*mph",
               r"\1mph gusts", t, flags=re.IGNORECASE)
    # Sustained winds -> 'Nmph winds'.
    t = re.sub(r"(?:damaging\s+)?winds?\s+(?:in excess of|up to|to|of)\s+"
               r"(\d+)\s*mph", r"\1mph winds", t, flags=re.IGNORECASE)
    # Remaining generic filler + spacing.
    t = re.sub(r"\bin excess of\b", ">", t, flags=re.IGNORECASE)
    t = re.sub(r"(\d+)\s*mph", r"\1mph", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _render(*, event_type, area_desc, geocoder_city, county, state,
             expires_epoch, lat, lon, now, prefix: str = "", d: dict = None) -> str:
    d = d or {}
    params = d.get("parameters") or {}
    desc = _parse_nws_description(d.get("description") or "")

    # SAME code drives emoji and line-3 branching
    same_code = ((d.get("eventCode") or {}).get("SAME") or [""])[0]
    emoji = _SAME_EMOJI.get(same_code) or _emoji_for_event(event_type)
    prefix_seg = f"{prefix}: " if prefix else ""

    # Line 1: emoji + event type (no office)
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
        area = cut + "\u2026"
    if time_seg and area:
        line2 = f"{time_seg} — {area}"
    elif time_seg:
        line2 = time_seg
    elif area:
        line2 = area
    else:
        line2 = ""

    # Line 3: hazard + certainty/threat (SAME-code branched)
    # Line 3: TIGHTENED hazard wording. Filler dropped; wind as '60mph winds',
    # hail as numeric inches ('1" hail'); certainty collapsed to radar/observed;
    # hazard groups and certainty joined by " · ".
    certainty = (d.get("certainty") or "").strip()
    line3 = ""
    if same_code == "TOR":
        detection = (params.get("tornadoDetection") or [""])[0]
        status = "on ground" if detection == "OBSERVED" else "radar"
        threat = (params.get("tornadoDamageThreat") or [""])[0]
        threat_seg = f" · {threat.lower()} damage" if threat else ""
        line3 = f"tornado {status}{threat_seg}"
    elif same_code == "SVR":
        wind = (params.get("maxWindGust") or [""])[0]
        hail = (params.get("maxHailSize") or [""])[0]
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
        # First sentence only, then tighten to the terse SVR idiom.
        if ". " in hazard_text:
            hazard_text = hazard_text.split(". ")[0]
        hazard_text = _tighten_hazard(hazard_text)
        # Infer flood cause from description
        desc_lower = (d.get("description") or "").lower()
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
    compass, speed_mph = _parse_motion(params)
    motion = f"Moving {compass} {speed_mph} mph" if compass and speed_mph else ""

    # Parse the (now-full) locations string into an ordered town list. Path
    # order = soonest-impact first ... farthest-along last. The tail element
    # frequently starts with "and " (e.g. "and Shoshone") -> strip that.
    raw = (desc.get("locations") or "").rstrip("., ")
    towns = [t.strip() for t in raw.split(",") if t.strip()]
    if towns:
        towns[-1] = re.sub(r"^and\s+", "", towns[-1], flags=re.IGNORECASE).strip()
        towns = [t for t in towns if t]

    def _dedup(seq):
        """Drop consecutive repeats (a short list can make first == middle),
        so we never render 'Buhl → Buhl → Shoshone'."""
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

    PACKET_LIMIT = budget_for("nws")

    # Location representations from richest to poorest: full comma list ->
    # first→middle→last path sample -> first→last -> first-only -> none. The
    # WHOLE message is measured against the budget for each and the first form
    # that fits wins. Crucially the "— {locs}" segment is only ever attached
    # when the full message fits, so we can never emit a dangling "— …": if no
    # location form fits we fall through to motion-only, then (if even that
    # overflows) drop line 4 entirely.
    loc_options = [", ".join(towns)]                      # full comma list
    if len(towns) >= 3:
        loc_options.append(" → ".join(
            _dedup([towns[0], towns[len(towns) // 2], towns[-1]])))
    if len(towns) >= 2:
        loc_options.append(" → ".join(_dedup([towns[0], towns[-1]])))
    if towns:
        loc_options.append(towns[0])
    loc_options.append("")                                # motion only / empty

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
    # overflow. Line 4 is already budget-fitted above, so this only ever trims
    # the leading lines — it can never manufacture a dangling "— …".
    return fit_to_budget(msg, PACKET_LIMIT)


def _category_to_event_type(category_raw: str) -> str:
    """Best-effort friendly-name derivation when data.event is missing.
    Turns 'wx.alert.severe_thunderstorm_warning' -> 'Severe Thunderstorm Warning'."""
    if not category_raw: return "Weather Alert"
    tail = category_raw.split(".")[-1] if "." in category_raw else category_raw
    return tail.replace("_", " ").title()


def _attach_commit(data: Optional[dict], *, cap_id: str,
                    event_log_row_id: Optional[int]) -> None:
    if not isinstance(data, dict): return

    def _on_commit(committed_at: float) -> None:
        try: conn = get_db()
        except Exception:
            logger.exception("nws commit: persistence unavailable"); return
        conn.execute(
            "UPDATE nws_alerts SET last_broadcast_at=?, "
            "first_broadcast_at=COALESCE(first_broadcast_at, ?) "
            "WHERE event_id=?",
            (int(committed_at), int(committed_at), cap_id))
        if event_log_row_id is not None:
            conn.execute("UPDATE event_log SET handled=1 WHERE id=?",
                          (int(event_log_row_id),))

    data["_on_broadcast_committed"] = _on_commit
    data["_broadcast_audit"] = {"table": "nws_alerts", "pk": cap_id}


def _coerce_severity(sev: Any) -> Optional[str]:
    if sev is None: return None
    if isinstance(sev, str): return sev or None
    try: return str(int(sev))
    except (TypeError, ValueError): return str(sev)


def _log_event(conn, *, now, source, category, severity_word,
                event_id_external, subject, handled, table_name, table_pk) -> None:
    conn.execute(
        "INSERT INTO event_log(received_at, source, category, severity_word, "
        "event_id_external, nats_subject, handled, table_name, table_pk) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (now, source, category, severity_word, event_id_external, subject,
         int(bool(handled)), table_name, table_pk))


def _log_event_returning_id(conn, *, now, source, category, severity_word,
                              event_id_external, subject, handled,
                              table_name, table_pk) -> int:
    cur = conn.execute(
        "INSERT INTO event_log(received_at, source, category, severity_word, "
        "event_id_external, nats_subject, handled, table_name, table_pk) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (now, source, category, severity_word, event_id_external, subject,
         int(bool(handled)), table_name, table_pk))
    return int(cur.lastrowid)
