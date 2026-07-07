"""Notification API routes with comprehensive testing."""

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from typing import Optional, List, Dict, Any

import contextlib
import io
from pathlib import Path

from meshai.config import RegionRouteMatrix, _dataclass_to_dict, _dict_to_dataclass, NotificationsConfig
from meshai.config_loader import save_section, get_config_dir_from_path, load_config

router = APIRouter(prefix="/notifications", tags=["notifications"])


class TestRequest(BaseModel):
    """Request body for test endpoint."""
    send: bool = False  # Legacy: True = send_test
    action: str = "preview"  # "preview", "send_test", "send_status", "send_live"


class ChannelTestRequest(BaseModel):
    """Request body for channel connectivity test."""
    type: str  # mesh_broadcast, mesh_dm, meshcore_broadcast, meshcore_dm, email, webhook
    # Mesh broadcast
    channel_index: Optional[int] = 0
    # Mesh DM
    node_ids: Optional[List[str]] = []
    # MeshCore broadcast
    meshcore_channel: Optional[str] = None
    # MeshCore DM
    meshcore_dm_contacts: Optional[List[str]] = []
    # Email
    smtp_host: Optional[str] = ""
    smtp_port: Optional[int] = 587
    smtp_user: Optional[str] = ""
    smtp_password: Optional[str] = ""
    smtp_tls: Optional[bool] = True
    from_address: Optional[str] = ""
    recipients: Optional[List[str]] = []
    # Webhook
    url: Optional[str] = ""
    headers: Optional[Dict[str, str]] = {}


class RuleSourcesRequest(BaseModel):
    """Request body for rule sources health check."""
    categories: List[str] = []


@router.get("/categories")
async def get_categories():
    """Get all alert categories with descriptions."""
    try:
        from ...notifications.categories import list_categories
        return list_categories()
    except ImportError:
        return []


@router.get("/families")
async def get_families():
    """List all routable families (toggles) for the Routing UI.

    Returns the built-in (static VALID_TOGGLES) families PLUS any families
    registered at runtime by generic/config-driven sources (Integration
    Phase A). Each entry is `{key, label, dynamic}` where `dynamic` is True
    for families that are NOT part of the static VALID_TOGGLES set — i.e.
    generic sources whose category was registered as a first-class family.
    The frontend merges these with its icon table so custom families render
    as assignable toggle rows. Read-only.
    """
    try:
        from ...notifications.categories import registered_families, VALID_TOGGLES
    except ImportError:
        return []
    return [
        {"key": key, "label": label, "dynamic": key not in VALID_TOGGLES}
        for key, label in registered_families().items()
    ]


@router.get("/rules")
async def get_rules(request: Request):
    """Get configured notification rules with stats."""
    notification_router = getattr(request.app.state, "notification_router", None)
    if not notification_router:
        return []

    rules = notification_router.get_rules()

    # Enhance rules with stats
    result = []
    for i, rule in enumerate(rules):
        rule_copy = dict(rule)
        stats = rule_copy.pop("_stats", {})
        rule_copy["stats"] = stats
        rule_copy["index"] = i
        result.append(rule_copy)

    return result


@router.get("/rules/{rule_index}/stats")
async def get_rule_stats(request: Request, rule_index: int):
    """Get statistics for a specific rule."""
    notification_router = getattr(request.app.state, "notification_router", None)
    if not notification_router:
        raise HTTPException(status_code=404, detail="Notification router not configured")

    rules_config = getattr(request.app.state, "config", None)
    if rules_config:
        rules_config = getattr(rules_config, "rules", [])
        if rule_index < 0 or rule_index >= len(rules_config):
            raise HTTPException(status_code=404, detail="Rule not found")

        rule = rules_config[rule_index]
        if hasattr(rule, "__dict__"):
            rule_dict = {k: v for k, v in rule.__dict__.items() if not k.startswith("_")}
        else:
            rule_dict = dict(rule)

        rule_name = rule_dict.get("name", f"Rule {rule_index}")
        return notification_router.get_rule_stats(rule_name)

    return {"last_fired": None, "last_test": None, "fire_count": 0}


