"""Watch Duty evacuation-alert (Group B) and report-message (Group C)
gating deciders.

Modeled on ``gating/fire.py::decide`` (same ``GateResult`` type, same
``get_db`` + defensive-row read pattern), but the state machine is its own:
Watch Duty evac level (order > warning > none) rises, falls, or holds with a
changed zone-text description, and each transition maps to its own message
``kind`` (see ``notifications/formatters/watchduty.py``).

State lives on the SAME ``fires`` row Watch Duty's match/enrichment writes to
(migration v31): ``watchduty_evac_state``, ``watchduty_evac_zone_text``,
``watchduty_evac_updated_at``, ``watchduty_evac_broadcast_at``. All four are
read FRESH from the row on every call (never cached) -- this decider owns
none of the fire's own broadcast-state columns (``last_broadcast_*``), only
the four ``watchduty_evac_*`` ones.

Canonical ``data`` dict consumed (built by
``env/watchduty.py::WatchDutyAdapter.to_event`` from a reading -- see
``_build_evac_readings``):
    irwin_id, wd_event_id, name, level, zone_text, wd_modified,
    lat, lon, county, state

Lifecycle / ``kind`` labels (also stamped into ``data_patch["kind"]`` for the
formatter):
    "order"       first reading is already an order, OR level rose to order
    "warning"     level rose from none to warning
    "downgraded"  order -> warning
    "lifted"      order or warning -> none
    "updated"     same level, zone text changed (past the text-update cooldown)
    "seed"        internal-only label for the silent first-reading write when
                  the first reading is NOT an order (no broadcast)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from meshai.adapter_config import adapter_config
from meshai.notifications.gating.base import GateResult
from meshai.persistence import get_db

logger = logging.getLogger(__name__)

_RANK = {"none": 0, "warning": 1, "order": 2}


def _parse_wd_modified_epoch(raw) -> Optional[float]:
    """Best-effort epoch (seconds) from Watch Duty's ``date_modified``.

    Accepts a numeric epoch (seconds, or milliseconds -- 13+ digit values are
    divided by 1000, mirroring ``env/fires.py::_parse_discovery_epoch``) or
    an ISO-8601 string (``Z`` suffix accepted). Returns None on anything
    unparseable -- never raises.
    """
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        val = float(raw)
        if val >= 1_000_000_000_000:
            val /= 1000.0
        return val
    if isinstance(raw, str):
        try:
            s = raw.strip()
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except (ValueError, TypeError):
            return None
    return None


def _make_evac_commit(irwin_id: str, level: str, zone_text: str,
                       updated_at_epoch: Optional[float]):
    """Deferred commit closure: UPSERT the four ``watchduty_evac_*`` columns
    on confirmed delivery. Mirrors ``gating/fire.py::_make_commit`` exactly
    (idempotent UPDATE, never raises out of the closure)."""

    def _commit(committed_at: float) -> None:
        try:
            conn = get_db()
        except Exception:
            logger.exception(
                "watchduty evac commit: persistence unavailable; state not "
                "updated for irwin=%s", irwin_id)
            return
        try:
            conn.execute(
                "UPDATE fires SET watchduty_evac_state=?, "
                "watchduty_evac_zone_text=?, watchduty_evac_broadcast_at=?, "
                "watchduty_evac_updated_at=? WHERE irwin_id=?",
                (level, zone_text, committed_at, updated_at_epoch, irwin_id),
            )
        except Exception:
            logger.exception(
                "watchduty evac commit: fires UPDATE failed irwin=%s", irwin_id)

    return _commit


def decide_evac(data: dict, *, source: str, now: float) -> GateResult:
    """Broadcast decision for a Watch Duty evacuation reading.

    Parameters
    ----------
    data:
        Canonical reading dict (see module docstring for schema).
    source:
        Adapter source name, e.g. "watchduty".
    now:
        Current epoch (from clock.now()) -- determinism seam.
    """
    irwin_id = data.get("irwin_id")
    level = data.get("level") or "none"
    zone_text = data.get("zone_text") or ""
    wd_modified = data.get("wd_modified")

    if not irwin_id:
        return GateResult(broadcast=False, lifecycle="suppress",
                          reason="evac reading without irwin_id")

    try:
        enabled = bool(adapter_config.watchduty.evac_alerts_enabled)
    except Exception:
        enabled = True
    if not enabled:
        return GateResult(broadcast=False, lifecycle="suppress",
                          reason="watchduty evac alerts disabled")

    try:
        conn = get_db()
    except Exception:
        logger.exception("watchduty evac decide: persistence unavailable")
        return GateResult(broadcast=False, lifecycle="suppress",
                          reason="persistence unavailable")

    try:
        row = conn.execute(
            "SELECT tombstoned_at, watchduty_evac_state, "
            "watchduty_evac_zone_text, watchduty_evac_broadcast_at "
            "FROM fires WHERE irwin_id = ?", (irwin_id,)).fetchone()
    except Exception:
        logger.exception(
            "watchduty evac decide: fires read failed irwin=%s", irwin_id)
        return GateResult(broadcast=False, lifecycle="suppress",
                          reason="fires row read failed")

    if row is None:
        return GateResult(broadcast=False, lifecycle="suppress",
                          reason=f"no fires row for irwin={irwin_id}")

    # Defensive row access -- never raise on an unexpected/missing column
    # (mirrors gating/fire.py's tombstoned_at read).
    try:
        tombstoned_at = row["tombstoned_at"]
    except (IndexError, KeyError, TypeError):
        tombstoned_at = None
    if tombstoned_at is not None:
        return GateResult(broadcast=False, lifecycle="suppress",
                          reason=f"incident tombstoned irwin={irwin_id}")

    try:
        prev = row["watchduty_evac_state"]
    except (IndexError, KeyError, TypeError):
        prev = None
    try:
        prev_zone_text = row["watchduty_evac_zone_text"] or ""
    except (IndexError, KeyError, TypeError):
        prev_zone_text = ""
    try:
        prev_broadcast_at = row["watchduty_evac_broadcast_at"]
    except (IndexError, KeyError, TypeError):
        prev_broadcast_at = None

    updated_at_epoch = _parse_wd_modified_epoch(wd_modified)

    def _broadcast(kind: str, reason: str) -> GateResult:
        return GateResult(
            broadcast=True, lifecycle=kind, reason=reason,
            data_patch={"kind": kind, "_severity_override": "priority"},
            commit=_make_evac_commit(irwin_id, level, zone_text, updated_at_epoch),
        )

    # ── First reading since match (prev is NULL) ─────────────────────────
    if prev is None:
        if level == "order":
            return _broadcast("order", f"first evac reading is order irwin={irwin_id}")
        # Silent seed: write state + zone_text immediately (NOT deferred --
        # nothing is being broadcast, so there is no delivery to confirm
        # against), then suppress.
        try:
            conn.execute(
                "UPDATE fires SET watchduty_evac_state=?, "
                "watchduty_evac_zone_text=? WHERE irwin_id=?",
                (level, zone_text, irwin_id),
            )
        except Exception:
            logger.exception(
                "watchduty evac decide: silent seed write failed irwin=%s",
                irwin_id)
        return GateResult(broadcast=False, lifecycle="seed",
                          reason=f"silent seed level={level} irwin={irwin_id}")

    prev_rank = _RANK.get(prev, 0)
    cur_rank = _RANK.get(level, 0)

    # ── Escalation ────────────────────────────────────────────────────────
    if cur_rank > prev_rank:
        return _broadcast(level, f"evac escalation {prev}->{level} irwin={irwin_id}")

    # ── De-escalation ────────────────────────────────────────────────────
    if cur_rank < prev_rank:
        kind = "downgraded" if level == "warning" else "lifted"
        return _broadcast(kind, f"evac de-escalation {prev}->{level} irwin={irwin_id}")

    # ── Same rank ────────────────────────────────────────────────────────
    if level == "none":
        return GateResult(broadcast=False, lifecycle="suppress",
                          reason="no active evac, unchanged")

    if zone_text == prev_zone_text:
        return GateResult(broadcast=False, lifecycle="suppress",
                          reason="same level, unchanged text")

    try:
        cooldown_s = int(adapter_config.watchduty.evac_text_update_cooldown_seconds)
    except Exception:
        cooldown_s = 3600
    if prev_broadcast_at is None or (now - float(prev_broadcast_at)) >= cooldown_s:
        return _broadcast("updated", f"evac text update irwin={irwin_id}")

    # Inside the cooldown: suppress WITHOUT touching the stored text, so the
    # latest text broadcasts once the cooldown passes.
    return GateResult(broadcast=False, lifecycle="suppress",
                      reason="evac text changed inside cooldown")


# ── Report-message gating decider (Group C) ─────────────────────────────────
#
# Modeled on decide_evac above, but the dedup ledger is its own table
# (``watchduty_reports_sent``, migration v31) rather than columns on the
# fires row: a report either has already been sent (row present, any
# ``seeded`` value) or it hasn't. Unlike decide_evac's UPSERT-on-fires
# commit, this decider's deferred commit is an INSERT OR IGNORE into
# watchduty_reports_sent -- safe to call more than once for the same
# report_id (multi-channel delivery), and a no-op if the row somehow
# already exists (e.g. a lazy seed raced ahead of an in-flight commit).
#
# Canonical ``data`` dict consumed (built by
# ``env/watchduty.py::WatchDutyAdapter.to_event`` from a reading -- see
# ``_poll_one_fire_reports``):
#     irwin_id, wd_event_id, name, report_id, text, date_created,
#     lat, lon, county, state


def _make_report_commit(report_id: str, irwin_id: str, wd_event_id):
    """Deferred commit closure: INSERT OR IGNORE the report as sent
    (seeded=0) on confirmed delivery. Never raises out of the closure."""

    def _commit(committed_at: float) -> None:
        try:
            conn = get_db()
        except Exception:
            logger.exception(
                "watchduty report commit: persistence unavailable; report "
                "not recorded report_id=%s", report_id)
            return
        try:
            conn.execute(
                "INSERT OR IGNORE INTO watchduty_reports_sent"
                "(report_id, irwin_id, geo_event_id, sent_at, seeded, created_at) "
                "VALUES (?,?,?,?,0,?)",
                (report_id, irwin_id, wd_event_id, committed_at, committed_at),
            )
        except Exception:
            logger.exception(
                "watchduty report commit: INSERT failed report_id=%s", report_id)

    return _commit


def decide_report(data: dict, *, source: str, now: float) -> GateResult:
    """Broadcast decision for a Watch Duty report-message reading.

    Parameters
    ----------
    data:
        Canonical reading dict (see module comment above for schema).
    source:
        Adapter source name, e.g. "watchduty".
    now:
        Current epoch (from clock.now()) -- determinism seam (unused here;
        the decision is purely dedup-by-report_id, not time-based).
    """
    irwin_id = data.get("irwin_id")
    report_id = data.get("report_id")
    wd_event_id = data.get("wd_event_id")

    if not irwin_id or not report_id:
        return GateResult(broadcast=False, lifecycle="suppress",
                          reason="report reading without irwin_id/report_id")

    try:
        enabled = bool(adapter_config.watchduty.report_alerts_enabled)
    except Exception:
        enabled = True
    if not enabled:
        return GateResult(broadcast=False, lifecycle="suppress",
                          reason="watchduty report alerts disabled")

    try:
        conn = get_db()
    except Exception:
        logger.exception("watchduty report decide: persistence unavailable")
        return GateResult(broadcast=False, lifecycle="suppress",
                          reason="persistence unavailable")

    try:
        fire_row = conn.execute(
            "SELECT tombstoned_at FROM fires WHERE irwin_id = ?",
            (irwin_id,)).fetchone()
    except Exception:
        logger.exception(
            "watchduty report decide: fires read failed irwin=%s", irwin_id)
        return GateResult(broadcast=False, lifecycle="suppress",
                          reason="fires row read failed")

    if fire_row is None:
        return GateResult(broadcast=False, lifecycle="suppress",
                          reason=f"no fires row for irwin={irwin_id}")

    try:
        tombstoned_at = fire_row["tombstoned_at"]
    except (IndexError, KeyError, TypeError):
        tombstoned_at = None
    if tombstoned_at is not None:
        return GateResult(broadcast=False, lifecycle="suppress",
                          reason=f"incident tombstoned irwin={irwin_id}")

    try:
        sent_row = conn.execute(
            "SELECT 1 FROM watchduty_reports_sent WHERE report_id = ?",
            (report_id,)).fetchone()
    except Exception:
        logger.exception(
            "watchduty report decide: watchduty_reports_sent read failed "
            "report_id=%s", report_id)
        return GateResult(broadcast=False, lifecycle="suppress",
                          reason="watchduty_reports_sent read failed")

    if sent_row is not None:
        return GateResult(broadcast=False, lifecycle="suppress",
                          reason=f"report already sent report_id={report_id}")

    return GateResult(
        broadcast=True, lifecycle="report",
        reason=f"new report report_id={report_id} irwin={irwin_id}",
        data_patch={"kind": "report"},
        commit=_make_report_commit(report_id, irwin_id, wd_event_id),
    )
