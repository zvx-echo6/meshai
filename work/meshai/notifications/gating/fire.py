"""WFIGS wildfire gating decider — Phase-3b migration.

Moves the broadcast DECISION out of ``central.wfigs_handler.handle_wfigs`` (the
New-vs-Update-vs-suppress state machine, the forward-only acres/containment
growth gate + 8h cooldown, the stale-fire age-gate, and the tombstone all-clear
eligibility) into a source-agnostic decider, mirroring quake/hydro.

The Central handler keeps ownership of:
  * the ``fires`` INSERT / UPDATE of ``current_*`` (unconditional state write),
  * the ``tombstoned_at`` stamp,
  * the ``event_log`` row + its handled flip,
  * the wire rendering (``_render`` / all-clear) it returns for back-compat.

This decider owns ONLY:
  * the read of the ``fires`` row for the gating decision,
  * the growth/cooldown/age math,
  * the ``data_patch`` stamps every broadcast carries
    (``category`` override, ``_severity_override``, ``_dedup_suffix``,
    ``_cooldown_suffix``, and the render hints ``is_update`` /
    ``last_bcast_acres`` / ``last_bcast_contained``),
  * the deferred ``commit`` closure that UPSERTs
    ``fires(last_broadcast_at, first_broadcast_at, last_broadcast_acres,
    last_broadcast_contained)`` on confirmed delivery.

Canonical ``data`` dict consumed (built by the handler from the normalized
WFIGS dict):
    _kind, irwin_id, incident_name, incident_type, acres, contained_pct,
    fire_cause, declared_at_epoch, unique_fire_id, lat, lon, county, state,
    landclass, geocoder_city

Three ``_kind`` values are handled:
    wfigs_incident   -> active-fire New/Update/suppress state machine
    wfigs_tombstone  -> all-clear ("contained & closed") eligibility
    wfigs_perimeter  -> never broadcasts (suppress)

Lifecycle labels (drive handler-side side effects):
    "new"      first-sight broadcast (INSERT or row-exists-never-broadcast)
    "update"   forward-only growth/containment broadcast after cooldown
    "closed"   tombstone all-clear broadcast
    "cooldown" case-(iii) suppress (no change OR inside cooldown) — the handler
               runs ``_cleanup_stale_fires`` for THIS suppress only
    "suppress" every other suppress (age-gate, never-broadcast tombstone,
               perimeter, unknown) — no cleanup
"""
from __future__ import annotations

import logging
from typing import Optional

from meshai.adapter_config import adapter_config
from meshai.notifications.gating.base import GateResult
from meshai.persistence import get_db

logger = logging.getLogger(__name__)


def _fire_too_old_to_announce(declared_at_epoch, now) -> bool:
    """Stale/closed-fire resurrection guard for first-announce ("New") only.

    Verbatim copy of ``wfigs_handler._fire_too_old_to_announce``: re-reads the
    knob at the use-site (GUI-live), fails OPEN (announces) when the gate is
    disabled or the fire has no discovery date.
    """
    try:
        max_age = int(adapter_config.wfigs.max_declare_age_seconds)
    except Exception:
        return False
    if max_age <= 0 or declared_at_epoch is None:
        return False
    return (now - int(declared_at_epoch)) >= max_age