@router.post("/channels/test")
async def test_channel(request: Request, body: ChannelTestRequest):
    """Test channel connectivity without sending actual alert content.

    Returns:
        {
            "success": bool,
            "message": str,  # Human-readable result
            "error": str,    # Detailed error if failed
            "details": {}    # Channel-specific details
        }
    """
    notification_router = getattr(request.app.state, "notification_router", None)
    if not notification_router:
        raise HTTPException(status_code=404, detail="Notification router not configured")

    # Build channel config from request
    channel_config = {"type": body.type}

    if body.type == "mesh_broadcast":
        channel_config["channel_index"] = body.channel_index or 0
    elif body.type == "mesh_dm":
        channel_config["node_ids"] = body.node_ids or []
    elif body.type == "meshcore_broadcast":
        channel_config["meshcore_channel"] = body.meshcore_channel or ""
    elif body.type == "meshcore_dm":
        channel_config["meshcore_dm_contacts"] = body.meshcore_dm_contacts or []
    elif body.type == "email":
        channel_config.update({
            "smtp_host": body.smtp_host or "",
            "smtp_port": body.smtp_port or 587,
            "smtp_user": body.smtp_user or "",
            "smtp_password": body.smtp_password or "",
            "smtp_tls": body.smtp_tls if body.smtp_tls is not None else True,
            "from_address": body.from_address or "",
            "recipients": body.recipients or [],
        })
    elif body.type == "webhook":
        channel_config.update({
            "url": body.url or "",
            "headers": body.headers or {},
        })
    else:
        return {
            "success": False,
            "message": "Unknown channel type",
            "error": f"Channel type '{body.type}' is not supported",
            "details": {}
        }

    result = await notification_router.test_channel(channel_config)
    return result


@router.post("/rules/{rule_index}/test")
async def test_rule(request: Request, rule_index: int, body: Optional[TestRequest] = None):
    """Test a notification rule against current conditions.

    Returns comprehensive test result including:
    - Live data from relevant environmental feeds
    - Matching alerts (conditions that would fire)
    - Near-misses (filtered by severity threshold)
    - Preview messages and delivery status
    - Source health (which feeds are enabled)
    - Rule statistics (last fired, fire count)
    """
    notification_router = getattr(request.app.state, "notification_router", None)
    if not notification_router:
        raise HTTPException(status_code=404, detail="Notification router not configured")

    alert_engine = getattr(request.app.state, "alert_engine", None)
    env_store = getattr(request.app.state, "env_store", None)
    health_engine = getattr(request.app.state, "health_engine", None)

    action = body.action if body else "preview"
    send = body.send if body else False

    # Legacy support
    if send and action == "preview":
        action = "send_test"

    result = await notification_router.test_rule_with_conditions(
        rule_index,
        alert_engine=alert_engine,
        env_store=env_store,
        health_engine=health_engine,
        action=action,
    )

    return result


@router.post("/rules/{rule_index}/preview")
async def preview_rule(request: Request, rule_index: int):
    """Preview what a rule would match right now (without sending)."""
    notification_router = getattr(request.app.state, "notification_router", None)
    if not notification_router:
        raise HTTPException(status_code=404, detail="Notification router not configured")

    alert_engine = getattr(request.app.state, "alert_engine", None)
    env_store = getattr(request.app.state, "env_store", None)
    health_engine = getattr(request.app.state, "health_engine", None)

    result = await notification_router.test_rule_with_conditions(
        rule_index,
        alert_engine=alert_engine,
        env_store=env_store,
        health_engine=health_engine,
        action="preview",
    )

    return result


@router.post("/rules/sources")
async def get_rule_sources(request: Request, body: RuleSourcesRequest):
    """Get data source health for a set of categories.

    Returns per-category source status:
    {
        "category_id": {
            "enabled": true/false,
            "active_events": number,
            "source": "nws"/"swpc"/etc,
            "status": "ok"/"disabled"/"no_data"
        }
    }
    """
    notification_router = getattr(request.app.state, "notification_router", None)
    if not notification_router:
        raise HTTPException(status_code=404, detail="Notification router not configured")

    env_store = getattr(request.app.state, "env_store", None)

    return notification_router.get_source_health(body.categories, env_store)


@router.post("/rules/{rule_index}/send-status")
async def send_rule_status(request: Request, rule_index: int):
    """Send current conditions summary through a rule's channel.

    Formats current live data as a readable message and delivers
    through the rule's configured channel with [STATUS] prefix.
    """
    notification_router = getattr(request.app.state, "notification_router", None)
    if not notification_router:
        raise HTTPException(status_code=404, detail="Notification router not configured")

    alert_engine = getattr(request.app.state, "alert_engine", None)
    env_store = getattr(request.app.state, "env_store", None)
    health_engine = getattr(request.app.state, "health_engine", None)

    result = await notification_router.test_rule_with_conditions(
        rule_index,
        alert_engine=alert_engine,
        env_store=env_store,
        health_engine=health_engine,
        action="send_status",
    )

    return result


@router.post("/rules/{rule_index}/send-test")
async def send_rule_test(request: Request, rule_index: int):
    """Send example alert message through a rule's channel.

    Sends the example_message from the rule's first category
    through the configured channel with [TEST] prefix.
    """
    notification_router = getattr(request.app.state, "notification_router", None)
    if not notification_router:
        raise HTTPException(status_code=404, detail="Notification router not configured")

    alert_engine = getattr(request.app.state, "alert_engine", None)
    env_store = getattr(request.app.state, "env_store", None)
    health_engine = getattr(request.app.state, "health_engine", None)

    result = await notification_router.test_rule_with_conditions(
        rule_index,
        alert_engine=alert_engine,
        env_store=env_store,
        health_engine=health_engine,
        action="send_test",
    )

    return result


