"""v0.5.10 USGS earthquakes handler.

Broadcast gate (any of these triggers):
    (a) magnitude >= 3.0 globally
    (b) magnitude >= 2.5 within 250 mi of Idaho centroid
    (c) tsunami_warning at any magnitude
    (d) PAGER alert level in {orange, red}

Wire format (multi-line, matches Fire/Roads style):
    Line 1: {emoji} {prefix} M{mag:.1f} — {place_string}
    Line 2: Depth: {depth} km · @ {lat:.3f}, {lon:.3f}
    Line 3: 🚨 TSUNAMI WARNING — only when tsunami flag is set

Emoji:
    Routine          -> 🌐
    M5+              -> ⚠️
    tsunami warning  -> 🚨

place_string: prefer data.place (USGS curated, e.g. "11 km SSW of Snowville,
Utah"); fall back to nearest_town anchor when missing.

Persistence: UPSERT into quake_events using USGS event_id. First sighting
fires New:; revisions UPSERT but don't re-broadcast (v0.5.9 no-Update rule).
"""
from __future__ import annotations
from meshai.adapter_config import adapter_config
from meshai.central.budget import budget_for, fit_to_budget

import logging
import math
import time
from typing import Any, Optional

from meshai.notifications import clock

from meshai.persistence import get_db

logger = logging.getLogger(__name__)


# v0.6-3b: regional gate geography, radius, magnitude floors, PAGER
# level set all live in adapter_config.usgs_quake. Read at use site so
# GUI edits take effect on the next envelope without restart.


def _now() -> int: return int(clock.now())


def _haversine_mi(lat1, lon1, lat2, lon2) -> float:
    R_mi = 3958.8
    p1 = math.radians(lat1); p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1); dl = math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * R_mi * math.atan2(math.sqrt(a), math.sqrt(1-a))


def within_250mi_of_idaho(lat: float, lon: float) -> bool:
    """Return True if (lat, lon) is within the regional gate radius.

    v0.6-3b: name retained for backward-compat with existing tests; the
    centroid + radius now come from adapter_config.usgs_quake.
    """
    if not (isinstance(lat, (int, float)) and isinstance(lon, (int, float))):
        return False
    cen = adapter_config.usgs_quake.regional_centroid
    radius = float(adapter_config.usgs_quake.regional_radius_mi)
    return _haversine_mi(lat, lon, float(cen[0]), float(cen[1])) <= radius


def _should_broadcast(mag: Optional[float], lat: Optional[float],
                       lon: Optional[float], tsunami: bool,
                       pager_alert: Optional[str]) -> bool:
    if tsunami: return True
    pager_set = {s.lower() for s in adapter_config.usgs_quake.broadcast_pager_alerts}
    if pager_alert and pager_alert.lower() in pager_set:
        return True
    if not isinstance(mag, (int, float)): return False
    if mag >= float(adapter_config.usgs_quake.global_mag_floor): return True
    if (mag >= float(adapter_config.usgs_quake.regional_mag_floor)
            and within_250mi_of_idaho(lat, lon)): return True
    return False


def _emoji_for(mag: Optional[float], tsunami: bool) -> str:
    if tsunami: return "🚨"
    if isinstance(mag, (int, float)) and mag >= float(
            adapter_config.usgs_quake.escalate_mag_floor):
        return "⚠️"
    return "🌐"


