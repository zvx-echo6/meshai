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


@router.get("/env/usgs/lookup/{site_id}")
async def lookup_usgs_site(request: Request, site_id: str):
    """Lookup USGS site metadata and NWS flood stages.

    Returns site name, location, and flood stage thresholds from NWS NWPS.
    Used by the config UI to auto-populate fields when adding a new gauge.
    """
    env_store = getattr(request.app.state, "env_store", None)

    if not env_store:
        return {"error": "Environmental feeds not enabled"}

    adapters = getattr(env_store, "_adapters", {})
    usgs_adapter = adapters.get("usgs")

    if not usgs_adapter:
        # Create a temporary adapter for lookup
        from meshai.env.usgs import USGSStreamsAdapter
        from meshai.config import USGSConfig
        usgs_adapter = USGSStreamsAdapter(USGSConfig())

    try:
        result = usgs_adapter.lookup_site(site_id)
        return result
    except Exception as e:
        return {"error": str(e), "site_id": site_id}


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


@router.get("/env/hotspots")
async def get_hotspots_data(request: Request):
    """Get NASA FIRMS satellite fire hotspots."""
    env_store = getattr(request.app.state, "env_store", None)

    if not env_store:
        return {"hotspots": [], "new_ignitions": 0}

    firms_adapter = getattr(env_store, "_firms", None)

    if not firms_adapter:
        return {"hotspots": [], "new_ignitions": 0, "enabled": False}

    hotspots = env_store.get_active(source="firms")
    new_ignitions = [h for h in hotspots if h.get("properties", {}).get("new_ignition")]

    return {
        "enabled": True,
        "hotspots": hotspots,
        "new_ignitions": len(new_ignitions),
    }
