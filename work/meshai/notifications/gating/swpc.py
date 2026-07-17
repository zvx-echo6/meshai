"""SWPC space-weather gating decider — Phase-1 refactor.

decide(data, *, source, now) -> GateResult:
    Canonical data schema consumed:
        event_id    str              — stable SWPC event identifier
        driver      str|None         — "kp" | "flare" | None (proton/S-scale;
                                        see _floor_for_scale — dispatched by
                                        scale_code prefix, not driver)
        scalar      float|str|None  — Kp value (float), flare class (str), or
                                       proton flux label (str, e.g. "10 pfu")
        scale_code  str              — pre-computed NOAA scale code
                                        ("G3", "R3", "S1")
        message     str              — SWPC alert message body (for format seam)
        issued_at   str|None         — ISO timestamp

    Emitted data_patch keys:
        _severity_override  str    — "priority" (level 3+), "immediate" (level 4+)
        _cooldown_suffix    str    — scale_code (collapses cross-sub-adapter
                                     events in the dispatcher cooldown window)

    commit(now: float) -> None:
        Idempotent UPSERT: sets last_broadcast_at on the swpc_events row.
        For kp driver: also defers the cross-adapter geomag window stamp into
        the commit closure (intentional divergence from swpc_handler.py's
        inline _geomag_recent[scale_code] = now stamp).

Gate logic:
    kp driver:      G3+ — scale_code numeric level >= 3
    flare driver:   R3+ — scale_code numeric level >= 3
    S-scale (proton, scale_code prefix "S"): S1+ — scale_code numeric level >= 1.
        Deliberately a LOWER floor than G/R. This matches the floor
        central/swpc_handler.py originally used verbatim: "Solar proton event
        >= 10 pfu @ >= 10 MeV (S1 minor radiation storm or higher)" (see that
        module's docstring) — Central never required S3+ for protons. Recovered
        here, not invented; see _floor_for_scale().

Cross-adapter geomag 600s window:
    kp events within 600s of a COMMITTED broadcast for the same scale_code are
    suppressed.  State lives in the module-level _geomag_window dict, replacing
    the _geomag_recent dict that used to live in swpc_handler.py (L61-62).
    The window stamp is deferred into the commit closure so it only ticks on
    confirmed delivery, not on decision.

First-sighting rule:
    Check swpc_events.last_broadcast_at for the event_id.
    None (row absent or last_broadcast_at IS NULL) → broadcast (lifecycle="new").
    last_broadcast_at IS NOT NULL → suppress (already broadcast; SWPC events
        are point-in-time, no re-broadcast on revision).
"""
from __future__ import annotations

import logging
from typing import Optional

from meshai.notifications.gating.base import GateResult
from meshai.persistence import get_db

logger = logging.getLogger(__name__)

# Cross-adapter geomag dedup window.
# Keyed on scale_code (e.g. "G3"); value = epoch of last committed broadcast.
# Cleared on process restart (acceptable — worst case one dup on restart).
GEOMAG_DEDUP_WINDOW_SECONDS = 600
_geomag_window: dict[str, float] = {}


def _scale_level(scale_code: str) -> int:
    """Parse numeric level from NOAA scale code e.g. 'G3' → 3, 'R5' → 5."""
    if not scale_code or len(scale_code) < 2:
        return 0
    try:
        return int(scale_code[1:])
    except (ValueError, TypeError):
        return 0


def _floor_for_scale(scale_code: str) -> int:
    """Broadcast floor (minimum NOAA scale level) for *scale_code*'s family.

    G/R: level >= 3 (G3/R3 "strong" or higher).
    S (proton, scale_code prefix "S"): level >= 1 (S1 "minor" or higher) —
    matches central/swpc_handler.py's original proton floor of >= 10 pfu
    @ >= 10 MeV, which IS the S1 threshold. Central intentionally used a
    lower floor for protons than for geomag/flare; this is not a new policy,
    it is the recovered original one.
    """
    if scale_code.startswith("S"):
        return 1
    return 3


def _severity_for_scale(scale_code: str) -> str:
    """Map NOAA scale code to meshai severity override.

    G5/R5 → immediate (extreme)
    G4/R4 → immediate (severe, mass-impact)
    G3/R3 → priority  (strong — broadcast-worthy but not life-safety)
    G1/G2/R1/R2 → routine (won't reach here via floor, safe fallback)
    """
    level = _scale_level(scale_code)
    if level >= 4:
        return "immediate"
    if level >= 3:
        return "priority"
    return "routine"


