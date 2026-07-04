"""WFIGS handler: persistence-backed change-detection + wire renderer.

v0.5.8b refactor: New: vs Update: decision now keys on `last_broadcast_at`,
not on row existence. Cold-start scenarios where the dispatcher drops the
broadcast (cold-start grace, stale filter, cooldown, dedup) leave the fires
row with NULL last_broadcast_at, so the NEXT successful broadcast still
gets the "New:" prefix -- it really is the first delivery for that fire.

Cases (resolved at handler entry):
    (i)   row missing                    -> INSERT, prefix="New",  return wire
    (ii)  row exists, last_broadcast_at IS NULL
                                          -> UPDATE current_*, prefix="New",
                                             return wire (never broadcast yet)
    (iii) row exists, last_broadcast_at NOT NULL
                                          -> UPDATE current_*, gate on change +
                                             8h cooldown. If pass: prefix="Update",
                                             return wire; else return None.

The last_broadcast_* UPDATE has moved OUT of the handler and INTO a callback
attached to event.data["_on_broadcast_committed"]. The dispatcher calls it
ONLY after a successful broadcast. The mesh_broadcasts_out audit row is now
inserted by the dispatcher (via event.data["_broadcast_audit"]) for the same
reason -- it should only exist for actually-delivered broadcasts.

Concurrency: each consumer thread gets its own SQLite connection via
meshai.persistence.get_db() (threading.local pool). Writes are serial
inside that connection's autocommit mode.
"""

from __future__ import annotations
from meshai.adapter_config import adapter_config
from meshai.central.budget import budget_for, fit_to_budget

import logging
import time
from typing import Any, Optional

from meshai.notifications import clock

from meshai.persistence import get_db

logger = logging.getLogger(__name__)

# v0.6-3b: cooldown lives in adapter_config.wfigs.cooldown_seconds
# (default 28800). Re-read on every cooldown-check, so a GUI edit takes
# effect on the next poll cycle. Module-level name retained as a
# backward-compat alias for test imports.
WFIGS_BROADCAST_COOLDOWN_S = 28800


_last_cleanup = 0


def _cleanup_stale_fires(conn) -> None:
    global _last_cleanup
    now = int(time.time())
    if now - _last_cleanup < 3600:
        return
    _last_cleanup = now
    cutoff_stale = now - (7 * 24 * 3600)
    cutoff_tomb = now - (30 * 24 * 3600)
    conn.execute("DELETE FROM fires WHERE last_event_at < ? AND tombstoned_at IS NULL", (cutoff_stale,))
    conn.execute("DELETE FROM fires WHERE tombstoned_at IS NOT NULL AND tombstoned_at < ?", (cutoff_tomb,))


def _now() -> int:
    return int(clock.now())


def _fire_too_old_to_announce(declared_at_epoch, now) -> bool:
    """Stale/closed-fire resurrection guard for first-announce ("New") only.

    Re-reads the knob INSIDE the call (cache-backed + GUI-invalidated, so a
    dashboard edit takes effect on the next poll cycle). Fails OPEN (announces)
    when the gate is disabled or the fire has no discovery date.
    """
    max_age = int(adapter_config.wfigs.max_declare_age_seconds)
    if max_age <= 0 or declared_at_epoch is None:
        return False   # disabled, or no discovery date -> fail OPEN (announce)
    return (now - int(declared_at_epoch)) >= max_age


# ---------- public entry --------------------------------------------------


