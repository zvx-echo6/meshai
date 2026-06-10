"""Debug API endpoints for development/ops use."""
import logging
from fastapi import APIRouter, Request

logger = logging.getLogger(__name__)
router = APIRouter(tags=["debug"])


@router.post("/debug/clear-cooldowns")
async def clear_cooldowns(request: Request):
    """Delete all dispatcher cooldown state (in-memory + SQLite)."""
    # Get dispatcher from pipeline components on the event bus
    bus = getattr(request.app.state, "bus", None)
    if bus is None:
        return {"cleared": False, "error": "event bus not available"}
    comps = getattr(bus, "_pipeline_components", None) or {}
    dispatcher = comps.get("dispatcher")
    if dispatcher is None:
        return {"cleared": False, "error": "dispatcher not available"}

    # Clear in-memory cooldown map
    dispatcher._toggle_cooldown.clear()

    # Clear SQLite cooldowns table
    try:
        from meshai.persistence import get_db
        conn = get_db()
        conn.execute("DELETE FROM dispatcher_cooldowns")
        logger.info("debug: cleared all dispatcher cooldowns")
    except Exception:
        logger.exception("debug: failed to clear dispatcher_cooldowns table")

    return {"cleared": True}
