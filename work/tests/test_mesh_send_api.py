"""API tests for the dashboard 'send test message' routes.

Uses a bare FastAPI() + TestClient with a hand-seeded ``app.state.connector``
(MagicMock-based fakes). The connector fakes mimic a CompositeTransport:
``transport_name=None`` and an explicit iterable ``children`` list.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meshai.dashboard.api.mesh_send_routes import router


def _child(transport_name, connected=True, known=None):
    """Build a fake child transport (meshtastic/meshcore)."""
    c = MagicMock()
    c.transport_name = transport_name
    c.connected = connected
    if known is not None:
        c.known_channels.return_value = list(known)
    return c


def _composite(children, send_result=True):
    """Build a fake CompositeTransport connector wrapping *children*.

    A bare MagicMock's auto-attrs are truthy and ``children`` is not
    iterable, so set both explicitly.
    """
    connector = MagicMock()
    connector.transport_name = None
    connector.children = list(children)
    connector.send_message.return_value = send_result
    return connector


def _client(connector):
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.state.connector = connector
    return TestClient(app)


# ============================================================================
# POST /api/mesh/test-send — meshcore
# ============================================================================


def test_meshcore_success():
    mc = _child("meshcore", connected=True, known=["aida", "emergency"])
    connector = _composite([mc], send_result=True)
    client = _client(connector)

    r = client.post("/api/mesh/test-send", json={"transport": "meshcore", "channel": "aida"})
    assert r.status_code == 200
    body = r.json()
    assert body["sent"] is True
    assert "aida" in body["detail"]

    connector.send_message.assert_called_once()
    _, kwargs = connector.send_message.call_args
    assert kwargs["meshcore_channel"] == "aida"
    assert kwargs["transport"] == "meshcore"
    assert kwargs["destination"] is None


def test_meshcore_unknown_channel():
    mc = _child("meshcore", connected=True, known=["aida"])
    connector = _composite([mc], send_result=False)
    client = _client(connector)

    r = client.post("/api/mesh/test-send", json={"transport": "meshcore", "channel": "ghost"})
    assert r.status_code == 200
    body = r.json()
    assert body["sent"] is False
    assert "not on companion" in body["detail"]
    assert "aida" in body["detail"]


def test_meshcore_inactive():
    mt = _child("meshtastic", connected=True)
    connector = _composite([mt])
    client = _client(connector)

    r = client.post("/api/mesh/test-send", json={"transport": "meshcore", "channel": "aida"})
    assert r.status_code == 200
    body = r.json()
    assert body["sent"] is False
    assert body["detail"] == "meshcore not connected"


# ============================================================================
# POST /api/mesh/test-send — meshtastic
# ============================================================================


def test_meshtastic_success():
    mt = _child("meshtastic", connected=True)
    connector = _composite([mt], send_result=True)
    client = _client(connector)

    r = client.post("/api/mesh/test-send", json={"transport": "meshtastic", "channel": 0})
    assert r.status_code == 200
    body = r.json()
    assert body["sent"] is True

    connector.send_message.assert_called_once()
    _, kwargs = connector.send_message.call_args
    assert kwargs["channel"] == 0
    assert kwargs["transport"] == "meshtastic"


# ============================================================================
# Default text
# ============================================================================


def test_default_text_when_omitted():
    mc = _child("meshcore", connected=True, known=["aida"])
    connector = _composite([mc], send_result=True)
    client = _client(connector)

    r = client.post("/api/mesh/test-send", json={"transport": "meshcore", "channel": "aida"})
    assert r.status_code == 200
    assert r.json()["sent"] is True

    args, kwargs = connector.send_message.call_args
    sent_text = kwargs["text"] if "text" in kwargs else args[0]
    assert isinstance(sent_text, str)
    assert sent_text.startswith("🧪 MeshAI test")


# ============================================================================
# GET /api/meshcore/channels
# ============================================================================


def test_meshcore_channels_active():
    mc = _child("meshcore", connected=True, known=["aida", "emergency"])
    connector = _composite([mc])
    client = _client(connector)

    r = client.get("/api/meshcore/channels")
    assert r.status_code == 200
    assert r.json() == {"active": True, "channels": ["aida", "emergency"]}


def test_meshcore_channels_no_meshcore():
    mt = _child("meshtastic", connected=True)
    connector = _composite([mt])
    client = _client(connector)

    r = client.get("/api/meshcore/channels")
    assert r.status_code == 200
    assert r.json() == {"active": False, "channels": []}


# ============================================================================
# GET /api/meshcore/contacts
# ============================================================================


_SAMPLE_ROSTER = [
    {
        "name": "Repeater One",
        "pubkey": "aa11deadbeef",
        "type": "repeater",
        "last_advert": 1000,
        "lat": 43.6,
        "lon": -116.2,
        "out_path_len": 2,
    },
    {
        "name": "Sensor Two",
        "pubkey": "bb22cafef00d",
        "type": "sensor",
        "last_advert": 2000,
        "lat": None,
        "lon": None,
        "out_path_len": -1,
    },
]


def test_meshcore_contacts_active():
    mc = _child("meshcore", connected=True)
    mc.get_contacts.return_value = list(_SAMPLE_ROSTER)
    connector = _composite([mc])
    client = _client(connector)

    r = client.get("/api/meshcore/contacts")
    assert r.status_code == 200
    assert r.json() == {"active": True, "contacts": _SAMPLE_ROSTER}


def test_meshcore_contacts_no_meshcore():
    mt = _child("meshtastic", connected=True)
    connector = _composite([mt])
    client = _client(connector)

    r = client.get("/api/meshcore/contacts")
    assert r.status_code == 200
    assert r.json() == {"active": False, "contacts": []}


def test_meshcore_contacts_disconnected():
    mc = _child("meshcore", connected=False)
    connector = _composite([mc])
    client = _client(connector)

    r = client.get("/api/meshcore/contacts")
    assert r.status_code == 200
    assert r.json() == {"active": False, "contacts": []}


# ============================================================================
# GET /api/meshcore/self
# ============================================================================


def test_meshcore_self_active():
    mc = _child("meshcore", connected=True)
    mc.self_info.return_value = {
        "name": "AIDA",
        "pubkey": "deadbeef1234",
        "connected": True,
        "host": "100.64.0.9",
        "port": 5050,
        "channel_count": 2,
    }
    connector = _composite([mc])
    client = _client(connector)

    r = client.get("/api/meshcore/self")
    assert r.status_code == 200
    body = r.json()
    assert body["connected"] is True
    assert body["pubkey"] == "deadbeef1234"
    assert body["name"] == "AIDA"
    assert body["channel_count"] == 2


def test_meshcore_self_no_meshcore():
    mt = _child("meshtastic", connected=True)
    connector = _composite([mt])
    client = _client(connector)

    r = client.get("/api/meshcore/self")
    assert r.status_code == 200
    assert r.json() == {"connected": False}


def test_meshcore_self_disconnected():
    mc = _child("meshcore", connected=False)
    connector = _composite([mc])
    client = _client(connector)

    r = client.get("/api/meshcore/self")
    assert r.status_code == 200
    assert r.json() == {"connected": False}