@router.post("/rules/{rule_index}/send-live")
async def send_rule_live(request: Request, rule_index: int):
    """Send actual live alert through a rule's channel.

    Only available when there are matching conditions.
    Sends one of the actual matching alerts with [LIVE TEST] prefix.
    """
    notification_router = getattr(request.app.state, "notification_router", None)
    if not notification_router:
        raise HTTPException(status_code=404, detail="Notification router not configured")

    alert_engine = getattr(request.app.state, "alert_engine", None)
    env_store = getattr(request.app.state, "env_store", None)
    health_engine = getattr(request.app.state, "health_engine", None)

    result = await notification_router.test_rule_with_conditions(
        rule_index,
        alert_engine=alert_engine,
        env_store=env_store,
        health_engine=health_engine,
        action="send_live",
    )

    return result


# ---------------------------------------------------------------------------
# P3 region-routing primitives
# ---------------------------------------------------------------------------


class RegionRoutingBody(BaseModel):
    """Request body for POST /notifications/region-routing."""
    enabled: bool = False
    cells: Dict[str, Any] = {}


@router.get("/regions")
async def get_regions(request: Request):
    """List distinct named region names from config.coverage.areas.

    Returns config-order, deduplicated list[str] of non-empty area names.
    Satpass observer labels are not included here (deferred to P2).
    """
    config = getattr(request.app.state, "config", None)

    # Prefer the ON-DISK coverage areas so newly-saved regions appear in the
    # routing matrix WITHOUT a bot restart. `coverage` is a restart-required
    # section, so the in-memory config intentionally lags disk until restart --
    # but that gate is about the engine re-scoping fetches, not about the routing
    # UI seeing region NAMES. Fall back to the in-memory config on any error.
    areas = None
    cfg_path = getattr(request.app.state, "config_path", None)
    if cfg_path is not None:
        try:
            cfg_dir = get_config_dir_from_path(Path(cfg_path))
            with contextlib.redirect_stdout(io.StringIO()):
                disk_cfg = load_config(cfg_dir)
            areas = getattr(getattr(disk_cfg, "coverage", None), "areas", None)
        except Exception:
            areas = None
    if areas is None:
        if config is None:
            return []
        areas = getattr(getattr(config, "coverage", None), "areas", None) or []
    seen: set[str] = set()
    result: list[str] = []
    for area in areas:
        # areas may be raw dicts (from config loader) or MonitoringArea objects
        if isinstance(area, dict):
            name = area.get("name") or None
        else:
            name = getattr(area, "name", None)
        if name and name not in seen:
            seen.add(name)
            result.append(name)
    return result


@router.get("/region-routing")
async def get_region_routing(request: Request):
    """Return the current region×family routing matrix.

    Shape: {"enabled": bool, "cells": {family: {region: {mt, mc, min_severity, enabled}}}}
    Defaults to enabled=false, cells={} when not configured.
    """
    config = getattr(request.app.state, "config", None)
    if config is None:
        return {"enabled": False, "cells": {}}

    rr = getattr(getattr(config, "notifications", None), "region_routes", None)
    if rr is None:
        return {"enabled": False, "cells": {}}

    return {"enabled": bool(rr.enabled), "cells": rr.cells}


@router.post("/region-routing")
async def save_region_routing(request: Request, body: RegionRoutingBody):
    """Save the region×family routing matrix (read-modify-write).

    Only region_routes is updated; all other notification fields
    (toggles, rules, destinations, etc.) survive untouched.

    Returns: {"ok": true, "saved": {"enabled": bool, "cells": {...}}}
    """
    config = getattr(request.app.state, "config", None)
    config_path = getattr(request.app.state, "config_path", None)
    if config is None or config_path is None:
        raise HTTPException(status_code=500, detail="Config not available")

    # Build the new RegionRouteMatrix from the request body.
    new_rr = RegionRouteMatrix(enabled=body.enabled, cells=body.cells)

    # Explicit server-side read-modify-write:
    # 1. Serialize the CURRENT notifications object to dict (preserves toggles/rules/destinations).
    current_notif = getattr(config, "notifications", None)
    if current_notif is None:
        from meshai.config import NotificationsConfig as _NC
        current_notif = _NC()

    notif_dict = _dataclass_to_dict(current_notif)

    # 2. Overlay ONLY region_routes with the new value.
    notif_dict["region_routes"] = _dataclass_to_dict(new_rr)

    # 3. Persist via the multi-file-aware save_section.
    try:
        config_dir = get_config_dir_from_path(config_path)
        save_section("notifications", notif_dict, config_dir)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Save failed: {exc}")

    # 4. Update in-memory config so a subsequent GET reflects the save.
    try:
        config.notifications.region_routes = new_rr
    except Exception:
        pass  # best-effort; disk is authoritative

    saved = {"enabled": new_rr.enabled, "cells": new_rr.cells}
    return {"ok": True, "saved": saved}
