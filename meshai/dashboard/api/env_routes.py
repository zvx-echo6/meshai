"""Environmental data API routes."""

from fastapi import APIRouter, Request

router = APIRouter(tags=["environment"])


@router.get("/env/status")
async def get_env_status(request: Request):
    """Get environmental feeds status."""
    env_store = getattr(request.app.state, "env_store", None)

    if not env_store:
        return {"enabled": False, "feeds": []}

    return {
        "enabled": True,
        "feeds": env_store.get_source_health(),
    }


@router.get("/env/active")
async def get_active_env(request: Request):
    """Get active environmental events."""
    env_store = getattr(request.app.state, "env_store", None)

    if not env_store:
        return []

    return env_store.get_active()


@router.get("/env/swpc")
async def get_swpc_data(request: Request):
    """Get SWPC space weather data."""
    env_store = getattr(request.app.state, "env_store", None)

    if not env_store:
        return {"enabled": False}

    status = env_store.get_swpc_status()
    if not status:
        return {"enabled": False}

    return {
        "enabled": True,
        **status,
    }


@router.get("/env/propagation")
async def get_rf_propagation(request: Request):
    """Get combined HF + UHF propagation data for dashboard."""
    env_store = getattr(request.app.state, "env_store", None)

    if not env_store:
        return {"hf": {}, "uhf_ducting": {}}

    return env_store.get_rf_propagation()


@router.get("/env/ducting")
async def get_ducting_data(request: Request):
    """Get tropospheric ducting assessment."""
    env_store = getattr(request.app.state, "env_store", None)

    if not env_store:
        return {"enabled": False}

    status = env_store.get_ducting_status()
    if not status:
        return {"enabled": False}

    return {
        "enabled": True,
        **status,
    }


@router.get("/env/fires")
async def get_fires_data(request: Request):
    """Get active wildfire perimeters."""
    env_store = getattr(request.app.state, "env_store", None)

    if not env_store:
        return []

    return env_store.get_active(source="nifc")


@router.get("/env/avalanche")
async def get_avalanche_data(request: Request):
    """Get avalanche advisories."""
    env_store = getattr(request.app.state, "env_store", None)

    if not env_store:
        return {"off_season": True, "advisories": []}

    adapters = getattr(env_store, "_adapters", {})
    avy_adapter = adapters.get("avalanche")

    if avy_adapter and avy_adapter.is_off_season():
        return {"off_season": True, "advisories": []}

    return {
        "off_season": False,
        "advisories": env_store.get_active(source="avalanche"),
    }

@router.get("/env/streams")
async def get_streams_data(request: Request):
    """Get USGS stream gauge readings."""
    env_store = getattr(request.app.state, "env_store", None)

    if not env_store:
        return []

    return env_store.get_active(source="usgs")


@router.get("/env/traffic")
async def get_traffic_data(request: Request):
    """Get TomTom traffic flow data."""
    env_store = getattr(request.app.state, "env_store", None)

    if not env_store:
        return []

    return env_store.get_active(source="traffic")


@router.get("/env/roads")
async def get_roads_data(request: Request):
    """Get 511 road conditions."""
    env_store = getattr(request.app.state, "env_store", None)

    if not env_store:
        return []

    return env_store.get_active(source="511")
