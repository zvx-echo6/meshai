"""Dashboard 'send test message' API routes (meshtastic + meshcore)."""

import logging
from datetime import datetime
from typing import Optional, Union

from fastapi import APIRouter, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter(tags=["mesh-send"])


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


@router.get("/meshcore/contacts")
async def meshcore_contacts(request: Request):
    """Roster of known MeshCore contacts if a meshcore transport is connected."""
    connector = getattr(request.app.state, "connector", None)
    mc = _find_child(connector, "meshcore")
    if mc is not None and getattr(mc, "connected", False):
        try:
            contacts = list(mc.get_contacts())
        except Exception:
            contacts = []
        return {"active": True, "contacts": contacts}
    return {"active": False, "contacts": []}


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
        ok = bool(mc.send_advert())
        detail = "Self-advert sent" if ok else "send_advert returned False"
        logger.info("dashboard: meshcore manual advert sent=%s", ok)
        return {"sent": ok, "detail": detail}
    except Exception as exc:
        logger.error("dashboard: meshcore advert error: %s", exc)
        return {"sent": False, "detail": str(exc)}


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
                ok = bool(connector.send_message(text, destination=None, channel=idx, transport="meshtastic"))
                result = {"sent": ok, "detail": f"sent to meshtastic channel {idx}" if ok else "send returned False"}
    elif body.transport == "meshcore":
        child = _find_child(connector, "meshcore")
        if child is None or not getattr(child, "connected", False):
            result = {"sent": False, "detail": "meshcore not connected"}
        else:
            name = str(body.channel)
            ok = bool(connector.send_message(text, destination=None, meshcore_channel=name, transport="meshcore"))
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