def handle_wfigs(normalized: dict, envelope: dict, subject: str,
                  data: Optional[dict] = None,
                  now: Optional[int] = None) -> Optional[str]:
    """Route a normalized WFIGS dict through persistence + change-detection.

    `data` is the mutable dict the caller (consumer._normalize) is composing
    into the Event. When a broadcast should fire, the handler attaches an
    `_on_broadcast_committed` callback and `_broadcast_audit` descriptor to
    it; the dispatcher invokes both AFTER a successful deliver().

    Returns a wire string when a broadcast should fire, None otherwise.
    """
    if not isinstance(normalized, dict):
        return None
    kind = normalized.get("_kind")
    if kind not in ("wfigs_incident", "wfigs_tombstone", "wfigs_perimeter"):
        return None

    now = now if now is not None else _now()
    inner = envelope.get("data") or {} if isinstance(envelope, dict) else {}
    category = inner.get("category") or ""
    severity_word = _coerce_severity(inner.get("severity"))
    irwin_id = normalized.get("irwin_id")

    try:
        conn = get_db()
    except Exception:
        logger.exception("wfigs_handler: persistence unavailable; "
                          "deferring to default pipeline")
        return None

    if kind in ("wfigs_tombstone", "wfigs_perimeter"):
        source = "wfigs_incidents" if kind == "wfigs_tombstone" else "wfigs_perimeters"
        log_id = _log_event_returning_id(
            conn, now=now, source=source, category=category,
            severity_word=severity_word, irwin_id=irwin_id,
            subject=subject, handled=0,
            table_name=None, table_pk=irwin_id)
        # v0.6-tail item 4: tombstone branch stamps fires.tombstoned_at so
        # the ReminderScheduler stops re-broadcasting the closed fire.
        # Only the tombstone kind closes the fire; perimeter polls don t.
        if kind == "wfigs_tombstone" and irwin_id:
            try:
                conn.execute(
                    "UPDATE fires SET tombstoned_at=COALESCE(tombstoned_at, ?) "
                    "WHERE irwin_id=?",
                    (now, irwin_id),
                )
            except Exception:
                logger.exception("wfigs: tombstoned_at stamp failed irwin=%s", irwin_id)

        # All-clear broadcast: only fires that previously made it to mesh
        # get a closure message. Silent for fires that were never broadcast.
        if kind == "wfigs_tombstone" and irwin_id:
            fire_row = conn.execute(
                "SELECT incident_name, current_acres, current_contained_pct, "
                "last_broadcast_at, county, state, lat, lon "
                "FROM fires WHERE irwin_id = ?", (irwin_id,)
            ).fetchone()
            if fire_row is not None and fire_row["last_broadcast_at"] is not None:
                name = fire_row["incident_name"] or "(unnamed fire)"
                # Build line 2 parts
                parts = []
                if fire_row["current_acres"] is not None:
                    parts.append(f"{int(fire_row['current_acres']):,} ac")
                if fire_row["current_contained_pct"] is not None:
                    parts.append(f"{int(fire_row['current_contained_pct'])}% contained")
                # Location via _location_anchor with a minimal normalized dict
                loc_dict = {
                    "lat": fire_row["lat"], "lon": fire_row["lon"],
                    "county": fire_row["county"], "state": fire_row["state"],
                }
                anchor = _location_anchor(loc_dict)
                if anchor and anchor != "(location unknown)":
                    parts.append(anchor)
                lines = [f"✅ {name} — contained & closed"]
                if parts:
                    lines.append(" | ".join(parts))
                wire = "\n".join(lines)
                if isinstance(data, dict):
                    data["category"] = "wildfire_closed"
                    data["_severity_override"] = "priority"
                _attach_commit_handles(
                    data, irwin_id=irwin_id,
                    acres=fire_row["current_acres"],
                    contained_pct=fire_row["current_contained_pct"],
                    event_log_row_id=log_id)
                if isinstance(data, dict):
                    data["_dedup_suffix"] = "closed"
                return wire

        return None

    # ---- active incident ----
    # v0.5.8b: log handled=0 initially. The commit callback UPDATEs this
    # row to handled=1 if/when the dispatcher actually broadcasts -- if it
    # drops (cold-start grace, staleness, cooldown, dedup), the row stays
    # handled=0 and we can grep the event_log to find the suppressed events.
    log_id = _log_event_returning_id(
        conn, now=now, source="wfigs_incidents", category=category,
        severity_word=severity_word, irwin_id=irwin_id,
        subject=subject, handled=0,
        table_name="fires", table_pk=irwin_id)

    row = conn.execute(
        "SELECT current_acres, current_contained_pct, last_broadcast_at, "
        "last_broadcast_acres, last_broadcast_contained "
        "FROM fires WHERE irwin_id = ?", (irwin_id,)).fetchone()

    acres = normalized.get("acres")
    contained_pct = normalized.get("contained_pct")

    # ---- (i) row missing -- INSERT, mark "New", but DO NOT set last_broadcast_*
    if row is None:
        conn.execute(
            "INSERT INTO fires(irwin_id, incident_name, incident_type, "
            "current_acres, current_contained_pct, status, lat, lon, "
            "county, state, landclass, declared_at, last_event_at, "
            "last_broadcast_at, last_broadcast_acres, last_broadcast_contained) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                irwin_id,
                normalized.get("incident_name"),
                normalized.get("incident_type"),
                acres, contained_pct,
                None,  # status reserved
                normalized.get("lat"), normalized.get("lon"),
                normalized.get("county"), normalized.get("state"),
                normalized.get("landclass"),
                normalized.get("declared_at_epoch"),
                now,  # last_event_at
                None, None, None,  # last_broadcast_* explicitly NULL
            ),
        )
        # Step 3 age-gate: keep the INSERT (so genuine future Updates work) but
        # suppress the "New" broadcast for fires whose declared_at is too old.
        if _fire_too_old_to_announce(normalized.get("declared_at_epoch"), now):
            return None
        wire = _render(normalized, prefix="New")
        # v0.7-fire-tracker-1: tag first-sight broadcasts with the new
        # wildfire_declared category so the dispatcher rules them apart
        # from acres/containment updates (wildfire_incident).
        if isinstance(data, dict):
            data["category"] = "wildfire_declared"
        # v0.6-3c: severity override for fire broadcasts (downgraded from
        # immediate to priority to prevent cooldown/grouper bypass)
        if isinstance(data, dict):
            data["_severity_override"] = "priority"
        _attach_commit_handles(data, irwin_id=irwin_id,
                                 acres=acres, contained_pct=contained_pct,
                                 event_log_row_id=log_id)
        return wire

    # ---- (ii) row exists but never broadcast -- UPDATE current_*, prefix="New"
    if row["last_broadcast_at"] is None:
        conn.execute(
            "UPDATE fires SET current_acres=?, current_contained_pct=?, "
            "lat=COALESCE(?, lat), lon=COALESCE(?, lon), last_event_at=? "
            "WHERE irwin_id=?",
            (acres, contained_pct, normalized.get("lat"),
             normalized.get("lon"), now, irwin_id),
        )
        # Step 3 age-gate: keep the UPDATE but suppress the "New" broadcast for
        # fires whose declared_at is too old (closed/stale-fire resurrection).
        if _fire_too_old_to_announce(normalized.get("declared_at_epoch"), now):
            return None
        wire = _render(normalized, prefix="New")
        # v0.7-fire-tracker-1: case-(ii) is also first-sight as far as
        # broadcast history goes -- the row exists because some prior
        # handler call ran but no actual broadcast went out.
        if isinstance(data, dict):
            data["category"] = "wildfire_declared"
        # v0.6-3c: severity override for fire broadcasts (downgraded from
        # immediate to priority to prevent cooldown/grouper bypass)
        if isinstance(data, dict):
            data["_severity_override"] = "priority"
        _attach_commit_handles(data, irwin_id=irwin_id,
                                 acres=acres, contained_pct=contained_pct,
                                 event_log_row_id=log_id)
        return wire

    # ---- (iii) row exists AND already broadcast -- gate on change + 8h cooldown
    conn.execute(
        "UPDATE fires SET current_acres=?, current_contained_pct=?, "
        "lat=COALESCE(?, lat), lon=COALESCE(?, lon), last_event_at=? "
        "WHERE irwin_id=?",
        (acres, contained_pct, normalized.get("lat"),
         normalized.get("lon"), now, irwin_id),
    )

    last_bcast_at = row["last_broadcast_at"]
    last_bcast_acres = row["last_broadcast_acres"]
    last_bcast_contained = row["last_broadcast_contained"]

    # Forward-only change detection: more acres or higher containment counts.
    # Downward revisions and unchanged values do not warrant re-broadcast.
    # v0.6-3b: each axis can be silenced via adapter_config toggles.
    changed_acres = (
        bool(adapter_config.wfigs.broadcast_on_acres)
        and acres is not None
        and (last_bcast_acres is None or acres > last_bcast_acres)
    )
    changed_contained = (
        bool(adapter_config.wfigs.broadcast_on_contained)
        and contained_pct is not None
        and (last_bcast_contained is None or contained_pct > last_bcast_contained)
    )
    cooldown_s = int(adapter_config.wfigs.cooldown_seconds)
    eight_hours_passed = (
        last_bcast_at is None
        or (now - int(last_bcast_at) >= cooldown_s)
    )

    if (changed_acres or changed_contained) and eight_hours_passed:
        wire = _render(normalized, prefix="Update",
                        last_bcast_acres=last_bcast_acres,
                        last_bcast_contained=last_bcast_contained)
        # v0.6-3c: severity override for fire updates (downgraded from
        # immediate to priority to prevent cooldown/grouper bypass)
        if isinstance(data, dict):
            data["_severity_override"] = "priority"
        _attach_commit_handles(data, irwin_id=irwin_id,
                                 acres=acres, contained_pct=contained_pct,
                                 event_log_row_id=log_id)
        return wire

    _cleanup_stale_fires(conn)
    return None


