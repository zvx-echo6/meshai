"""Notification API routes."""

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from typing import Optional

router = APIRouter(prefix="/notifications", tags=["notifications"])


class TestRequest(BaseModel):
    """Request body for test endpoint."""
    send: bool = False  # True = actually deliver, False = preview only


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
    """Get configured notification rules."""
    notification_router = getattr(request.app.state, "notification_router", None)
    if not notification_router:
        return []
    return notification_router.get_rules()


@router.post("/rules/{rule_index}/test")
async def test_rule(request: Request, rule_index: int, body: Optional[TestRequest] = None):
    """Test a notification rule against current conditions.

    Returns:
        {
            "conditions_matched": int,       # Number of matching alerts
            "preview_messages": list[str],   # Messages that would send
            "is_example": bool,              # True if using example messages
            "delivered": bool,               # True if actually sent
            "delivery_method": str,          # e.g. "mesh_broadcast"
            "delivery_result": str,          # Result message
        }
    """
    notification_router = getattr(request.app.state, "notification_router", None)
    if not notification_router:
        raise HTTPException(status_code=404, detail="Notification router not configured")

    alert_engine = getattr(request.app.state, "alert_engine", None)
    env_store = getattr(request.app.state, "env_store", None)

    send = body.send if body else False

    result = await notification_router.test_rule_with_conditions(
        rule_index,
        alert_engine=alert_engine,
        env_store=env_store,
        send=send,
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

    result = await notification_router.test_rule_with_conditions(
        rule_index,
        alert_engine=alert_engine,
        env_store=env_store,
        send=False,
    )

    return result
