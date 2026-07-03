"""Tests for MeshCore telemetry auto-poll (backend).

Fully mocked — no real socket, no meshcore lib required.  A minimal fake
``meshcore`` module is injected into sys.modules before the production code's
lazy import triggers, mirroring test_meshcore_transport.py.
"""

import asyncio
import sys
import threading
import types
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fake meshcore module (registered before production imports)
# ---------------------------------------------------------------------------

def _build_fake_meshcore():
    mod = types.ModuleType("meshcore")

    class EventType:
        CONTACT_MSG_RECV = "CONTACT_MSG_RECV"
        CHANNEL_MSG_RECV = "CHANNEL_MSG_RECV"
        DISCONNECTED = "DISCONNECTED"
        CONNECTED = "CONNECTED"

    mod.EventType = EventType

    class _FakeMeshCore:
        self_info = {"public_key": "aabbccdd1122", "name": "FakeNode"}
        contacts = {}

        async def start_auto_message_fetching(self):
            pass

        async def stop_auto_message_fetching(self):
            pass

        async def disconnect(self):
            pass

        def subscribe(self, event_type, callback):
            pass

        def get_contact_by_key_prefix(self, prefix):
            return None

        @classmethod
        async def create_tcp(cls, host, port,
                             auto_reconnect=True, max_reconnect_attempts=5):
            return cls()

        class commands:
            @staticmethod
            async def req_telemetry_sync(contact, timeout=0, min_timeout=0):
                return None

    mod.MeshCore = _FakeMeshCore
    return mod


sys.modules.setdefault("meshcore", _build_fake_meshcore())


# ---------------------------------------------------------------------------
# Production imports
# ---------------------------------------------------------------------------

from meshai.config import (                                    # noqa: E402
    ConnectionConfig, _dataclass_to_dict, _dict_to_dataclass,
)
from meshai.transport.meshcore_transport import (              # noqa: E402
    MeshCoreTransport,
    _TELEMETRY_MAX_FAILURES,
    _TELEMETRY_MIN_INTERVAL_SECONDS,
)
from meshai.dashboard.api.mesh_send_routes import router       # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mc_config(**overrides):
    cfg = ConnectionConfig(meshcore_host="127.0.0.1", meshcore_port=5050)
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _transport_with_mock_mc(mc_overrides=None, **cfg_overrides):
    """MeshCoreTransport with a MagicMock _mc + a live dedicated loop thread."""
    cfg = _mc_config(**cfg_overrides)
    t = MeshCoreTransport(cfg)

    mc = MagicMock()
    mc.get_contact_by_key_prefix.return_value = None
    mc.get_contact_by_name.return_value = None
    if mc_overrides:
        for k, v in mc_overrides.items():
            setattr(mc, k, v)

    t._mc = mc
    t._connected = True

    loop = asyncio.new_event_loop()
    t._loop = loop
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    t._loop_thread = thread
    return t, mc, loop


def _cleanup(t):
    try:
        if t._loop and t._loop.is_running():
            t._loop.call_soon_threadsafe(t._loop.stop)
        if t._loop_thread and t._loop_thread.is_alive():
            t._loop_thread.join(timeout=2.0)
    except Exception:
        pass


# A sample telemetry lpp list: voltage, temperature, humidity, battery %, and
# an unknown id (200) that must fall through to lpp_200.
_SAMPLE_LPP = [
    {"channel": 0, "type": 116, "value": 3.98},
    {"channel": 1, "type": 103, "value": 21.5},
    {"channel": 2, "type": 104, "value": 44},
    {"channel": 3, "type": 120, "value": 87},
    {"channel": 4, "type": 200, "value": 999},
]


# ---------------------------------------------------------------------------
# 1. _decode_lpp
# ---------------------------------------------------------------------------

class TestDecodeLpp:
    def test_maps_known_ids_and_preserves_raw(self):
        data = MeshCoreTransport._decode_lpp(_SAMPLE_LPP)
        assert data["voltage"] == 3.98
        assert data["temperature"] == 21.5
        assert data["humidity"] == 44
        assert data["battery_pct"] == 87
        # Unknown id → lpp_<id>
        assert data["lpp_200"] == 999
        # raw is always the original list
        assert data["raw"] == _SAMPLE_LPP

    def test_empty_list_yields_only_raw(self):
        data = MeshCoreTransport._decode_lpp([])
        assert data == {"raw": []}

    def test_none_yields_raw_none(self):
        data = MeshCoreTransport._decode_lpp(None)
        assert data["raw"] is None


