"""Environmental data API routes."""

from fastapi import APIRouter, HTTPException, Request

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
    """Get active environmental events with local zone marking."""
    env_store = getattr(request.app.state, "env_store", None)

    if not env_store:
        return []

    events = env_store.get_active()
    mesh_zones = set(getattr(env_store, '_mesh_zones', []))

    # Dedup by event_id and add is_local field
    seen_ids = set()
    result = []
    for event in events:
        event_id = event.get("event_id")
        if event_id and event_id in seen_ids:
            continue
        if event_id:
            seen_ids.add(event_id)

        # Mark as local if event zones overlap with configured mesh zones
        event_zones = set(event.get("areas", []))
        event["is_local"] = bool(event_zones & mesh_zones)
        result.append(event)

    return result


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

    v0.6-tail-3: when usgs.feed_source != native, this endpoint returns 404
    instead of creating a temporary USGSStreamsAdapter. The pre-tail-3
    behavior was an AND-mode anti-pattern -- meshai was in central-feed
    mode for usgs but the lookup helper hit USGS.gov directly anyway.
    With this change, the lookup is only available when meshai itself
    is the polling source. In central-feed mode the GUI must source
    values manually or via Central."""
    env_store = getattr(request.app.state, "env_store", None)

    if not env_store:
        raise HTTPException(
            status_code=404,
            detail="Environmental feeds not enabled",
        )

    adapters = getattr(env_store, "_adapters", {})
    usgs_adapter = adapters.get("usgs")

    if not usgs_adapter:
        # No native usgs adapter on the env_store means usgs is either
        # disabled or running on a non-native feed_source (central). In
        # central-feed mode meshai must NOT make direct upstream API calls;
        # that's the AND-model anti-pattern Central's v0.10.2 report
        # called out explicitly. Surface this to the UI as a 404 so the
        # frontend can switch the form to manual-entry mode.
        raise HTTPException(
            status_code=404,
            detail=("site lookup unavailable in central-feed mode; values "
                     "must be entered manually or sourced from Central"),
        )

    try:
        result = usgs_adapter.lookup_site(site_id)
        return result
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


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
