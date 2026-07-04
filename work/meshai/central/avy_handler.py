"""Central avalanche advisory handler — Phase-1 refactored bridge.

Subscribes to CENTRAL_AVY stream via consumer.py routing.
Adapter: avalanche_org
Subjects: central.avy.advisory.> (active + tombstones in one consumer)

Phase-1 refactor: ALL gating decisions are now delegated to
`meshai.notifications.gating.avalanche.decide()`.  This function is kept
as a thin compatibility bridge so existing call-sites and tests remain stable
while the new formatter+decider architecture is established.

centralseverity → NAADS mapping (tier-b):
    Central's envelope carries `severity` (= centralseverity) on a COMPRESSED
    5-point scale where 2=Considerable, 3=High, 4=Extreme (documented in the
    pre-refactor TODO comment).  The canonical danger_level is NAADS 1–5.

    NAADS mapping table (see also gating/avalanche.py docstring):
        centralseverity 0 → NAADS 1 (Low)           [INFERRED, needs validation]
        centralseverity 1 → NAADS 2 (Moderate)      [INFERRED, needs validation]
        centralseverity 2 → NAADS 3 (Considerable)  [documented]
        centralseverity 3 → NAADS 4 (High)           [documented]
        centralseverity 4 → NAADS 5 (Extreme)        [documented]
        other             → NAADS 0 (No Rating)

    ⚠ NEEDS LIVE VALIDATION IN-SEASON (October+): confirm by reading a live
    Central avalanche envelope for a zone known to be rated Considerable
    and verifying centralseverity == 2.

    Strategy: prefer data.data.danger_level (direct from avalanche.org API,
    NAADS 1–5); fall back to _remap_centralseverity(inner.severity) when
    data.danger_level is absent or non-numeric.

Tombstones (central.avy.advisory.removed.*): handler returns None.
"""

import logging
import re
import time
from typing import Any, Optional

from meshai.adapter_config import adapter_config
from meshai.central.budget import budget_for, fit_to_budget
from meshai.persistence import get_db

logger = logging.getLogger(__name__)


# NAADS danger level names (for fallback when data.danger_name is absent)
_NAADS_NAMES: dict[int, str] = {
    0: "No Rating",
    1: "Low",
    2: "Moderate",
    3: "Considerable",
    4: "High",
    5: "Extreme",
}


def _remap_centralseverity(centralseverity: Any) -> int:
    """Map Central's compressed severity int → NAADS 1–5 danger_level.

    Mapping table (⚠ needs live validation in-season):
        0 → 1 (Low)            [inferred]
        1 → 2 (Moderate)       [inferred]
        2 → 3 (Considerable)   [documented in pre-refactor avy_handler comments]
        3 → 4 (High)           [documented]
        4 → 5 (Extreme)        [documented]
        other → 0 (No Rating)

    Formula: naads = centralseverity + 1  for  0 ≤ centralseverity ≤ 4
    """
    try:
        sev = int(centralseverity)
    except (TypeError, ValueError):
        return 0
    if 0 <= sev <= 4:
        return sev + 1
    return 0


def _canonical_danger_level(d: dict, inner: dict) -> Optional[int]:
    """Derive canonical NAADS danger_level from envelope fields.

    Strategy (in order):
      1. data.data.danger_level — direct from avalanche.org, should be NAADS.
      2. _remap_centralseverity(inner.severity) — fallback via mapping table.
      3. None — caller should suppress.

    Returns int or None.
    """
    # Primary: data.data.danger_level (NAADS from avalanche.org)
    raw = d.get("danger_level")
    if isinstance(raw, (int, float)) and raw >= 0:
        return int(raw)
    # Fallback: remap from centralseverity
    fallback = _remap_centralseverity(inner.get("severity"))
    if fallback > 0:
        logger.debug(
            "avy_handler: danger_level absent in data.data — "
            "remapped centralseverity %r → NAADS %d",
            inner.get("severity"), fallback,
        )
        return fallback
    return None


