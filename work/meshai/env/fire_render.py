"""WFIGS handler: persistence-backed change-detection + wire renderer.

Relocated from `meshai.central.wfigs_handler` during the Central ripout
(central/ handler retirement, chore/ripout-2d). ``handle_wfigs`` has no live
production caller (Central's NATS consumer that drove it is gone), but it
remains the parity-tested legacy contract for the WFIGS wildfire wire format
(see `tests/test_fire_refactor.py`, `tests/test_wfigs_handler.py`) -- kept
verbatim, not deleted, per that oracle. ``_render`` IS live: it is imported
by `meshai.env.fire_fusion._handle_pass_boundary` on the FIRMS growth path
(`env/firms.py` -> `ingest_hotspot_pixel` -> ... -> `_handle_pass_boundary`
-> `_render`) to render the `wildfire_growth` wire.

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
from meshai.geo import haversine_distance, _bearing_compass
from meshai.notifications.formatters._budget import budget_for, fit_to_budget

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


def _build_canonical(normalized: dict, kind: str) -> dict:
    """Flat canonical dict consumed by gating.fire.decide + formatters.fire.

    Carries the render-ready WFIGS fields the decider (gating decision) and the
    formatter (wire) read.  ``_kind`` routes the decider between the active-fire
    state machine, the tombstone all-clear, and the never-broadcast perimeter.
    """
    return {
        "_kind": kind,
        "irwin_id": normalized.get("irwin_id"),
        "incident_name": normalized.get("incident_name"),
        "incident_type": normalized.get("incident_type"),
        "acres": normalized.get("acres"),
        "contained_pct": normalized.get("contained_pct"),
        "fire_cause": normalized.get("fire_cause"),
        "declared_at_epoch": normalized.get("declared_at_epoch"),
        "unique_fire_id": normalized.get("unique_fire_id"),
        "lat": normalized.get("lat"),
        "lon": normalized.get("lon"),
        "county": normalized.get("county"),
        "state": normalized.get("state"),
        "landclass": normalized.get("landclass"),
        "geocoder_city": normalized.get("geocoder_city"),
    }


def handle_wfigs(normalized: dict, envelope: dict, subject: str,
                  data: Optional[dict] = None,
                  now: Optional[int] = None) -> Optional[str]:
    """Route a normalized WFIGS dict through persistence + change-detection.

    Phase-3b refactor: the broadcast DECISION (New/Update/suppress + cooldown +
    age-gate + all-clear eligibility) now lives in
    ``meshai.notifications.gating.fire.decide``.  This handler keeps ownership
    of the inline ``fires`` INSERT/UPDATE of ``current_*`` (unconditional state
    write), the ``tombstoned_at`` stamp, the ``event_log`` row + handled flip,
    and the wire it returns (mirror quake/nwis).

    `data` is the mutable dict the caller (consumer._normalize) is composing
    into the Event. When a broadcast should fire, the handler attaches an
    `_on_broadcast_committed` callback and `_broadcast_audit` descriptor to
    it; the dispatcher invokes both AFTER a successful deliver().

    Cutover gate: when the emitted category is explicitly cut over, the handler
    writes ``gate.data_patch`` + wraps ``gate.commit`` (new path live);
    otherwise it keeps the legacy ``_attach_commit_handles`` / all-clear
    stamping VERBATIM so the live broadcast stays byte-for-byte identical while
    the new formatter+decider bake in shadow.

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

    from meshai.notifications.cutover import is_cutover
    from meshai.notifications.gating.fire import decide as _gate_decide

    canonical = _build_canonical(normalized, kind)

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
        # UNCONDITIONAL state write — stays inline, mirror the original.
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
        # The DECISION (row exists AND last_broadcast_at NOT NULL) is delegated
        # to the decider; the WIRE is still rendered here from the fire row for
        # byte-identity, and the not-cutover data stamping stays verbatim.
        if kind == "wfigs_tombstone" and irwin_id:
            gate = _gate_decide(canonical, source="wfigs", now=float(now))
            if gate.broadcast:
                fire_row = conn.execute(
                    "SELECT incident_name, current_acres, current_contained_pct, "
                    "last_broadcast_at, county, state, lat, lon "
                    "FROM fires WHERE irwin_id = ?", (irwin_id,)
                ).fetchone()
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
                    if is_cutover("wildfire_closed"):
                        # NEW PATH: formatter re-renders from data_patch fields.
                        data.update(gate.data_patch)
                        data["_broadcast_audit"] = {"table": "fires", "pk": irwin_id}
                        _raw_commit = gate.commit
                        _log_row_id = log_id

                        def _on_commit(committed_at: float) -> None:
                            if _raw_commit is not None:
                                _raw_commit(committed_at)
                            if _log_row_id is not None:
                                try:
                                    get_db().execute(
                                        "UPDATE event_log SET handled=1 WHERE id=?",
                                        (int(_log_row_id),))
                                except Exception:
                                    logger.exception(
                                        "wfigs closed commit: event_log update failed")

                        data["_on_broadcast_committed"] = _on_commit
                    else:
                        # LEGACY verbatim (byte-for-byte identical live output).
                        data["category"] = "wildfire_closed"
                        data["_severity_override"] = "priority"
                        _attach_commit_handles(
                            data, irwin_id=irwin_id,
                            acres=fire_row["current_acres"],
                            contained_pct=fire_row["current_contained_pct"],
                            event_log_row_id=log_id)
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

    acres = normalized.get("acres")
    contained_pct = normalized.get("contained_pct")

    # Delegate the New/Update/suppress + age-gate + cooldown decision. The
    # decider READS the fires row (pre-write) exactly as the original inline
    # branch did; the inline INSERT/UPDATE below stays handler-owned.
    gate = _gate_decide(canonical, source="wfigs", now=float(now))

    # ---- inline state write (UNCONDITIONAL) -- INSERT or UPDATE current_*.
    # Re-read the row (same pre-write state the decider saw) to pick the write.
    row = conn.execute(
        "SELECT last_broadcast_at FROM fires WHERE irwin_id = ?",
        (irwin_id,)).fetchone()
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
    else:
        conn.execute(
            "UPDATE fires SET current_acres=?, current_contained_pct=?, "
            "lat=COALESCE(?, lat), lon=COALESCE(?, lon), last_event_at=? "
            "WHERE irwin_id=?",
            (acres, contained_pct, normalized.get("lat"),
             normalized.get("lon"), now, irwin_id),
        )

    if not gate.broadcast:
        # Case-(iii) suppress (no forward change OR inside cooldown) ran the
        # stale-fire cleanup in the original; the age-gate suppress did not.
        if gate.lifecycle == "cooldown":
            _cleanup_stale_fires(conn)
        return None

    # ---- broadcast: render the wire (back-compat) + stamp data.
    prefix = "Update" if gate.lifecycle == "update" else "New"
    wire = _render(normalized, prefix=prefix,
                    last_bcast_acres=gate.data_patch.get("last_bcast_acres"),
                    last_bcast_contained=gate.data_patch.get("last_bcast_contained"))

    # Cutover gate keys on the category THIS broadcast carries: New first-sight
    # is wildfire_declared, growth Update stays wildfire_incident.
    _cut = (is_cutover("wildfire_incident") if gate.lifecycle == "update"
            else is_cutover("wildfire_declared"))
    if isinstance(data, dict):
        if _cut:
            # NEW PATH: formatter re-renders from canonical + data_patch.
            data.update(canonical)
            data.update(gate.data_patch)
            data["_broadcast_audit"] = {"table": "fires", "pk": irwin_id}
            _raw_commit = gate.commit
            _log_row_id = log_id

            def _on_commit(committed_at: float) -> None:
                if _raw_commit is not None:
                    _raw_commit(committed_at)
                if _log_row_id is not None:
                    try:
                        get_db().execute(
                            "UPDATE event_log SET handled=1 WHERE id=?",
                            (int(_log_row_id),))
                    except Exception:
                        logger.exception(
                            "wfigs commit: event_log update failed")

            data["_on_broadcast_committed"] = _on_commit
        else:
            # LEGACY verbatim (byte-for-byte identical live output).
            # New first-sight tags wildfire_declared; Update keeps the
            # envelope-derived wildfire_incident (no category override).
            if gate.lifecycle != "update":
                data["category"] = "wildfire_declared"
            data["_severity_override"] = "priority"
            _attach_commit_handles(data, irwin_id=irwin_id,
                                     acres=acres, contained_pct=contained_pct,
                                     event_log_row_id=log_id)
    return wire


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
    lines.append(f"🔥 {name} — {prefix}")

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
            rows = get_db().execute(
                "SELECT name, lat, lon FROM town_anchors WHERE lat IS NOT NULL AND lon IS NOT NULL"
            ).fetchall()
            best = None
            best_d = float("inf")
            for row in rows:
                d = haversine_distance(lat, lon, row["lat"], row["lon"])
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
            # Kept as a lazy, per-call import (not hoisted to the top-level
            # geo import above) because several tests monkeypatch
            # `meshai.geo.nearest_town` and rely on this function doing a
            # fresh attribute lookup on every call to pick that up.
            from meshai.geo import nearest_town
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
