"""Environmental data API routes."""

import json
import logging

from fastapi import APIRouter, HTTPException, Request

from meshai.persistence import get_db

router = APIRouter(tags=["environment"])
log = logging.getLogger(__name__)


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
    """Get active environmental events from DB."""
    db = get_db()

    rows = []

    # Fires: active (not tombstoned), seen in last 7 days
    try:
        fire_rows = db.execute(
            "SELECT irwin_id AS event_id, 'nifc' AS source, 'wildfire' AS event_type,"
            "  incident_name AS headline, 'immediate' AS severity,"
            "  lat, lon, county, state, last_event_at AS fetched_at,"
            "  last_broadcast_at"
            " FROM fires"
            " WHERE tombstoned_at IS NULL"
            "  AND last_event_at > CAST(strftime('%s','now','-7 days') AS INTEGER)"
        ).fetchall()
        rows.extend(fire_rows)
    except Exception as e:
        log.warning("env/active fires query failed: %s", e)

    # NWS alerts: not expired
    try:
        nws_rows = db.execute(
            "SELECT event_id, 'nws' AS source, alert_type AS event_type,"
            "  headline, severity, county, state,"
            "  first_seen_at AS fetched_at, last_broadcast_at"
            " FROM nws_alerts"
            " WHERE expires_at IS NULL OR expires_at > CAST(strftime('%s','now') AS INTEGER)"
        ).fetchall()
        rows.extend(nws_rows)
    except Exception as e:
        log.warning("env/active nws query failed: %s", e)

    # Earthquakes: last 24h, magnitude >= 2.5
    try:
        quake_rows = db.execute(
            "SELECT event_id, 'usgs' AS source, 'earthquake' AS event_type,"
            "  ('M' || ROUND(magnitude,1) || ' \u2014 ' || place) AS headline,"
            "  CASE WHEN magnitude >= 5.0 THEN 'immediate'"
            "       WHEN magnitude >= 3.5 THEN 'priority'"
            "       ELSE 'routine' END AS severity,"
            "  lat, lon, NULL AS county, NULL AS state,"
            "  occurred_at AS fetched_at, last_broadcast_at"
            " FROM quake_events"
            " WHERE occurred_at > CAST(strftime('%s','now','-1 day') AS INTEGER)"
            "  AND magnitude >= 2.5"
        ).fetchall()
        rows.extend(quake_rows)
    except Exception as e:
        log.warning("env/active quake query failed: %s", e)

    # Convert to dicts, add is_local, sort by fetched_at desc
    result = []
    for row in rows:
        d = dict(row)
        d["is_local"] = False
        result.append(d)

    result.sort(key=lambda e: e.get("fetched_at") or 0, reverse=True)
    return result


@router.get("/env/swpc")
async def get_swpc_data(request: Request):
    """Get band conditions from DB."""
    db = get_db()

    try:
        row = db.execute(
            "SELECT ratings_json, source, sent_at, scheduled_for"
            " FROM band_conditions_broadcasts"
            " WHERE ratings_json IS NOT NULL"
            "  AND source != 'skipped_no_data'"
            " ORDER BY scheduled_for DESC"
            " LIMIT 1"
        ).fetchone()
    except Exception as e:
        log.warning("env/swpc band_conditions query failed: %s", e)
        return {"enabled": False}

    if not row:
        return {"enabled": False}

    try:
        ratings = json.loads(row["ratings_json"])
    except (json.JSONDecodeError, TypeError):
        return {"enabled": False}

    # Derive slot_label from scheduled_for hour in Mountain Time
    slot_label = _slot_label(row["scheduled_for"])

    return {
        "enabled": True,
        "ratings": ratings,
        "slot_label": slot_label,
        "sent_at": row["sent_at"],
        "source": row["source"],
    }


def _slot_label(epoch: int) -> str:
    """Derive slot label from scheduled_for epoch in Mountain Time."""
    from datetime import datetime, timezone
    try:
        from zoneinfo import ZoneInfo
        mt = ZoneInfo("America/Boise")
    except ImportError:
        # Fallback: UTC offset -6/-7 not critical for label
        mt = timezone.utc

    dt = datetime.fromtimestamp(epoch, tz=mt)
    hour = dt.hour

    if 6 <= hour < 14:
        return "Day Propagation"
    elif 14 <= hour < 22:
        return "Day Propagation"
    else:
        return "Night Propagation"


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
