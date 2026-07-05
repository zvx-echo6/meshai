"""Incident/Roads gating decider — Phase-2 reference implementation.

Mirrors incident_handler change-detection EXACTLY:
  * NEW external_id → INSERT into traffic_events, broadcast lifecycle="new"
  * Existing row, last_broadcast_at IS NULL → broadcast lifecycle="new"
    (cold-start: dispatcher dropped the prior broadcast)
  * adapter_config.incident.broadcast_on_update is False → suppress
  * magnitude stepped up OR delay doubled OR icon changed → Update
  * otherwise → suppress

decide(data, *, source, now) -> GateResult:

    Canonical data schema consumed (same keys as the central bridge
    produces; subset used by gating):
        external_id   str | None   — None for native adapters (always broadcast)
        source        str          — "tomtom_incidents" | "state_511_atis" | "itd_511" | …
        sub_type      str | None
        road          str | None
        direction     str | None
        mile_start    int | None
        mile_end      int | None
        county        str | None
        state         str | None
        lat           float | None
        lon           float | None
        impact        str | None
        start_at      int | None
        end_at        int | None
        magnitude     int | None   — magnitude_of_delay
        delay_seconds int | None
        icon_category str | None

    Emitted data_patch keys:
        is_update     bool  — True on Update: broadcasts, False on New:
        _dedup_suffix str   — empty (external_id is the dedup key)

    commit(now: float) -> None (deferred idempotent):
        UPSERT last_broadcast_at / first_broadcast_at /
        last_broadcast_magnitude / last_broadcast_delay_seconds /
        last_broadcast_icon_category on the traffic_events row.
        Safe to call N times (all writes are conditional UPSERTs).

Native adapters (external_id is None or empty):
    No traffic_events lookup; always broadcast with lifecycle="native".
    The event bus inhibitor + group_key handle dedup for native events.
"""
from __future__ import annotations

import logging
from typing import Optional

from meshai.adapter_config import adapter_config
from meshai.notifications.gating.base import GateResult
from meshai.persistence import get_db

logger = logging.getLogger(__name__)