# ---------- commit-callback factory ---------------------------------------


def _attach_commit_handles(data: Optional[dict], *, irwin_id: str,
                            acres: Optional[float],
                            contained_pct: Optional[int],
                            event_log_row_id: Optional[int] = None) -> None:
    """Attach `_on_broadcast_committed` callback + `_broadcast_audit`
    descriptor to the event-data dict. Both are read by the dispatcher
    AFTER a successful broadcast.

    The callback closure captures the irwin_id + acres + contained_pct that
    triggered THIS broadcast. The dispatcher passes the actual delivery
    timestamp, which we record in last_broadcast_at. This keeps cold-start
    races correct: if the dispatcher drops the broadcast, the callback is
    not invoked and last_broadcast_at stays NULL -- so the NEXT successful
    broadcast still labels itself "New:".
    """
    if not isinstance(data, dict):
        return

    def _on_commit(committed_at: float) -> None:
        try:
            conn = get_db()
        except Exception:
            logger.exception(
                "wfigs commit callback: persistence unavailable; "
                "last_broadcast_* not updated for irwin=%s", irwin_id)
            return
        conn.execute(
            "UPDATE fires SET last_broadcast_at=?, "
            "first_broadcast_at=COALESCE(first_broadcast_at, ?), "
            "last_broadcast_acres=?, last_broadcast_contained=? WHERE irwin_id=?",
            (int(committed_at), int(committed_at), acres, contained_pct, irwin_id),
        )
        # Flip the matching event_log row to handled=1. A NULL row id
        # (caller forgot to thread it) is silently skipped -- the broadcast
        # still went out.
        if event_log_row_id is not None:
            conn.execute(
                "UPDATE event_log SET handled=1 WHERE id=?",
                (int(event_log_row_id),),
            )

    data["_on_broadcast_committed"] = _on_commit
    data["_broadcast_audit"] = {"table": "fires", "pk": irwin_id}
    data["_cooldown_suffix"] = irwin_id
    # v0.6-4: WFIGS publishes the SAME envelope id (IrwinID) for every sweep
    # over an incident's life, so the dispatcher's (source, id) dedup
    # permanently swallowed every post-"New" lifecycle broadcast (growth /
    # containment updates) this handler deliberately synthesized. Stamping
    # the state that justified THIS broadcast into the dedup suffix lets
    # unchanged re-deliveries dedup as before while genuine updates pass.
    data["_dedup_suffix"] = f"{acres}|{contained_pct}"