def _coerce_severity(sev: Any) -> Optional[str]:
    if sev is None:
        return None
    if isinstance(sev, str):
        return sev or None
    try:
        return str(int(sev))
    except (TypeError, ValueError):
        return str(sev)


def _now() -> int:
    return int(time.time())


def handle_avy(envelope: dict, subject: str,
               data: Optional[dict] = None) -> Optional[str]:
    """Central path handler for NWAC/CAIC avalanche advisories.

    Phase-1 refactor: delegates gating to
    `meshai.notifications.gating.avalanche.decide()`.  Formats via
    `_render()` for backward-compat callers (existing tests); the registered
    `formatters.avalanche.format()` re-renders from event.data at dispatch
    time with any tier-b changes (is_update prefix).

    On broadcast:
      - Writes canonical data fields into the shared `data` dict so the Event
        carries structured fields (the formatter reads them at dispatch time).
      - Applies GateResult.data_patch (is_update, _severity_override).
      - Attaches _on_broadcast_committed for event_log.handled update.
      - Returns the _render() wire string for backward-compat.

    On suppress: returns None (default-deny unchanged).
    """
    if not isinstance(envelope, dict):
        return None

    inner = envelope.get("data") or {}
    if (inner.get("adapter") or "") != "avalanche_org":
        return None

    category = inner.get("category") or ""

    # Tombstone — consume silently, no broadcast.
    if "removed" in category:
        logger.debug("avy_handler: tombstone for %s — acking silently", category)
        return None

    d = inner.get("data") or {}
    geo = inner.get("geo") or {}
    severity_word = _coerce_severity(inner.get("severity"))

    # ── Canonical danger_level (NAADS 1–5) ───────────────────────────────────
    danger_level = _canonical_danger_level(d, inner)
    if danger_level is None:
        return None

    danger_name = (
        d.get("danger_name") or _NAADS_NAMES.get(danger_level, str(danger_level))
    )
    zone_name = d.get("zone_name") or "Unknown Zone"
    center_id = d.get("center_id") or ""
    travel = (d.get("travel_advice") or "").strip()

    # ── Centroid from geo.centroid [lon, lat] ─────────────────────────────────
    centroid = geo.get("centroid") or []
    if isinstance(centroid, (list, tuple)) and len(centroid) >= 2:
        lat, lon = centroid[1], centroid[0]
    else:
        lat = d.get("latitude")
        lon = d.get("longitude")

    # ── Build canonical data dict ─────────────────────────────────────────────
    canonical: dict = {
        "danger_level": danger_level,
        "danger_name": danger_name,
        "zone_name": zone_name,
        "center_id": center_id,
        "travel_advice": travel,
        "lat": lat,
        "lon": lon,
        "is_update": False,  # Central path: no per-zone trend detection yet
    }

    # Optional expires timestamp
    expires_str = inner.get("expires")
    if expires_str and isinstance(expires_str, str):
        from datetime import datetime
        try:
            canonical["expires"] = datetime.fromisoformat(
                expires_str.replace("Z", "+00:00")
            ).timestamp()
        except Exception:
            pass

    # ── Persist to event_log ──────────────────────────────────────────────────
    conn = get_db()
    if conn is None:
        logger.warning("avy_handler: persistence unavailable, skipping")
        return None

    log_id = _log_event_returning_id(
        conn, now=_now(), source="avalanche_org",
        category=category, severity_word=severity_word,
        event_id_external=f"{center_id}:{zone_name}",
        subject=subject, handled=0,
        table_name="event_log", table_pk=None,
    )

    # ── Delegate to gating module ─────────────────────────────────────────────
    from meshai.notifications.gating.avalanche import decide as _gate_decide
    gate = _gate_decide(canonical, source="avalanche", now=float(_now()))

    if not gate.broadcast:
        return None

    # ── Write canonical into shared data dict ────────────────────────────────
    # Cutover gate: `category` here is the envelope category (e.g.
    # "avalanche_warning" or "avalanche_watch") — same strings registered in
    # the formatter/gating registries.  When cut over, gate.data_patch (which
    # carries is_update + _severity_override) flows to the live Event; when not
    # cut over, old-style _attach_commit preserves pre-Phase-1 live behavior.
    from meshai.notifications.cutover import is_cutover
    if isinstance(data, dict):
        data.update(canonical)
        if is_cutover(category):
            # NEW PATH: gate.data_patch provides is_update + _severity_override.
            data.update(gate.data_patch)
            data["_broadcast_audit"] = {"table": "event_log", "pk": log_id}

            _raw_gate_commit = gate.commit
            _log_row_id = log_id

            def _on_commit(committed_at: float) -> None:
                """Idempotent: mark event_log row handled on confirmed delivery."""
                if _raw_gate_commit is not None:
                    _raw_gate_commit(committed_at)
                if _log_row_id is not None:
                    try:
                        c = get_db()
                        c.execute(
                            "UPDATE event_log SET handled=1 WHERE id=?",
                            (int(_log_row_id),),
                        )
                    except Exception:
                        logger.exception(
                            "avy commit: event_log update failed for log_id=%s",
                            _log_row_id,
                        )

            data["_on_broadcast_committed"] = _on_commit
        else:
            # NOT cutover: old-style commit (canonical only, no data_patch).
            _attach_commit(data, log_id=log_id)

    # ── Return _render() wire (backward compat — existing tests check this) ───
    # At dispatch time compose_mesh_message() calls the registered formatter
    # which re-renders from event.data (tier-b: is_update prefix applies there).
    return _render(
        danger_level=danger_level,
        danger_name=danger_name,
        zone_name=zone_name,
        center_id=center_id,
        travel=travel,
    )


