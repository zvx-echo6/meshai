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
