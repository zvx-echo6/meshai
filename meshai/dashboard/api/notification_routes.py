"""Notification API routes with comprehensive testing."""

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from typing import Optional, List, Dict, Any

router = APIRouter(prefix="/notifications", tags=["notifications"])


class TestRequest(BaseModel):
    """Request body for test endpoint."""
    send: bool = False  # Legacy: True = send_test
    action: str = "preview"  # "preview", "send_test", "send_status", "send_live"


class ChannelTestRequest(BaseModel):
    """Request body for channel connectivity test."""
    type: str  # mesh_broadcast, mesh_dm, email, webhook
    # Mesh broadcast
    channel_index: Optional[int] = 0
    # Mesh DM
    node_ids: Optional[List[str]] = []
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


class SinkTestRequest(BaseModel):
    """Request body for sink connectivity test."""
    name: str  # Sink name from config


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


# =============================================================================
# SINKS ENDPOINTS (Phase A of routing revamp)
# =============================================================================

@router.get("/sinks")
async def get_sinks(request: Request):
    """Get configured notification sinks.

    Returns list of named sinks with their type and config.
    Phase A: read-only list; Phase B adds edit endpoints.
    """
    config = getattr(request.app.state, "config", None)
    if not config or not hasattr(config, "notifications"):
        return []

    sinks = getattr(config.notifications, "sinks", {})
    if not sinks:
        return []

    result = []
    for name, sink in sinks.items():
        # Convert dataclass to dict if needed
        if hasattr(sink, "__dataclass_fields__"):
            sink_dict = {
                "name": name,
                "type": sink.type,
                "channel": getattr(sink, "channel", 0),
                "node_ids": getattr(sink, "node_ids", []),
                "smtp_host": getattr(sink, "smtp_host", ""),
                "smtp_port": getattr(sink, "smtp_port", 587),
                "from_address": getattr(sink, "from_address", ""),
                "recipients": getattr(sink, "recipients", []),
                "webhook_url": getattr(sink, "webhook_url", ""),
            }
        else:
            sink_dict = {"name": name, **sink}

        result.append(sink_dict)

    return result


@router.post("/sinks/test")
async def test_sink(request: Request, body: SinkTestRequest):
    """Test a named sink's connectivity.

    Uses the existing channel test_connection() method.
    """
    config = getattr(request.app.state, "config", None)
    if not config or not hasattr(config, "notifications"):
        raise HTTPException(status_code=404, detail="Notifications not configured")

    sinks = getattr(config.notifications, "sinks", {})
    if not sinks:
        raise HTTPException(status_code=404, detail="No sinks configured")

    if body.name not in sinks:
        raise HTTPException(status_code=404, detail=f"Sink not found: {body.name}")

    sink = sinks[body.name]

    # Get connector for mesh channels
    connector = getattr(request.app.state, "connector", None)

    try:
        from ...notifications.channels import create_channel_from_sink
        channel = create_channel_from_sink(sink, connector=connector)
        result = await channel.test_connection()
        return {
            "sink": body.name,
            "type": sink.type,
            **result
        }
    except ValueError as e:
        return {
            "sink": body.name,
            "type": getattr(sink, "type", "unknown"),
            "success": False,
            "message": str(e),
            "error": str(e),
        }
    except Exception as e:
        return {
            "sink": body.name,
            "type": getattr(sink, "type", "unknown"),
            "success": False,
            "message": f"Test failed: {str(e)}",
            "error": str(e),
        }
