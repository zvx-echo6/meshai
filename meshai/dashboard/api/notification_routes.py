"""Notification API routes."""

from fastapi import APIRouter, Request, HTTPException

router = APIRouter(prefix="/notifications", tags=["notifications"])


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
async def test_rule(request: Request, rule_index: int):
    """Send a test alert through a specific rule."""
    notification_router = getattr(request.app.state, "notification_router", None)
    if not notification_router:
        raise HTTPException(status_code=404, detail="Notification router not configured")

    success, message = await notification_router.test_rule(rule_index)
    return {"success": success, "message": message}
