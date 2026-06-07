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

import logging
import time
from typing import Any, Optional

from meshai.persistence import get_db

logger = logging.getLogger(__name__)

# v0.6-3b: cooldown lives in adapter_config.wfigs.cooldown_seconds
# (default 28800). Re-read on every cooldown-check, so a GUI edit takes
# effect on the next poll cycle. Module-level name retained as a
# backward-compat alias for test imports.
WFIGS_BROADCAST_COOLDOWN_S = 28800


def _now() -> int:
    return int(time.time())


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
        _log_event(conn, now=now, source=source, category=category,
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
        wire = _render(normalized, prefix="New")
        # v0.7-fire-tracker-1: tag first-sight broadcasts with the new
        # wildfire_declared category so the dispatcher rules them apart
        # from acres/containment updates (wildfire_incident).
        if isinstance(data, dict):
            data["category"] = "wildfire_declared"
        # v0.6-3c: severity override for fire broadcasts
        if isinstance(data, dict):
            data["_severity_override"] = "immediate" if (acres and acres > 1000) or contained_pct == 0 else "priority"
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
        wire = _render(normalized, prefix="New")
        # v0.7-fire-tracker-1: case-(ii) is also first-sight as far as
        # broadcast history goes -- the row exists because some prior
        # handler call ran but no actual broadcast went out.
        if isinstance(data, dict):
            data["category"] = "wildfire_declared"
        # v0.6-3c: severity override for fire broadcasts
        if isinstance(data, dict):
            data["_severity_override"] = "immediate" if (acres and acres > 1000) or contained_pct == 0 else "priority"
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
        wire = _render(normalized, prefix="Update")
        # v0.6-3c: severity override for fire updates
        if isinstance(data, dict):
            data["_severity_override"] = "immediate" if (acres and acres > 1000) or contained_pct == 0 else "priority"
        _attach_commit_handles(data, irwin_id=irwin_id,
                                 acres=acres, contained_pct=contained_pct,
                                 event_log_row_id=log_id)
        return wire

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


def _render(n: dict, *, prefix: str = "") -> str:
    """MEDIUM-style mesh wire string. See spec in module docstring."""
    name = n.get("incident_name") or "(unnamed)"
    itype = n.get("incident_type") or "incident"
    lat = n.get("lat")
    lon = n.get("lon")

    anchor = _location_anchor(n)
    acres = n.get("acres")
    contained = n.get("contained_pct")

    acres_str = "N/A" if acres is None else f"{int(acres):,} ac"
    contained_str = (
        "containment unknown" if contained is None
        else f"{int(contained)}% contained"
    )

    prefix_str = f"{prefix}: " if prefix else ""
    head = f"🔥 {prefix_str}{name} ({itype}), {anchor}"
    body = f"{acres_str}, {contained_str}"

    coords = ""
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        coords = f", @ {lat:.3f},{lon:.3f}"

    return f"{head}: {body}{coords}"


def _location_anchor(n: dict) -> str:
    """Anchor priority: geocoder.city > nearest_town > landclass > county."""
    city = n.get("geocoder_city")
    if city:
        return str(city)

    lat = n.get("lat")
    lon = n.get("lon")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
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
                    return f"near {town}"
                return f"{int(round(d))} mi {bearing or ''} of {town}".strip()
            return f"near {town}"

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
