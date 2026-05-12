"""Environmental data API routes (Phase 1 placeholder)."""

from fastapi import APIRouter, Request

router = APIRouter(tags=["environment"])


@router.get("/env/status")
async def get_env_status(request: Request):
    """Get environmental feeds status."""
    env_store = request.app.state.env_store

    if not env_store:
        return {"enabled": False, "feeds": []}

    # Will be populated in Phase 1 when env_store exists
    return {
        "enabled": True,
        "feeds": [],
    }


@router.get("/env/active")
async def get_active_env(request: Request):
    """Get active environmental conditions."""
    env_store = request.app.state.env_store

    if not env_store:
        return []

    # Will be populated in Phase 1
    return []


@router.get("/env/swpc")
async def get_swpc_data(request: Request):
    """Get SWPC space weather data."""
    env_store = request.app.state.env_store

    if not env_store:
        return {"enabled": False}

    # Will be populated in Phase 1
    return {"enabled": False}