def decide(data: dict, *, source: str, now: float) -> GateResult:
    """Broadcast decision for a normalized WFIGS event.

    Parameters
    ----------
    data:
        Canonical WFIGS dict (see module docstring for schema).
    source:
        Adapter source name, e.g. "wfigs".
    now:
        Current epoch (from clock.now()) — determinism seam.

    Returns
    -------
    GateResult with broadcast/lifecycle/data_patch/commit populated.  The
    handler still performs the inline ``fires`` INSERT/UPDATE and renders the
    wire; ``data_patch`` supplies the stamps + render hints, ``commit`` arms
    the ``last_broadcast_*`` columns on confirmed delivery.
    """
    kind = data.get("_kind")
    irwin_id = data.get("irwin_id")

    try:
        conn = get_db()
    except Exception:
        logger.exception("fire decide: persistence unavailable")
        return GateResult(broadcast=False, lifecycle="suppress",
                          reason="persistence unavailable")

    # ── Tombstone all-clear ─────────────────────────────────────────────────
    if kind == "wfigs_tombstone":
        if not irwin_id:
            return GateResult(broadcast=False, lifecycle="suppress",
                              reason="tombstone without irwin_id")
        fire_row = conn.execute(
            "SELECT incident_name, current_acres, current_contained_pct, "
            "last_broadcast_at, county, state, lat, lon "
            "FROM fires WHERE irwin_id = ?", (irwin_id,)
        ).fetchone()
        # Only fires that previously reached the mesh get a closure message.
        if fire_row is None or fire_row["last_broadcast_at"] is None:
            return GateResult(broadcast=False, lifecycle="suppress",
                              reason="tombstone for never-broadcast fire")
        acres = fire_row["current_acres"]
        contained_pct = fire_row["current_contained_pct"]
        patch: dict = {
            "category": "wildfire_closed",
            "_severity_override": "priority",
            # legacy _attach_commit_handles sets _cooldown_suffix=irwin_id then
            # the handler overwrites _dedup_suffix with "closed".
            "_cooldown_suffix": irwin_id,
            "_dedup_suffix": "closed",
            # render hints for the all-clear formatter (from the fire ROW, not
            # the tombstone payload): name/size/loc — no geocoder_city/landclass
            # so the anchor falls to resolve_anchor -> county exactly as legacy.
            "_allclear": True,
            "incident_name": fire_row["incident_name"],
            "acres": acres,
            "contained_pct": contained_pct,
            "lat": fire_row["lat"],
            "lon": fire_row["lon"],
            "county": fire_row["county"],
            "state": fire_row["state"],
        }
        return GateResult(
            broadcast=True, lifecycle="closed",
            reason=f"all-clear irwin={irwin_id}",
            data_patch=patch,
            commit=_make_commit(irwin_id, acres, contained_pct),
        )

    # ── Perimeter / unknown never broadcast ─────────────────────────────────
    if kind != "wfigs_incident":
        return GateResult(broadcast=False, lifecycle="suppress",
                          reason=f"non-broadcast kind {kind}")

    # ── Active incident state machine ───────────────────────────────────────
    acres = data.get("acres")
    contained_pct = data.get("contained_pct")
    declared_at_epoch = data.get("declared_at_epoch")

    row = conn.execute(
        "SELECT current_acres, current_contained_pct, last_broadcast_at, "
        "last_broadcast_acres, last_broadcast_contained "
        "FROM fires WHERE irwin_id = ?", (irwin_id,)).fetchone()

    # Stamps every active broadcast (New + Update) carries. category override
    # is added only for New (Update keeps the envelope-derived wildfire_incident).
    def _broadcast_patch(*, is_update: bool,
                          last_bcast_acres=None, last_bcast_contained=None,
                          new_category: bool) -> dict:
        p = {
            "_severity_override": "priority",
            "_dedup_suffix": f"{acres}|{contained_pct}",
            "_cooldown_suffix": irwin_id,
            "is_update": is_update,
            "last_bcast_acres": last_bcast_acres,
            "last_bcast_contained": last_bcast_contained,
        }
        if new_category:
            p["category"] = "wildfire_declared"
        return p

    # (i) row missing  &  (ii) row exists but never broadcast -> "New".
    if row is None or row["last_broadcast_at"] is None:
        # Age-gate suppresses only the "New" BROADCAST; the handler still writes
        # current_* unconditionally. Suppress carries NO stamps (legacy returns
        # None before setting category/severity) and does NOT trigger cleanup.
        if _fire_too_old_to_announce(declared_at_epoch, now):
            return GateResult(broadcast=False, lifecycle="suppress",
                              reason="new fire too old to announce")
        return GateResult(
            broadcast=True, lifecycle="new",
            reason="first sighting",
            data_patch=_broadcast_patch(is_update=False, new_category=True),
            commit=_make_commit(irwin_id, acres, contained_pct),
        )

    # (iii) row exists AND already broadcast -> forward-only growth + cooldown.
    last_bcast_at = row["last_broadcast_at"]
    last_bcast_acres = row["last_broadcast_acres"]
    last_bcast_contained = row["last_broadcast_contained"]

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
        return GateResult(
            broadcast=True, lifecycle="update",
            reason="forward-only growth after cooldown",
            data_patch=_broadcast_patch(
                is_update=True,
                last_bcast_acres=last_bcast_acres,
                last_bcast_contained=last_bcast_contained,
                new_category=False),
            commit=_make_commit(irwin_id, acres, contained_pct),
        )

    # No change OR inside cooldown -> suppress; handler runs _cleanup_stale_fires
    # for this case only (legacy did the cleanup in the case-(iii) suppress arm).
    return GateResult(broadcast=False, lifecycle="cooldown",
                      reason="no forward change or inside cooldown")


def _make_commit(irwin_id: str, acres, contained_pct):
    """Build the deferred commit closure: idempotent UPSERT of the
    ``fires`` broadcast-state columns.  Mirrors
    ``wfigs_handler._attach_commit_handles._on_commit`` exactly (minus the
    event_log flip, which the handler wraps back on)."""

    def _commit(committed_at: float) -> None:
        try:
            conn = get_db()
        except Exception:
            logger.exception(
                "fire commit: persistence unavailable; last_broadcast_* not "
                "updated for irwin=%s", irwin_id)
            return
        try:
            conn.execute(
                "UPDATE fires SET last_broadcast_at=?, "
                "first_broadcast_at=COALESCE(first_broadcast_at, ?), "
                "last_broadcast_acres=?, last_broadcast_contained=? "
                "WHERE irwin_id=?",
                (int(committed_at), int(committed_at), acres, contained_pct,
                 irwin_id),
            )
        except Exception:
            logger.exception("fire commit: fires UPSERT failed irwin=%s", irwin_id)

    return _commit