# ---------- helpers -------------------------------------------------------


def _coerce_severity(sev: Any) -> Optional[str]:
    if sev is None: return None
    if isinstance(sev, str): return sev or None
    try: return str(int(sev))
    except (TypeError, ValueError): return str(sev)


def _log_event(conn, *, now, source, category, severity_word, irwin_id,
                subject, handled, table_name, table_pk) -> None:
    """Insert an event_log row; void return (used for tombstones/perimeters
    where the handled flag is fixed at write-time)."""
    conn.execute(
        "INSERT INTO event_log(received_at, source, category, severity_word, "
        "event_id_external, nats_subject, handled, table_name, table_pk) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (now, source, category, severity_word, irwin_id, subject,
         int(bool(handled)), table_name, table_pk),
    )


def _log_event_returning_id(conn, *, now, source, category, severity_word,
                              irwin_id, subject, handled, table_name,
                              table_pk) -> int:
    """Insert an event_log row and return its primary key id.

    Used for active-incident logging where the commit callback updates
    the same row to handled=1 once a broadcast actually goes out.
    """
    cur = conn.execute(
        "INSERT INTO event_log(received_at, source, category, severity_word, "
        "event_id_external, nats_subject, handled, table_name, table_pk) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (now, source, category, severity_word, irwin_id, subject,
         int(bool(handled)), table_name, table_pk),
    )
    return int(cur.lastrowid)


# ---------- renderer ------------------------------------------------------


