"""WFIGS wire renderer + shared fire helpers.

Relocated from `meshai.central.wfigs_handler` during the Central ripout
(central/ handler retirement, chore/ripout-2d).

chore/ripout-2dii: the dead Central NATS-envelope entrypoint ``handle_wfigs``
(the New:/Update:/tombstone dispatch + event_log accounting + the
`is_cutover` legacy-vs-new branch) has been REMOVED -- it had zero live
production callers (Central's consumer that drove it is gone). ``_render``
IS live: it is called directly by
`meshai.env.fire_fusion._handle_pass_boundary` on the FIRMS growth path
(`env/firms.py` -> `ingest_hotspot_pixel` -> ... -> `_handle_pass_boundary`
-> `_render`) to render the `wildfire_growth` wire, and is used as a
byte-identity oracle by `tests/test_fire_refactor.py` /
`tests/test_wfigs_handler.py` against the shared, LIVE fire formatter
(`notifications/formatters/fire.py`, reached for `wildfire_declared` /
`wildfire_incident` via the native WFIGS adapter `env/fires.py` ->
`env/store.py::_emit_event` -> `gating.fire.decide`).

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
from typing import Optional

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


# chore/ripout-2dii: the dead Central NATS-envelope entrypoint ``handle_wfigs``
# (envelope-driven New/Update/tombstone dispatch, event_log accounting, the
# `is_cutover` legacy-vs-new branch) has been REMOVED -- it had zero live
# production callers (Central's consumer that drove it is gone). The LIVE
# WFIGS path is `env/fires.py` (native adapter) -> `env/store.py::_emit_event`
# (forced onto `gating.fire.decide` + the shared fire formatter via
# `cutover.NATIVE_ALWAYS_DECIDE`, independent of this dead handler) -- see
# `tests/test_fire_native_growth.py`. `_render` (below) remains LIVE: it is
# called directly by `env.fire_fusion._handle_pass_boundary` on the FIRMS
# `wildfire_growth` path, and by `tests/test_fire_refactor.py` /
# `tests/test_wfigs_handler.py` as a byte-identity oracle for the shared fire
# formatter (`notifications/formatters/fire.py`).


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
                "SELECT name, lat, lon FROM town_anchors WHERE lat IS NOT NULL AND lon IS NOT NULL AND enabled = 1"
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
