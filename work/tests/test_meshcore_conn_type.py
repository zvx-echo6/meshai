"""Tests for MeshCore multi-transport connection type support.

Covers:
  - ConnectionConfig validation of meshcore_conn_type
  - meshcore_enabled() factory helper
  - _do_connect() selects the right MeshCore.create_* per mode
"""

import asyncio
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Build a recording fake meshcore module
# ---------------------------------------------------------------------------

def _build_recording_meshcore():
    """Build a fake meshcore module that records which create_* was called."""
    mod = types.ModuleType("meshcore")

    class EventType:
        CONTACT_MSG_RECV = "CONTACT_MSG_RECV"
        CHANNEL_MSG_RECV = "CHANNEL_MSG_RECV"
        DISCONNECTED = "DISCONNECTED"
        CONNECTED = "CONNECTED"
        ACK = "ACK"
        NEW_CONTACT = "NEW_CONTACT"

    mod.EventType = EventType

    class _FakeMeshCore:
        # Shared call log: list of (method_name, args, kwargs)
        _calls: list = []

        self_info = {"public_key": "aabbccdd1122", "name": "FakeNode"}
        contacts = {}

        async def start_auto_message_fetching(self):
            pass

        async def stop_auto_message_fetching(self):
            pass

        async def disconnect(self):
            pass

        async def ensure_contacts(self, follow=False):
            return True

        def subscribe(self, event_type, callback):
            pass

        def get_contact_by_key_prefix(self, prefix):
            return None

        @classmethod
        async def create_tcp(cls, host, port,
                             auto_reconnect=True, max_reconnect_attempts=5,
                             debug=False, only_error=False, default_timeout=None):
            cls._calls.append(("create_tcp", (host, port), {
                "auto_reconnect": auto_reconnect,
                "max_reconnect_attempts": max_reconnect_attempts,
            }))
            return cls()

        @classmethod
        async def create_serial(cls, port, baudrate=115200,
                                auto_reconnect=True, max_reconnect_attempts=5,
                                debug=False, only_error=False, default_timeout=None,
                                cx_dly=0.1):
            cls._calls.append(("create_serial", (port,), {
                "baudrate": baudrate,
                "auto_reconnect": auto_reconnect,
                "max_reconnect_attempts": max_reconnect_attempts,
            }))
            return cls()

        @classmethod
        async def create_ble(cls, address=None, client=None, device=None, pin=None,
                             debug=False, only_error=False, default_timeout=None,
                             auto_reconnect=True, max_reconnect_attempts=5):
            cls._calls.append(("create_ble", (), {
                "address": address,
                "auto_reconnect": auto_reconnect,
                "max_reconnect_attempts": max_reconnect_attempts,
            }))
            return cls()

        class commands:
            @staticmethod
            async def send_chan_msg(chan_idx, text):
                result = MagicMock()
                result.is_error.return_value = False
                return result

            @staticmethod
            async def send_msg(dst, text):
                result = MagicMock()
                result.is_error.return_value = False
                return result

            @staticmethod
            async def send_advert(flood=False):
                pass

    mod.MeshCore = _FakeMeshCore
    return mod


# Register before production imports so lazy-import finds the mock.
# Use a unique key so we don't clobber test_meshcore_transport.py's module.
_fake_mc_mod = _build_recording_meshcore()
sys.modules.setdefault("meshcore", _fake_mc_mod)


# ---------------------------------------------------------------------------
# Production imports (after mock is registered)
# ---------------------------------------------------------------------------

from meshai.config import ConnectionConfig          # noqa: E402
from meshai.transport.factory import build_transport, meshcore_enabled  # noqa: E402
from meshai.transport.meshcore_transport import MeshCoreTransport  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_fake_mc():
    """Return the _FakeMeshCore class (reachable via sys.modules)."""
    return sys.modules["meshcore"].MeshCore


def _clear_calls():
    _get_fake_mc()._calls.clear()


