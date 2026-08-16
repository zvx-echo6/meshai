"""Custom scheduled-announcement REST API (v30 custom_announcements table).

New resource type (owner-authored free-text broadcasts on a clock-slot
schedule -- see notifications/scheduled/custom_announcements.py), so this is
a NEW router file rather than an extension of notification_routes.py:
notification_routes.py's CRUD-shaped endpoints (region-routing, rules) are
all config-file-backed (save_section/load_config over YAML), while
announcements are individual SQLite rows with their own id, closer in shape
to adapter_config_routes.py's raw get_db()/HTTPException style -- which this
file follows (grouped list, per-row GET/PUT/DELETE, explicit 400s with a
clear message per bad field, no ORM).

Endpoints:
    GET    /api/announcements                 -- list all, newest first
    POST   /api/announcements                  -- create (ALWAYS starts disabled)
    PUT    /api/announcements/{id}             -- partial update (validated as a whole)
    DELETE /api/announcements/{id}             -- delete
    POST   /api/announcements/{id}/preview     -- exact wire text + char/byte
                                                   count against budget.
                                                   NEVER sends.

There is deliberately NO "send now" / test-send route anywhere in this file.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from meshai.notifications.formatters._budget import budget_for, fit_to_budget

logger = logging.getLogger(__name__)

router = APIRouter(tags=["announcements"])

_VALID_KINDS = {"daily", "interval_days", "weekly", "monthly"}
_VALID_TRANSPORTS = {"meshtastic", "meshcore"}


# ============================================================================
# request bodies
# ============================================================================


class AnnouncementCreateBody(BaseModel):
    name: str
    message: str
    schedule_kind: str
    time_of_day: str
    interval_days: Optional[int] = None
    dow_mask: Optional[List[Any]] = None
    day_of_month: Optional[int] = None
    timezone: str = "America/Boise"
    channels: List[Dict[str, Any]]


class AnnouncementUpdateBody(BaseModel):
    """Partial update -- only fields present in the request change.

    The MERGED result (existing row + these overrides) is re-validated as a
    whole, so e.g. switching schedule_kind to "weekly" without also
    supplying dow_mask is rejected even though dow_mask itself wasn't
    touched by this request.
    """
    name: Optional[str] = None
    message: Optional[str] = None
    schedule_kind: Optional[str] = None
    time_of_day: Optional[str] = None
    interval_days: Optional[int] = None
    dow_mask: Optional[List[Any]] = None
    day_of_month: Optional[int] = None
    timezone: Optional[str] = None
    channels: Optional[List[Dict[str, Any]]] = None
    enabled: Optional[bool] = None


# ============================================================================
# helpers
# ============================================================================


def _get_conn():
    from meshai.persistence import get_db
    return get_db()


def _row_to_dict(r) -> dict:
    channels_raw = r["channels"]
    try:
        channels = json.loads(channels_raw) if channels_raw else []
    except Exception:
        channels = []
    dow_mask_raw = r["dow_mask"]
    try:
        dow_mask = json.loads(dow_mask_raw) if dow_mask_raw else None
    except Exception:
        dow_mask = None
    return {
        "announcement_id": r["announcement_id"],
        "name": r["name"],
        "message": r["message"],
        "schedule_kind": r["schedule_kind"],
        "time_of_day": r["time_of_day"],
        "interval_days": r["interval_days"],
        "dow_mask": dow_mask,
        "day_of_month": r["day_of_month"],
        "timezone": r["timezone"],
        "channels": channels,
        "enabled": bool(r["enabled"]),
        "last_sent_at": r["last_sent_at"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


def _validate_time_of_day(value: Any) -> str:
    if not isinstance(value, str) or ":" not in value:
        raise HTTPException(400, f"time_of_day must be 'HH:MM', got {value!r}")
    parts = value.split(":")
    if len(parts) != 2:
        raise HTTPException(400, f"time_of_day must be 'HH:MM', got {value!r}")
    try:
        hh, mm = int(parts[0]), int(parts[1])
    except ValueError:
        raise HTTPException(400, f"time_of_day must be 'HH:MM' with numeric parts, got {value!r}")
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise HTTPException(400, f"time_of_day out of range, got {value!r}")
    return f"{hh:02d}:{mm:02d}"


def _validate_timezone(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(400, "timezone must be a non-empty string")
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(value)
    except Exception:
        raise HTTPException(400, f"unknown timezone {value!r}")
    return value


def _validate_channels(value: Any) -> List[Dict[str, Any]]:
    """SHAPE validation only. Rejects an empty list (nowhere to send) but
    places NO upper bound on length -- any number of mixed meshtastic/
    meshcore targets is valid. Does NOT check whether a channel is
    currently configured on a live radio (that is a send-time concern --
    dispatch_scheduled_custom_broadcast logs a warning and skips a target
    that no longer exists rather than failing the whole announcement)."""
    if not isinstance(value, list) or len(value) == 0:
        raise HTTPException(400, "channels must be a non-empty list")
    out = []
    for i, item in enumerate(value):
        if not isinstance(item, dict):
            raise HTTPException(400, f"channels[{i}] must be an object")
        transport = item.get("transport")
        if transport not in _VALID_TRANSPORTS:
            raise HTTPException(
                400,
                f"channels[{i}].transport must be one of {sorted(_VALID_TRANSPORTS)}, "
                f"got {transport!r}",
            )
        channel = item.get("channel")
        if transport == "meshtastic":
            if isinstance(channel, bool) or not isinstance(channel, int):
                raise HTTPException(
                    400, f"channels[{i}].channel must be an int meshtastic "
                         f"channel index, got {channel!r}")
        else:  # meshcore
            if not isinstance(channel, str) or not channel.strip():
                raise HTTPException(
                    400, f"channels[{i}].channel must be a non-empty "
                         f"meshcore channel name, got {channel!r}")
        entry = {"transport": transport, "channel": channel}
        name = item.get("name")
        if name is not None:
            entry["name"] = name
        out.append(entry)
    return out


def _validate_dow_mask(value: Any) -> List[bool]:
    if not isinstance(value, list) or len(value) != 7 or not all(
        isinstance(b, bool) for b in value
    ):
        raise HTTPException(
            400, f"dow_mask must be a list of exactly 7 booleans, got {value!r}")
    return value


def _validate_full(merged: dict) -> dict:
    """Validate a full (post-merge) announcement record. Returns a cleaned
    dict ready to persist. Raises HTTPException(400) on the first bad field."""
    name = merged.get("name")
    if not isinstance(name, str) or not name.strip():
        raise HTTPException(400, "name must be a non-empty string")

    message = merged.get("message")
    if not isinstance(message, str) or not message.strip():
        raise HTTPException(400, "message must be a non-empty string")

    kind = merged.get("schedule_kind")
    if kind not in _VALID_KINDS:
        raise HTTPException(
            400, f"schedule_kind must be one of {sorted(_VALID_KINDS)}, got {kind!r}")

    time_of_day = _validate_time_of_day(merged.get("time_of_day"))
    tz_name = _validate_timezone(merged.get("timezone") or "America/Boise")
    channels = _validate_channels(merged.get("channels"))

    interval_days = merged.get("interval_days")
    dow_mask = merged.get("dow_mask")
    day_of_month = merged.get("day_of_month")

    if kind == "interval_days":
        if not isinstance(interval_days, int) or isinstance(interval_days, bool) or interval_days < 1:
            raise HTTPException(
                400, f"interval_days must be an int >= 1 for schedule_kind="
                     f"'interval_days', got {interval_days!r}")
        dow_mask = None
        day_of_month = None
    elif kind == "weekly":
        dow_mask = _validate_dow_mask(dow_mask)
        interval_days = None
        day_of_month = None
    elif kind == "monthly":
        if (not isinstance(day_of_month, int) or isinstance(day_of_month, bool)
                or not (1 <= day_of_month <= 31)):
            raise HTTPException(
                400, f"day_of_month must be an int 1-31 for schedule_kind="
                     f"'monthly', got {day_of_month!r}")
        interval_days = None
        dow_mask = None
    else:  # daily
        interval_days = None
        dow_mask = None
        day_of_month = None

    return {
        "name": name.strip(),
        "message": message,
        "schedule_kind": kind,
        "time_of_day": time_of_day,
        "interval_days": interval_days,
        "dow_mask": json.dumps(dow_mask) if dow_mask is not None else None,
        "day_of_month": day_of_month,
        "timezone": tz_name,
        "channels": json.dumps(channels),
    }


# ============================================================================
# endpoints
# ============================================================================


@router.get("/announcements")
async def list_announcements(request: Request) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM custom_announcements ORDER BY announcement_id DESC"
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


@router.post("/announcements")
async def create_announcement(request: Request, body: AnnouncementCreateBody) -> dict:
    """Create a new announcement. ALWAYS starts disabled (enabled=0) --
    the owner arms it explicitly via PUT after reviewing a preview. This
    endpoint never sends anything."""
    clean = _validate_full(body.model_dump())
    now = time.time()
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO custom_announcements "
        "(name, message, schedule_kind, time_of_day, interval_days, "
        "dow_mask, day_of_month, timezone, channels, enabled, "
        "last_sent_at, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,0,NULL,?,?)",
        (clean["name"], clean["message"], clean["schedule_kind"],
         clean["time_of_day"], clean["interval_days"], clean["dow_mask"],
         clean["day_of_month"], clean["timezone"], clean["channels"],
         now, now),
    )
    new_id = cur.lastrowid
    logger.info("announcement created id=%s name=%r (disabled)", new_id, clean["name"])
    r = conn.execute(
        "SELECT * FROM custom_announcements WHERE announcement_id=?", (new_id,)
    ).fetchone()
    return _row_to_dict(r)


@router.put("/announcements/{announcement_id}")
async def update_announcement(
    announcement_id: int, request: Request, body: AnnouncementUpdateBody
) -> dict:
    conn = _get_conn()
    existing = conn.execute(
        "SELECT * FROM custom_announcements WHERE announcement_id=?",
        (announcement_id,),
    ).fetchone()
    if existing is None:
        raise HTTPException(404, f"announcement {announcement_id} not found")

    current = _row_to_dict(existing)
    overrides = {k: v for k, v in body.model_dump().items() if v is not None}
    enabled_override = overrides.pop("enabled", None)
    merged = {**current, **overrides}
    clean = _validate_full(merged)

    new_enabled = current["enabled"] if enabled_override is None else bool(enabled_override)

    now = time.time()
    conn.execute(
        "UPDATE custom_announcements SET name=?, message=?, schedule_kind=?, "
        "time_of_day=?, interval_days=?, dow_mask=?, day_of_month=?, "
        "timezone=?, channels=?, enabled=?, updated_at=? "
        "WHERE announcement_id=?",
        (clean["name"], clean["message"], clean["schedule_kind"],
         clean["time_of_day"], clean["interval_days"], clean["dow_mask"],
         clean["day_of_month"], clean["timezone"], clean["channels"],
         1 if new_enabled else 0, now, announcement_id),
    )
    logger.info("announcement updated id=%s enabled=%s", announcement_id, new_enabled)
    r = conn.execute(
        "SELECT * FROM custom_announcements WHERE announcement_id=?",
        (announcement_id,),
    ).fetchone()
    return _row_to_dict(r)


@router.delete("/announcements/{announcement_id}")
async def delete_announcement(announcement_id: int, request: Request) -> dict:
    conn = _get_conn()
    existing = conn.execute(
        "SELECT announcement_id FROM custom_announcements WHERE announcement_id=?",
        (announcement_id,),
    ).fetchone()
    if existing is None:
        raise HTTPException(404, f"announcement {announcement_id} not found")
    conn.execute(
        "DELETE FROM custom_announcements WHERE announcement_id=?",
        (announcement_id,),
    )
    logger.info("announcement deleted id=%s", announcement_id)
    return {"ok": True, "deleted": announcement_id}


@router.post("/announcements/{announcement_id}/preview")
async def preview_announcement(announcement_id: int, request: Request) -> dict:
    """Return the exact wire text and its size against the mesh packet
    budget. Does NOT send -- there is no send-now endpoint in this API."""
    conn = _get_conn()
    r = conn.execute(
        "SELECT message FROM custom_announcements WHERE announcement_id=?",
        (announcement_id,),
    ).fetchone()
    if r is None:
        raise HTTPException(404, f"announcement {announcement_id} not found")

    budget = budget_for("custom_announcements")
    wire = fit_to_budget(r["message"] or "", budget)
    return {
        "wire_text": wire,
        "char_count": len(wire),
        "byte_count": len(wire.encode("utf-8")),
        "budget": budget,
        "truncated": wire != (r["message"] or ""),
    }
