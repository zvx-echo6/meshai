"""FIRMS fire-fusion gating decider — Phase-3c migration.

Moves the three FIRMS *broadcast decisions* out of
``central.firms_handler`` into a source-agnostic decider, mirroring
quake/wfigs.  It covers ONLY the decision + the deferred latch; every bit of
attribution / pass-aggregation / centroid / perimeter plumbing STAYS INLINE in
the handler (that data plumbing is analogous to hydro's ``gauge_readings``
INSERT and does not belong in a decider).

Three broadcast categories are handled, discriminated by an internal
``_kind`` the handler stamps onto the decider input:

    firms_growth    -> wildfire_growth   (drift >= threshold at a pass boundary)
    firms_spotting  -> wildfire_spotting (pixel outside prior perimeter, cooldown)
    firms_halt      -> wildfire_halted   (fire idle >= halt_minimum_seconds)

The unattributed-hotspot cluster path is LIVE (shipped in ``d479ca53`` / #73)
and is NOT represented here -- it is not one of the three broadcast
categories this decider covers. Native ``env/fires.py`` hotspot broadcasts
(new_ignition / wildfire_hotspot) are a deferred follow-up and are not
migrated.

Tier-b deferral (intended behavior change, validated by gate-sequence, NOT
golden byte-parity):
    The eager latch writes the legacy handler performed *before* returning the
    wire — ``fires.last_spotting_broadcast_at`` (spotting) and
    ``fires.halt_broadcast_at`` (halt) — move into the deferred ``commit``
    closure.  A broadcast that is composed but then dropped by the dispatcher
    (cold-start grace / dedup / toggle) therefore no longer burns the latch, so
    the next eligible pixel can still fire.  ``wildfire_growth`` has no latch
    (its re-eligibility is governed by the inline pass cursor), so its
    ``commit`` is None.

The decider input dicts the handler passes:

    firms_growth:   {_kind, irwin_id, boundary, drift_mi,
                     drift_direction, drift_mi_per_hour}
    firms_spotting: {_kind, irwin_id, dist_mi, direction, incident_name}
    firms_halt:     {_kind}
"""
from __future__ import annotations

import logging

from meshai.adapter_config import adapter_config
from meshai.notifications.gating.base import GateResult
from meshai.persistence import get_db

logger = logging.getLogger(__name__)