def handle_quake(envelope: dict, subject: str,
                  data: Optional[dict] = None,
                  now: Optional[int] = None) -> Optional[str]:
    """Central path handler for USGS earthquake envelopes.

    Phase-1 refactor: ALL gating decisions are now delegated to
    `meshai.notifications.gating.quake.decide()`.  This function is kept as a
    thin compatibility bridge so existing call-sites and tests remain stable
    while the new formatter+decider architecture is established.

    On broadcast:
      - Writes canonical data fields into the shared `data` dict so the Event
        carries structured fields (the formatter reads them at dispatch time).
      - Applies GateResult.data_patch (distance_km, is_update, _severity_override,
        _dedup_suffix) into the same dict.
      - Attaches GateResult.commit + _broadcast_audit keys.
      - Returns the _render() wire string for backward-compat (existing tests).
        At dispatch time compose_mesh_message() intercepts via the registered
        formatter and re-renders from event.data (tier-b changes apply there).

    On suppress: returns None (default-deny unchanged).
    """
    if not isinstance(envelope, dict): return None
    inner = envelope.get("data") or {}
    if (inner.get("adapter") or "") != "usgs_quake": return None

    d = inner.get("data") or {}
    geo = inner.get("geo") or {}
    now = now if now is not None else _now()
    category_raw = inner.get("category") or ""
    severity_word = _coerce_severity(inner.get("severity"))

    # ── Extract fields (same normalization as before) ─────────────────────
    event_id = d.get("id") or inner.get("id")
    if not event_id:
        return None

    mag = d.get("magnitude") or d.get("mag")
    if isinstance(mag, str):
        try: mag = float(mag)
        except ValueError: mag = None
    elif isinstance(mag, (int, float)):
        mag = float(mag)

    depth_km = d.get("depth_km") if d.get("depth_km") is not None else d.get("depth")
    place = d.get("place")
    tsunami = bool(d.get("tsunami") or d.get("tsunami_warning"))
    pager_alert = d.get("alert")

    cent = geo.get("centroid") or []
    if isinstance(cent, list) and len(cent) >= 2:
        lon, lat = cent[0], cent[1]
    else:
        lat = lon = None

    occurred_at = None
    tms = d.get("time_ms")
    if isinstance(tms, (int, float)) and tms > 1e12:
        occurred_at = int(tms / 1000)
    elif tms and isinstance(tms, (int, float)):
        occurred_at = int(tms)

    # ── Build canonical data dict (written into shared `data` on broadcast) ─
    canonical: dict = {
        "magnitude": mag,
        "depth_km": depth_km,
        "lat": lat,
        "lon": lon,
        "place": place,
        "tsunami": tsunami,
        "pager": pager_alert,
        "occurred_at": occurred_at,
        "event_id": event_id,
    }

    # ── Delegate to gating module ─────────────────────────────────────────
    from meshai.notifications.gating.quake import decide as _gate_decide
    gate = _gate_decide(canonical, source="usgs_quake", now=float(now))

    # ── Persistence logging (unchanged from original) ─────────────────────
    try:
        conn = get_db()
    except Exception:
        logger.exception("quake_handler: persistence unavailable")
        return None

    if not gate.broadcast:
        _log_event(conn, now=now, source="usgs_quake", category=category_raw,
                    severity_word=severity_word, event_id_external=event_id,
                    subject=subject, handled=0,
                    table_name="quake_events", table_pk=event_id)
        return None

    log_id = _log_event_returning_id(
        conn, now=now, source="usgs_quake", category=category_raw,
        severity_word=severity_word, event_id_external=event_id,
        subject=subject, handled=0,
        table_name="quake_events", table_pk=event_id)

    # ── Write canonical fields into shared data dict ──────────────────────
    # Cutover gate: when the category has been explicitly cut over, write
    # gate.data_patch and use gate.commit (new path live).  Otherwise use the
    # old-style _attach_commit so the live broadcast stays byte-for-byte
    # identical to pre-Phase-1 behavior while the new path bakes in shadow.
    from meshai.notifications.cutover import is_cutover
    if isinstance(data, dict):
        data.update(canonical)
        if is_cutover("earthquake_event"):
            # NEW PATH: gate.data_patch provides distance_km, _severity_override, etc.
            data.update(gate.data_patch)
            data["_broadcast_audit"] = {"table": "quake_events", "pk": event_id}

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
                        logger.exception("quake commit: event_log update failed")

            data["_on_broadcast_committed"] = _on_commit
        else:
            # NOT cutover: old-style attach (canonical fields only, no data_patch).
            # Preserves exact pre-Phase-1 live behavior; new formatter bakes in shadow.
            _attach_commit(data, event_id=event_id, event_log_row_id=log_id)

    # ── Return _render() wire (backward compat — existing tests check this) ─
    # At dispatch time compose_mesh_message() calls the registered formatter
    # (if cutover) which re-renders from event.data with tier-b changes (PAGER
    # + update-prefix).  The _render() wire here is used by both paths and by
    # direct handle_quake() callers (tests, legacy paths).
    return _render(mag=mag, place=place, depth_km=depth_km, lat=lat, lon=lon,
                    tsunami=tsunami, is_update=False)


def _render(*, mag, place, depth_km, lat, lon, tsunami, is_update=False) -> str:
    emoji = _emoji_for(mag, tsunami)
    mag_str = f"{mag:.1f}" if isinstance(mag, (int, float)) else "?"
    place_str = place if place else "unknown location"
    prefix = "Update:" if is_update else "New:"

    # Line 1: prefix + magnitude + place
    line1 = f"{emoji} {prefix} M{mag_str} \u2014 {place_str}"

    # Line 2: depth + coords
    parts = []
    if isinstance(depth_km, (int, float)):
        parts.append(f"Depth: {int(round(depth_km))} km")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        parts.append(f"@ {lat:.3f}, {lon:.3f}")
    line2 = " \u00b7 ".join(parts) if parts else None

    # Line 3: tsunami warning (only when present)
    line3 = "\U0001f6a8 TSUNAMI WARNING" if tsunami else None

    msg = "\n".join(l for l in [line1, line2, line3] if l)
    # Safety cap: fit the broadcast string to the mesh packet budget.
    return fit_to_budget(msg, budget_for("usgs_quake"))


def _attach_commit(data: Optional[dict], *, event_id: str,
                    event_log_row_id: Optional[int]) -> None:
    if not isinstance(data, dict): return

    def _on_commit(committed_at: float) -> None:
        try: conn = get_db()
        except Exception:
            logger.exception("quake commit: persistence unavailable"); return
        conn.execute(
            "UPDATE quake_events SET last_broadcast_at=?, "
            "first_broadcast_at=COALESCE(first_broadcast_at, ?) WHERE event_id=?",
            (int(committed_at), int(committed_at), event_id))
        if event_log_row_id is not None:
            conn.execute("UPDATE event_log SET handled=1 WHERE id=?",
                          (int(event_log_row_id),))

    data["_on_broadcast_committed"] = _on_commit
    data["_broadcast_audit"] = {"table": "quake_events", "pk": event_id}


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
