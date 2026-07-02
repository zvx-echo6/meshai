"""Central avalanche advisory handler (avalanche_org adapter).

Subscribes to CENTRAL_AVY stream via consumer.py routing.
Adapter: avalanche_org
Subjects: central.avy.advisory.> (active + tombstones in one consumer)

Wire format: multi-line, _meshai_precomposed=True (bypasses composer
whitespace-collapse). Same pattern as nws_handler / quake_handler.

Severity gate: uses danger_level (0-5) from data.data directly.

TODO (verify before Central swap, October+):
    Confirm data.data.danger_level uses the NAADS 5-point scale:
      1=Low, 2=Moderate, 3=Considerable, 4=High, 5=Extreme
    The native path uses this scale and min_danger_level=3 means
    "Considerable and above" — correct for southern Idaho touring.
    Central's centralseverity uses a COMPRESSED scale (2=Considerable,
    3=High, 4=Extreme). If Central's danger_level follows centralseverity
    rather than NAADS, min_danger_level=3 silently becomes "High and above"
    at flip time, dropping every Considerable advisory.
    CHECK: read data.data.danger_level from a live CENTRAL_AVY envelope
    for a zone known to be rated Considerable. If the value is 2 (not 3),
    either remap min_danger_level=2 at flip time OR normalize inside
    handle_avy() before the gate comparison.
    Do not flip feed_source="central" without confirming this first.
Do NOT use centralseverity as a gate — Central's scale is higher=more
severe (4=Extreme, 3=High, 2=Considerable), which is the inverse of
meshai's broadcast priority convention. Gate on danger_level only.

Off-season note: CENTRAL_AVY is empty June–September. Handler will
receive no envelopes during off-season — this is correct and expected.
The consumer sits idle; no action needed.

Tombstones (central.avy.advisory.removed.*): handler returns None
(no broadcast). The env_store's native-path change-detection handles
zone retraction on the native path; on the central path, tombstones
are consumed and acked silently so they don't pile up in the stream.
Future: retraction broadcast ("AVY advisory lifted") could be added here.
"""

import logging
import re
import time
from typing import Any, Optional

from meshai.adapter_config import adapter_config
from meshai.central.budget import budget_for, fit_to_budget
from meshai.persistence import get_db

logger = logging.getLogger(__name__)


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
    """Handle a single CENTRAL_AVY envelope.

    Returns the wire string when a broadcast should fire, None otherwise.
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
    severity_word = _coerce_severity(inner.get("severity"))

    # Danger level gate — read from data.data, NOT centralseverity.
    danger_level = d.get("danger_level")
    if not isinstance(danger_level, (int, float)):
        return None

    min_level = int(adapter_config.avalanche.min_danger_level)
    if danger_level < min_level:
        return None

    # Field extraction.
    zone_name   = d.get("zone_name") or "Unknown Zone"
    danger_name = d.get("danger_name") or str(danger_level)
    center_id   = d.get("center_id") or ""
    travel      = (d.get("travel_advice") or "").strip()
    lat         = d.get("latitude")
    lon         = d.get("longitude")

    # Category → broadcast category for event_log.
    category_raw = category

    # Persist to event_log (store-only, no change-detection needed —
    # Central deduplicates upstream; we log every envelope we receive).
    conn = get_db()
    if conn is None:
        logger.warning("avy_handler: persistence unavailable, skipping")
        return None

    log_id = _log_event_returning_id(
        conn, now=_now(), source="avalanche_org",
        category=category_raw, severity_word=severity_word,
        event_id_external=f"{center_id}:{zone_name}",
        subject=subject, handled=0,
        table_name="event_log", table_pk=None,
    )

    # Render multi-line wire string.
    wire = _render(
        danger_level=int(danger_level),
        danger_name=danger_name,
        zone_name=zone_name,
        center_id=center_id,
        travel=travel,
    )

    _attach_commit(data, log_id=log_id)
    return wire


def _render(*, danger_level: int, danger_name: str, zone_name: str,
            center_id: str, travel: str) -> str:
    emoji = "\u26f7"
    # Warning for High/Extreme (4-5), Watch for Considerable (3).
    prefix = "WARNING:" if danger_level >= 4 else "Watch:"

    line1 = f"{emoji} AVY {prefix} {zone_name} \u2014 {danger_name} ({danger_level})"
    # Travel advice: FIRST SENTENCE only (up to and including the first
    # sentence terminator), instead of a fixed character slice.
    line2 = None
    if travel and travel.strip():
        t = travel.strip()
        m = re.search(r"[.!?]", t)
        line2 = t[: m.end()] if m else t
    line3 = f"{center_id} \u00b7 valid today" if center_id else "valid today"

    msg = "\n".join(l for l in [line1, line2, line3] if l)
    return fit_to_budget(msg, budget_for("avalanche"))


def _attach_commit(data: Optional[dict], *, log_id: Optional[int]) -> None:
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
