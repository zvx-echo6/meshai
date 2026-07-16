"""IPAWS civil-alert gating decider.

Mirrors the NWS decider (gating/nws.py) EXACTLY in shape, but keys on the
IPAWS-owned ``ipaws_alerts`` table (NOT ``nws_alerts``) so IPAWS and NWS CAP
lifecycles never collide:

    - Tombstone: msgType in {Cancel, Expire} -> suppress
    - First-sighting (ipaws_alerts row is None): broadcast, prefix=""/"Update"
    - Cold-start race (row exists, last_broadcast_at IS NULL): broadcast
    - Dedup-window re-broadcast (>= duplicate_allowed_after_seconds):
      broadcast, prefix="Active"
    - Within dedup window: suppress

decide(data, *, source, now) -> GateResult

    Canonical data schema consumed:
        cap_id, msgType, references, event, area_desc, geocoder,
        cap_severity, expires_at, description, category, headline

    Emitted data_patch keys:
        _ipaws_prefix   str   — "", "Update", or "Active"

    commit(now: float) -> None:
        Idempotent UPDATE of last_broadcast_at + first_broadcast_at.
"""
from __future__ import annotations

import logging

from meshai.adapter_config import adapter_config
from meshai.notifications.gating.base import GateResult
from meshai.persistence import get_db

logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_update(conn, references: list) -> bool:
    """True if any CAP id in `references` was previously broadcast (Update)."""
    if not references:
        return False
    ref_ids = [r["identifier"] for r in references
               if isinstance(r, dict) and r.get("identifier")]
    if not ref_ids:
        return False
    placeholders = ",".join("?" * len(ref_ids))
    row = conn.execute(
        f"SELECT 1 FROM ipaws_alerts WHERE event_id IN ({placeholders}) "
        "AND last_broadcast_at IS NOT NULL LIMIT 1",
        ref_ids,
    ).fetchone()
    return row is not None


def _make_commit(cap_id: str):
    """Return an idempotent commit closure that arms last_broadcast_at."""
    def _commit(committed_at: float) -> None:
        try:
            c = get_db()
            c.execute(
                "UPDATE ipaws_alerts SET last_broadcast_at=?, "
                "first_broadcast_at=COALESCE(first_broadcast_at, ?) "
                "WHERE event_id=?",
                (int(committed_at), int(committed_at), cap_id),
            )
        except Exception:
            logger.exception("ipaws commit: persistence update failed for %s", cap_id)
    return _commit


# ── Public API ────────────────────────────────────────────────────────────────

def decide(data: dict, *, source: str, now: float) -> GateResult:
    """Gate + first-sighting decision for IPAWS civil alerts (ipaws_alerts table)."""
    cap_id = data.get("cap_id")
    if not cap_id:
        return GateResult(
            broadcast=False, lifecycle="suppress",
            reason="no cap_id in canonical data",
        )

    msg_type = data.get("msgType") or ""
    references = data.get("references") or []
    expires_at = data.get("expires_at")
    area_desc = data.get("area_desc") or ""
    cap_severity = data.get("cap_severity") or ""
    event_type = data.get("event") or ""
    geocoder = data.get("geocoder") or {}
    county = geocoder.get("county") or area_desc
    state = geocoder.get("state") or ""
    description = data.get("description") or ""
    headline = data.get("headline") or ""

    # ── Tombstone: Cancel/Expire → suppress ───────────────────────────────────
    try:
        tombstone_types = set(adapter_config.ipaws.tombstone_msgtypes)
    except Exception:
        tombstone_types = {"Cancel", "Expire"}
    if msg_type in tombstone_types:
        return GateResult(
            broadcast=False, lifecycle="tombstone",
            reason=f"msgType={msg_type!r} is a tombstone",
        )

    # ── Persistence ───────────────────────────────────────────────────────────
    try:
        conn = get_db()
    except Exception:
        logger.exception("ipaws decide: persistence unavailable")
        return GateResult(
            broadcast=False, lifecycle="suppress",
            reason="persistence unavailable",
        )

    row = conn.execute(
        "SELECT last_broadcast_at FROM ipaws_alerts WHERE event_id=?",
        (cap_id,),
    ).fetchone()

    # ── First sighting ────────────────────────────────────────────────────────
    if row is None:
        _prefix = "Update" if _is_update(conn, references) else ""
        conn.execute(
            "INSERT INTO ipaws_alerts(event_id, alert_type, severity, county, "
            "state, headline, description, expires_at, first_seen_at, "
            "last_broadcast_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (cap_id, event_type, cap_severity, county, state,
             headline, description,
             int(expires_at) if expires_at is not None else None,
             int(now), None),
        )
        return GateResult(
            broadcast=True,
            lifecycle="new",
            reason=f"first sighting cap_id={cap_id}",
            data_patch={"_ipaws_prefix": _prefix},
            commit=_make_commit(cap_id),
        )

    # ── Cold-start race: row exists but broadcast was previously dropped ───────
    if row["last_broadcast_at"] is None:
        _prefix = "Update" if _is_update(conn, references) else ""
        return GateResult(
            broadcast=True,
            lifecycle="cold_start",
            reason=f"cold-start race cap_id={cap_id}",
            data_patch={"_ipaws_prefix": _prefix},
            commit=_make_commit(cap_id),
        )

    # ── Dedup-window check ────────────────────────────────────────────────────
    last_bcast = float(row["last_broadcast_at"])
    try:
        window_s = int(adapter_config.ipaws.duplicate_allowed_after_seconds)
    except Exception:
        window_s = 10800  # 3 hours default
    if window_s > 0 and (now - last_bcast) >= window_s:
        return GateResult(
            broadcast=True,
            lifecycle="rebroadcast",
            reason=f"dedup window expired ({window_s}s) for cap_id={cap_id}",
            data_patch={"_ipaws_prefix": "Active"},
            commit=_make_commit(cap_id),
        )

    return GateResult(
        broadcast=False, lifecycle="suppress",
        reason=f"within {window_s}s dedup window for cap_id={cap_id}",
    )