def _render(n: dict, *, prefix: str = "",
            last_bcast_acres=None, last_bcast_contained=None,
            movement=None) -> str:
    """MEDIUM-style mesh wire string with delta/bold logic for updates."""
    import datetime as _dt

    name = n.get("incident_name") or "(unnamed)"
    acres = n.get("acres")
    contained_pct = n.get("contained_pct")
    cause = n.get("fire_cause")
    unique_fire_id = n.get("unique_fire_id")
    declared_at_epoch = n.get("declared_at_epoch")
    anchor = _location_anchor(n)

    lines: list[str] = []

    # Line 1: header
    lines.append(f"🔥 {name} \u2014 {prefix}")

    # Line 2: size / containment with delta (plain text -- no bold markdown).
    acres_str = f"{int(acres):,} ac" if acres is not None else "size unknown"
    delta_str = ""
    if prefix == "Update" and last_bcast_acres is not None and acres is not None and acres > last_bcast_acres:
        delta_str = f" (+{int(acres - last_bcast_acres):,})"
    contained_str = f"containment {int(contained_pct)}%" if contained_pct is not None else "containment unknown"
    lines.append(f"{acres_str}{delta_str} · {contained_str}")

    # Line 3: movement or plain anchor (no bold markdown).
    if (isinstance(movement, dict)
            and movement.get("direction") and movement.get("speed_mph") is not None):
        lines.append(f"Moving {movement['direction']} {movement['speed_mph']:.1f} mi/h · {anchor}")
    else:
        lines.append(f"{anchor}")

    # Line 4: cause / discovered (DATE ONLY -- no time-of-day). "Discovered <date>".
    cause_part = cause if cause else None
    disc_part = None
    if declared_at_epoch is not None:
        try:
            dt = _dt.datetime.fromtimestamp(declared_at_epoch,
                                            tz=_dt.timezone(_dt.timedelta(hours=-6)))
            disc_part = dt.strftime("%b %-d")
        except Exception:
            pass
    if cause_part and disc_part:
        lines.append(f"Cause: {cause_part} · Discovered {disc_part}")
    elif cause_part:
        lines.append(f"Cause: {cause_part}")
    elif disc_part:
        lines.append(f"Discovered {disc_part}")

    # NOTE: the trailing `ID: {unique_fire_id}` line was dropped in the
    # budget-fit rework -- the unique fire id is not mesh-actionable.

    return fit_to_budget("\n".join(lines), budget_for("wfigs"))


def _location_anchor(n: dict) -> str:
    """Anchor priority: geocoder.city > nearest_town > landclass > county."""
    city = n.get("geocoder_city")
    if city:
        return str(city)

    lat = n.get("lat")
    lon = n.get("lon")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        # Try curated town_anchors first
        try:
            from meshai.persistence import get_db
            from meshai.central_normalizer import _haversine_miles as _haversine_mi
            from meshai.central_normalizer import _bearing_compass
            rows = get_db().execute(
                "SELECT name, lat, lon FROM town_anchors WHERE lat IS NOT NULL AND lon IS NOT NULL"
            ).fetchall()
            best = None
            best_d = float("inf")
            for row in rows:
                d = _haversine_mi(lat, lon, row["lat"], row["lon"])
                if d < best_d:
                    best_d = d
                    best = row
            if best and best_d <= float(adapter_config.wfigs.anchor_max_mi):
                bearing = _bearing_compass(lat, lon, best["lat"], best["lon"])
                d_int = int(round(best_d))
                if d_int < 1:
                    return f"near {best['name'].title()}"
                return f"{d_int} mi {bearing} of {best['name'].title()}"
        except Exception:
            logger.exception("town_anchors lookup failed; falling back to Photon")

        try:
            from meshai.central_normalizer import nearest_town
            nt = nearest_town(lat, lon, max_distance_mi=float(adapter_config.wfigs.anchor_max_mi))
        except Exception:
            logger.exception("nearest_town failed; falling through")
            nt = None
        if nt and nt.get("name"):
            town = nt["name"]
            d = nt.get("distance_mi")
            bearing = nt.get("bearing")
            if isinstance(d, (int, float)):
                if d < 1:
                    return f"near {town.title()}"
                return f"{int(round(d))} mi {bearing or ''} of {town.title()}".strip()
            return f"near {town.title()}"

    landclass = n.get("landclass")
    if landclass:
        return str(landclass)

    county = n.get("county")
    state = n.get("state")
    if county and state:
        return f"{county} Co {state}"
    if state:
        return str(state)
    return "(location unknown)"
