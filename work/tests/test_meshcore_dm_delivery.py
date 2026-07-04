"""Focused tests for the MeshCore DM delivery fix.

Verifies that send_message(..., destination=...) calls send_msg_with_retry
(not the old fire-and-forget send_msg) and correctly maps its return value
to True/False:
  - non-error Event returned  → True  (ACKed, delivered)
  - None returned             → False (no ACK, delivery not confirmed)

The meshcore lib is mocked via sys.modules (same pattern as the existing
transport test module).  _run_coro is patched to execute the coroutine
synchronously so no background event-loop thread is needed.
"""

import asyncio
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Minimal fake meshcore module (guards against import errors if the real lib
# is absent, and avoids side-effects from the sys.modules entry in the other
# transport test module racing this one).
# ---------------------------------------------------------------------------

def _ensure_fake_meshcore():
    if "meshcore" in sys.modules:
        return
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
    sys.modules["meshcore"] = mod


_ensure_fake_meshcore()

from meshai.config import ConnectionConfig                            # noqa: E402
from meshai.transport.meshcore_transport import MeshCoreTransport    # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mc_config():
    return ConnectionConfig(meshcore_host="127.0.0.1", meshcore_port=5050)


def _transport_with_mc_mock():
    """Return a MeshCoreTransport with _mc as a MagicMock (no loop thread).

    _run_coro is patched on the instance to run the coroutine synchronously via
    asyncio.get_event_loop().run_until_complete(), bypassing the thread bridge.
    This keeps tests fast and deterministic.
    """
    cfg = _mc_config()
    t = MeshCoreTransport(cfg)
    mc = MagicMock()
    t._mc = mc
    t._connected = True

    def _sync_run_coro(coro, timeout=None):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    t._run_coro = _sync_run_coro
    return t, mc


# ---------------------------------------------------------------------------
# DM delivery tests
# ---------------------------------------------------------------------------

class TestMeshCoreDMDelivery:
    """send_message with destination= must use send_msg_with_retry, not send_msg."""

    def test_acked_dm_returns_true_and_uses_send_msg_with_retry(self):
        """ACK received (non-error Event) → returns True; send_msg_with_retry called."""
        t, mc = _transport_with_mc_mock()

        ok_event = MagicMock()
        ok_event.is_error.return_value = False
        mc.commands.send_msg_with_retry = AsyncMock(return_value=ok_event)
        mc.commands.send_msg = AsyncMock()  # must NOT be called

        result = t.send_message("reply text", destination="aabbccdd1122")

        assert result is True
        mc.commands.send_msg_with_retry.assert_awaited_once_with("aabbccdd1122", "reply text")
        mc.commands.send_msg.assert_not_awaited()

    def test_no_ack_returns_false(self):
        """No ACK (send_msg_with_retry returns None) → returns False."""
        t, mc = _transport_with_mc_mock()

        mc.commands.send_msg_with_retry = AsyncMock(return_value=None)

        result = t.send_message("reply text", destination="aabbccdd1122")

        assert result is False
        mc.commands.send_msg_with_retry.assert_awaited_once_with("aabbccdd1122", "reply text")

    def test_error_event_returns_false(self):
        """Error event returned by send_msg_with_retry → returns False."""
        t, mc = _transport_with_mc_mock()

        err_event = MagicMock()
        err_event.is_error.return_value = True
        mc.commands.send_msg_with_retry = AsyncMock(return_value=err_event)

        result = t.send_message("fail text", destination="deadbeef0011")

        assert result is False

    def test_no_ack_logs_warning(self, caplog):
        """No ACK → a warning mentioning the destination is logged."""
        import logging
        t, mc = _transport_with_mc_mock()
        mc.commands.send_msg_with_retry = AsyncMock(return_value=None)

        with caplog.at_level(logging.WARNING):
            t.send_message("msg", destination="deadbeef0011")

        assert any(
            "not ACKed" in r.getMessage() or "delivery not confirmed" in r.getMessage()
            for r in caplog.records
        )
