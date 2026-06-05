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

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

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


def _now() -> int: return int(time.time())


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
    if not isinstance(envelope, dict): return None
    inner = envelope.get("data") or {}
    if (inner.get("adapter") or "") != "nws": return None

    d = inner.get("data") or {}
    geo = inner.get("geo") or {}
    ge = (d.get("_enriched") or {}).get("geocoder") or {}
    now = now if now is not None else _now()
    category_raw = inner.get("category") or ""
    severity_word = _coerce_severity(inner.get("severity"))

    try:
        conn = get_db()
    except Exception:
        logger.exception("nws_handler: persistence unavailable")
        return None

    cap_id = d.get("id") or inner.get("id")
    if not cap_id:
        return None

    # Tombstone: msgType in {Cancel, Expire} -> log handled=0, no broadcast.
    msg_type = d.get("msgType")
    if msg_type in set(adapter_config.nws.tombstone_msgtypes):
        _log_event(conn, now=now, source="nws", category=category_raw,
                    severity_word=severity_word, event_id_external=cap_id,
                    subject=subject, handled=0,
                    table_name="nws_alerts", table_pk=cap_id)
        return None

    # Severity gate (CAP string from data.severity, fall back to category
    # heuristic for envelopes that lack the field).
    cap_sev = d.get("severity")
    if cap_sev not in set(adapter_config.nws.broadcast_severities):
        # Heuristic: category like wx.alert.severe_thunderstorm_warning ->
        # treat as Severe even when CAP severity field is missing.
        # v0.6-3b: gated by adapter_config.nws.warning_suffix_promotes.
        if (not bool(adapter_config.nws.warning_suffix_promotes)) or not (
                category_raw.endswith("_warning") or category_raw.endswith(".warning")):
            _log_event(conn, now=now, source="nws", category=category_raw,
                        severity_word=severity_word, event_id_external=cap_id,
                        subject=subject, handled=0,
                        table_name="nws_alerts", table_pk=cap_id)
            return None

    # Per-CAP-id dedup.
    log_id = _log_event_returning_id(
        conn, now=now, source="nws", category=category_raw,
        severity_word=severity_word, event_id_external=cap_id,
        subject=subject, handled=0,
        table_name="nws_alerts", table_pk=cap_id)

    row = conn.execute(
        "SELECT last_broadcast_at FROM nws_alerts WHERE event_id=?",
        (cap_id,)).fetchone()

    event_type = d.get("event") or _category_to_event_type(category_raw)
    area_desc = d.get("areaDesc")
    headline = d.get("headline")
    description = d.get("description")
    cap_severity = d.get("severity")
    county = d.get("areaDesc") or ge.get("county")
    state = ge.get("state") or d.get("state")
    expires_epoch = _parse_iso(d.get("expires"))

    lat = lon = None
    cent = geo.get("centroid") or []
    if isinstance(cent, list) and len(cent) >= 2:
        lon, lat = cent[0], cent[1]

    if row is None:
        conn.execute(
            "INSERT INTO nws_alerts(event_id, alert_type, severity, county, "
            "state, headline, description, expires_at, first_seen_at, "
            "last_broadcast_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (cap_id, event_type, cap_severity, county, state,
             headline, description, expires_epoch, now, None),
        )
        wire = _render(event_type=event_type, area_desc=area_desc,
                        geocoder_city=ge.get("city"), county=county, state=state,
                        expires_epoch=expires_epoch, lat=lat, lon=lon, now=now)
        _attach_commit(data, cap_id=cap_id, event_log_row_id=log_id)
        return wire

    if row["last_broadcast_at"] is None:
        # Cold-start race: row exists but broadcast was previously dropped.
        wire = _render(event_type=event_type, area_desc=area_desc,
                        geocoder_city=ge.get("city"), county=county, state=state,
                        expires_epoch=expires_epoch, lat=lat, lon=lon, now=now)
        _attach_commit(data, cap_id=cap_id, event_log_row_id=log_id)
        return wire

    # Already broadcast -- no Update for v0.5.10 (mirrors v0.5.9 incident rule).
    return None


def _render(*, event_type, area_desc, geocoder_city, county, state,
             expires_epoch, lat, lon, now) -> str:
    emoji = _emoji_for_event(event_type)
    anchor = _location_anchor(area_desc, geocoder_city, county, state)
    expires_seg = _format_expires_short(expires_epoch, now=now)
    coords = ""
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        coords = f", @ {lat:.3f},{lon:.3f}"
    return f"{emoji} {event_type or 'Weather Alert'}: {anchor}, {expires_seg}{coords}"


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
        conn.execute("UPDATE nws_alerts SET last_broadcast_at=? WHERE event_id=?",
                      (int(committed_at), cap_id))
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