def decide(data: dict, *, source: str, now: float) -> GateResult:
    """Broadcast decision for a FIRMS fusion event.

    Parameters
    ----------
    data:
        Discriminated decider input (see module docstring for the per-kind
        schema).  ``data["_kind"]`` selects the growth / spotting / halt path.
    source:
        Adapter source name, e.g. "firms" (accepted for signature parity).
    now:
        Current epoch (float) — determinism seam.

    Returns
    -------
    GateResult with broadcast/lifecycle/data_patch/commit populated.  The
    handler still renders the wire inline and, on the not-cutover live path,
    keeps its eager-latch / stamping VERBATIM; ``data_patch`` supplies the
    stamps + render hints and ``commit`` arms the deferred latch on delivery.
    """
    if not isinstance(data, dict):
        return GateResult(broadcast=False, lifecycle="suppress",
                          reason="bad decider input")
    kind = data.get("_kind")

    # ── wildfire_growth ─────────────────────────────────────────────────────
    if kind == "firms_growth":
        boundary = bool(data.get("boundary"))
        drift_mi = data.get("drift_mi")
        threshold = float(adapter_config.fires.growth_drift_threshold_mi)
        if (not boundary) or drift_mi is None or drift_mi < threshold:
            return GateResult(broadcast=False, lifecycle="suppress",
                              reason="no pass boundary or drift below threshold")
        irwin_id = data.get("irwin_id")
        drift_direction = data.get("drift_direction")
        drift_mi_per_hour = data.get("drift_mi_per_hour")
        patch: dict = {
            # Legacy stamps (verbatim on the not-cutover path).
            "category": "wildfire_growth",
            "_severity_override": "immediate",
            "_cooldown_suffix": irwin_id,
            # Render hints for the fire formatter (cutover path). The growth
            # wire reuses formatters/fire.py's incident render; feeding ONLY
            # these fields reproduces the legacy _render() key-mismatch output
            # (size unknown · containment unknown + movement line) byte-for-byte.
            "is_update": True,
            "movement": {"direction": drift_direction,
                         "speed_mph": (drift_mi_per_hour or 0.0)},
        }
        # Growth has no latch — re-eligibility is the inline pass cursor.
        return GateResult(broadcast=True, lifecycle="growth",
                          reason="centroid drift >= threshold at pass boundary",
                          data_patch=patch, commit=None)

    # ── wildfire_spotting ───────────────────────────────────────────────────
    if kind == "firms_spotting":
        irwin_id = data.get("irwin_id")
        cooldown_s = int(adapter_config.fires.spotting_cooldown_seconds)
        try:
            conn = get_db()
        except Exception:
            logger.exception("firms decide (spotting): persistence unavailable")
            return GateResult(broadcast=False, lifecycle="suppress",
                              reason="persistence unavailable")
        # Cooldown gate — mirrors _check_spotting's inline read exactly.
        row = conn.execute(
            "SELECT last_spotting_broadcast_at FROM fires WHERE irwin_id=?",
            (irwin_id,),
        ).fetchone()
        if row is not None:
            last_ts = row["last_spotting_broadcast_at"]
            if last_ts is not None and (float(now) - float(last_ts)) < cooldown_s:
                return GateResult(broadcast=False, lifecycle="cooldown",
                                  reason="inside spotting cooldown")
        patch = {
            # Legacy stamps (verbatim on the not-cutover path). issue #121:
            # must be _severity_override -- consumer.py only ever honors that
            # key; a plain "severity" key here is a silent no-op (same class
            # of bug as #118, fixed for firms_handler.py in PR #120).
            "category": "wildfire_spotting",
            "_severity_override": "immediate",
            # Render hints for the firms formatter (cutover path).
            "dist_mi": data.get("dist_mi"),
            "direction": data.get("direction"),
            "incident_name": data.get("incident_name"),
        }
        return GateResult(broadcast=True, lifecycle="spotting",
                          reason="pixel outside prior perimeter beyond threshold",
                          data_patch=patch,
                          commit=_spotting_commit(irwin_id))

    # ── wildfire_halted ─────────────────────────────────────────────────────
    if kind == "firms_halt":
        try:
            conn = get_db()
        except Exception:
            logger.exception("firms decide (halt): persistence unavailable")
            return GateResult(broadcast=False, lifecycle="suppress",
                              reason="persistence unavailable")
        minimum_s = int(adapter_config.fires.halt_minimum_seconds)
        cutoff = float(now) - float(minimum_s)
        # Mirror _maybe_emit_halt's SELECT exactly (same ORDER/LIMIT so the
        # chosen fire is identical).
        row = conn.execute(
            "SELECT irwin_id, incident_name, last_pass_at FROM fires "
            "WHERE tombstoned_at IS NULL "
            "AND last_pass_at IS NOT NULL AND last_pass_at <= ? "
            "AND (halt_broadcast_at IS NULL "
            "     OR halt_broadcast_at < last_pass_at) "
            "ORDER BY last_pass_at ASC LIMIT 1",
            (cutoff,),
        ).fetchone()
        if row is None:
            return GateResult(broadcast=False, lifecycle="suppress",
                              reason="no fire meets halt criteria")
        irwin_id = row["irwin_id"]
        name = row["incident_name"] or "(unnamed fire)"
        hours = max(0, int((float(now) - float(row["last_pass_at"])) / 3600.0))
        patch = {
            # Legacy stamps (verbatim on the not-cutover path). issue #121:
            # must be _severity_override -- consumer.py only ever honors that
            # key; a plain "severity" key here is a silent no-op (same class
            # of bug as #118, fixed for firms_handler.py in PR #120).
            "category": "wildfire_halted",
            "_severity_override": "routine",
            # Render hints + the identity the handler needs for the wire/latch.
            "incident_name": name,
            "hours": hours,
            "irwin_id": irwin_id,
        }
        return GateResult(broadcast=True, lifecycle="halt",
                          reason=f"fire idle irwin={irwin_id}",
                          data_patch=patch,
                          commit=_halt_commit(irwin_id))

    # Unknown kind (e.g. the decider was invoked from the shadow/cutover harness
    # with a real FIRMS event.data before FIRMS is shadowed) — suppress. FIRMS
    # shadow/cutover is a deferred follow-up.
    return GateResult(broadcast=False, lifecycle="suppress",
                      reason=f"unhandled firms kind {kind!r}")


def _spotting_commit(irwin_id):
    """Deferred latch: stamp fires.last_spotting_broadcast_at on delivery.

    Tier-b change: legacy stamped this eagerly with the handler ``now`` before
    returning the wire; here it fires only on confirmed delivery, at the
    delivery timestamp.  Idempotent (a plain UPDATE), never raises.
    """
    def _commit(committed_at: float) -> None:
        try:
            conn = get_db()
        except Exception:
            logger.exception(
                "firms spotting commit: persistence unavailable; "
                "last_spotting_broadcast_at not stamped for irwin=%s", irwin_id)
            return
        try:
            conn.execute(
                "UPDATE fires SET last_spotting_broadcast_at=? WHERE irwin_id=?",
                (float(committed_at), irwin_id),
            )
        except Exception:
            logger.exception(
                "firms spotting commit: latch UPDATE failed irwin=%s", irwin_id)

    return _commit


def _halt_commit(irwin_id):
    """Deferred latch: stamp fires.halt_broadcast_at on delivery.

    Tier-b change: legacy stamped this eagerly with the handler ``now``; here it
    fires only on confirmed delivery, at the delivery timestamp.  The halt
    re-eligibility filter (``halt_broadcast_at IS NULL OR halt_broadcast_at <
    last_pass_at``) still holds because committed_at > now > last_pass_at.
    Idempotent (a plain UPDATE), never raises.
    """
    def _commit(committed_at: float) -> None:
        try:
            conn = get_db()
        except Exception:
            logger.exception(
                "firms halt commit: persistence unavailable; "
                "halt_broadcast_at not stamped for irwin=%s", irwin_id)
            return
        try:
            conn.execute(
                "UPDATE fires SET halt_broadcast_at=? WHERE irwin_id=?",
                (float(committed_at), irwin_id),
            )
        except Exception:
            logger.exception(
                "firms halt commit: latch UPDATE failed irwin=%s", irwin_id)

    return _commit
