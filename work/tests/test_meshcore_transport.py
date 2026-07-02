"""Tests for MeshCoreTransport (Phase 2).

All tests are fully mocked — no real socket, no meshcore lib required.
The fake meshcore module is injected into sys.modules before any lazy-import
triggers, so the production code's lazy ``from meshcore import MeshCore``
gets the mock transparently.
"""

import asyncio
import sys
import threading
import types
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Build and register a minimal fake meshcore module
# (must happen before importing production code that lazy-imports meshcore)
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
        """Minimal stand-in for meshcore.MeshCore."""

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

    mod.MeshCore = _FakeMeshCore
    return mod


# Register before production imports so lazy-import finds the mock.
sys.modules.setdefault("meshcore", _build_fake_meshcore())


# ---------------------------------------------------------------------------
# Production imports (after mock is in sys.modules)
# ---------------------------------------------------------------------------

from meshai.config import ConnectionConfig          # noqa: E402
from meshai.connector import MeshMessage            # noqa: E402
from meshai.transport.base import MeshTransport     # noqa: E402
from meshai.transport.factory import build_transport  # noqa: E402
from meshai.transport.meshcore_transport import MeshCoreTransport  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mc_config(**overrides):
    """Return a ConnectionConfig wired for meshcore."""
    cfg = ConnectionConfig(
        transport="meshcore",
        meshcore_host="127.0.0.1",
        meshcore_port=5050,
        meshcore_channel_index=0,
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _transport_with_mock_mc(mc_overrides=None):
    """Create a MeshCoreTransport with _mc injected as a MagicMock.

    Also starts a real dedicated loop thread so _run_coro works.
    Returns (transport, mc_mock, loop).
    """
    cfg = _mc_config()
    t = MeshCoreTransport(cfg)

    mc = MagicMock()
    mc.get_contact_by_key_prefix.return_value = None
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
    """Stop the transport's dedicated event loop."""
    try:
        if t._loop and t._loop.is_running():
            t._loop.call_soon_threadsafe(t._loop.stop)
        if t._loop_thread and t._loop_thread.is_alive():
            t._loop_thread.join(timeout=2.0)
    except Exception:
        pass


def _make_dm_event(text="hello", pubkey_prefix="aabbcc001122", **extra):
    e = MagicMock()
    e.payload = {"type": "PRIV", "pubkey_prefix": pubkey_prefix, "text": text, **extra}
    return e


def _make_channel_event(text="chan msg", channel_idx=2, **extra):
    e = MagicMock()
    e.payload = {"type": "CHAN", "channel_idx": channel_idx, "text": text, **extra}
    return e


# ---------------------------------------------------------------------------
# 1. Factory / subclass tests
# ---------------------------------------------------------------------------

class TestBuildTransport:
    def test_returns_meshcore_transport(self):
        t = build_transport(_mc_config())
        assert isinstance(t, MeshCoreTransport)

    def test_is_mesh_transport_subclass(self):
        t = build_transport(_mc_config())
        assert isinstance(t, MeshTransport)


# ---------------------------------------------------------------------------
# 2. send_message — channel (no destination)
# ---------------------------------------------------------------------------

class TestSendMessageChannel:
    def test_returns_true_on_non_error_event(self):
        t, mc, _ = _transport_with_mock_mc()
        try:
            ok = MagicMock()
            ok.is_error.return_value = False
            mc.commands.send_chan_msg = AsyncMock(return_value=ok)
            assert t.send_message("hello") is True
            mc.commands.send_chan_msg.assert_awaited_once()
        finally:
            _cleanup(t)

    def test_returns_false_on_error_event(self):
        t, mc, _ = _transport_with_mock_mc()
        try:
            err = MagicMock()
            err.is_error.return_value = True
            mc.commands.send_chan_msg = AsyncMock(return_value=err)
            assert t.send_message("hello") is False
        finally:
            _cleanup(t)

    def test_returns_false_when_not_connected(self):
        cfg = _mc_config()
        t = MeshCoreTransport(cfg)
        # _mc is None, no loop started
        assert t.send_message("test") is False

    def _transport_with_configured_index(self, index):
        """Build a MeshCoreTransport whose config sets meshcore_channel_index."""
        cfg = _mc_config(meshcore_channel_index=index)
        t = MeshCoreTransport(cfg)
        ok = MagicMock()
        ok.is_error.return_value = False

        loop = asyncio.new_event_loop()
        mc = MagicMock()
        mc.commands.send_chan_msg = AsyncMock(return_value=ok)
        t._mc = mc
        t._connected = True
        t._loop = loop
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        t._loop_thread = thread
        return t, mc

    def test_uses_config_channel_index_when_channel_zero(self):
        t, mc = self._transport_with_configured_index(3)
        try:
            t.send_message("hi", channel=0)
            # channel=0 must not be treated as falsy-fallthrough: broadcasts
            # always use the configured meshcore_channel_index=3.
            mc.commands.send_chan_msg.assert_awaited_once_with(3, "hi")
        finally:
            _cleanup(t)

    def test_uses_config_channel_index_when_channel_default(self):
        t, mc = self._transport_with_configured_index(3)
        try:
            t.send_message("hi")  # default channel param
            mc.commands.send_chan_msg.assert_awaited_once_with(3, "hi")
        finally:
            _cleanup(t)

    def test_ignores_meshtastic_channel_index(self):
        # channel=8 carries Meshtastic channel-index semantics that do NOT map
        # to MeshCore's channel table; the configured index (3) is authoritative.
        t, mc = self._transport_with_configured_index(3)
        try:
            t.send_message("hi", channel=8)
            mc.commands.send_chan_msg.assert_awaited_once_with(3, "hi")
        finally:
            _cleanup(t)


# ---------------------------------------------------------------------------
# 3. send_message — DM (destination provided)
# ---------------------------------------------------------------------------

class TestSendMessageDM:
    def test_dispatches_send_msg(self):
        t, mc, _ = _transport_with_mock_mc()
        try:
            ok = MagicMock()
            ok.is_error.return_value = False
            mc.commands.send_msg = AsyncMock(return_value=ok)
            result = t.send_message("hi DM", destination="aabbcc")
            assert result is True
            mc.commands.send_msg.assert_awaited_once_with("aabbcc", "hi DM")
        finally:
            _cleanup(t)

    def test_send_msg_error_returns_false(self):
        t, mc, _ = _transport_with_mock_mc()
        try:
            err = MagicMock()
            err.is_error.return_value = True
            mc.commands.send_msg = AsyncMock(return_value=err)
            assert t.send_message("hi", destination="deadbeef") is False
        finally:
            _cleanup(t)


# ---------------------------------------------------------------------------
# 4. Inbound normalization — direct method calls (hermetic, no threads)
# ---------------------------------------------------------------------------

class TestNormalizeDmEvent:
    def _t(self, mc=None):
        t = MeshCoreTransport(_mc_config())
        t._mc = mc
        return t

    def test_is_dm_true(self):
        msg = self._t()._normalize_dm_event(_make_dm_event())
        assert msg is not None
        assert msg.is_dm is True

    def test_transport_tag(self):
        msg = self._t()._normalize_dm_event(_make_dm_event())
        assert msg.transport == "meshcore"

    def test_text_preserved(self):
        msg = self._t()._normalize_dm_event(_make_dm_event(text="test text"))
        assert msg.text == "test text"

    def test_packet_is_none(self):
        msg = self._t()._normalize_dm_event(_make_dm_event())
        assert msg.packet is None

    def test_sender_id_is_pubkey_prefix(self):
        msg = self._t()._normalize_dm_event(_make_dm_event(pubkey_prefix="deadbeef"))
        assert msg.sender_id == "deadbeef"

    def test_sender_name_resolved_from_contact(self):
        mc = MagicMock()
        mc.get_contact_by_key_prefix.return_value = {"adv_name": "Alice"}
        msg = self._t(mc)._normalize_dm_event(_make_dm_event(pubkey_prefix="aabbcc"))
        assert msg.sender_name == "Alice"

    def test_sender_name_falls_back_to_prefix_when_no_contact(self):
        mc = MagicMock()
        mc.get_contact_by_key_prefix.return_value = None
        msg = self._t(mc)._normalize_dm_event(_make_dm_event(pubkey_prefix="ffff00"))
        assert msg.sender_name == "ffff00"

    def test_empty_text_returns_none(self):
        assert self._t()._normalize_dm_event(_make_dm_event(text="")) is None


class TestNormalizeChannelEvent:
    def _t(self):
        t = MeshCoreTransport(_mc_config())
        return t

    def test_is_dm_false(self):
        msg = self._t()._normalize_channel_event(_make_channel_event())
        assert msg is not None
        assert msg.is_dm is False

    def test_transport_tag(self):
        msg = self._t()._normalize_channel_event(_make_channel_event())
        assert msg.transport == "meshcore"

    def test_channel_idx_set(self):
        msg = self._t()._normalize_channel_event(_make_channel_event(channel_idx=5))
        assert msg.channel == 5

    def test_text_preserved(self):
        msg = self._t()._normalize_channel_event(_make_channel_event(text="RF traffic"))
        assert msg.text == "RF traffic"

    def test_empty_text_returns_none(self):
        assert self._t()._normalize_channel_event(_make_channel_event(text="")) is None


# ---------------------------------------------------------------------------
# 5. Inbound dispatch → callback delivery (via _dispatch_message)
# ---------------------------------------------------------------------------

class TestInboundDispatch:
    """Verify that _dispatch_message delivers MeshMessage to registered callback."""

    def _run_drain(self, loop, seconds=0.1):
        async def _drain():
            await asyncio.sleep(seconds)
        loop.run_until_complete(_drain())

    def test_dm_event_delivered_to_callback(self):
        loop = asyncio.new_event_loop()
        received = []

        async def cb(msg):
            received.append(msg)

        t = MeshCoreTransport(_mc_config())
        t.set_message_callback(cb, loop)

        event = _make_dm_event(text="callback test")
        msg = t._normalize_dm_event(event)
        t._dispatch_message(msg)

        self._run_drain(loop)
        loop.close()

        assert len(received) == 1
        assert received[0].is_dm is True
        assert received[0].text == "callback test"
        assert received[0].transport == "meshcore"

    def test_channel_event_delivered_to_callback(self):
        loop = asyncio.new_event_loop()
        received = []

        async def cb(msg):
            received.append(msg)

        t = MeshCoreTransport(_mc_config())
        t.set_message_callback(cb, loop)

        event = _make_channel_event(text="chan callback", channel_idx=1)
        msg = t._normalize_channel_event(event)
        t._dispatch_message(msg)

        self._run_drain(loop)
        loop.close()

        assert len(received) == 1
        assert received[0].is_dm is False
        assert received[0].channel == 1
        assert received[0].transport == "meshcore"

    def test_none_msg_not_dispatched(self):
        loop = asyncio.new_event_loop()
        received = []

        async def cb(msg):
            received.append(msg)

        t = MeshCoreTransport(_mc_config())
        t.set_message_callback(cb, loop)
        t._dispatch_message(None)

        self._run_drain(loop)
        loop.close()

        assert received == []


# ---------------------------------------------------------------------------
# 6. my_node_id property
# ---------------------------------------------------------------------------

class TestMyNodeId:
    def test_returns_public_key_from_self_info(self):
        t = MeshCoreTransport(_mc_config())
        t._self_info = {"public_key": "deadbeef1234", "name": "TestNode"}
        assert t.my_node_id == "deadbeef1234"

    def test_returns_none_before_connect(self):
        t = MeshCoreTransport(_mc_config())
        assert t.my_node_id is None

    def test_returns_none_when_self_info_empty(self):
        t = MeshCoreTransport(_mc_config())
        t._self_info = {}
        assert t.my_node_id is None
