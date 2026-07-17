"""Dashboard 'send test message' API routes (meshtastic + meshcore)."""

import logging
from datetime import datetime, timezone
from typing import Optional, Union

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from meshai import secrets_store
from meshai.meshcore_roster import check_route_health, find_name_collisions

logger = logging.getLogger(__name__)
router = APIRouter(tags=["mesh-send"])

# Roster export envelope: identifies the file on the way back in so an import
# can reject something that was never a meshai roster.
_ROSTER_EXPORT_FORMAT = "meshai.meshcore.roster"
_ROSTER_EXPORT_VERSION = 1


def _find_child(connector, name: str):
    """Find a child transport by transport_name — handles bare transport or CompositeTransport."""
    if connector is None:
        return None
    if getattr(connector, "transport_name", None) == name:
        return connector
    children = getattr(connector, "children", None)
    if children:
        for c in children:
            if getattr(c, "transport_name", None) == name:
                return c
    return None


@router.get("/meshcore/channels")
async def meshcore_channels(request: Request):
    """List enumerated MeshCore channel names if a meshcore transport is connected."""
    connector = getattr(request.app.state, "connector", None)
    mc = _find_child(connector, "meshcore")
    if mc is not None and getattr(mc, "connected", False):
        try:
            names = list(mc.known_channels())
        except Exception:
            names = []
        return {"active": True, "channels": names}
    return {"active": False, "channels": []}


@router.get("/meshcore/channels/detail")
async def meshcore_channels_detail(request: Request):
    """Enumerated MeshCore channels with on-air hash, if connected.

    Returns {"active": bool, "channels": [{"name": str, "hash": str|null, "key": str|null}]}.
    Routes by channel NAME (no slot/index), so no index is exposed here.
    """
    connector = getattr(request.app.state, "connector", None)
    mc = _find_child(connector, "meshcore")
    if mc is not None and getattr(mc, "connected", False):
        try:
            channels = list(mc.channel_details())
        except Exception:
            channels = []
        return {"active": True, "channels": channels}
    return {"active": False, "channels": []}


class AddChannelRequest(BaseModel):
    name: str
    key: Optional[str] = None


