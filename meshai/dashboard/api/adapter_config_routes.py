"""v0.6-3c adapter_config REST API.

CRUD over the adapter_config + adapter_meta tables seeded in v0.6-3a.1.
Every PUT / reset / meta-update calls invalidate_cache() so the next
handler read sees the new value -- no container restart required.

Endpoints:

    GET  /api/adapter-config
        Grouped view: {adapter: [{key, value, default, type, description}, ...]}
    GET  /api/adapter-config/{adapter}
        One adapter's keys.
    GET  /api/adapter-config/{adapter}/{key}
        Single key.
    PUT  /api/adapter-config/{adapter}/{key}    body {"value": <typed>}
        Validates against the row's declared `type`; 400 on mismatch.
        Cache invalidated on success.
    POST /api/adapter-config/{adapter}/{key}/reset
        Sets value_json = default_json. Cache invalidated.

    GET  /api/adapter-meta
        {adapter: {display_name, include_in_llm_context, description}}
    PUT  /api/adapter-meta/{adapter}    body {"include_in_llm_context"?: bool,
                                              "display_name"?: str}
        Partial update; only the provided fields change.

Type validation rules:
    int    -- accepts int or int-castable JSON number with no fractional part
    float  -- accepts int or float (cast to float)
    str    -- accepts str
    bool   -- accepts bool (true/false only)
    json   -- accepts any JSON value (list / dict / scalar / null)
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from meshai.adapter_config import invalidate_cache

logger = logging.getLogger(__name__)


router = APIRouter(tags=["adapter_config"])


# ============================================================================
# helpers
# ============================================================================


def _get_conn():
    """Return a sqlite connection via the persistence layer."""
    from meshai.persistence import get_db
    return get_db()


def _row_to_dict(r) -> dict:
    """sqlite3.Row -> plain dict with value_json decoded to a Python value."""
    out = {
        "adapter": r["adapter"],
        "key": r["key"],
        "type": r["type"],
        "description": r["description"] or "",
        "updated_at": r["updated_at"],
    }
    try: out["value"] = json.loads(r["value_json"])
    except Exception: out["value"] = None
    try: out["default"] = json.loads(r["default_json"])
    except Exception: out["default"] = None
    return out


def _validate_value(value: Any, type_tag: str) -> Any:
    """Coerce + validate a request value against the declared type.

    Raises HTTPException(400) on mismatch.
    Returns the coerced Python value (suitable for json.dumps).
    """
    if type_tag == "int":
        if isinstance(value, bool):
            raise HTTPException(400, "int expected, got bool")
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        raise HTTPException(400, f"int expected, got {type(value).__name__}: {value!r}")

    if type_tag == "float":
        if isinstance(value, bool):
            raise HTTPException(400, "float expected, got bool")
        if isinstance(value, (int, float)):
            return float(value)
        raise HTTPException(400, f"float expected, got {type(value).__name__}: {value!r}")

    if type_tag == "str":
        if not isinstance(value, str):
            raise HTTPException(400, f"str expected, got {type(value).__name__}: {value!r}")
        return value

    if type_tag == "bool":
        if isinstance(value, bool):
            return value
        raise HTTPException(400, f"bool expected, got {type(value).__name__}: {value!r}")

    if type_tag == "json":
        # Any JSON-encodable value: scalar, list, dict, null. Verify by round-trip.
        try:
            json.dumps(value)
            return value
        except (TypeError, ValueError) as e:
            raise HTTPException(400, f"json expected (not serializable): {e}")

    raise HTTPException(500, f"unknown type tag {type_tag!r} in adapter_config row")


# ============================================================================
# adapter_config endpoints
# ============================================================================


@router.get("/adapter-config")
async def list_adapter_config(request: Request) -> dict:
    """Returns a grouped view: {adapter: [{key, value, default, type, description}, ...]}.

    Ordered: adapter names alphabetically, keys within each adapter alphabetically.
    """
    conn = _get_conn()
    rows = conn.execute(
        "SELECT adapter, key, value_json, default_json, type, description, updated_at "
        "FROM adapter_config ORDER BY adapter, key"
    ).fetchall()
    grouped: dict[str, list[dict]] = {}
    for r in rows:
        grouped.setdefault(r["adapter"], []).append(_row_to_dict(r))
    return grouped


@router.get("/adapter-config/{adapter}")
async def list_adapter(adapter: str, request: Request) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT adapter, key, value_json, default_json, type, description, updated_at "
        "FROM adapter_config WHERE adapter=? ORDER BY key",
        (adapter,),
    ).fetchall()
    if not rows:
        # Empty list is valid for an adapter that has zero config keys (e.g.
        # itd_511 post v0.6-3a.1). The caller can distinguish from a fully
        # unknown adapter via /api/adapter-meta.
        return []
    return [_row_to_dict(r) for r in rows]


@router.get("/adapter-config/{adapter}/{key}")
async def get_one(adapter: str, key: str, request: Request) -> dict:
    conn = _get_conn()
    r = conn.execute(
        "SELECT adapter, key, value_json, default_json, type, description, updated_at "
        "FROM adapter_config WHERE adapter=? AND key=?",
        (adapter, key),
    ).fetchone()
    if r is None:
        raise HTTPException(404, f"adapter_config: {adapter}.{key} not found")
    return _row_to_dict(r)


@router.put("/adapter-config/{adapter}/{key}")
async def put_one(adapter: str, key: str, request: Request) -> dict:
    """Update value_json. Body must be {"value": <typed-or-json-compatible>}.

    Validates against the row's declared type; 400 on mismatch.
    Invalidates the accessor cache on success.
    """
    body = await request.json()
    if not isinstance(body, dict) or "value" not in body:
        raise HTTPException(400, "body must be a JSON object with a 'value' field")

    conn = _get_conn()
    r = conn.execute(
        "SELECT type FROM adapter_config WHERE adapter=? AND key=?",
        (adapter, key),
    ).fetchone()
    if r is None:
        raise HTTPException(404, f"adapter_config: {adapter}.{key} not found")

    coerced = _validate_value(body["value"], r["type"])
    new_value_json = json.dumps(coerced)
    conn.execute(
        "UPDATE adapter_config SET value_json=?, updated_at=? "
        "WHERE adapter=? AND key=?",
        (new_value_json, time.time(), adapter, key),
    )

    # Drop the in-process cache so the next handler read sees the new value.
    invalidate_cache()
    logger.info("adapter_config PUT: %s.%s = %r", adapter, key, coerced)

    # Return the updated row.
    r2 = conn.execute(
        "SELECT adapter, key, value_json, default_json, type, description, updated_at "
        "FROM adapter_config WHERE adapter=? AND key=?",
        (adapter, key),
    ).fetchone()
    return _row_to_dict(r2)


@router.post("/adapter-config/{adapter}/{key}/reset")
async def reset_one(adapter: str, key: str, request: Request) -> dict:
    """Set value_json = default_json. Invalidates the accessor cache."""
    conn = _get_conn()
    r = conn.execute(
        "SELECT default_json FROM adapter_config WHERE adapter=? AND key=?",
        (adapter, key),
    ).fetchone()
    if r is None:
        raise HTTPException(404, f"adapter_config: {adapter}.{key} not found")

    conn.execute(
        "UPDATE adapter_config SET value_json=?, updated_at=? "
        "WHERE adapter=? AND key=?",
        (r["default_json"], time.time(), adapter, key),
    )
    invalidate_cache()
    logger.info("adapter_config RESET: %s.%s -> default", adapter, key)

    r2 = conn.execute(
        "SELECT adapter, key, value_json, default_json, type, description, updated_at "
        "FROM adapter_config WHERE adapter=? AND key=?",
        (adapter, key),
    ).fetchone()
    return _row_to_dict(r2)


# ============================================================================
# adapter_meta endpoints
# ============================================================================


@router.get("/adapter-meta")
async def list_meta(request: Request) -> dict:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT adapter, display_name, include_in_llm_context, description "
        "FROM adapter_meta ORDER BY adapter"
    ).fetchall()
    return {
        r["adapter"]: {
            "display_name": r["display_name"],
            "include_in_llm_context": bool(r["include_in_llm_context"]),
            "description": r["description"] or "",
        }
        for r in rows
    }


@router.put("/adapter-meta/{adapter}")
async def put_meta(adapter: str, request: Request) -> dict:
    """Partial update: only the fields present in the body change.

    Accepted fields:
        include_in_llm_context : bool
        display_name           : str (non-empty)
    """
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "body must be a JSON object")

    conn = _get_conn()
    existing = conn.execute(
        "SELECT display_name, include_in_llm_context, description "
        "FROM adapter_meta WHERE adapter=?",
        (adapter,),
    ).fetchone()
    if existing is None:
        raise HTTPException(404, f"adapter_meta: {adapter} not found")

    new_include = existing["include_in_llm_context"]
    new_name = existing["display_name"]

    if "include_in_llm_context" in body:
        v = body["include_in_llm_context"]
        if not isinstance(v, bool):
            raise HTTPException(400, "include_in_llm_context must be bool")
        new_include = 1 if v else 0

    if "display_name" in body:
        v = body["display_name"]
        if not isinstance(v, str) or not v.strip():
            raise HTTPException(400, "display_name must be a non-empty string")
        new_name = v.strip()

    conn.execute(
        "UPDATE adapter_meta SET display_name=?, include_in_llm_context=?, updated_at=? "
        "WHERE adapter=?",
        (new_name, new_include, time.time(), adapter),
    )
    # adapter_meta doesn't affect the in-memory cache directly, but the meta
    # might gate LLM-context inclusion in the env_reporter (commit #5).
    # Invalidating is cheap insurance.
    invalidate_cache()
    logger.info("adapter_meta PUT: %s -> %s", adapter, body)

    r = conn.execute(
        "SELECT display_name, include_in_llm_context, description "
        "FROM adapter_meta WHERE adapter=?",
        (adapter,),
    ).fetchone()
    return {
        "display_name": r["display_name"],
        "include_in_llm_context": bool(r["include_in_llm_context"]),
        "description": r["description"] or "",
    }