def _run(coro):
    """Run a coroutine in a fresh event loop."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Part 1: ConnectionConfig validation
# ---------------------------------------------------------------------------

class TestConnectionConfigValidation:
    def test_invalid_conn_type_raises(self):
        with pytest.raises(ValueError, match="meshcore_conn_type"):
            ConnectionConfig(meshcore_conn_type="bogus")

    def test_tcp_accepted(self):
        cfg = ConnectionConfig(meshcore_conn_type="tcp")
        assert cfg.meshcore_conn_type == "tcp"

    def test_serial_accepted(self):
        cfg = ConnectionConfig(meshcore_conn_type="serial")
        assert cfg.meshcore_conn_type == "serial"

    def test_ble_accepted(self):
        cfg = ConnectionConfig(meshcore_conn_type="ble")
        assert cfg.meshcore_conn_type == "ble"

    def test_default_is_tcp(self):
        cfg = ConnectionConfig()
        assert cfg.meshcore_conn_type == "tcp"

    def test_new_fields_have_defaults(self):
        cfg = ConnectionConfig()
        assert cfg.meshcore_serial_port == ""
        assert cfg.meshcore_baud == 115200
        assert cfg.meshcore_ble_address == ""


# ---------------------------------------------------------------------------
# Part 2: meshcore_enabled() factory helper
# ---------------------------------------------------------------------------

class TestMeshcoreEnabled:
    def test_tcp_with_host_true(self):
        cfg = ConnectionConfig(meshcore_conn_type="tcp", meshcore_host="127.0.0.1")
        assert meshcore_enabled(cfg) is True

    def test_tcp_without_host_false(self):
        cfg = ConnectionConfig(meshcore_conn_type="tcp", meshcore_host="")
        assert meshcore_enabled(cfg) is False

    def test_serial_with_port_true(self):
        cfg = ConnectionConfig(meshcore_conn_type="serial", meshcore_serial_port="/dev/ttyACM0")
        assert meshcore_enabled(cfg) is True

    def test_serial_without_port_false(self):
        cfg = ConnectionConfig(meshcore_conn_type="serial", meshcore_serial_port="")
        assert meshcore_enabled(cfg) is False

    def test_serial_ignores_host(self):
        """serial mode: having meshcore_host set does NOT enable MeshCore."""
        cfg = ConnectionConfig(
            meshcore_conn_type="serial",
            meshcore_host="127.0.0.1",
            meshcore_serial_port="",
        )
        assert meshcore_enabled(cfg) is False

    def test_ble_with_address_true(self):
        cfg = ConnectionConfig(meshcore_conn_type="ble", meshcore_ble_address="AA:BB:CC:DD:EE:FF")
        assert meshcore_enabled(cfg) is True

    def test_ble_without_address_false(self):
        cfg = ConnectionConfig(meshcore_conn_type="ble", meshcore_ble_address="")
        assert meshcore_enabled(cfg) is False

    def test_back_compat_tcp_default(self):
        """Existing config: meshcore_host set, default conn_type='tcp' → True."""
        cfg = ConnectionConfig(meshcore_host="100.64.0.9")
        # conn_type defaults to "tcp"
        assert cfg.meshcore_conn_type == "tcp"
        assert meshcore_enabled(cfg) is True

    def test_whitespace_only_host_false(self):
        cfg = ConnectionConfig(meshcore_conn_type="tcp", meshcore_host="   ")
        assert meshcore_enabled(cfg) is False


# ---------------------------------------------------------------------------
# Part 3: _do_connect selects correct create_* method
# ---------------------------------------------------------------------------

class TestDoConnectSelection:
    """Call _do_connect directly (it's just an async method) via asyncio.run."""

    def setup_method(self):
        _clear_calls()

    def _make_transport(self, **cfg_kwargs):
        cfg = ConnectionConfig(**cfg_kwargs)
        return MeshCoreTransport(cfg), cfg

    def test_tcp_calls_create_tcp(self):
        t, cfg = self._make_transport(
            meshcore_conn_type="tcp",
            meshcore_host="127.0.0.1",
            meshcore_port=5050,
            meshcore_auto_reconnect=True,
            meshcore_max_reconnect_attempts=3,
        )
        _run(t._do_connect(
            "tcp", "127.0.0.1:5050",
            True, 3,
            host="127.0.0.1", port=5050,
            serial_port="", baud=115200,
            ble_address="",
        ))
        calls = _get_fake_mc()._calls
        assert len(calls) == 1
        name, args, kwargs = calls[0]
        assert name == "create_tcp"
        assert args == ("127.0.0.1", 5050)
        assert kwargs["auto_reconnect"] is True
        assert kwargs["max_reconnect_attempts"] == 3

    def test_serial_calls_create_serial(self):
        t, cfg = self._make_transport(
            meshcore_conn_type="serial",
            meshcore_serial_port="/dev/ttyACM0",
            meshcore_baud=115200,
            meshcore_auto_reconnect=True,
            meshcore_max_reconnect_attempts=5,
        )
        _run(t._do_connect(
            "serial", "serial:/dev/ttyACM0@115200",
            True, 5,
            serial_port="/dev/ttyACM0", baud=115200,
        ))
        calls = _get_fake_mc()._calls
        assert len(calls) == 1
        name, args, kwargs = calls[0]
        assert name == "create_serial"
        assert args == ("/dev/ttyACM0",)
        assert kwargs["baudrate"] == 115200
        assert kwargs["auto_reconnect"] is True
        assert kwargs["max_reconnect_attempts"] == 5

    def test_serial_does_not_pass_cx_dly(self):
        """create_serial should use default cx_dly (not passed explicitly)."""
        t, _ = self._make_transport(meshcore_conn_type="serial")
        _run(t._do_connect(
            "serial", "serial:/dev/ttyACM0@115200",
            True, 5,
            serial_port="/dev/ttyACM0", baud=115200,
        ))
        calls = _get_fake_mc()._calls
        assert "cx_dly" not in calls[0][2]

    def test_ble_calls_create_ble(self):
        t, _ = self._make_transport(
            meshcore_conn_type="ble",
            meshcore_ble_address="AA:BB:CC:DD:EE:FF",
            meshcore_auto_reconnect=False,
            meshcore_max_reconnect_attempts=2,
        )
        _run(t._do_connect(
            "ble", "ble:AA:BB:CC:DD:EE:FF",
            False, 2,
            ble_address="AA:BB:CC:DD:EE:FF",
        ))
        calls = _get_fake_mc()._calls
        assert len(calls) == 1
        name, args, kwargs = calls[0]
        assert name == "create_ble"
        assert kwargs["address"] == "AA:BB:CC:DD:EE:FF"
        assert kwargs["auto_reconnect"] is False
        assert kwargs["max_reconnect_attempts"] == 2

    def test_ble_empty_address_passes_none(self):
        """Empty ble_address → address=None (scan for any device)."""
        t, _ = self._make_transport(meshcore_conn_type="ble")
        _run(t._do_connect(
            "ble", "ble:auto",
            True, 5,
            ble_address="",
        ))
        calls = _get_fake_mc()._calls
        assert calls[0][2]["address"] is None
