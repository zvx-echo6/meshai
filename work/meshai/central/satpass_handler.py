"""v0.7 Satellite pass handler.

Broadcast regional satellite passes from Central's CENTRAL_SAT stream.

Filter criteria:
    (a) Pass must be for an observer in adapter_config.satpass.observers
        (empty list = all observers)
    (b) Max elevation must meet adapter_config.satpass.min_elevation (default 30)
    (c) Opt-in NORAD ID filter via adapter_config.satpass.norad_ids
        (empty list = broadcast NOTHING — opt-in only)

Rate cap: adapter_config.satpass.max_broadcasts_per_hour (default 4).
Dry-run: adapter_config.satpass.dry_run (default True) — logs wire text
    at INFO with "DRY-RUN would air:" prefix, does not dispatch.

Dedup bucketing: canonical event_id = {norad_id}:{aos_bucket}
where aos_bucket = floor(aos_epoch / 3600) -- one broadcast per satellite
per hour window, consolidated across all observers.

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

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from meshai.adapter_config import adapter_config
from meshai.notifications.formatters._budget import budget_for, fit_to_budget
from meshai.persistence import get_db

logger = logging.getLogger(__name__)

# Mountain time for broadcast display
_TZ = ZoneInfo("America/Boise")

# Module-level signal: consolidation IDs that need timer scheduling.
# Consumer polls this after each satpass _normalize() call.
_pending_consolidation_ids: set[str] = set()

# Baseline consolidation delay, in seconds, from a pending row's arrival to
# when its consolidated broadcast should fire. This is the DURABLE fire-time
# basis persisted as satpass_pending.due_at (= received_at + this).
#
# It matches the live consumer's baseline: the consumer schedules the timer
# at `5.0 + N*60` where N is the count of OTHER in-flight timers (a runtime
# anti-thundering-herd stagger). The `+N*60` term depends on transient
# in-memory scheduler state that has no meaning across a restart, so it is
# deliberately NOT persisted; only the N=0 baseline (5s) is durable. The
# live in-memory timer still drives normal operation exactly as before —
# due_at is purely the reboot-recovery backstop the in-memory timer can't be.
CONSOLIDATION_DELAY = 5


def drain_pending_consolidation_ids() -> set[str]:
    """Atomically drain and return all pending consolidation IDs."""
    ids = _pending_consolidation_ids.copy()
    _pending_consolidation_ids.clear()
    return ids


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


def _elevation_bucket(max_el: float) -> str:
    """Map max elevation to human-readable bucket name.

    Retained for the DM/other paths; the broadcast wire now shows numeric
    degrees (`max NN°`) instead of a bucket word.
    """
    if max_el >= 60:
        return "overhead"
    if max_el >= 30:
        return "high pass"
    return "low pass"


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
        core = f"max {el}\u00B0"
        if compass_seg:
            core += f" {compass_seg}"
        line = (
            f"\U0001F6F0\uFE0F {name} {rise_str} {ampm} {tz}{date_lbl}, "
            f"{core} ({dur_min} min){region}"
        )

        # Safety cap: fit the broadcast string to the mesh packet budget.
        return fit_to_budget(line, budget_for("satpass"))
    else:
        # DM format: compact with exact degrees
        aos_str = _format_time_24h(aos_epoch)
        los_str = _format_time_24h(los_epoch)
        tz = _tz_abbr(aos_epoch)
        return (f"{sat_name} {aos_str}\u2013{los_str} {tz} "
                f"max {int(max_el)}\u00B0 "
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


def _cleanup_pending(conn, consolidated_id: str) -> None:
    """Remove all pending rows for a consolidated ID."""
    conn.execute("DELETE FROM satpass_pending WHERE consolidated_id=?",
                 (consolidated_id,))


def handle_satpass(envelope: dict, subject: str,
                   data: Optional[dict] = None,
                   now: Optional[int] = None) -> Optional[str]:
    """Process a satellite pass event from Central.

    Per-observer arrivals are accumulated into satpass_pending table.
    Returns None (suppressing immediate broadcast).
    Consolidation ID is added to _pending_consolidation_ids for consumer
    to schedule a 5s timer.
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
    # Compass directions: prefer precomputed _compass strings (n2yo path),
    # fall back to converting raw azimuth degrees (satpass_predict path).
    aos_compass = d.get("azimuth_at_aos_compass") or (
        _azimuth_to_compass(d["azimuth_at_aos"]) if d.get("azimuth_at_aos") is not None else "")
    los_compass = d.get("azimuth_at_los_compass") or (
        _azimuth_to_compass(d["azimuth_at_los"]) if d.get("azimuth_at_los") is not None else "")
    direction = d.get("azimuth_at_peak_compass") or (
        _azimuth_to_compass(d["azimuth_at_peak"]) if d.get("azimuth_at_peak") is not None else "")
    # Use peak direction as fallback for aos_compass only if aos is still empty
    aos_compass = aos_compass or direction or ""

    if norad_id is None or max_el is None:
        logger.debug("satpass_handler: missing norad_id or max_elevation_deg")
        return None

    aos_epoch = _parse_iso_epoch(aos_iso)
    los_epoch = _parse_iso_epoch(los_iso)

    if aos_epoch is None:
        logger.debug("satpass_handler: could not parse aos time")
        return None

    # Staleness guard: reject passes whose window already ended
    if los_epoch is not None and los_epoch < now:
        logger.debug("satpass_handler: pass already ended (los %d < now %d), skipping",
                     los_epoch, now)
        return None

    # AOS horizon guard: reject passes too far in the future (likely stale prediction)
    max_horizon_h = float(getattr(cfg, "max_aos_horizon_hours", 24))
    if max_horizon_h > 0 and aos_epoch > now + max_horizon_h * 3600:
        logger.debug(
            "satpass_handler: AOS %d is %.1fh away, beyond %gh horizon; skipping",
            aos_epoch, (aos_epoch - now) / 3600, max_horizon_h)
        return None

    # Observer filter (empty = all)
    observers = getattr(cfg, "observers", []) or []
    if observers and observer not in observers:
        logger.debug("satpass_handler: observer %r not in configured list", observer)
        return None

    # OPT-IN NORAD ID filter: empty list = broadcast NOTHING
    norad_ids_raw = getattr(cfg, "norad_ids", []) or []
    if not norad_ids_raw:
        if not getattr(handle_satpass, "_no_norad_ids_logged", False):
            logger.info("satpass: no norad_ids configured; pass broadcasts disabled")
            handle_satpass._no_norad_ids_logged = True
        return None
    # Coerce to int set — GUI may save as strings (["25544"]), wire
    # delivers int.  Accept both shapes forever.
    allow_set = {int(x) for x in norad_ids_raw if str(x).strip().isdigit()}
    if norad_id not in allow_set:
        logger.debug("satpass_handler: norad_id %d not in configured list", norad_id)
        return None

    # Elevation floor
    min_el = float(getattr(cfg, "min_elevation", 30))
    if max_el < min_el:
        logger.debug("satpass_handler: max_el %.1f below floor %.1f", max_el, min_el)
        return None

    # Generate consolidated canonical ID (observer-independent)
    consolidated_id = _canonical_id(norad_id, aos_epoch)
    severity_word = _map_severity(max_el)
    category_raw = inner.get("category") or "sat.pass"

    try:
        conn = get_db()
    except Exception:
        logger.exception("satpass_handler: persistence unavailable")
        return None

    # Log the per-observer event arrival
    _log_event_returning_id(
        conn, now=now, source="satpass", category=category_raw,
        severity_word=severity_word, event_id_external=consolidated_id,
        subject=subject, handled=0,
        table_name="satpass_pending", table_pk=f"{consolidated_id}:{observer}")

    # Accumulate into pending table. due_at is the durable fire-time backstop
    # (received_at + baseline delay) so a restart can reconstruct a consolidation
    # timer for rows the in-memory scheduler would otherwise orphan.
    due_at = now + CONSOLIDATION_DELAY
    conn.execute(
        "INSERT OR REPLACE INTO satpass_pending("
        "consolidated_id, observer, sat_name, norad_id, max_elevation, "
        "aos_at, los_at, aos_compass, los_compass, peak_compass, received_at, "
        "due_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (consolidated_id, observer, sat_name, norad_id, max_el,
         aos_epoch, los_epoch, aos_compass, los_compass, direction, now,
         due_at))

    # Signal consumer to schedule consolidation timer
    _pending_consolidation_ids.add(consolidated_id)

    # Suppress immediate broadcast
    return None


