"""Satellite pass wire formatting + the shared source-agnostic broadcast gate.

Relocated from `meshai.central.satpass_handler` (the retired Central
NATS-consumer service) during the Central ripout. This module now has one
caller: the native SGP4 adapter (`meshai.env.satpass`), which computes ALL
observers for a satellite in a single `tick()`, consolidates across
observers in-memory, and calls `gate_consolidated_pass` directly — no
`satpass_pending` buffer, no consumer/timer.

Severity mapping:
    4 = immediate (>= 60 deg max elevation)
    3 = priority  (>= 45 deg max elevation)
    <= 2 = routine

Broadcast wire format (single line, LoRa-tight, absolute local time — a
~12h-ahead heads-up):
    🛰️ {short_name} {rise} {AM/PM} {TZ}[ tomorrow], max {el}° {compass} ({dur} min)[ (region)]
    - short_name: short ham designation (ISS/AO-27/AO-91), else cleaned catalog
    - compass: aos→peak→los with consecutive duplicates collapsed (no E→E→E)
    - region: appended ONLY for a genuine multi-observer sweep with different
      friendly names; dropped for a single observer or the synthetic coverage_center
DM wire format (compact, exact degrees):
    {name} {HH:MM}–{HH:MM} {TZ} max {el}° {aos_compass}→{los_compass}
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from meshai.adapter_config import adapter_config
from meshai.notifications.formatters._budget import budget_for, fit_to_budget
from meshai.persistence import get_db

logger = logging.getLogger(__name__)

# Mountain time for broadcast display
_TZ = ZoneInfo("America/Boise")


# Short ham designations for common broadcast satellites, keyed by NORAD id.
# Listeners recognize "AO-91" far faster than the cluttered catalog name
# "RADFXSAT (FOX-1B)".
_SHORT_SAT_NAMES = {
    25544: "ISS",
    22825: "AO-27",
    43017: "AO-91",
}

# Name-substring fallback (upper-cased contains) for when the NORAD id isn't
# in the map but the catalog name is recognizable.
_SHORT_NAME_SUBSTR = (
    ("ZARYA", "ISS"),
    ("EYESAT", "AO-27"),
    ("AO-27", "AO-27"),
    ("RADFXSAT", "AO-91"),
    ("FOX-1B", "AO-91"),
)


def _short_sat_name(norad_id: Optional[int], sat_name: Optional[str]) -> str:
    """Resolve a short, listener-friendly satellite name.

    NORAD-id map first, then a name-substring fallback, then a cleaned
    catalog name (parenthetical stripped, e.g. "RADFXSAT (FOX-1B)" ->
    "RADFXSAT"). Always returns a non-empty string.
    """
    nid: Optional[int] = None
    if norad_id is not None:
        try:
            nid = int(norad_id)
        except (TypeError, ValueError):
            nid = None
    if nid is not None and nid in _SHORT_SAT_NAMES:
        return _SHORT_SAT_NAMES[nid]

    up = (sat_name or "").upper()
    for sub, short in _SHORT_NAME_SUBSTR:
        if sub in up:
            return short

    cleaned = re.sub(r"\s*\(.*?\)", "", sat_name or "").strip()
    return cleaned or (sat_name or "").strip() or "SAT"


def _collapse_compass(*points: Optional[str]) -> str:
    """Join compass points, dropping empties and consecutive duplicates.

    "E","E","E" -> "E"; "E","SE","SE" -> "E→SE"; "S","W","NW" -> "S→W→NW".
    """
    out: list[str] = []
    for p in points:
        if not p:
            continue
        if not out or out[-1] != p:
            out.append(p)
    return "→".join(out)


# Synthetic coverage-centroid observer markers — these are meaningless to
# listeners, so the region parenthetical is dropped when either endpoint is one.
_SYNTHETIC_OBSERVERS = {"coverage_center", "coverage center"}


def _is_synthetic_observer(label: Optional[str]) -> bool:
    return bool(label) and label.strip().lower() in _SYNTHETIC_OBSERVERS


def _region_paren(entry: Optional[str], exit_: Optional[str]) -> str:
    """Region suffix for a genuine multi-observer sweep, else ''.

    Only rendered when both endpoints exist, differ, and neither is the
    synthetic coverage-centroid observer. Single observer / synthetic ->
    no parenthetical.
    """
    if not entry or not exit_ or entry == exit_:
        return ""
    if _is_synthetic_observer(entry) or _is_synthetic_observer(exit_):
        return ""
    return f" ({entry}→{exit_})"


def _format_time_12h(epoch: Optional[int]) -> str:
    """Format epoch to h:mm AM/PM in America/Boise."""
    if epoch is None:
        return "?"
    try:
        dt = datetime.fromtimestamp(epoch, tz=_TZ)
        # Use %-I for no-leading-zero hour on Linux, fall back to %I
        try:
            return dt.strftime("%-I:%M")
        except ValueError:
            return dt.strftime("%I:%M").lstrip("0")
    except Exception:
        return "?"


def _format_ampm(epoch: Optional[int]) -> str:
    """Return AM or PM for an epoch in America/Boise."""
    if epoch is None:
        return ""
    try:
        dt = datetime.fromtimestamp(epoch, tz=_TZ)
        return dt.strftime("%p")
    except Exception:
        return ""


def _format_time_24h(epoch: Optional[int]) -> str:
    """Format epoch to HH:MM local time string (24h)."""
    if epoch is None:
        return "?"
    try:
        dt = datetime.fromtimestamp(epoch, tz=_TZ)
        return dt.strftime("%H:%M")
    except Exception:
        return "?"


def _tz_abbr(epoch: Optional[int]) -> str:
    """Return timezone abbreviation for an epoch in America/Boise."""
    if epoch is None:
        return "MDT"
    try:
        dt = datetime.fromtimestamp(epoch, tz=_TZ)
        return dt.strftime("%Z")
    except Exception:
        return "MDT"


def _azimuth_to_compass(az_deg: float) -> str:
    """Convert azimuth in degrees to 8-point compass direction."""
    az = az_deg % 360
    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    idx = int((az + 22.5) / 45) % 8
    return dirs[idx]


def _date_label(epoch: Optional[int]) -> str:
    """Return a date qualifier for passes not happening today.

    Returns '' for today, 'tomorrow' for tomorrow, or 'Mon Jun 17'
    for anything further out.
    """
    if epoch is None:
        return ""
    try:
        now_local = datetime.now(tz=_TZ)
        pass_local = datetime.fromtimestamp(epoch, tz=_TZ)
        delta_days = (pass_local.date() - now_local.date()).days
        if delta_days == 0:
            return ""
        if delta_days == 1:
            return " tomorrow"
        return pass_local.strftime(" %a %b %-d")
    except Exception:
        return ""


def format_pass(*, sat_name: str, max_el: float,
                aos_epoch: Optional[int], los_epoch: Optional[int],
                aos_compass: str, los_compass: str,
                broadcast: bool = True,
                entry_observer: Optional[str] = None,
                exit_observer: Optional[str] = None,
                peak_compass: Optional[str] = None,
                norad_id: Optional[int] = None) -> str:
    """Unified pass formatter with mode switch.

    broadcast=True:  Single clean line, absolute local time (a ~12h-ahead
        heads-up), LoRa budget.
        🛰️ {short_name} {rise} {AM/PM} {TZ}[ tomorrow], max {el}° {compass} ({dur} min)[ (region)]

    broadcast=False: Compact DM format with exact degrees.
        {name} {HH:MM}–{HH:MM} {TZ} max {el}° {aos_compass}→[peak→]{los_compass}

    peak_compass: compass direction at peak elevation. Threaded through the
        compass sweep (aos→peak→los); consecutive duplicate points are
        collapsed so a degenerate "E→E→E" renders as "E".

    norad_id: used to resolve the short ham name for the broadcast wire.
    """
    # Compass sweep segment: aos->peak->los with consecutive duplicates dropped.
    compass_seg = _collapse_compass(aos_compass, peak_compass, los_compass)

    # Duration in whole minutes
    if aos_epoch is not None and los_epoch is not None:
        dur_min = max(1, round((los_epoch - aos_epoch) / 60))
    else:
        dur_min = 0

    if broadcast:
        name = _short_sat_name(norad_id, sat_name)
        rise_str = _format_time_12h(aos_epoch)
        ampm = _format_ampm(aos_epoch)
        tz = _tz_abbr(aos_epoch)
        date_lbl = _date_label(aos_epoch)
        el = int(round(max_el)) if max_el is not None else 0
        region = _region_paren(entry_observer, exit_observer)

        # Elevation + compass; the compass may be empty when no azimuth data
        # was available, in which case it is simply omitted (no stray space).
        core = f"max {el}°"
        if compass_seg:
            core += f" {compass_seg}"
        line = (
            f"\U0001F6F0️ {name} {rise_str} {ampm} {tz}{date_lbl}, "
            f"{core} ({dur_min} min){region}"
        )

        # Safety cap: fit the broadcast string to the mesh packet budget.
        return fit_to_budget(line, budget_for("satpass"))
    else:
        # DM format: compact with exact degrees
        aos_str = _format_time_24h(aos_epoch)
        los_str = _format_time_24h(los_epoch)
        tz = _tz_abbr(aos_epoch)
        return (f"{sat_name} {aos_str}–{los_str} {tz} "
                f"max {int(max_el)}° "
                f"{compass_seg}")


def _map_severity(max_el: float) -> str:
    """Map max elevation to severity word."""
    if max_el >= 60:
        return "immediate"
    if max_el >= 45:
        return "priority"
    return "routine"


def _canonical_id(norad_id: int, aos_epoch: int) -> str:
    """Generate consolidated canonical event ID (observer-independent)."""
    bucket = aos_epoch // 3600
    return f"{norad_id}:{bucket}"


def _check_rate_cap(conn, now: int, max_per_hour: int) -> tuple[bool, int]:
    """Check if broadcast rate cap has been reached.

    Returns (allowed, suppressed_count) where suppressed_count is the
    number of broadcasts already made in the current hour window.
    """
    hour_start = (now // 3600) * 3600
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM satpass_events "
        "WHERE last_broadcast_at >= ? AND last_broadcast_at IS NOT NULL",
        (hour_start,),
    ).fetchone()
    count = row["cnt"] if row else 0
    return (count < max_per_hour, count)


def gate_consolidated_pass(consolidated: dict, *,
                           now: int) -> tuple[str, dict] | None:
    """Source-agnostic broadcast gate for an already-consolidated pass.

    The native env.satpass adapter (which consolidates in-memory across
    observers in one tick) calls this single function so the broadcast
    decision is deterministic and self-contained.

    `consolidated` is a dict describing one merged pass with keys:
        consolidated_id (str, = {norad}:{aos_epoch//3600}),
        norad_id, sat_name, max_elevation,
        aos_epoch, los_epoch (int epoch seconds),
        aos_compass, los_compass, peak_compass,
        entry_observer, exit_observer,
        observer_list (comma-joined observer slugs for the audit column).

    Applies, in order: dedup-vs-`satpass_events`, rate cap, wire build,
    dry-run gate, `satpass_events` upsert, and the deferred `_attach_commit`
    that upserts last/first_broadcast_at on delivery. Returns (wire, data) to
    broadcast, or None if suppressed. `now` is threaded explicitly for
    determinism (rate-cap window + first_seen_at).
    """
    try:
        conn = get_db()
    except Exception:
        logger.exception("satpass gate: persistence unavailable")
        return None

    cfg = adapter_config.satpass

    consolidated_id = consolidated["consolidated_id"]
    norad_id = consolidated["norad_id"]
    sat_name = consolidated["sat_name"]
    max_el = consolidated["max_elevation"]
    aos_epoch = consolidated["aos_epoch"]
    los_epoch = consolidated["los_epoch"]
    aos_compass = consolidated["aos_compass"]
    los_compass = consolidated["los_compass"]
    peak_compass = consolidated.get("peak_compass")
    entry_obs = consolidated.get("entry_observer")
    exit_obs = consolidated.get("exit_observer")
    observer_list = consolidated.get("observer_list") or (entry_obs or "")

    # Dedup against satpass_events
    existing = conn.execute(
        "SELECT last_broadcast_at FROM satpass_events WHERE event_id=?",
        (consolidated_id,)).fetchone()
    if existing and existing["last_broadcast_at"] is not None:
        return None

    # Rate cap
    max_per_hour = int(getattr(cfg, "max_broadcasts_per_hour", 4))
    allowed, count = _check_rate_cap(conn, now, max_per_hour)
    if not allowed:
        logger.info("satpass: rate cap reached (%d/%d), suppressing consolidated pass %s",
                     count, max_per_hour, consolidated_id)
        return None

    # Build consolidated wire — always pass observer names for region context
    wire = format_pass(sat_name=sat_name, max_el=max_el, norad_id=norad_id,
                      aos_epoch=aos_epoch, los_epoch=los_epoch,
                      aos_compass=aos_compass, los_compass=los_compass,
                      peak_compass=peak_compass,
                      entry_observer=entry_obs, exit_observer=exit_obs)

    # Dry-run gate
    dry_run = getattr(cfg, "dry_run", True)
    if dry_run:
        logger.info("DRY-RUN would air (consolidated): %s", wire)
        return None

    # Upsert consolidated record into satpass_events
    _upsert_satpass(conn, event_id=consolidated_id, norad_id=norad_id,
                    sat_name=sat_name, observer=observer_list,
                    max_elevation=max_el, aos_at=aos_epoch,
                    los_at=los_epoch, payload_json=None,
                    first_seen_at=now, set_last_broadcast=False)

    # Prepare data dict with callbacks
    severity_word = _map_severity(max_el)
    data = {"_meshai_precomposed": True, "_severity_override": severity_word}
    _attach_commit(data, event_id=consolidated_id, event_log_row_id=None)

    return wire, data


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
