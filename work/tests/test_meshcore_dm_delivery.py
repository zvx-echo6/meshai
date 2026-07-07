"""Focused tests for the MeshCore DM ACK-confirmed fast-path delivery.

Verifies that send_message(..., destination=...) resolves the destination to
the full contact object, sends the reply DIRECTLY first (fast path) and only
runs _establish_direct_path (path discovery via send_path_discovery_sync) on
the no-ACK fallback — never before the first send.  Uses plain send_msg (NOT
send_msg_with_retry), and correctly maps the return value to True/False:
  - non-error Event returned  → True  (sent, route logged)
  - None returned             → False (no send result)
  - error Event returned      → False
  - contact not in roster     → False (logged warning, send never called)

Note: these mocks provide no dispatcher ACK, so _wait_for_ack returns False and
every send exercises the no-ACK fallback (discovery + resend) leg.

The meshcore lib is mocked via sys.modules (same pattern as the existing
transport test module).  _run_coro is patched to execute the coroutine
synchronously so no background event-loop thread is needed.
"""

import asyncio
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Contact dict returned by the fake roster — 64-hex public_key, adv_name, out_path_len=-1 (flood).
_CONTACT_DICT = {
    "public_key": "a" * 64,
    "adv_name": "K7ZVX Matt",
    "out_path_len": -1,
}


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
        ACK = "ACK"

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
            return _CONTACT_DICT

        async def ensure_contacts(self, follow=False):
            return True

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
            async def send_msg(dst, text, timestamp=None, attempt=0):
                result = MagicMock()
                result.is_error.return_value = False
                result.payload = {"type": 0, "expected_ack": "00000000"}
                return result

            @staticmethod
            async def send_path_discovery_sync(dst, timeout=0, min_timeout=0):
                result = MagicMock()
                result.is_error.return_value = False
                return result

            @staticmethod
            async def get_advert_path(key):
                result = MagicMock()
                result.is_error.return_value = True  # default: not found
                return result

            @staticmethod
            async def update_contact(contact, path=None, flags=None, path_hash_mode=None):
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


def _ok_event(route_type: int = 0):
    """Non-error Event-like mock with MSG_SENT payload (0=direct, 1=flood)."""
    ev = MagicMock()
    ev.is_error.return_value = False
    ev.payload = {"type": route_type, "expected_ack": "deadbeef"}
    return ev


def _err_event():
    ev = MagicMock()
    ev.is_error.return_value = True
    ev.payload = {"reason": "test error"}
    return ev


def _path_event():
    """Non-error PATH_RESPONSE-like Event mock."""
    ev = MagicMock()
    ev.is_error.return_value = False
    return ev


