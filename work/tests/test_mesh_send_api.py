"""API tests for the dashboard 'send test message' routes.

Uses a bare FastAPI() + TestClient with a hand-seeded ``app.state.connector``
(MagicMock-based fakes). The connector fakes mimic a CompositeTransport:
``transport_name=None`` and an explicit iterable ``children`` list.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

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
    # Wire async variants so routes can await them; side_effect preserves the
    # sync mock's return_value and call recording for existing assertions.
    c.send_advert_async = AsyncMock(side_effect=lambda: c.send_advert())
    c.req_telemetry_async = AsyncMock(side_effect=lambda cid: c.req_telemetry(cid))
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
    # Wire the async variant — routes now call send_message_async; side_effect
    # delegates to the sync mock so existing call_args assertions still pass.
    connector.send_message_async = AsyncMock(
        side_effect=lambda *a, **kw: connector.send_message(*a, **kw)
    )
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
    mc.contacts_synced_at.return_value = 1700000000.0
    connector = _composite([mc])
    client = _client(connector)

    r = client.get("/api/meshcore/contacts")
    assert r.status_code == 200
    assert r.json() == {
        "active": True,
        "contacts": _SAMPLE_ROSTER,
        "last_synced_at": 1700000000.0,
    }


def test_meshcore_contacts_no_meshcore():
    mt = _child("meshtastic", connected=True)
    connector = _composite([mt])
    client = _client(connector)

    r = client.get("/api/meshcore/contacts")
    assert r.status_code == 200
    assert r.json() == {"active": False, "contacts": [], "last_synced_at": None}


def test_meshcore_contacts_disconnected():
    mc = _child("meshcore", connected=False)
    connector = _composite([mc])
    client = _client(connector)

    r = client.get("/api/meshcore/contacts")
    assert r.status_code == 200
    assert r.json() == {"active": False, "contacts": [], "last_synced_at": None}


# ============================================================================
# POST /api/meshcore/contacts/refresh — full resync + reconcile
# ============================================================================

_REFRESH_STATS = {
    "before": 3, "after": 3, "added": 1, "removed": 1, "updated": 0,
    "added_keys": ["cc" * 32], "removed_keys": ["bb" * 32],
}


_CHANNEL_STATS = {"before": 4, "after": 5, "added": ["#new-chan"], "removed": []}


def test_meshcore_refresh_returns_contact_and_channel_stats():
    """The resync re-reads BOTH halves of the device view, and reports each."""
    mc = _child("meshcore", connected=True, known=["#aida", "#new-chan"])
    mc.resync.return_value = {"contacts": dict(_REFRESH_STATS), "channels": dict(_CHANNEL_STATS)}
    mc.get_contacts.return_value = list(_SAMPLE_ROSTER)
    mc.contacts_synced_at.return_value = 1700000000.0
    client = _client(_composite([mc]))

    r = client.post("/api/meshcore/contacts/refresh")

    assert r.status_code == 200
    body = r.json()
    assert body["stats"] == _REFRESH_STATS
    assert body["channel_stats"] == _CHANNEL_STATS
    assert body["contacts"] == _SAMPLE_ROSTER
    assert body["channels"] == ["#aida", "#new-chan"]
    assert body["last_synced_at"] == 1700000000.0
    mc.resync.assert_called_once()


def test_meshcore_refresh_conflict_when_disconnected():
    mc = _child("meshcore", connected=False)
    client = _client(_composite([mc]))

    r = client.post("/api/meshcore/contacts/refresh")

    assert r.status_code == 409
    mc.resync.assert_not_called()


def test_meshcore_refresh_surfaces_companion_failure():
    """A failed fetch must surface, not be reported as a successful resync."""
    mc = _child("meshcore", connected=True)
    mc.resync.side_effect = RuntimeError("contact refresh failed: timeout")
    client = _client(_composite([mc]))

    r = client.post("/api/meshcore/contacts/refresh")

    assert r.status_code == 502
    assert "timeout" in r.json()["detail"]


# ============================================================================
# DELETE /api/meshcore/contacts/{pubkey}
# ============================================================================

def test_meshcore_delete_contact_removes_and_returns_roster():
    mc = _child("meshcore", connected=True)
    mc.get_contacts.return_value = list(_SAMPLE_ROSTER)
    client = _client(_composite([mc]))

    r = client.delete(f"/api/meshcore/contacts/{'aa' * 32}")

    assert r.status_code == 200
    assert r.json() == {"active": True, "contacts": _SAMPLE_ROSTER}
    mc.remove_contact.assert_called_once_with("aa" * 32)


def test_meshcore_delete_contact_rejects_bad_key():
    mc = _child("meshcore", connected=True)
    mc.remove_contact.side_effect = ValueError("A full 64-character hex pubkey is required")
    client = _client(_composite([mc]))

    r = client.delete("/api/meshcore/contacts/aa11")

    assert r.status_code == 400


def test_meshcore_delete_contact_conflict_when_disconnected():
    mc = _child("meshcore", connected=False)
    client = _client(_composite([mc]))

    r = client.delete(f"/api/meshcore/contacts/{'aa' * 32}")

    assert r.status_code == 409
    mc.remove_contact.assert_not_called()


# ============================================================================
# GET /api/meshcore/contacts/export
# ============================================================================

def test_meshcore_export_returns_envelope_and_attachment():
    mc = _child("meshcore", connected=True)
    mc.export_roster.return_value = [{"name": "N", "pubkey": "aa" * 32, "type": 1}]
    mc.self_info.return_value = {
        "name": "AIDA", "pubkey": "a6" * 32,
        "conn_type": "serial", "target": "serial:/dev/meshcore-rak@115200",
    }
    mc.contacts_synced_at.return_value = 1700000000.0
    client = _client(_composite([mc]))

    r = client.get("/api/meshcore/contacts/export")

    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    body = r.json()
    assert body["format"] == "meshai.meshcore.roster"
    assert body["count"] == 1
    # The roster is only meaningful paired with the device it came from.
    assert body["device"]["conn_type"] == "serial"
    assert body["device"]["target"] == "serial:/dev/meshcore-rak@115200"


def test_meshcore_export_conflict_when_disconnected():
    mc = _child("meshcore", connected=False)
    client = _client(_composite([mc]))

    assert client.get("/api/meshcore/contacts/export").status_code == 409


# ============================================================================
# POST /api/meshcore/contacts/import
# ============================================================================

def test_meshcore_import_writes_each_record():
    mc = _child("meshcore", connected=True)
    client = _client(_composite([mc]))

    r = client.post("/api/meshcore/contacts/import", json={
        "contacts": [{"pubkey": "aa" * 32}, {"pubkey": "bb" * 32}],
    })

    assert r.status_code == 200
    assert r.json() == {"active": True, "imported": 2, "failed": 0, "errors": []}
    assert mc.import_contact.call_count == 2


def test_meshcore_import_collects_per_record_errors():
    """One bad record must not strand the batch with no report of what landed."""
    mc = _child("meshcore", connected=True)
    mc.import_contact.side_effect = [None, ValueError("bad pubkey")]
    client = _client(_composite([mc]))

    r = client.post("/api/meshcore/contacts/import", json={
        "contacts": [{"pubkey": "aa" * 32}, {"pubkey": "nope"}],
    })

    body = r.json()
    assert body["imported"] == 1
    assert body["failed"] == 1
    assert body["errors"][0]["pubkey"] == "nope"


def test_meshcore_import_rejects_empty_payload():
    mc = _child("meshcore", connected=True)
    client = _client(_composite([mc]))

    assert client.post("/api/meshcore/contacts/import", json={"contacts": []}).status_code == 400


# ============================================================================
# GET /api/meshcore/route-health
# ============================================================================

def _config_with_cells(cells, mc_enabled=True):
    return SimpleNamespace(
        notifications=SimpleNamespace(
            region_routes=SimpleNamespace(mt_enabled=True, mc_enabled=mc_enabled, cells=cells)
        )
    )


def _health_client(connector, config):
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.state.connector = connector
    app.state.config = config
    return TestClient(app)


def test_route_health_flags_dangling_room_cell():
    mc = _child("meshcore", connected=True, known=["#aida"])
    mc.get_contacts.return_value = []
    config = _config_with_cells({"fire": {"SC Idaho": {"mc": f"room:{'de' * 32}", "enabled": True}}})
    client = _health_client(_composite([mc]), config)

    body = client.get("/api/meshcore/route-health").json()

    assert body["active"] is True
    assert len(body["dangling"]) == 1
    assert body["dangling"][0]["reason"] == "room_not_found"
    assert body["dangling_enabled"] == 1


def test_route_health_clean_when_targets_resolve():
    mc = _child("meshcore", connected=True, known=["#aida"])
    mc.get_contacts.return_value = [
        {"pubkey": "aa" * 32, "name": "Room", "type": 3},
    ]
    config = _config_with_cells({
        "weather": {
            "SW Idaho": {"mc": "#aida", "enabled": True},
            "SC Idaho": {"mc": f"room:{'aa' * 32}", "enabled": True},
        }
    })
    client = _health_client(_composite([mc]), config)

    body = client.get("/api/meshcore/route-health").json()

    assert body["dangling"] == []
    assert body["checked"] == 2


def test_route_health_reports_name_collisions():
    mc = _child("meshcore", connected=True, known=[])
    mc.get_contacts.return_value = [
        {"pubkey": "aa" * 32, "name": "SC ID AIDA Alerts", "type": 3},
        {"pubkey": "bb" * 32, "name": "SC ID AIDA Alerts", "type": 3},
    ]
    client = _health_client(_composite([mc]), _config_with_cells({}))

    body = client.get("/api/meshcore/route-health").json()

    assert len(body["collisions"]) == 1
    assert body["collisions"][0]["count"] == 2


def test_route_health_inactive_when_disconnected():
    """A disconnected companion is not evidence that a route is broken."""
    mc = _child("meshcore", connected=False)
    config = _config_with_cells({"fire": {"SC Idaho": {"mc": "room:dead", "enabled": True}}})
    client = _health_client(_composite([mc]), config)

    body = client.get("/api/meshcore/route-health").json()

    assert body["active"] is False
    assert body["dangling"] == []


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


# ============================================================================
# POST /api/meshcore/advert
# ============================================================================


def test_meshcore_advert_connected_returns_sent_true():
    """POST /api/meshcore/advert → {sent: true} when meshcore is connected."""
    mc = _child("meshcore", connected=True)
    mc.send_advert.return_value = True
    connector = _composite([mc])
    client = _client(connector)

    r = client.post("/api/meshcore/advert")
    assert r.status_code == 200
    body = r.json()
    assert body["sent"] is True
    assert "detail" in body
    mc.send_advert.assert_called_once()


def test_meshcore_advert_connected_send_returns_false():
    """POST /api/meshcore/advert → {sent: false} when send_advert() returns False."""
    mc = _child("meshcore", connected=True)
    mc.send_advert.return_value = False
    connector = _composite([mc])
    client = _client(connector)

    r = client.post("/api/meshcore/advert")
    assert r.status_code == 200
    body = r.json()
    assert body["sent"] is False


def test_meshcore_advert_not_connected():
    """POST /api/meshcore/advert → {sent: false, detail: 'MeshCore not connected'}."""
    mc = _child("meshcore", connected=False)
    connector = _composite([mc])
    client = _client(connector)

    r = client.post("/api/meshcore/advert")
    assert r.status_code == 200
    body = r.json()
    assert body["sent"] is False
    assert body["detail"] == "MeshCore not connected"


def test_meshcore_advert_no_meshcore_child():
    """POST /api/meshcore/advert → {sent: false} when there is no meshcore transport."""
    mt = _child("meshtastic", connected=True)
    connector = _composite([mt])
    client = _client(connector)

    r = client.post("/api/meshcore/advert")
    assert r.status_code == 200
    body = r.json()
    assert body["sent"] is False
    assert body["detail"] == "MeshCore not connected"


# ============================================================================
# Config round-trip: meshcore_advert_interval_seconds
# ============================================================================


def test_connection_config_advert_interval_default():
    """meshcore_advert_interval_seconds defaults to 10800 (3 h)."""
    from meshai.config import ConnectionConfig
    cfg = ConnectionConfig()
    assert cfg.meshcore_advert_interval_seconds == 10800


def test_connection_config_advert_interval_zero():
    """meshcore_advert_interval_seconds = 0 disables periodic advert."""
    from meshai.config import ConnectionConfig
    cfg = ConnectionConfig(meshcore_advert_interval_seconds=0)
    assert cfg.meshcore_advert_interval_seconds == 0


def test_connection_config_advert_interval_round_trips_yaml():
    """meshcore_advert_interval_seconds survives YAML serialize → deserialize."""
    from meshai.config import ConnectionConfig, _dataclass_to_dict, _dict_to_dataclass
    cfg = ConnectionConfig(meshcore_advert_interval_seconds=7200)
    data = _dataclass_to_dict(cfg)
    assert data["meshcore_advert_interval_seconds"] == 7200
    cfg2 = _dict_to_dataclass(ConnectionConfig, data)
    assert cfg2.meshcore_advert_interval_seconds == 7200