def _render(*, danger_level: int, danger_name: str, zone_name: str,
            center_id: str, travel: str) -> str:
    """Wire string renderer — backward-compat entrypoint for direct callers.

    The registered formatters/avalanche.format() is the canonical render path
    at dispatch time.  This function is kept for existing test callers
    (test_adapter_avalanche, etc.) and for the handle_avy() return value.
    """
    emoji = "⛷"
    # Warning for High/Extreme (4-5), Watch for Considerable (3).
    prefix = "WARNING:" if danger_level >= 4 else "Watch:"

    line1 = f"{emoji} AVY {prefix} {zone_name} — {danger_name} ({danger_level})"
    # Travel advice: FIRST SENTENCE only (up to and including the first
    # sentence terminator), instead of a fixed character slice.
    line2 = None
    if travel and travel.strip():
        t = travel.strip()
        m = re.search(r"[.!?]", t)
        line2 = t[: m.end()] if m else t
    line3 = f"{center_id} · valid today" if center_id else "valid today"

    msg = "\n".join(l for l in [line1, line2, line3] if l)
    return fit_to_budget(msg, budget_for("avalanche"))


def _attach_commit(data: Optional[dict], *, log_id: Optional[int]) -> None:
    """Legacy helper — used by pre-refactor callers (kept for import compat)."""
    if not isinstance(data, dict):
        return

    def _on_commit(committed_at: float) -> None:
        try:
            conn = get_db()
        except Exception:
            logger.exception("avy commit: persistence unavailable")
            return
        if log_id is not None:
            conn.execute(
                "UPDATE event_log SET handled=1 WHERE id=?",
                (int(log_id),),
            )

    data["_on_broadcast_committed"] = _on_commit
    data["_broadcast_audit"] = {
        "table": "event_log",
        "pk": log_id,
    }


def _log_event_returning_id(
    conn, *, now, source, category, severity_word,
    event_id_external, subject, handled,
    table_name, table_pk,
) -> Optional[int]:
    cursor = conn.execute(
        "INSERT INTO event_log(received_at, source, category, severity_word, "
        "event_id_external, nats_subject, handled, table_name, table_pk) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (now, source, category, severity_word, event_id_external,
         subject, int(bool(handled)), table_name, table_pk),
    )
    return cursor.lastrowid