# ---------------------------------------------------------------------------
# 2. req_telemetry (sync wrapper, bridged)
# ---------------------------------------------------------------------------

class TestReqTelemetry:
    def test_returns_decoded_dict_on_lpp(self):
        t, mc, _ = _transport_with_mock_mc()
        try:
            mc.get_contact_by_key_prefix.return_value = {"adv_name": "Sensor"}
            mc.commands.req_telemetry_sync = AsyncMock(return_value=_SAMPLE_LPP)
            data = t.req_telemetry("aabbcc")
            assert data is not None
            assert data["voltage"] == 3.98
            assert data["temperature"] == 21.5
            mc.commands.req_telemetry_sync.assert_awaited_once()
            # min_timeout is passed so a node gets a reasonable window.
            _, kwargs = mc.commands.req_telemetry_sync.call_args
            assert kwargs.get("min_timeout") == 5
        finally:
            _cleanup(t)

    def test_returns_none_on_timeout(self):
        t, mc, _ = _transport_with_mock_mc()
        try:
            mc.get_contact_by_key_prefix.return_value = {"adv_name": "Sensor"}
            mc.commands.req_telemetry_sync = AsyncMock(return_value=None)
            assert t.req_telemetry("aabbcc") is None
        finally:
            _cleanup(t)

    def test_returns_none_when_unresolved(self):
        t, mc, _ = _transport_with_mock_mc()
        try:
            mc.get_contact_by_key_prefix.return_value = None
            mc.get_contact_by_name.return_value = None
            mc.commands.req_telemetry_sync = AsyncMock(return_value=_SAMPLE_LPP)
            assert t.req_telemetry("ghost") is None
            mc.commands.req_telemetry_sync.assert_not_awaited()
        finally:
            _cleanup(t)

    def test_resolves_by_name_when_prefix_misses(self):
        t, mc, _ = _transport_with_mock_mc()
        try:
            mc.get_contact_by_key_prefix.return_value = None
            mc.get_contact_by_name.return_value = {"adv_name": "ByName"}
            mc.commands.req_telemetry_sync = AsyncMock(return_value=_SAMPLE_LPP)
            data = t.req_telemetry("ByName")
            assert data is not None and data["humidity"] == 44
        finally:
            _cleanup(t)

    def test_returns_none_when_not_connected(self):
        t = MeshCoreTransport(_mc_config())  # _mc None, no loop
        assert t.req_telemetry("aabbcc") is None


# ---------------------------------------------------------------------------
# 3. Poller bookkeeping — via _req_telemetry_async on the loop
# ---------------------------------------------------------------------------