def decide(data: dict, *, source: str, now: float) -> GateResult:
    """Gate + first-sighting decision for geomagnetic_storm and rf_propagation_alert.

    Parameters
    ----------
    data:
        Canonical Event.data dict (see module docstring for schema).
    source:
        Adapter source name, e.g. "swpc".
    now:
        Current epoch (from clock.now()) — determinism seam, no time.time().

    Returns
    -------
    GateResult with:
        broadcast=True   lifecycle="new"       data_patch+commit populated
        broadcast=False  lifecycle="suppress"  data_patch={} commit=None
    """
    event_id = data.get("event_id")
    if not event_id:
        return GateResult(
            broadcast=False, lifecycle="suppress",
            reason="no event_id in canonical data",
        )

    driver = data.get("driver")        # "kp" | "flare"
    scale_code = data.get("scale_code") or ""

    # ── Scale floor check (G3+/R3+) ─────────────────────────────────────────
    if not scale_code:
        return GateResult(
            broadcast=False, lifecycle="suppress",
            reason=f"no scale_code for event_id={event_id} driver={driver}",
        )

    level = _scale_level(scale_code)
    floor = _floor_for_scale(scale_code)
    if level < floor:
        return GateResult(
            broadcast=False, lifecycle="suppress",
            reason=f"scale {scale_code} below floor (level={level} < {floor})",
        )

    # ── Geomag cross-adapter 600s window (kp driver only) ───────────────────
    # The window stamp is deferred into commit() so only confirmed deliveries
    # count.  _cooldown_suffix also triggers dispatcher-level dedup as a
    # complementary mechanism.
    if driver == "kp":
        prev_ts = _geomag_window.get(scale_code)
        if prev_ts is not None and (now - prev_ts) < GEOMAG_DEDUP_WINDOW_SECONDS:
            logger.debug(
                "swpc gating: geomag dedup — %s from source=%s "
                "already broadcast %.0fs ago (window=%ds)",
                scale_code, source, now - prev_ts, GEOMAG_DEDUP_WINDOW_SECONDS,
            )
            return GateResult(
                broadcast=False, lifecycle="suppress",
                reason=(
                    f"geomag dedup: {scale_code} committed {now - prev_ts:.0f}s ago "
                    f"(window={GEOMAG_DEDUP_WINDOW_SECONDS}s)"
                ),
            )

    # ── Persistence: first-sighting check ───────────────────────────────────
    try:
        conn = get_db()
    except Exception:
        logger.exception("swpc decide: persistence unavailable")
        return GateResult(
            broadcast=False, lifecycle="suppress",
            reason="persistence unavailable",
        )

    row = conn.execute(
        "SELECT last_broadcast_at FROM swpc_events WHERE event_id=?",
        (event_id,),
    ).fetchone()

    if row is not None and row["last_broadcast_at"] is not None:
        return GateResult(
            broadcast=False, lifecycle="suppress",
            reason=f"already broadcast at {row['last_broadcast_at']}",
        )

    # ── Insert row (first-sighting) ──────────────────────────────────────────
    # INSERT OR IGNORE: safe against concurrent arrivals for the same event_id.
    # event_type and payload_json are filled in by handle_swpc via _upsert_swpc
    # (which UPDATE-patches after this INSERT).
    if row is None:
        try:
            conn.execute(
                "INSERT OR IGNORE INTO swpc_events"
                "(event_id, event_type, severity_int, payload_json, "
                "occurred_at, first_seen_at, last_broadcast_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (event_id, driver or "unknown", None, None,
                 int(now), int(now), None),
            )
        except Exception:
            logger.exception("swpc decide: INSERT failed for %s", event_id)

    # ── Build data_patch ─────────────────────────────────────────────────────
    sev_override = _severity_for_scale(scale_code)

    patch: dict = {
        # Tier-b fix: set severity from scale instead of always "routine".
        "_severity_override": sev_override,
        # Collates cross-sub-adapter events in the dispatcher's cooldown dedup.
        "_cooldown_suffix": scale_code,
    }

    # ── Build commit closure ─────────────────────────────────────────────────
    _event_id = event_id
    _driver = driver
    _scale_code = scale_code

    def _commit(committed_at: float) -> None:
        """Idempotent UPSERT: arm last_broadcast_at. Defer geomag window stamp."""
        try:
            c = get_db()
            c.execute(
                "UPDATE swpc_events "
                "SET last_broadcast_at=?, "
                "first_broadcast_at=COALESCE(first_broadcast_at, ?) "
                "WHERE event_id=?",
                (int(committed_at), int(committed_at), _event_id),
            )
        except Exception:
            logger.exception(
                "swpc commit: persistence update failed for %s", _event_id
            )
        # Deferred geomag window stamp — only ticks on confirmed delivery.
        # Intentional divergence from swpc_handler.py's inline stamp.
        if _driver == "kp":
            _geomag_window[_scale_code] = committed_at

    return GateResult(
        broadcast=True,
        lifecycle="new",
        reason=(
            f"first sighting {scale_code} event_id={event_id} source={source}"
        ),
        data_patch=patch,
        commit=_commit,
    )