def decide(data: dict, *, source: str, now: float) -> GateResult:
    """Gate + dedup decision for incident/roads events.

    Parameters
    ----------
    data   : Canonical event.data (see module docstring for schema).
    source : Adapter source name, e.g. "tomtom_incidents".
    now    : Current epoch from clock.now() — determinism seam.

    Returns
    -------
    GateResult with:
        broadcast=True  lifecycle="new" | "update"  data_patch + commit set
        broadcast=False lifecycle="suppress"          data_patch={} commit=None
    """
    external_id = data.get("external_id") or None
    source_val = data.get("source") or source

    # ── Native adapters: no external_id, no traffic_events dedup ─────────
    if not external_id:
        return GateResult(
            broadcast=True,
            lifecycle="native",
            reason=f"native adapter {source_val!r} — no external_id, no dedup",
            data_patch={"is_update": False, "_dedup_suffix": ""},
        )

    # ── Persistence lookup ────────────────────────────────────────────────
    try:
        conn = get_db()
    except Exception:
        logger.exception("incident decide: persistence unavailable")
        return GateResult(
            broadcast=False,
            lifecycle="suppress",
            reason="persistence unavailable",
        )

    now_int = int(now)
    magnitude = data.get("magnitude")
    delay_seconds = data.get("delay_seconds")
    icon_category = data.get("icon_category")

    row = conn.execute(
        "SELECT first_seen_at, last_broadcast_at, "
        "last_broadcast_magnitude, last_broadcast_delay_seconds, "
        "last_broadcast_icon_category "
        "FROM traffic_events WHERE source=? AND external_id=?",
        (source_val, external_id),
    ).fetchone()

    # ── NEW external_id ───────────────────────────────────────────────────
    if row is None:
        # INSERT with last_broadcast_at = NULL (armed by commit on delivery).
        try:
            conn.execute(
                "INSERT INTO traffic_events("
                "source, external_id, road, direction, "
                "mile_start, mile_end, county, state, lat, lon, "
                "sub_type, impact, start_at, end_at, "
                "first_seen_at, last_seen_at, last_broadcast_at, "
                "magnitude_of_delay, delay_seconds, icon_category, "
                "last_broadcast_magnitude, last_broadcast_delay_seconds, "
                "last_broadcast_icon_category"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    source_val, external_id,
                    data.get("road"), data.get("direction"),
                    data.get("mile_start"), data.get("mile_end"),
                    data.get("county"), data.get("state"),
                    data.get("lat"), data.get("lon"),
                    data.get("sub_type"), data.get("impact"),
                    data.get("start_at"), data.get("end_at"),
                    now_int, now_int, None,
                    magnitude, delay_seconds, icon_category,
                    None, None, None,
                ),
            )
        except Exception:
            logger.exception("incident decide: INSERT failed for %s|%s",
                             source_val, external_id)

        patch = {"is_update": False, "_dedup_suffix": ""}

        def _commit_new(committed_at: float) -> None:
            _do_commit(committed_at, source_val, external_id,
                       magnitude, delay_seconds, icon_category)

        return GateResult(
            broadcast=True,
            lifecycle="new",
            reason=f"new {source_val}|{external_id}",
            data_patch=patch,
            commit=_commit_new,
        )

    # ── EXISTING row: always refresh last_seen_at + current fields ────────
    try:
        conn.execute(
            "UPDATE traffic_events "
            "SET sub_type=?, impact=?, magnitude_of_delay=?, "
            "delay_seconds=?, icon_category=?, last_seen_at=?, "
            "lat=COALESCE(?, lat), lon=COALESCE(?, lon), "
            "direction=COALESCE(?, direction), road=COALESCE(?, road) "
            "WHERE source=? AND external_id=?",
            (
                data.get("sub_type"), data.get("impact"),
                magnitude, delay_seconds, icon_category,
                now_int,
                data.get("lat"), data.get("lon"),
                data.get("direction"), data.get("road"),
                source_val, external_id,
            ),
        )
    except Exception:
        logger.exception("incident decide: UPDATE last_seen_at failed")

    last_bcast_at = row["last_broadcast_at"]

    # ── Cold-start: row exists but was never delivered ────────────────────
    if last_bcast_at is None:
        patch = {"is_update": False, "_dedup_suffix": ""}

        def _commit_cold(committed_at: float) -> None:
            _do_commit(committed_at, source_val, external_id,
                       magnitude, delay_seconds, icon_category)

        return GateResult(
            broadcast=True,
            lifecycle="new",
            reason=f"cold-start {source_val}|{external_id}",
            data_patch=patch,
            commit=_commit_cold,
        )

    # ── Post-first-broadcast: check broadcast_on_update ───────────────────
    if not bool(adapter_config.incident.broadcast_on_update):
        return GateResult(
            broadcast=False,
            lifecycle="suppress",
            reason="broadcast_on_update=False",
        )

    last_bcast_mag = row["last_broadcast_magnitude"]
    last_bcast_delay = row["last_broadcast_delay_seconds"]
    last_bcast_icon = row["last_broadcast_icon_category"]

    mag_stepped_up = (
        magnitude is not None
        and (last_bcast_mag is None or magnitude > last_bcast_mag)
    )
    delay_doubled = (
        delay_seconds is not None
        and last_bcast_delay is not None
        and last_bcast_delay > 0
        and delay_seconds >= 2 * last_bcast_delay
    )
    icon_changed = (
        icon_category is not None
        and last_bcast_icon is not None
        and icon_category != last_bcast_icon
    )

    if not (mag_stepped_up or delay_doubled or icon_changed):
        return GateResult(
            broadcast=False,
            lifecycle="suppress",
            reason=(
                f"no update condition met "
                f"(mag={magnitude} last={last_bcast_mag}, "
                f"delay={delay_seconds} last={last_bcast_delay}, "
                f"icon={icon_category!r} last={last_bcast_icon!r})"
            ),
        )

    patch = {"is_update": True, "_dedup_suffix": ""}

    def _commit_update(committed_at: float) -> None:
        _do_commit(committed_at, source_val, external_id,
                   magnitude, delay_seconds, icon_category)

    reason_parts = []
    if mag_stepped_up:
        reason_parts.append(f"mag {last_bcast_mag}→{magnitude}")
    if delay_doubled:
        reason_parts.append(f"delay {last_bcast_delay}→{delay_seconds}s")
    if icon_changed:
        reason_parts.append(f"icon {last_bcast_icon!r}→{icon_category!r}")

    return GateResult(
        broadcast=True,
        lifecycle="update",
        reason=f"update {source_val}|{external_id}: {', '.join(reason_parts)}",
        data_patch=patch,
        commit=_commit_update,
    )


def _do_commit(
    committed_at: float,
    source_val: str,
    external_id: str,
    magnitude: Optional[int],
    delay_seconds: Optional[int],
    icon_category: Optional[str],
) -> None:
    """Idempotent UPSERT: arm last_broadcast_at on confirmed delivery."""
    try:
        conn = get_db()
        conn.execute(
            "UPDATE traffic_events "
            "SET last_broadcast_at=?, "
            "first_broadcast_at=COALESCE(first_broadcast_at, ?), "
            "last_broadcast_magnitude=?, "
            "last_broadcast_delay_seconds=?, "
            "last_broadcast_icon_category=? "
            "WHERE source=? AND external_id=?",
            (
                int(committed_at), int(committed_at),
                magnitude, delay_seconds, icon_category,
                source_val, external_id,
            ),
        )
    except Exception:
        logger.exception(
            "incident commit: persistence update failed for %s|%s",
            source_val, external_id,
        )