def _transport_with_mc_mock(contact=_CONTACT_DICT):
    """Return a MeshCoreTransport with _mc as a MagicMock (no loop thread).

    _run_coro is patched on the instance to run the coroutine synchronously via
    asyncio.new_event_loop().run_until_complete(), bypassing the thread bridge.
    This keeps tests fast and deterministic.

    The mock exposes:
      - mc.get_contact_by_key_prefix(prefix) → contact dict (or None when contact=None)
      - mc.ensure_contacts is an AsyncMock (async, returns True)
      - mc.commands.send_path_discovery_sync: AsyncMock → _path_event()
      - mc.commands.send_msg: AsyncMock → _ok_event()
      - mc.commands.get_advert_path: AsyncMock → _err_event() (not found by default)
      - mc.commands.update_contact: AsyncMock → _ok_event()
    """
    cfg = _mc_config()
    t = MeshCoreTransport(cfg)
    mc = MagicMock()
    mc.get_contact_by_key_prefix.return_value = contact
    mc.ensure_contacts = AsyncMock(return_value=True)
    mc.commands.send_path_discovery_sync = AsyncMock(return_value=_path_event())
    mc.commands.send_msg = AsyncMock(return_value=_ok_event())
    mc.commands.get_advert_path = AsyncMock(return_value=_err_event())
    mc.commands.update_contact = AsyncMock(return_value=_ok_event())
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
    """send_message with destination= sends directly first (ACK fast path); path
    discovery is a no-ACK fallback that runs after the first send, not before."""

    def test_successful_dm_returns_true(self):
        """Non-error send_msg result → True."""
        t, mc = _transport_with_mc_mock()

        result = t.send_message("reply text", destination="aabbccdd1122")

        assert result is True

    def test_fast_path_sends_before_discovery(self):
        """ACK-confirmed fast path: send_msg goes out FIRST; path discovery only
        runs on the no-ACK fallback (i.e. AFTER the first send), never before it.

        (This mock has no dispatcher ACK, so _wait_for_ack returns False and the
        no-ACK fallback always fires — which is exactly what exercises the
        discovery-then-resend leg here.)
        """
        t, mc = _transport_with_mc_mock()
        call_order = []

        async def fake_pds(dst, timeout=0, min_timeout=0):
            call_order.append("discovery")
            return _path_event()

        async def fake_send(dst, text, timestamp=None, attempt=0):
            call_order.append("send_msg")
            return _ok_event()

        mc.commands.send_path_discovery_sync = fake_pds
        mc.commands.send_msg = fake_send

        t.send_message("hello", destination="aabbccdd1122")

        assert "send_msg" in call_order, "send_msg was never called"
        assert "discovery" in call_order, "send_path_discovery_sync was never called (no-ACK fallback)"
        # The reply is sent DIRECTLY first; discovery is a fallback that comes after.
        assert call_order.index("send_msg") < call_order.index("discovery"), (
            "fast path must send the reply before running path discovery"
        )

    def test_send_msg_used_not_send_msg_with_retry(self):
        """Plain send_msg (not send_msg_with_retry) must be used for DMs."""
        t, mc = _transport_with_mc_mock()
        mc.commands.send_msg_with_retry = AsyncMock()  # must NOT be called

        t.send_message("reply text", destination="aabbccdd1122")

        mc.commands.send_msg.assert_awaited()
        mc.commands.send_msg_with_retry.assert_not_awaited()

    def test_send_msg_receives_contact_dict(self):
        """send_msg must receive the resolved contact dict as its first argument."""
        t, mc = _transport_with_mc_mock()

        t.send_message("reply text", destination="aabbccdd1122")

        args, _ = mc.commands.send_msg.await_args
        assert args[0] == _CONTACT_DICT, (
            f"send_msg first arg should be the contact dict, got {args[0]!r}"
        )

    def test_none_send_result_returns_false(self):
        """send_msg returning None → False."""
        t, mc = _transport_with_mc_mock()
        mc.commands.send_msg = AsyncMock(return_value=None)

        result = t.send_message("reply text", destination="aabbccdd1122")

        assert result is False

    def test_none_send_result_logs_warning(self, caplog):
        """send_msg returning None → warning is logged."""
        import logging
        t, mc = _transport_with_mc_mock()
        mc.commands.send_msg = AsyncMock(return_value=None)

        with caplog.at_level(logging.WARNING):
            t.send_message("msg", destination="deadbeef0011")

        assert any("no send result" in r.getMessage() for r in caplog.records)

    def test_error_event_returns_false(self):
        """Error event returned by send_msg → False."""
        t, mc = _transport_with_mc_mock()
        mc.commands.send_msg = AsyncMock(return_value=_err_event())

        result = t.send_message("fail text", destination="deadbeef0011")

        assert result is False

    def test_unresolved_contact_returns_false_without_calling_send(self):
        """When get_contact_by_key_prefix returns None, returns False and never calls send_msg."""
        t, mc = _transport_with_mc_mock(contact=None)

        result = t.send_message("hello", destination="deadbeef0011")

        assert result is False
        mc.commands.send_msg.assert_not_awaited()

    def test_unresolved_contact_logs_warning(self, caplog):
        """Unresolved contact → a warning about inability to reply is logged."""
        import logging
        t, mc = _transport_with_mc_mock(contact=None)

        with caplog.at_level(logging.WARNING):
            t.send_message("hello", destination="deadbeef0011")

        assert any("cannot reply" in r.getMessage() for r in caplog.records)

    def test_path_discovery_failure_does_not_prevent_send(self):
        """If path discovery raises, send_msg is still attempted (best-effort)."""
        t, mc = _transport_with_mc_mock()
        mc.commands.send_path_discovery_sync = AsyncMock(
            side_effect=RuntimeError("discovery timed out")
        )

        result = t.send_message("hello", destination="aabbccdd1122")

        mc.commands.send_msg.assert_awaited()
        assert result is True

    def test_advert_path_fallback_invoked_when_still_flood(self):
        """When contact is still flood after path discovery, get_advert_path + update_contact are called."""
        t, mc = _transport_with_mc_mock()
        # Path discovery returns None (no PATH_RESPONSE) — contact stays flood.
        mc.commands.send_path_discovery_sync = AsyncMock(return_value=None)
        # Advert path returns a real direct path.
        ap_ev = MagicMock()
        ap_ev.is_error.return_value = False
        ap_ev.payload = {"path": "aabbcc1122dd", "path_len": 2, "path_hash_mode": 0}
        mc.commands.get_advert_path = AsyncMock(return_value=ap_ev)

        t.send_message("hello", destination="aabbccdd1122")

        mc.commands.get_advert_path.assert_awaited()
        mc.commands.update_contact.assert_awaited()

    def test_direct_route_type_logged(self, caplog):
        """When send_msg reports route type 0 (direct), 'direct' is logged."""
        import logging
        t, mc = _transport_with_mc_mock()
        mc.commands.send_msg = AsyncMock(return_value=_ok_event(route_type=0))

        with caplog.at_level(logging.INFO):
            t.send_message("hi", destination="aabbccdd1122")

        assert any("direct" in r.getMessage() for r in caplog.records)
