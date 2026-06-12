"""v0.7 Satellite pass handler.

Broadcast regional satellite passes from Central's CENTRAL_SAT stream.

Filter criteria:
    (a) Pass must be for an observer in adapter_config.satpass.observers
        (empty list = all observers)
    (b) Max elevation must meet adapter_config.satpass.min_elevation (default 30)
    (c) Optional NORAD ID filter via adapter_config.satpass.norad_ids

Dedup bucketing: canonical event_id = {norad_id}:{observer}:{aos_bucket}
where aos_bucket = floor(aos_epoch / 3600) -- one broadcast per satellite
per observer per hour window.

Severity mapping:
    4 = immediate (>= 60 deg max elevation)
    3 = priority  (>= 45 deg max elevation)
    <= 2 = routine

Wire format (multi-line, LoRa-tight):
    Line 1: satellite emoji {sat_name} Pass -- {max_el} deg max
    Line 2: AOS {aos_time} . LOS {los_time}
    Line 3: {observer} . {direction}
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from meshai.adapter_config import adapter_config
from meshai.persistence import get_db

logger = logging.getLogger(__name__)


def _now() -> int:
    return int(time.time())


def _coerce_float(v) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _coerce_int(v) -> Optional[int]:
    if v is None:
        return None
    if isinstance(v, int):
        return v
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _parse_iso_epoch(s) -> Optional[int]:
    """Parse ISO-8601 timestamp to epoch seconds."""
    if not s or not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except Exception:
        return None


def _format_time(epoch: Optional[int]) -> str:
    """Format epoch to HH:MM local time string."""
    if epoch is None:
        return "?"
    try:
        dt = datetime.fromtimestamp(epoch)
        return dt.strftime("%H:%M")
    except Exception:
        return "?"


def _map_severity(max_el: float) -> str:
    """Map max elevation to severity word."""
    if max_el >= 60:
        return "immediate"
    if max_el >= 45:
        return "priority"
    return "routine"


def _canonical_id(norad_id: int, observer: str, aos_epoch: int) -> str:
    """Generate dedup-bucketed canonical event ID.

    Bucket = floor(aos_epoch / 3600) -- one broadcast per sat/observer/hour.
    """
    bucket = aos_epoch // 3600
    return f"{norad_id}:{observer}:{bucket}"


def handle_satpass(envelope: dict, subject: str,
                   data: Optional[dict] = None,
                   now: Optional[int] = None) -> Optional[str]:
    """Process a satellite pass event from Central.

    Returns wire message string if pass should be broadcast, None otherwise.
    """
    if not isinstance(envelope, dict):
        return None

    inner = envelope.get("data") or {}
    adapter = inner.get("adapter") or ""

    # Only handle pass prediction adapters (wire names from Central)
    if adapter not in ("n2yo_visualpasses", "satpass_predict"):
        return None

    # Enabled gate: silently drop when disabled, log once at INFO
    cfg = adapter_config.satpass
    if not getattr(cfg, "enabled", False):
        if not getattr(handle_satpass, "_disabled_logged", False):
            logger.info("satpass disabled; sat pass events dropped")
            handle_satpass._disabled_logged = True
        return None

    d = inner.get("data") or {}
    now = now if now is not None else _now()

    # Extract pass data
    norad_id = _coerce_int(d.get("norad_id") or d.get("satid"))
    sat_name = d.get("satellite_name") or f"SAT-{norad_id}"
    observer = d.get("observer_name") or d.get("observer_slug") or "unknown"
    max_el = _coerce_float(d.get("max_elevation_deg"))
    aos_iso = d.get("aos_time")
    los_iso = d.get("los_time")
    direction = d.get("azimuth_at_peak_compass") or ""

    if norad_id is None or max_el is None:
        logger.debug("satpass_handler: missing norad_id or max_elevation_deg")
        return None

    aos_epoch = _parse_iso_epoch(aos_iso)
    los_epoch = _parse_iso_epoch(los_iso)

    if aos_epoch is None:
        logger.debug("satpass_handler: could not parse aos time")
        return None

    # Observer filter (empty = all)
    observers = getattr(cfg, "observers", []) or []
    if observers and observer not in observers:
        logger.debug("satpass_handler: observer %r not in configured list", observer)
        return None

    # NORAD ID filter (empty = all)
    norad_ids = getattr(cfg, "norad_ids", []) or []
    if norad_ids and norad_id not in norad_ids:
        logger.debug("satpass_handler: norad_id %d not in configured list", norad_id)
        return None

    # Elevation floor
    min_el = float(getattr(cfg, "min_elevation", 30))
    if max_el < min_el:
        logger.debug("satpass_handler: max_el %.1f below floor %.1f", max_el, min_el)
        return None

    # Generate canonical dedup ID
    event_id = _canonical_id(norad_id, observer, aos_epoch)
    severity_word = _map_severity(max_el)
    category_raw = inner.get("category") or "sat.pass"

    try:
        conn = get_db()
    except Exception:
        logger.exception("satpass_handler: persistence unavailable")
        return None

    # Check for existing broadcast
    row = conn.execute(
        "SELECT last_broadcast_at FROM satpass_events WHERE event_id=?",
        (event_id,)).fetchone()

    payload_json = None
    try:
        payload_json = json.dumps(d, default=str)[:8000]
    except Exception:
        pass

    # Log the event
    log_id = _log_event_returning_id(
        conn, now=now, source="satpass", category=category_raw,
        severity_word=severity_word, event_id_external=event_id,
        subject=subject, handled=0,
        table_name="satpass_events", table_pk=event_id)

    if row is None:
        # First time seeing this pass bucket
        _upsert_satpass(conn, event_id=event_id, norad_id=norad_id,
                        sat_name=sat_name, observer=observer,
                        max_elevation=max_el, aos_at=aos_epoch,
                        los_at=los_epoch, payload_json=payload_json,
                        first_seen_at=now, set_last_broadcast=False)
        wire = _render(sat_name, max_el, aos_epoch, los_epoch, observer, direction)
        _attach_commit(data, event_id=event_id, event_log_row_id=log_id)
        return wire

    if row["last_broadcast_at"] is None:
        # Seen but not yet broadcast
        wire = _render(sat_name, max_el, aos_epoch, los_epoch, observer, direction)
        _attach_commit(data, event_id=event_id, event_log_row_id=log_id)
        return wire

    # Already broadcast this pass bucket
    return None


def _render(sat_name: str, max_el: float, aos_epoch: Optional[int],
            los_epoch: Optional[int], observer: str, direction: str) -> str:
    """Render wire format message."""
    aos_str = _format_time(aos_epoch)
    los_str = _format_time(los_epoch)

    line1 = f"\U0001F6F0\uFE0F {sat_name} Pass \u2014 {int(max_el)}\u00B0 max"
    line2 = f"AOS {aos_str} \u00B7 LOS {los_str}"
    line3 = f"{observer}"
    if direction:
        line3 += f" \u00B7 {direction}"

    return "\n".join([line1, line2, line3])


def _upsert_satpass(conn, *, event_id, norad_id, sat_name, observer,
                    max_elevation, aos_at, los_at, payload_json,
                    first_seen_at, set_last_broadcast=False,
                    broadcast_at=None) -> None:
    """Insert or update satpass_events row."""
    existing = conn.execute(
        "SELECT 1 FROM satpass_events WHERE event_id=?", (event_id,)).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO satpass_events(event_id, norad_id, sat_name, observer, "
            "max_elevation, aos_at, los_at, payload_json, first_seen_at, "
            "last_broadcast_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (event_id, norad_id, sat_name, observer, max_elevation, aos_at,
             los_at, payload_json, first_seen_at,
             broadcast_at if set_last_broadcast else None))
    else:
        conn.execute(
            "UPDATE satpass_events SET sat_name=?, max_elevation=?, "
            "payload_json=? WHERE event_id=?",
            (sat_name, max_elevation, payload_json, event_id))


def _attach_commit(data: Optional[dict], *, event_id: str,
                   event_log_row_id: Optional[int]) -> None:
    """Attach post-broadcast commit callback."""
    if not isinstance(data, dict):
        return

    def _on_commit(committed_at: float) -> None:
        try:
            conn = get_db()
        except Exception:
            logger.exception("satpass commit: persistence unavailable")
            return
        conn.execute(
            "UPDATE satpass_events SET last_broadcast_at=?, "
            "first_broadcast_at=COALESCE(first_broadcast_at, ?) WHERE event_id=?",
            (int(committed_at), int(committed_at), event_id))
        if event_log_row_id is not None:
            conn.execute("UPDATE event_log SET handled=1 WHERE id=?",
                         (int(event_log_row_id),))

    data["_on_broadcast_committed"] = _on_commit
    data["_broadcast_audit"] = {"table": "satpass_events", "pk": event_id}


def _log_event_returning_id(conn, *, now, source, category, severity_word,
                            event_id_external, subject, handled,
                            table_name, table_pk) -> int:
    """Insert event_log row and return its ID."""
    cur = conn.execute(
        "INSERT INTO event_log(received_at, source, category, severity_word, "
        "event_id_external, nats_subject, handled, table_name, table_pk) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (now, source, category, severity_word, event_id_external, subject,
         int(bool(handled)), table_name, table_pk))
    return int(cur.lastrowid)


# Schema for satpass_events table (run once at startup via persistence)
SCHEMA_SATPASS_EVENTS = """
CREATE TABLE IF NOT EXISTS satpass_events (
    event_id TEXT PRIMARY KEY,
    norad_id INTEGER,
    sat_name TEXT,
    observer TEXT,
    max_elevation REAL,
    aos_at INTEGER,
    los_at INTEGER,
    payload_json TEXT,
    first_seen_at INTEGER,
    first_broadcast_at INTEGER,
    last_broadcast_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_satpass_norad ON satpass_events(norad_id);
CREATE INDEX IF NOT EXISTS idx_satpass_observer ON satpass_events(observer);
CREATE INDEX IF NOT EXISTS idx_satpass_aos ON satpass_events(aos_at);
"""