class TestPollerBookkeeping:
    def _run(self, t, coro):
        return t._run_coro(coro, timeout=5.0)

    def test_caches_reading_for_contact(self):
        t, mc, _ = _transport_with_mock_mc()
        try:
            mc.get_contact_by_key_prefix.return_value = {"adv_name": "Sensor"}
            mc.commands.req_telemetry_sync = AsyncMock(return_value=_SAMPLE_LPP)
            self._run(t, t._req_telemetry_async("nodeA"))
            cache = {e["contact"]: e for e in t.get_telemetry_cache()}
            assert "nodeA" in cache
            assert cache["nodeA"]["available"] is True
            assert cache["nodeA"]["data"]["voltage"] == 3.98
            assert cache["nodeA"]["polled_at"] is not None
        finally:
            _cleanup(t)

    def test_marks_unavailable_after_max_failures(self):
        t, mc, _ = _transport_with_mock_mc()
        try:
            mc.get_contact_by_key_prefix.return_value = {"adv_name": "Sensor"}
            mc.commands.req_telemetry_sync = AsyncMock(return_value=None)
            for _ in range(_TELEMETRY_MAX_FAILURES):
                self._run(t, t._req_telemetry_async("nodeB"))
            cache = {e["contact"]: e for e in t.get_telemetry_cache()}
            assert cache["nodeB"]["available"] is False
            assert t._telemetry_failures["nodeB"] >= _TELEMETRY_MAX_FAILURES
        finally:
            _cleanup(t)

    def test_stays_available_before_max_failures(self):
        t, mc, _ = _transport_with_mock_mc()
        try:
            mc.get_contact_by_key_prefix.return_value = {"adv_name": "Sensor"}
            mc.commands.req_telemetry_sync = AsyncMock(return_value=None)
            # One miss (< max) — still available.
            self._run(t, t._req_telemetry_async("nodeC"))
            cache = {e["contact"]: e for e in t.get_telemetry_cache()}
            assert cache["nodeC"]["available"] is True
        finally:
            _cleanup(t)

    def test_success_after_failures_flips_back_available(self):
        t, mc, _ = _transport_with_mock_mc()
        try:
            mc.get_contact_by_key_prefix.return_value = {"adv_name": "Sensor"}
            # Drive it unavailable.
            mc.commands.req_telemetry_sync = AsyncMock(return_value=None)
            for _ in range(_TELEMETRY_MAX_FAILURES):
                self._run(t, t._req_telemetry_async("nodeD"))
            cache = {e["contact"]: e for e in t.get_telemetry_cache()}
            assert cache["nodeD"]["available"] is False
            # A later success un-sticks it and resets the counter.
            mc.commands.req_telemetry_sync = AsyncMock(return_value=_SAMPLE_LPP)
            self._run(t, t._req_telemetry_async("nodeD"))
            cache = {e["contact"]: e for e in t.get_telemetry_cache()}
            assert cache["nodeD"]["available"] is True
            assert cache["nodeD"]["data"]["voltage"] == 3.98
            assert t._telemetry_failures["nodeD"] == 0
        finally:
            _cleanup(t)

    def test_manual_poll_unsticks_unavailable(self):
        """The sync req_telemetry wrapper shares bookkeeping: a manual poll
        after failures flips availability back on."""
        t, mc, _ = _transport_with_mock_mc()
        try:
            mc.get_contact_by_key_prefix.return_value = {"adv_name": "Sensor"}
            mc.commands.req_telemetry_sync = AsyncMock(return_value=None)
            for _ in range(_TELEMETRY_MAX_FAILURES):
                t.req_telemetry("nodeE")
            cache = {e["contact"]: e for e in t.get_telemetry_cache()}
            assert cache["nodeE"]["available"] is False
            mc.commands.req_telemetry_sync = AsyncMock(return_value=_SAMPLE_LPP)
            assert t.req_telemetry("nodeE") is not None
            cache = {e["contact"]: e for e in t.get_telemetry_cache()}
            assert cache["nodeE"]["available"] is True
        finally:
            _cleanup(t)


# ---------------------------------------------------------------------------
# 4. Effective interval (min-floor airtime guard)
# ---------------------------------------------------------------------------

class TestEffectiveInterval:
    def test_below_floor_clamped_up(self):
        t = MeshCoreTransport(_mc_config(meshcore_telemetry_interval_seconds=60))
        assert t._effective_telemetry_interval() == _TELEMETRY_MIN_INTERVAL_SECONDS
        assert t._effective_telemetry_interval() == 300

    def test_above_floor_preserved(self):
        t = MeshCoreTransport(_mc_config(meshcore_telemetry_interval_seconds=1800))
        assert t._effective_telemetry_interval() == 1800

    def test_zero_disables(self):
        t = MeshCoreTransport(_mc_config(meshcore_telemetry_interval_seconds=0))
        assert t._effective_telemetry_interval() is None


# ---------------------------------------------------------------------------
# 5. Poller scheduler lifecycle
# ---------------------------------------------------------------------------

class TestPollerScheduler:
    def test_task_armed_when_interval_nonzero(self):
        import time
        cfg = _mc_config(meshcore_telemetry_interval_seconds=1800,
                         meshcore_advert_interval_seconds=0)
        t = MeshCoreTransport(cfg)
        try:
            t.connect()
            time.sleep(0.1)
            assert t._telemetry_task is not None
        finally:
            t.disconnect()

    def test_task_not_armed_when_interval_zero(self):
        import time
        cfg = _mc_config(meshcore_telemetry_interval_seconds=0,
                         meshcore_advert_interval_seconds=0)
        t = MeshCoreTransport(cfg)
        try:
            t.connect()
            time.sleep(0.1)
            assert t._telemetry_task is None
        finally:
            t.disconnect()

    def test_task_cleared_after_disconnect(self):
        import time
        cfg = _mc_config(meshcore_telemetry_interval_seconds=1800,
                         meshcore_advert_interval_seconds=0)
        t = MeshCoreTransport(cfg)
        t.connect()
        time.sleep(0.1)
        assert t._telemetry_task is not None
        t.disconnect()
        assert t._telemetry_task is None