@router.post("/meshcore/channels")
async def meshcore_add_channel(request: Request, body: AddChannelRequest):
    """Provision a new MeshCore channel (name + PSK) onto the companion.

    Body: {"name": str, "key"?: str}. ``key`` is a 32-char hex string (16
    bytes) — omit it (or leave empty) for a public channel, which requires
    ``name`` to start with "#" so the companion derives the PSK from the
    name. Returns the refreshed channel list on success.
    """
    connector = getattr(request.app.state, "connector", None)
    mc = _find_child(connector, "meshcore")
    if mc is None or not getattr(mc, "connected", False):
        raise HTTPException(status_code=409, detail="MeshCore not connected")

    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Channel name must not be empty")

    key = (body.key or "").strip()
    secret: Optional[bytes] = None
    if key:
        try:
            secret = bytes.fromhex(key)
        except ValueError:
            raise HTTPException(status_code=400, detail="Channel key must be valid hex")
        if len(secret) != 16:
            raise HTTPException(
                status_code=400,
                detail="Channel key must be exactly 32 hex characters (16 bytes)",
            )
    elif not name.startswith("#"):
        raise HTTPException(
            status_code=400,
            detail="A channel key is required unless the name starts with '#' (public)",
        )

    try:
        mc.add_channel(name, secret)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        logger.error("dashboard: meshcore add_channel error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))

    logger.info("dashboard: meshcore channel '%s' added", name)
    return {"active": True, "channels": list(mc.known_channels())}


@router.delete("/meshcore/channels/{name}")
async def meshcore_remove_channel(request: Request, name: str):
    """Remove a provisioned MeshCore channel from the companion by name.

    Returns the refreshed channel list on success; 404 if the name is not
    on the companion's channel table.
    """
    connector = getattr(request.app.state, "connector", None)
    mc = _find_child(connector, "meshcore")
    if mc is None or not getattr(mc, "connected", False):
        raise HTTPException(status_code=409, detail="MeshCore not connected")

    try:
        mc.remove_channel(name)
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.error("dashboard: meshcore remove_channel error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))

    logger.info("dashboard: meshcore channel '%s' removed", name)
    return {"active": True, "channels": list(mc.known_channels())}


@router.get("/meshcore/rooms")
async def meshcore_rooms(request: Request):
    """List MeshCore room servers if a meshcore transport is connected.

    Returns {"active": bool, "rooms": [{"name", "pubkey", "prefix",
    "path_established"}]}. Parallels /meshcore/channels — the frontend uses
    this to offer room targets for the ``room:<pubkey>`` routing cell.
    """
    connector = getattr(request.app.state, "connector", None)
    mc = _find_child(connector, "meshcore")
    if mc is not None and getattr(mc, "connected", False):
        try:
            rooms = list(mc.get_rooms())
        except Exception:
            rooms = []
        for r in rooms:
            try:
                r["password_set"] = secrets_store.room_password_is_set(r.get("pubkey") or "")
            except Exception:
                r["password_set"] = False
        return {"active": True, "rooms": rooms}
    return {"active": False, "rooms": []}


@router.get("/meshcore/contacts")
async def meshcore_contacts(request: Request):
    """Roster of known MeshCore contacts if a meshcore transport is connected.

    ``last_synced_at`` (epoch seconds, or null) is when the roster was last
    pulled from the companion, so the UI can present it as a snapshot with a
    known age rather than implying it is live.
    """
    connector = getattr(request.app.state, "connector", None)
    mc = _find_child(connector, "meshcore")
    if mc is not None and getattr(mc, "connected", False):
        try:
            contacts = list(mc.get_contacts())
        except Exception:
            contacts = []
        try:
            last_synced_at = mc.contacts_synced_at()
        except Exception:
            last_synced_at = None
        return {"active": True, "contacts": contacts, "last_synced_at": last_synced_at}
    return {"active": False, "contacts": [], "last_synced_at": None}


@router.post("/meshcore/contacts/refresh")
async def meshcore_refresh_contacts(request: Request):
    """Re-read the companion's device view — contacts AND channels.

    meshai builds its picture of the device at connect and never re-reads it,
    so a contact removed (or a channel provisioned) on the radio afterwards is
    invisible until a restart. This is the resync path.

    Unlike the passive roster read, the contact reconcile DROPS entries the
    companion no longer has (the lib's own fetch only ever merges). Returns the
    reconcile stats, the channel delta, and the refreshed roster + channel list.
    """
    connector = getattr(request.app.state, "connector", None)
    mc = _find_child(connector, "meshcore")
    if mc is None or not getattr(mc, "connected", False):
        raise HTTPException(status_code=409, detail="MeshCore not connected")

    try:
        result = mc.resync()
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        logger.error("dashboard: meshcore resync error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))

    stats = result.get("contacts", {})
    channel_stats = result.get("channels", {})

    try:
        contacts = list(mc.get_contacts())
    except Exception:
        contacts = []
    try:
        channels = list(mc.known_channels())
    except Exception:
        channels = []
    try:
        last_synced_at = mc.contacts_synced_at()
    except Exception:
        last_synced_at = None

    logger.info(
        "dashboard: meshcore resync — contacts +%d/-%d (now %d), channels +%d/-%d",
        stats.get("added", 0), stats.get("removed", 0), stats.get("after", 0),
        len(channel_stats.get("added", [])), len(channel_stats.get("removed", [])),
    )
    return {
        "active": True,
        "stats": stats,
        "channel_stats": channel_stats,
        "contacts": contacts,
        "channels": channels,
        "last_synced_at": last_synced_at,
    }


@router.get("/meshcore/contacts/export")
async def meshcore_export_contacts(request: Request):
    """Download the roster as JSON.

    Records carry the full lib field set (not the display projection), so an
    export can be re-imported onto a replacement companion.
    """
    connector = getattr(request.app.state, "connector", None)
    mc = _find_child(connector, "meshcore")
    if mc is None or not getattr(mc, "connected", False):
        raise HTTPException(status_code=409, detail="MeshCore not connected")

    try:
        records = mc.export_roster()
    except Exception as exc:
        logger.error("dashboard: meshcore export_roster error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))

    try:
        info = mc.self_info()
    except Exception:
        info = {}
    try:
        last_synced_at = mc.contacts_synced_at()
    except Exception:
        last_synced_at = None

    payload = {
        "format": _ROSTER_EXPORT_FORMAT,
        "version": _ROSTER_EXPORT_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "last_synced_at": last_synced_at,
        # Which device this roster came off — a roster is only meaningful
        # paired with the companion it was read from.
        "device": {
            "name": info.get("name"),
            "pubkey": info.get("pubkey"),
            "conn_type": info.get("conn_type"),
            "target": info.get("target"),
        },
        "count": len(records),
        "contacts": records,
    }
    filename = f"meshcore-roster-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
    return JSONResponse(
        content=payload,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


class ImportContactsRequest(BaseModel):
    contacts: list[dict]


@router.post("/meshcore/contacts/import")
async def meshcore_import_contacts(request: Request, body: ImportContactsRequest):
    """Write exported roster records onto the companion.

    Intended for migrating a roster to a replacement companion rather than
    waiting to rediscover every node by advert. Additive and idempotent: each
    record is an upsert, nothing is removed, and no mesh traffic is generated.

    Per-record failures are collected rather than aborting the batch, so one bad
    record cannot strand a partial import with no report of what landed.
    """
    connector = getattr(request.app.state, "connector", None)
    mc = _find_child(connector, "meshcore")
    if mc is None or not getattr(mc, "connected", False):
        raise HTTPException(status_code=409, detail="MeshCore not connected")

    records = body.contacts or []
    if not records:
        raise HTTPException(status_code=400, detail="No contacts supplied")

    imported = 0
    errors: list[dict] = []
    for record in records:
        try:
            mc.import_contact(record)
            imported += 1
        except (ValueError, RuntimeError) as exc:
            errors.append({"pubkey": record.get("pubkey"), "detail": str(exc)})
        except Exception as exc:
            logger.error("dashboard: meshcore import_contact error: %s", exc)
            errors.append({"pubkey": record.get("pubkey"), "detail": str(exc)})

    logger.info(
        "dashboard: meshcore roster import — %d/%d written, %d failed",
        imported, len(records), len(errors),
    )
    return {"active": True, "imported": imported, "failed": len(errors), "errors": errors}


@router.delete("/meshcore/contacts/{pubkey}")
async def meshcore_remove_contact(request: Request, pubkey: str):
    """Remove a contact from the companion by full pubkey.

    Requires the FULL 64-hex key — a prefix could match the wrong node, and a
    wrongly-deleted contact is unrecoverable without rediscovery.
    """
    connector = getattr(request.app.state, "connector", None)
    mc = _find_child(connector, "meshcore")
    if mc is None or not getattr(mc, "connected", False):
        raise HTTPException(status_code=409, detail="MeshCore not connected")

    try:
        mc.remove_contact(pubkey)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        logger.error("dashboard: meshcore remove_contact error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))

    try:
        contacts = list(mc.get_contacts())
    except Exception:
        contacts = []
    logger.info("dashboard: meshcore contact %s removed", pubkey)
    return {"active": True, "contacts": contacts}


@router.get("/meshcore/route-health")
async def meshcore_route_health(request: Request):
    """Flag region-routing cells whose MeshCore target no longer exists.

    Preventive: a cell pointing at a room pubkey or channel the companion does
    not have will fail silently at send time — the alert is simply never
    delivered, with nothing surfaced to the operator. Resolving every cell
    up-front turns that silence into a visible warning. Read-only; sends nothing.

    Also reports same-name/different-pubkey roster collisions, which are what
    make a name-based picker ambiguous in the first place.

    Returns {active, dangling, dangling_enabled, collisions, checked, mc_enabled}.
    ``active: false`` (with empty results) when MeshCore is not connected — an
    unreachable companion is not evidence that a route is broken.
    """
    connector = getattr(request.app.state, "connector", None)
    mc = _find_child(connector, "meshcore")
    if mc is None or not getattr(mc, "connected", False):
        return {
            "active": False,
            "dangling": [],
            "dangling_enabled": 0,
            "collisions": [],
            "checked": 0,
            "mc_enabled": False,
        }

    config = getattr(request.app.state, "config", None)
    rr = getattr(getattr(config, "notifications", None), "region_routes", None)
    cells = getattr(rr, "cells", None) or {}
    mc_enabled = bool(getattr(rr, "mc_enabled", False))

    try:
        contacts = list(mc.get_contacts())
    except Exception:
        contacts = []
    try:
        channels = list(mc.known_channels())
    except Exception:
        channels = []

    dangling = check_route_health(cells, channels, contacts)
    collisions = find_name_collisions(contacts)
    checked = sum(
        1
        for regions in cells.values()
        if isinstance(regions, dict)
        for cell in regions.values()
        if isinstance(cell, dict) and cell.get("mc")
    )

    return {
        "active": True,
        "dangling": dangling,
        "dangling_enabled": sum(1 for d in dangling if d.get("enabled")),
        "collisions": collisions,
        "checked": checked,
        "mc_enabled": mc_enabled,
    }


@router.get("/meshcore/self")
async def meshcore_self(request: Request):
    """Companion self/connection status if a meshcore transport is connected."""
    connector = getattr(request.app.state, "connector", None)
    mc = _find_child(connector, "meshcore")
    if mc is not None and getattr(mc, "connected", False):
        try:
            return mc.self_info()
        except Exception:
            return {"connected": False}
    return {"connected": False}


@router.post("/meshcore/advert")
async def meshcore_send_advert(request: Request):
    """Broadcast a signed self-advertisement (flood=True) via MeshCore.

    Returns {sent: bool, detail: str}.  Returns {sent: false} when MeshCore
    is not connected.
    """
    connector = getattr(request.app.state, "connector", None)
    mc = _find_child(connector, "meshcore")
    if mc is None or not getattr(mc, "connected", False):
        return {"sent": False, "detail": "MeshCore not connected"}
    try:
        send_fn = getattr(mc, "send_advert_async", None)
        if send_fn is not None:
            ok = bool(await send_fn())
        else:
            ok = bool(mc.send_advert())
        detail = "Self-advert sent" if ok else "send_advert returned False"
        logger.info("dashboard: meshcore manual advert sent=%s", ok)
        return {"sent": ok, "detail": detail}
    except Exception as exc:
        logger.error("dashboard: meshcore advert error: %s", exc)
        return {"sent": False, "detail": str(exc)}


@router.get("/meshcore/telemetry")
async def meshcore_telemetry(request: Request):
    """Cached telemetry readings for auto-polled MeshCore contacts.

    Returns {active: bool, entries: list}.  entries is [] (and active False)
    when MeshCore is not connected.
    """
    connector = getattr(request.app.state, "connector", None)
    mc = _find_child(connector, "meshcore")
    if mc is not None and getattr(mc, "connected", False):
        try:
            entries = list(mc.get_telemetry_cache())
        except Exception:
            entries = []
        return {"active": True, "entries": entries}
    return {"active": False, "entries": []}


@router.post("/meshcore/telemetry/poll")
async def meshcore_telemetry_poll(request: Request):
    """On-demand ('Poll now') telemetry request for a single MeshCore contact.

    Body: {"contact": "<name-or-pubkey>"}.  Returns {available, contact, data}
    on success, or {available: False, detail: ...} when unavailable/inactive.
    """
    connector = getattr(request.app.state, "connector", None)
    mc = _find_child(connector, "meshcore")
    if mc is None or not getattr(mc, "connected", False):
        return {"available": False, "detail": "MeshCore not connected"}
    try:
        body = await request.json()
    except Exception:
        body = {}
    contact = (body or {}).get("contact")
    if not contact:
        return {"available": False, "detail": "Missing 'contact'"}
    try:
        poll_fn = getattr(mc, "req_telemetry_async", None)
        if poll_fn is not None:
            data = await poll_fn(contact)
        else:
            data = mc.req_telemetry(contact)
        if data is None:
            return {"available": False, "contact": contact, "detail": "No telemetry response"}
        return {"available": True, "contact": contact, "data": data}
    except Exception as exc:
        logger.error("dashboard: meshcore telemetry poll error: %s", exc)
        return {"available": False, "contact": contact, "detail": str(exc)}


class TestSendRequest(BaseModel):
    transport: str
    channel: Union[str, int]
    text: Optional[str] = None


@router.post("/mesh/test-send")
async def test_send(request: Request, body: TestSendRequest):
    """Send a one-off test message over the requested transport/channel."""
    connector = getattr(request.app.state, "connector", None)
    text = (body.text or "").strip() or f"🧪 MeshAI test — {datetime.now().strftime('%H:%M')}"

    if body.transport == "meshtastic":
        child = _find_child(connector, "meshtastic")
        if child is None or not getattr(child, "connected", False):
            result = {"sent": False, "detail": "meshtastic not connected"}
        else:
            try:
                idx = int(body.channel)
            except (ValueError, TypeError):
                result = {"sent": False, "detail": f"invalid meshtastic channel index: {body.channel!r}"}
            else:
                ok = bool(await connector.send_message_async(text, destination=None, channel=idx, transport="meshtastic"))
                result = {"sent": ok, "detail": f"sent to meshtastic channel {idx}" if ok else "send returned False"}
    elif body.transport == "meshcore":
        child = _find_child(connector, "meshcore")
        if child is None or not getattr(child, "connected", False):
            result = {"sent": False, "detail": "meshcore not connected"}
        else:
            name = str(body.channel)
            ok = bool(await connector.send_message_async(text, destination=None, meshcore_channel=name, transport="meshcore"))
            if ok:
                result = {"sent": True, "detail": f"sent to '{name}'"}
            else:
                known = list(child.known_channels())  # re-checked after send (lazy re-enum may have run)
                if name not in known:
                    result = {"sent": False, "detail": f"channel '{name}' not on companion — known: {known}"}
                else:
                    result = {"sent": False, "detail": f"send failed for channel '{name}'"}
    else:
        result = {"sent": False, "detail": f"unknown transport: {body.transport!r}"}

    logger.info("dashboard: test-send transport=%s channel=%s sent=%s", body.transport, body.channel, result["sent"])
    return result