def gate_consolidated_pass(consolidated: dict, *,
                           now: int) -> tuple[str, dict] | None:
    """Source-agnostic broadcast gate for an already-consolidated pass.

    Both the Central consumer path (`consolidate_satpass_pending`, which
    merges buffered per-observer rows from `satpass_pending`) and the native
    env.satpass adapter (which consolidates in-memory across observers in one
    tick) call THIS single function so the broadcast decision is byte-identical
    regardless of source. It deliberately does NOT touch `satpass_pending` —
    that buffer is a Central-consumer implementation detail owned by the caller.

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


def consolidate_satpass_pending(consolidated_id: str) -> tuple[str, dict] | None:
    """Called by consumer when 5s consolidation timer fires.

    Reads the buffered per-observer rows for this canonical id, merges them
    across observers (earliest AOS / latest LOS / max-elevation observer
    supplies max_elevation + peak_compass / entry+exit observers), then
    delegates the actual broadcast decision to the shared, source-agnostic
    `gate_consolidated_pass`. Pending rows are cleaned up afterward regardless
    of the gate's decision (dedup, rate-cap, dry-run, and success all consume
    the buffer identically, as before).

    Returns (wire_string, data_dict) or None if suppressed.
    """
    try:
        conn = get_db()
    except Exception:
        logger.exception("satpass consolidation: persistence unavailable")
        return None

    rows = conn.execute(
        "SELECT * FROM satpass_pending WHERE consolidated_id=?",
        (consolidated_id,)).fetchall()
    if not rows:
        return None

    # Consolidate observers
    sorted_by_aos = sorted(rows, key=lambda r: r["aos_at"])
    sorted_by_los = sorted(rows, key=lambda r: r["los_at"])
    entry = sorted_by_aos[0]    # earliest AOS
    exit_ = sorted_by_los[-1]   # latest LOS
    best = max(rows, key=lambda r: r["max_elevation"])

    consolidated = {
        "consolidated_id": consolidated_id,
        "norad_id": best["norad_id"],
        "sat_name": best["sat_name"],
        "max_elevation": best["max_elevation"],
        "aos_epoch": entry["aos_at"],
        "los_epoch": exit_["los_at"],
        "aos_compass": entry["aos_compass"],
        "los_compass": exit_["los_compass"],
        # Peak belongs to whoever saw the highest elevation.
        "peak_compass": best["peak_compass"],
        "entry_observer": entry["observer"],
        "exit_observer": exit_["observer"],
        "observer_list": ",".join(r["observer"] for r in sorted_by_aos),
    }

    result = gate_consolidated_pass(consolidated, now=_now())

    # Clean up pending rows regardless of the gate's decision.
    _cleanup_pending(conn, consolidated_id)

    return result


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

SCHEMA_SATPASS_PENDING = """
CREATE TABLE IF NOT EXISTS satpass_pending (
    consolidated_id TEXT NOT NULL,
    observer        TEXT NOT NULL,
    sat_name        TEXT,
    norad_id        INTEGER,
    max_elevation   REAL,
    aos_at          INTEGER,
    los_at          INTEGER,
    aos_compass     TEXT,
    los_compass     TEXT,
    peak_compass    TEXT,
    received_at     INTEGER,
    due_at          INTEGER,
    PRIMARY KEY (consolidated_id, observer)
);
"""


def load_pending_schedule() -> list[tuple[str, int]]:
    """Return [(consolidated_id, due_at)] for every cid with pending rows.

    Used by the consumer's startup sweep to reconstruct consolidation timers
    that were lost with the in-memory scheduler on restart. One entry per
    distinct consolidated_id, keyed on the EARLIEST due_at across its observer
    rows (MIN) so the reconstructed fire time matches the live timer, which is
    armed off the first arrival and never re-armed for later observers.

    A row written before due_at existed (pre-v22, or a partial write) has
    due_at IS NULL; COALESCE falls it back to received_at + baseline delay so
    such a row is still recoverable rather than silently stranded.
    """
    try:
        conn = get_db()
    except Exception:
        logger.exception("satpass sweep: persistence unavailable")
        return []
    rows = conn.execute(
        "SELECT consolidated_id, "
        "MIN(COALESCE(due_at, received_at + ?)) AS due_at "
        "FROM satpass_pending GROUP BY consolidated_id",
        (CONSOLIDATION_DELAY,),
    ).fetchall()
    out: list[tuple[str, int]] = []
    for r in rows:
        try:
            cid = r["consolidated_id"]
            due = r["due_at"]
            if cid is None or due is None:
                continue
            out.append((str(cid), int(due)))
        except Exception:
            logger.exception("satpass sweep: skipping malformed pending row")
    return out
