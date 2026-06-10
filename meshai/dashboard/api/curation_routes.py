"""v0.6-4 REST API for gauge_sites + town_anchors curation tables.

Endpoints (mirrors the adapter_config CRUD shape):

  gauge_sites:
    GET    /api/gauge-sites                 list (enabled and disabled)
    GET    /api/gauge-sites/{site_id}       single row
    POST   /api/gauge-sites                 add new row
    PUT    /api/gauge-sites/{site_id}       partial update
    DELETE /api/gauge-sites/{site_id}       remove row

  town_anchors:
    GET    /api/town-anchors                list
    GET    /api/town-anchors/{anchor_id}    single
    POST   /api/town-anchors                add new
    PUT    /api/town-anchors/{anchor_id}    partial update
    DELETE /api/town-anchors/{anchor_id}    remove

Every mutating call invalidates the in-process curation cache so
handler-side reads (lookup_gauge_site, lookup_town_anchor) see the new
state on the next call -- no container restart.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request

from meshai.persistence.curation import invalidate_curation_cache

logger = logging.getLogger(__name__)


router = APIRouter(tags=["curation"])


def _get_conn():
    from meshai.persistence import get_db
    return get_db()


def _gauge_row_to_dict(r) -> dict:
    return {
        "site_id": r["site_id"],
        "gauge_name": r["gauge_name"],
        "lat": r["lat"],
        "lon": r["lon"],
        "action_ft": r["action_ft"],
        "flood_minor_ft": r["flood_minor_ft"],
        "flood_moderate_ft": r["flood_moderate_ft"],
        "flood_major_ft": r["flood_major_ft"],
        "enabled": bool(r["enabled"]),
        "updated_at": r["updated_at"],
    }


def _town_row_to_dict(r) -> dict:
    return {
        "anchor_id": r["anchor_id"],
        "name": r["name"],
        "lat": r["lat"],
        "lon": r["lon"],
        "state": r["state"],
        "enabled": bool(r["enabled"]),
        "updated_at": r["updated_at"],
    }


# ============================================================================
# gauge_sites
# ============================================================================


@router.get("/gauge-sites")
async def list_gauges(request: Request) -> list[dict]:
    conn = _get_conn()
    return [_gauge_row_to_dict(r) for r in conn.execute(
        "SELECT * FROM gauge_sites ORDER BY site_id"
    ).fetchall()]


@router.get("/gauge-sites/{site_id}")
async def get_gauge(site_id: str, request: Request) -> dict:
    conn = _get_conn()
    r = conn.execute(
        "SELECT * FROM gauge_sites WHERE site_id=?", (site_id,)
    ).fetchone()
    if r is None:
        raise HTTPException(404, f"gauge_sites: {site_id} not found")
    return _gauge_row_to_dict(r)


@router.post("/gauge-sites")
async def add_gauge(request: Request) -> dict:
    body = await request.json()
    if not isinstance(body, dict): raise HTTPException(400, "body must be a JSON object")
    required = ("site_id", "gauge_name", "lat", "lon")
    for f in required:
        if f not in body:
            raise HTTPException(400, f"missing required field: {f}")
    if not isinstance(body["site_id"], str) or not body["site_id"].strip():
        raise HTTPException(400, "site_id must be a non-empty string")
    try:
        lat = float(body["lat"]); lon = float(body["lon"])
    except (TypeError, ValueError):
        raise HTTPException(400, "lat/lon must be numeric")

    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO gauge_sites(site_id, gauge_name, lat, lon, action_ft, "
            "flood_minor_ft, flood_moderate_ft, flood_major_ft, enabled, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (body["site_id"].strip(),
             str(body["gauge_name"]).strip(),
             lat, lon,
             body.get("action_ft"),
             body.get("flood_minor_ft"),
             body.get("flood_moderate_ft"),
             body.get("flood_major_ft"),
             1 if body.get("enabled", True) else 0,
             time.time()),
        )
    except Exception as e:
        raise HTTPException(400, f"insert failed: {e}")
    invalidate_curation_cache()
    return await get_gauge(body["site_id"].strip(), request)


@router.put("/gauge-sites/{site_id}")
async def update_gauge(site_id: str, request: Request) -> dict:
    body = await request.json()
    if not isinstance(body, dict): raise HTTPException(400, "body must be a JSON object")

    conn = _get_conn()
    existing = conn.execute(
        "SELECT * FROM gauge_sites WHERE site_id=?", (site_id,)
    ).fetchone()
    if existing is None:
        raise HTTPException(404, f"gauge_sites: {site_id} not found")

    fields = []
    args: list[Any] = []
    for col, validator in (
        ("gauge_name", lambda v: str(v).strip()),
        ("lat", float), ("lon", float),
        ("action_ft", lambda v: float(v) if v is not None else None),
        ("flood_minor_ft", lambda v: float(v) if v is not None else None),
        ("flood_moderate_ft", lambda v: float(v) if v is not None else None),
        ("flood_major_ft", lambda v: float(v) if v is not None else None),
        ("enabled", lambda v: 1 if bool(v) else 0),
    ):
        if col in body:
            try: args.append(validator(body[col]))
            except (TypeError, ValueError):
                raise HTTPException(400, f"invalid value for {col}: {body[col]!r}")
            fields.append(f"{col}=?")
    if not fields:
        raise HTTPException(400, "no editable fields provided")

    fields.append("updated_at=?")
    args.append(time.time())
    args.append(site_id)
    conn.execute(
        f"UPDATE gauge_sites SET {', '.join(fields)} WHERE site_id=?",
        tuple(args),
    )
    invalidate_curation_cache()
    return await get_gauge(site_id, request)


@router.delete("/gauge-sites/{site_id}")
async def delete_gauge(site_id: str, request: Request) -> dict:
    conn = _get_conn()
    cur = conn.execute("DELETE FROM gauge_sites WHERE site_id=?", (site_id,))
    if cur.rowcount == 0:
        raise HTTPException(404, f"gauge_sites: {site_id} not found")
    invalidate_curation_cache()
    return {"deleted": site_id}


# ============================================================================
# town_anchors
# ============================================================================


@router.get("/town-anchors")
async def list_towns(request: Request) -> list[dict]:
    conn = _get_conn()
    return [_town_row_to_dict(r) for r in conn.execute(
        "SELECT * FROM town_anchors ORDER BY name"
    ).fetchall()]


@router.get("/town-anchors/{anchor_id}")
async def get_town(anchor_id: int, request: Request) -> dict:
    conn = _get_conn()
    r = conn.execute(
        "SELECT * FROM town_anchors WHERE anchor_id=?", (anchor_id,)
    ).fetchone()
    if r is None:
        raise HTTPException(404, f"town_anchors: {anchor_id} not found")
    return _town_row_to_dict(r)


@router.post("/town-anchors")
async def add_town(request: Request) -> dict:
    body = await request.json()
    if not isinstance(body, dict): raise HTTPException(400, "body must be a JSON object")
    for f in ("name", "lat", "lon"):
        if f not in body: raise HTTPException(400, f"missing required field: {f}")
    name = str(body["name"]).strip().lower()
    if not name: raise HTTPException(400, "name must be non-empty")
    try:
        lat = float(body["lat"]); lon = float(body["lon"])
    except (TypeError, ValueError):
        raise HTTPException(400, "lat/lon must be numeric")

    conn = _get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO town_anchors(name, lat, lon, state, enabled, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (name, lat, lon, body.get("state"),
             1 if body.get("enabled", True) else 0, time.time()),
        )
    except Exception as e:
        raise HTTPException(400, f"insert failed: {e}")
    invalidate_curation_cache()
    return await get_town(cur.lastrowid, request)


@router.put("/town-anchors/{anchor_id}")
async def update_town(anchor_id: int, request: Request) -> dict:
    body = await request.json()
    if not isinstance(body, dict): raise HTTPException(400, "body must be a JSON object")

    conn = _get_conn()
    existing = conn.execute(
        "SELECT * FROM town_anchors WHERE anchor_id=?", (anchor_id,)
    ).fetchone()
    if existing is None:
        raise HTTPException(404, f"town_anchors: {anchor_id} not found")

    fields = []
    args: list[Any] = []
    for col, validator in (
        ("name", lambda v: str(v).strip().lower()),
        ("lat", float), ("lon", float),
        ("state", lambda v: str(v).strip() if v else None),
        ("enabled", lambda v: 1 if bool(v) else 0),
    ):
        if col in body:
            try: args.append(validator(body[col]))
            except (TypeError, ValueError):
                raise HTTPException(400, f"invalid value for {col}: {body[col]!r}")
            fields.append(f"{col}=?")
    if not fields:
        raise HTTPException(400, "no editable fields provided")
    fields.append("updated_at=?")
    args.append(time.time())
    args.append(anchor_id)
    conn.execute(
        f"UPDATE town_anchors SET {', '.join(fields)} WHERE anchor_id=?",
        tuple(args),
    )
    invalidate_curation_cache()
    return await get_town(anchor_id, request)


@router.delete("/town-anchors/{anchor_id}")
async def delete_town(anchor_id: int, request: Request) -> dict:
    conn = _get_conn()
    cur = conn.execute("DELETE FROM town_anchors WHERE anchor_id=?", (anchor_id,))
    if cur.rowcount == 0:
        raise HTTPException(404, f"town_anchors: {anchor_id} not found")
    invalidate_curation_cache()
    return {"deleted": anchor_id}
