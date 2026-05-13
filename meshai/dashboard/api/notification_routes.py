"""Notification API routes."""

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from typing import Optional

router = APIRouter(prefix="/notifications", tags=["notifications"])


class ChannelCreate(BaseModel):
    """Channel creation request."""
    id: str
    type: str
    enabled: bool = True
    channel_index: int = 0
    node_ids: list[str] = []
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_tls: bool = True
    from_address: str = ""
    recipients: list[str] = []
    url: str = ""
    headers: dict = {}


class RuleCreate(BaseModel):
    """Rule creation request."""
    name: str
    categories: list[str] = []
    min_severity: str = "warning"
    channel_ids: list[str] = []
    override_quiet: bool = False


class QuietHoursUpdate(BaseModel):
    """Quiet hours update request."""
    start: str
    end: str


@router.get("/categories")
async def get_categories():
    """Get all alert categories with descriptions."""
    try:
        from ...notifications.categories import list_categories
        return list_categories()
    except ImportError:
        return []


@router.get("/channels")
async def get_channels(request: Request):
    """Get configured notification channels."""
    notification_router = getattr(request.app.state, "notification_router", None)
    if not notification_router:
        return []
    return notification_router.get_channels()


@router.post("/channels")
async def create_channel(request: Request, channel: ChannelCreate):
    """Create a new notification channel."""
    # This would require runtime config modification
    # For now, return not implemented
    raise HTTPException(status_code=501, detail="Channel creation requires config file edit")


@router.post("/channels/{channel_id}/test")
async def test_channel(request: Request, channel_id: str):
    """Send a test alert to a channel."""
    notification_router = getattr(request.app.state, "notification_router", None)
    if not notification_router:
        raise HTTPException(status_code=404, detail="Notification router not configured")

    success, message = await notification_router.test_channel(channel_id)
    return {"success": success, "message": message}


@router.get("/rules")
async def get_rules(request: Request):
    """Get configured notification rules."""
    notification_router = getattr(request.app.state, "notification_router", None)
    if not notification_router:
        return []
    return notification_router.get_rules()


@router.post("/rules")
async def create_rule(request: Request, rule: RuleCreate):
    """Create a new notification rule."""
    # This would require runtime config modification
    raise HTTPException(status_code=501, detail="Rule creation requires config file edit")


@router.get("/quiet-hours")
async def get_quiet_hours(request: Request):
    """Get quiet hours configuration."""
    config = getattr(request.app.state, "config", None)
    if not config or not hasattr(config, "notifications"):
        return {"start": "22:00", "end": "06:00"}
    return {
        "start": config.notifications.quiet_hours_start,
        "end": config.notifications.quiet_hours_end,
    }


@router.put("/quiet-hours")
async def update_quiet_hours(request: Request, quiet_hours: QuietHoursUpdate):
    """Update quiet hours configuration."""
    # This would require runtime config modification
    raise HTTPException(status_code=501, detail="Quiet hours update requires config file edit")
