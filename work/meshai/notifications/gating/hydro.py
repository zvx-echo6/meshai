"""USGS NWIS stream-gauge (hydro) gating decider — Phase-3 migration.

Moves the threshold-crossing decision out of central.nwis_handler.handle_nwis
(the inline prior-reading SELECT, the 00060/00065 stage back-look, the
THRESHOLD_RANK upward-crossing check, and the broadcast_on_recede toggle).

Broadcast rule (verbatim from the old handler):
    Compare the current reading's threshold_state to the most recent PRIOR
    reading (strictly earlier reading_time, any parameter) for the same site,
    on the ranked scale {normal < action < flood_minor < flood_moderate <
    flood_major}.
      * cur_rank > prior_rank  → upward crossing → broadcast
      * cur_rank == prior_rank → unchanged band  → suppress
      * cur_rank < prior_rank  → receding        → suppress, UNLESS
        adapter_config.usgs_nwis.broadcast_on_recede is set → broadcast

00060 (discharge) back-look:
    A discharge-only envelope carries no stage → its threshold band is
    inherited from the latest 00065 (gage-height, reading_unit='ft') reading at
    the site.  The decider performs that look-back and returns the resolved
    (threshold_state, stage_ft) in data_patch so the handler can INSERT the
    gauge_readings row and render the wire with the same values.

IMPORTANT — division of labour with the handler:
    * The append-only gauge_readings INSERT stays INLINE in the Central handler
      (unconditional time-series write).  This decider only READS gauge_readings
      to determine prior state; it NEVER writes it.
    * The decider MUST run BEFORE the handler's INSERT (the old handler did its
      prior-SELECT and 00060 back-look before inserting the new row), so the
      reads never see the current reading.  The prior-SELECT additionally
      filters `reading_time < current` for defence-in-depth.
    * Hydro has no per-event broadcast-state table (unlike quake_events), so
      the decider owns NO deferred state write → commit is None.  The
      event_log.handled flip stays handler-owned (mirrors the old
      _attach_commit, wrapped in the cutover branch like quake_handler).

data_patch keys (populated on EVERY call, broadcast or suppress, so the
handler's unconditional INSERT + render use the back-looked values):
    threshold_state : str    — resolved band (post-00060 back-look)
    stage_ft        : float|None — resolved stage (post-00060 back-look)
"""
from __future__ import annotations

import logging
from typing import Optional

from meshai.adapter_config import adapter_config
from meshai.central.idaho_gauge_sites import THRESHOLD_RANK
from meshai.notifications.gating.base import GateResult
from meshai.persistence import get_db

logger = logging.getLogger(__name__)


def _rank(state: Optional[str]) -> int:
    """THRESHOLD_RANK index for *state*, treating unknown as 0 (normal)."""
    try:
        return THRESHOLD_RANK.index(state)
    except ValueError:
        return 0


def decide(data: dict, *, source: str, now: float) -> GateResult:
    """Upward-threshold-crossing decision for USGS NWIS stream-gauge readings.

    Parameters
    ----------
    data:
        Canonical Event.data dict.  Consumed keys:
            site_id, reading_time, parameter_code, threshold_state, stage_ft
        (gauge_name / flow_cfs / lat / lon are carried through by the handler
        for the formatter but are not needed for the gate decision.)
    source:
        Adapter source name, e.g. "nwis".
    now:
        Current epoch (from clock.now()) — determinism seam, unused here (hydro
        gating is state-comparison only, no time window).

    Returns
    -------
    GateResult:
        broadcast=True   lifecycle="new"       upward crossing (or recede+toggle)
        broadcast=False  lifecycle="suppress"  unchanged band or receding
        data_patch always carries the resolved threshold_state + stage_ft.
        commit is always None (no decider-owned state write).
    """
    site_id = data.get("site_id")
    reading_time = data.get("reading_time")
    pc = data.get("parameter_code")
    threshold_state = data.get("threshold_state") or "normal"
    stage_ft = data.get("stage_ft")

    try:
        conn = get_db()
    except Exception:
        logger.exception("nwis decide: persistence unavailable")
        return GateResult(
            broadcast=False, lifecycle="suppress",
            reason="persistence unavailable",
        )

    # Most recent PRIOR reading (strictly earlier) — same as the old handler.
    prior = conn.execute(
        "SELECT threshold_state FROM gauge_readings "
        "WHERE site_id=? AND reading_time < ? "
        "ORDER BY reading_time DESC LIMIT 1",
        (site_id, reading_time),
    ).fetchone()
    prior_state = prior["threshold_state"] if prior else "normal"

    # 00060 (discharge) back-look: inherit stage + band from the latest 00065
    # (gage-height) reading at this site.  Discharge alone has no threshold band.
    if pc == "00060":
        last_stage = conn.execute(
            "SELECT reading_value, threshold_state FROM gauge_readings "
            "WHERE site_id=? AND reading_unit='ft' "
            "ORDER BY reading_time DESC LIMIT 1",
            (site_id,),
        ).fetchone()
        if last_stage:
            stage_ft = last_stage["reading_value"]
            threshold_state = last_stage["threshold_state"] or "normal"

    # Resolved values returned so the handler's inline INSERT + render match.
    patch: dict = {"threshold_state": threshold_state, "stage_ft": stage_ft}

    prior_rank = _rank(prior_state)
    cur_rank = _rank(threshold_state)

    # Unchanged band — no broadcast.
    if cur_rank == prior_rank:
        return GateResult(
            broadcast=False, lifecycle="suppress",
            reason=f"unchanged band {threshold_state}",
            data_patch=patch, commit=None,
        )

    # Receding without the recede toggle — silent.
    if cur_rank < prior_rank and not bool(
            adapter_config.usgs_nwis.broadcast_on_recede):
        return GateResult(
            broadcast=False, lifecycle="suppress",
            reason=f"receding {prior_state}->{threshold_state}",
            data_patch=patch, commit=None,
        )

    # Upward crossing (or recede with the toggle enabled) — broadcast.
    return GateResult(
        broadcast=True, lifecycle="new",
        reason=f"crossing {prior_state}->{threshold_state}",
        data_patch=patch, commit=None,
    )
