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
      to determine prior state for source="nwis"; it NEVER writes for that
      source.  See "Native persistence" below for source="usgs".
    * The decider MUST run BEFORE the handler's INSERT (the old handler did its
      prior-SELECT and 00060 back-look before inserting the new row), so the
      reads never see the current reading.  The prior-SELECT additionally
      filters `reading_time < current` for defence-in-depth.
    * Hydro has no per-event broadcast-state table (unlike quake_events), so
      the decider owns NO deferred state write → commit is None.  The
      event_log.handled flip stays handler-owned (mirrors the old
      _attach_commit, wrapped in the cutover branch like quake_handler).

Native persistence (fix/native-gauge-flood-alerts):
    env/usgs.py (source="usgs") has no handler module of its own with a
    persistence connection the way central.nwis_handler does for source="nwis"
    -- it is a pure HTTP-fetch-and-translate adapter.  Central stopped writing
    gauge_readings when nwis_handler stopped running, so without SOME writer
    every native prior-state SELECT above would return "normal" (no row)
    forever, and every elevated reading would rank as an upward crossing and
    rebroadcast on every 15-minute tick instead of once per crossing.  This
    decider owns that write for source != "nwis" (see _persist_native_reading
    below), keeping the "decider reads-only, handler writes" contract intact
    for the Central source.

    KNOWN LIMITATION: env/usgs.py's to_event() only ever emits ELEVATED
    readings (action stage or above) by design -- a routine/below-action
    reading has no flood_status and is intentionally never turned into an
    Event, so it never reaches this decider and a "back to normal" row is
    never written.  If a gauge fully recedes below action stage and later
    re-crosses into action stage, the most recent gauge_readings row for that
    site is still the last ELEVATED tier it saw (not "normal"), so the
    re-crossing will compare equal-rank and be SUPPRESSED rather than
    broadcast as a new crossing.  This degrades toward silence, not spam (the
    safe direction), and only resolves once the gauge reaches a HIGHER tier
    than it last alerted at.  Fixing it properly would mean also emitting
    normal-state readings from env/usgs.py so a recede-to-normal row gets
    written, which is a materially bigger behavior change to the native
    emission/inhibition pipeline than this fix's scope -- left for the
    Central rip-out follow-up.

data_patch keys (populated on EVERY call, broadcast or suppress, so the
handler's unconditional INSERT + render use the back-looked values):
    threshold_state : str    — resolved band (post-00060 back-look)
    stage_ft        : float|None — resolved stage (post-00060 back-look)
"""
from __future__ import annotations

import logging
from typing import Optional

from meshai.adapter_config import adapter_config
from meshai.notifications.gating.base import GateResult
from meshai.persistence import get_db

logger = logging.getLogger(__name__)

# Ordered list of threshold names from low to high. Used to compare
# "is current threshold higher than prior" (upward crossing detection).
# Formerly meshai.central.idaho_gauge_sites.THRESHOLD_RANK -- inlined here
# since this decider was its only consumer.
THRESHOLD_RANK = ["normal", "action", "flood_minor", "flood_moderate", "flood_major"]


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

    # Native persistence: own the write for the native source only (source=
    # "nwis" keeps its inline handler-owned INSERT, per the module docstring).
    # Must run AFTER the reads above (same ordering rule as the Central
    # handler's INSERT) so this reading is never its own "prior".
    if source != "nwis":
        _persist_native_reading(conn, data, threshold_state=threshold_state,
                                 stage_ft=stage_ft, now=now)

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


def _persist_native_reading(conn, data: dict, *, threshold_state: str,
                             stage_ft: Optional[float], now: float) -> None:
    """Unconditional INSERT of a native (env/usgs.py) gauge reading.

    Mirrors central.nwis_handler's inline INSERT into the same table with the
    same column shape, but owned here because env/usgs.py has no handler
    module of its own with a persistence connection.  Runs regardless of the
    broadcast decision -- exactly like the Central handler's INSERT -- so the
    NEXT native reading's prior-state SELECT above sees this one.  Never
    raises: a persistence failure degrades the next lookup (falls back to
    "no prior" / first-crossing) rather than blocking the current decision.
    """
    reading_time = data.get("reading_time")
    if not isinstance(reading_time, (int, float)):
        reading_time = now
    try:
        conn.execute(
            "INSERT INTO gauge_readings(site_id, gauge_name, reading_value, "
            "reading_unit, threshold_state, flow_cfs, reading_time, lat, lon) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                data.get("site_id"),
                data.get("gauge_name"),
                stage_ft,
                data.get("unit"),
                threshold_state,
                data.get("flow_cfs"),
                int(reading_time),
                data.get("lat"),
                data.get("lon"),
            ),
        )
    except Exception:
        logger.exception("hydro decide: native gauge_readings persist failed")