# ---------------------------------------------------------------------------
# 6. Dashboard endpoints
# ---------------------------------------------------------------------------

def _child(transport_name, connected=True):
    c = MagicMock()
    c.transport_name = transport_name
    c.connected = connected
    return c


def _composite(children):
    connector = MagicMock()
    connector.transport_name = None
    connector.children = list(children)
    return connector


def _client(connector):
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.state.connector = connector
    return TestClient(app)


class TestTelemetryEndpoints:
    def test_get_active_returns_entries(self):
        mc = _child("meshcore", connected=True)
        mc.get_telemetry_cache.return_value = [
            {"contact": "nodeA", "data": {"voltage": 3.98}, "polled_at": "x", "available": True}
        ]
        client = _client(_composite([mc]))
        r = client.get("/api/meshcore/telemetry")
        assert r.status_code == 200
        body = r.json()
        assert body["active"] is True
        assert body["entries"][0]["contact"] == "nodeA"

    def test_get_inactive_when_not_connected(self):
        mc = _child("meshcore", connected=False)
        client = _client(_composite([mc]))
        r = client.get("/api/meshcore/telemetry")
        assert r.json() == {"active": False, "entries": []}

    def test_get_inactive_when_no_meshcore(self):
        mt = _child("meshtastic", connected=True)
        client = _client(_composite([mt]))
        r = client.get("/api/meshcore/telemetry")
        assert r.json() == {"active": False, "entries": []}

    def test_poll_available(self):
        mc = _child("meshcore", connected=True)
        mc.req_telemetry.return_value = {"voltage": 3.98, "raw": []}
        client = _client(_composite([mc]))
        r = client.post("/api/meshcore/telemetry/poll", json={"contact": "nodeA"})
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is True
        assert body["contact"] == "nodeA"
        assert body["data"]["voltage"] == 3.98

    def test_poll_no_response(self):
        mc = _child("meshcore", connected=True)
        mc.req_telemetry.return_value = None
        client = _client(_composite([mc]))
        r = client.post("/api/meshcore/telemetry/poll", json={"contact": "nodeA"})
        body = r.json()
        assert body["available"] is False
        assert body["detail"] == "No telemetry response"

    def test_poll_missing_contact(self):
        mc = _child("meshcore", connected=True)
        client = _client(_composite([mc]))
        r = client.post("/api/meshcore/telemetry/poll", json={})
        body = r.json()
        assert body["available"] is False
        assert "Missing" in body["detail"]

    def test_poll_not_connected(self):
        mc = _child("meshcore", connected=False)
        client = _client(_composite([mc]))
        r = client.post("/api/meshcore/telemetry/poll", json={"contact": "nodeA"})
        body = r.json()
        assert body["available"] is False
        assert body["detail"] == "MeshCore not connected"


# ---------------------------------------------------------------------------
# 7. Config round-trip
# ---------------------------------------------------------------------------

class TestConfigRoundTrip:
    def test_defaults(self):
        cfg = ConnectionConfig()
        assert cfg.meshcore_telemetry_contacts == []
        assert cfg.meshcore_telemetry_interval_seconds == 1800

    def test_construct_with_values(self):
        cfg = ConnectionConfig(
            meshcore_telemetry_contacts=["abc"],
            meshcore_telemetry_interval_seconds=900,
        )
        assert cfg.meshcore_telemetry_contacts == ["abc"]
        assert cfg.meshcore_telemetry_interval_seconds == 900

    def test_independent_default_lists(self):
        a = ConnectionConfig()
        b = ConnectionConfig()
        a.meshcore_telemetry_contacts.append("x")
        assert b.meshcore_telemetry_contacts == []

    def test_yaml_round_trip(self):
        cfg = ConnectionConfig(
            meshcore_telemetry_contacts=["n1", "n2"],
            meshcore_telemetry_interval_seconds=600,
        )
        data = _dataclass_to_dict(cfg)
        assert data["meshcore_telemetry_contacts"] == ["n1", "n2"]
        assert data["meshcore_telemetry_interval_seconds"] == 600
        cfg2 = _dict_to_dataclass(ConnectionConfig, data)
        assert cfg2.meshcore_telemetry_contacts == ["n1", "n2"]
        assert cfg2.meshcore_telemetry_interval_seconds == 600
