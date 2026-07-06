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
        ACK = "ACK"
        NEW_CONTACT = "NEW_CONTACT"

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

        async def ensure_contacts(self, follow=False):
            return True

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
                # No return value required for advert.
                pass

            @staticmethod
            async def set_autoadd_config(value):
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
    """Return a ConnectionConfig with meshcore_host set (activates MeshCore)."""
    cfg = ConnectionConfig(
        meshcore_host="127.0.0.1",
        meshcore_port=5050,
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
    _install_channel_table(mc)
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


# Default fake companion channel table: NAME -> slot.  Slots not present here
# report an empty channel_name, so _enumerate_channels stops after the empty run.
_FAKE_CHANNEL_TABLE = {2: "AIDA", 3: "Fire"}


def _install_channel_table(mc, table=None):
    """Wire ``mc.commands.get_channel`` to enumerate a fake channel table.

    ``table`` maps slot index -> channel name.  get_channel(idx) returns a
    non-error event whose payload carries ``channel_name``/``channel_idx``
    exactly like the real firmware; unlisted slots report an empty name so
    _enumerate_channels ends on the contiguous-empty run.
    """
    if table is None:
        table = _FAKE_CHANNEL_TABLE

    async def _get_channel(idx):
        e = MagicMock()
        e.is_error.return_value = False
        e.payload = {"channel_name": table.get(idx, ""), "channel_idx": idx}
        return e

    mc.commands.get_channel = _get_channel


# ---------------------------------------------------------------------------
# 1. Factory / subclass tests
# ---------------------------------------------------------------------------

class TestBuildTransport:
    def test_returns_composite_when_meshcore_host_set(self):
        """meshcore_host set → build_transport returns CompositeTransport."""
        from meshai.transport.composite_transport import CompositeTransport
        t = build_transport(_mc_config())
        assert isinstance(t, CompositeTransport)
        # MeshCoreTransport must be the second child.
        assert isinstance(t.children[1], MeshCoreTransport)

    def test_is_mesh_transport_subclass(self):
        """CompositeTransport (returned when meshcore_host is set) is a MeshTransport."""
        t = build_transport(_mc_config())
        assert isinstance(t, MeshTransport)


# ---------------------------------------------------------------------------
# 2. send_message — channel (no destination)
# ---------------------------------------------------------------------------

class TestSendMessageChannel:
    def test_returns_true_on_non_error_event(self):
        """With a resolvable channel name, send_chan_msg is called and True returned."""
        t, mc, _ = _transport_with_mock_mc()
        try:
            ok = MagicMock()
            ok.is_error.return_value = False
            mc.commands.send_chan_msg = AsyncMock(return_value=ok)
            assert t.send_message("hello", meshcore_channel="AIDA") is True
            mc.commands.send_chan_msg.assert_awaited_once()
        finally:
            _cleanup(t)

    def test_returns_false_on_error_event(self):
        """With a resolvable channel name, an error result returns False."""
        t, mc, _ = _transport_with_mock_mc()
        try:
            err = MagicMock()
            err.is_error.return_value = True
            mc.commands.send_chan_msg = AsyncMock(return_value=err)
            assert t.send_message("hello", meshcore_channel="AIDA") is False
        finally:
            _cleanup(t)

    def test_returns_false_when_not_connected(self):
        cfg = _mc_config()
        t = MeshCoreTransport(cfg)
        # _mc is None, no loop started — fails before channel check.
        assert t.send_message("test", meshcore_channel="AIDA") is False

    def test_meshcore_channel_none_skips_broadcast(self):
        """meshcore_channel=None → silent no-op (True) without calling send_chan_msg."""
        t, mc, _ = _transport_with_mock_mc()
        try:
            mc.commands.send_chan_msg = AsyncMock()
            result = t.send_message("hello", meshcore_channel=None)
            assert result is True  # no-op success
            mc.commands.send_chan_msg.assert_not_awaited()
        finally:
            _cleanup(t)

    def test_meshcore_channel_default_skips_broadcast(self):
        """Default meshcore_channel=None (no arg) → silent no-op."""
        t, mc, _ = _transport_with_mock_mc()
        try:
            mc.commands.send_chan_msg = AsyncMock()
            result = t.send_message("hello")  # no meshcore_channel arg
            assert result is True
            mc.commands.send_chan_msg.assert_not_awaited()
        finally:
            _cleanup(t)

    def _transport_with_mock_send_chan_msg(self):
        """Build a MeshCoreTransport with a mock mc, a fake channel table, and
        an async send_chan_msg recorder."""
        cfg = _mc_config()
        t = MeshCoreTransport(cfg)
        ok = MagicMock()
        ok.is_error.return_value = False

        loop = asyncio.new_event_loop()
        mc = MagicMock()
        _install_channel_table(mc)  # {"AIDA": 2, "Fire": 3}
        mc.commands.send_chan_msg = AsyncMock(return_value=ok)
        t._mc = mc
        t._connected = True
        t._loop = loop
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        t._loop_thread = thread
        return t, mc

    def test_resolves_name_to_slot_for_broadcast(self):
        """meshcore_channel='AIDA' → resolves to slot 2 → send_chan_msg(2, text)."""
        t, mc = self._transport_with_mock_send_chan_msg()
        try:
            assert t.send_message("hi", meshcore_channel="AIDA") is True
            mc.commands.send_chan_msg.assert_awaited_once_with(2, "hi")
        finally:
            _cleanup(t)

    def test_resolves_second_named_channel(self):
        """meshcore_channel='Fire' → resolves to slot 3 → send_chan_msg(3, text)."""
        t, mc = self._transport_with_mock_send_chan_msg()
        try:
            assert t.send_message("hi", meshcore_channel="Fire") is True
            mc.commands.send_chan_msg.assert_awaited_once_with(3, "hi")
        finally:
            _cleanup(t)

    def test_ignores_meshtastic_channel_uses_meshcore_name(self):
        """channel=8 (Meshtastic) is irrelevant; the MeshCore NAME is authoritative."""
        t, mc = self._transport_with_mock_send_chan_msg()
        try:
            t.send_message("hi", channel=8, meshcore_channel="Fire")
            mc.commands.send_chan_msg.assert_awaited_once_with(3, "hi")
        finally:
            _cleanup(t)

    def test_unknown_name_warns_and_never_sends(self, caplog):
        """An unresolved channel name → no send_chan_msg, returns False, warns."""
        import logging
        t, mc = self._transport_with_mock_send_chan_msg()
        try:
            with caplog.at_level(logging.WARNING):
                result = t.send_message("hi", meshcore_channel="Nonexistent")
            assert result is False
            mc.commands.send_chan_msg.assert_not_awaited()
            assert any("Nonexistent" in r.getMessage() for r in caplog.records)
        finally:
            _cleanup(t)


# ---------------------------------------------------------------------------
# 3. send_message — DM (destination provided)
# ---------------------------------------------------------------------------

_DM_CONTACT = {"public_key": "a" * 64, "adv_name": "TestContact", "out_path_len": -1}


class TestSendMessageDM:
    def test_dispatches_send_msg_with_retry(self):
        """DM send: path discovery then plain send_msg (not send_msg_with_retry)."""
        t, mc, _ = _transport_with_mock_mc()
        try:
            # Supply a resolved contact so _resolve_contact succeeds.
            mc.get_contact_by_key_prefix.return_value = _DM_CONTACT
            mc.ensure_contacts = AsyncMock(return_value=True)
            # Path discovery must be awaitable.
            path_ev = MagicMock()
            path_ev.is_error.return_value = False
            mc.commands.send_path_discovery_sync = AsyncMock(return_value=path_ev)
            ok = MagicMock()
            ok.is_error.return_value = False
            ok.payload = {"type": 0, "expected_ack": "00000000"}
            mc.commands.send_msg = AsyncMock(return_value=ok)
            result = t.send_message("hi DM", destination="aabbcc")
            assert result is True
            # Must use send_msg (not send_msg_with_retry) with the CONTACT OBJECT.
            mc.commands.send_msg.assert_awaited_once_with(_DM_CONTACT, "hi DM")
        finally:
            _cleanup(t)

    def test_send_msg_with_retry_error_returns_false(self):
        """Error event from send_msg → False."""
        t, mc, _ = _transport_with_mock_mc()
        try:
            mc.get_contact_by_key_prefix.return_value = _DM_CONTACT
            mc.ensure_contacts = AsyncMock(return_value=True)
            path_ev = MagicMock()
            path_ev.is_error.return_value = False
            mc.commands.send_path_discovery_sync = AsyncMock(return_value=path_ev)
            err = MagicMock()
            err.is_error.return_value = True
            err.payload = {"reason": "test"}
            mc.commands.send_msg = AsyncMock(return_value=err)
            assert t.send_message("hi", destination="deadbeef") is False
        finally:
            _cleanup(t)


# ---------------------------------------------------------------------------
# 3b. send_message — DM path-discovery skip / self-heal logic
# ---------------------------------------------------------------------------

class TestSendMessageDMPathSkip:
    """Verify the out_path_len guard and stale-route self-heal retry."""

    def _make_ok_event(self):
        ev = MagicMock()
        ev.is_error.return_value = False
        ev.payload = {"type": 0}
        return ev

    def _make_err_event(self):
        ev = MagicMock()
        ev.is_error.return_value = True
        ev.payload = {"reason": "no ack"}
        return ev

    def _transport_with_contact(self, out_path_len):
        """Return (transport, mc) with mc.get_contact_by_key_prefix returning a
        contact carrying the given out_path_len.  Path-discovery helpers are
        AsyncMocks so they never block.  _establish_direct_path is left intact
        (caller can monkeypatch as needed)."""
        t, mc, _ = _transport_with_mock_mc()
        contact = {
            "public_key": "a" * 64,
            "adv_name": "TestNode",
            "out_path_len": out_path_len,
        }
        mc.get_contact_by_key_prefix.return_value = contact
        mc.ensure_contacts = AsyncMock(return_value=True)
        # Path-discovery support (used by _establish_direct_path).
        path_ev = MagicMock()
        path_ev.is_error.return_value = False
        mc.commands.send_path_discovery_sync = AsyncMock(return_value=path_ev)
        mc.commands.get_advert_path = AsyncMock(return_value=path_ev)
        return t, mc

    def test_routed_contact_skips_discovery(self):
        """out_path_len=0 (already routed): _establish_direct_path must NOT be called."""
        t, mc = self._transport_with_contact(out_path_len=0)
        try:
            # Replace _establish_direct_path with a spy so we can assert it's not called.
            from unittest.mock import Mock
            spy = Mock()
            t._establish_direct_path = spy

            mc.commands.send_msg = AsyncMock(return_value=self._make_ok_event())

            result = t.send_message("hi", destination="7d4e07237294")

            spy.assert_not_called()
            mc.commands.send_msg.assert_awaited_once()
            assert result is True
        finally:
            _cleanup(t)

    def test_flood_contact_runs_discovery(self):
        """out_path_len=-1 (flood/unknown): _establish_direct_path MUST be called once."""
        t, mc = self._transport_with_contact(out_path_len=-1)
        try:
            from unittest.mock import Mock
            spy = Mock()
            t._establish_direct_path = spy

            mc.commands.send_msg = AsyncMock(return_value=self._make_ok_event())

            result = t.send_message("hi", destination="7d4e07237294")

            spy.assert_called_once()
            mc.commands.send_msg.assert_awaited_once()
            assert result is True
        finally:
            _cleanup(t)

    def test_stale_routed_contact_self_heals(self):
        """out_path_len=0 but first send fails (stale route): discovery runs,
        send retried once, final return is True."""
        t, mc = self._transport_with_contact(out_path_len=0)
        try:
            from unittest.mock import Mock
            spy = Mock()
            t._establish_direct_path = spy

            # First send fails, second succeeds.
            mc.commands.send_msg = AsyncMock(
                side_effect=[self._make_err_event(), self._make_ok_event()]
            )

            result = t.send_message("hi", destination="7d4e07237294")

            # Discovery was triggered exactly once (the self-heal retry).
            spy.assert_called_once()
            # send_msg was called twice (first attempt + retry).
            assert mc.commands.send_msg.await_count == 2
            assert result is True
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


# ---------------------------------------------------------------------------
# 7. get_contacts() — roster mapping
# ---------------------------------------------------------------------------

# Sample companion contact table: pubkey_hex -> raw contact dict, mirroring the
# meshcore lib's ``mc.contacts`` shape (repeater + sensor, with/without pos).
_SAMPLE_CONTACTS = {
    "aa11": {
        "adv_name": "Repeater One",
        "public_key": "aa11deadbeef",
        "type": "repeater",
        "last_advert": 1000,
        "adv_lat": 43.6,
        "adv_lon": -116.2,
        "out_path_len": 2,
    },
    "bb22": {
        "adv_name": "Sensor Two",
        "public_key": "bb22cafef00d",
        "type": "sensor",
        "last_advert": 2000,
        "adv_lat": None,
        "adv_lon": None,
        "out_path_len": -1,
    },
}


class TestGetContacts:
    def test_maps_contacts_into_roster_shape(self):
        """mc.contacts dict is mapped into the roster shape (repeater + sensor)."""
        t, mc, _ = _transport_with_mock_mc()
        try:
            mc.ensure_contacts = AsyncMock(return_value=None)
            mc.contacts = dict(_SAMPLE_CONTACTS)
            roster = t.get_contacts()
            assert isinstance(roster, list)
            assert len(roster) == 2
            by_name = {r["name"]: r for r in roster}

            rep = by_name["Repeater One"]
            assert rep == {
                "name": "Repeater One",
                "pubkey": "aa11deadbeef",
                "type": "repeater",
                "last_advert": 1000,
                "lat": 43.6,
                "lon": -116.2,
                "out_path_len": 2,
            }

            sensor = by_name["Sensor Two"]
            assert sensor["type"] == "sensor"
            assert sensor["pubkey"] == "bb22cafef00d"
            assert sensor["lat"] is None
            assert sensor["lon"] is None
            assert sensor["out_path_len"] == -1
        finally:
            _cleanup(t)

    def test_pubkey_falls_back_to_hex_key(self):
        """When a contact carries no public_key, the dict hex key is used."""
        t, mc, _ = _transport_with_mock_mc()
        try:
            mc.ensure_contacts = AsyncMock(return_value=None)
            mc.contacts = {"ff00": {"adv_name": "NoKey", "type": "chat"}}
            roster = t.get_contacts()
            assert len(roster) == 1
            assert roster[0]["pubkey"] == "ff00"
            assert roster[0]["name"] == "NoKey"
        finally:
            _cleanup(t)

    def test_works_without_ensure_contacts(self):
        """A companion lacking ensure_contacts still yields the roster."""
        t, mc, _ = _transport_with_mock_mc()
        try:
            mc.ensure_contacts = None
            mc.contacts = dict(_SAMPLE_CONTACTS)
            roster = t.get_contacts()
            assert len(roster) == 2
        finally:
            _cleanup(t)

    def test_returns_empty_when_not_connected(self):
        """A fresh, unconnected transport (_mc is None) returns []."""
        t = MeshCoreTransport(_mc_config())
        assert t.get_contacts() == []


# ---------------------------------------------------------------------------
# 8. self_info() — companion self/connection status
# ---------------------------------------------------------------------------

class TestSelfInfo:
    def test_connected_returns_status_dict(self):
        """Connected: returns name/pubkey/connected/host/port/channel_count."""
        t, mc, _ = _transport_with_mock_mc()
        try:
            # Build _chan_name_to_idx from the fixture's fake channel table so
            # channel_count is non-zero (fixture wires get_channel but doesn't enumerate).
            t._enumerate_channels()
            t._self_info = {"public_key": "deadbeef1234", "name": "AIDA"}
            info = t.self_info()
            assert info["name"] == "AIDA"
            assert info["pubkey"] == "deadbeef1234"
            assert info["connected"] is True
            assert info["host"] == "127.0.0.1"
            assert info["port"] == 5050
            # channel_count reflects the installed fake channel table.
            assert info["channel_count"] == len(t.known_channels())
            assert info["channel_count"] == 2
        finally:
            _cleanup(t)

    def test_not_connected_returns_disconnected(self):
        """A fresh, unconnected transport (_mc is None) returns {connected: False}."""
        t = MeshCoreTransport(_mc_config())
        assert t.self_info() == {"connected": False}

    def test_connected_includes_last_advert_sent(self):
        """self_info() includes last_advert_sent (None before first advert)."""
        t, mc, _ = _transport_with_mock_mc()
        try:
            t._self_info = {"public_key": "abc123", "name": "TestNode"}
            info = t.self_info()
            assert "last_advert_sent" in info
            assert info["last_advert_sent"] is None  # no advert sent yet
        finally:
            _cleanup(t)

    def test_self_info_last_advert_sent_updated_after_send_advert(self):
        """self_info() reflects last_advert_sent after send_advert() succeeds."""
        import time
        t, mc, _ = _transport_with_mock_mc()
        try:
            mc.commands.send_advert = AsyncMock(return_value=None)
            t._self_info = {"public_key": "abc123", "name": "TestNode"}
            before = time.time()
            t.send_advert()
            info = t.self_info()
            assert info["last_advert_sent"] is not None
            assert info["last_advert_sent"] >= before
        finally:
            _cleanup(t)


# ---------------------------------------------------------------------------
# 9. send_advert()
# ---------------------------------------------------------------------------

class TestSendAdvert:
    def test_connected_calls_lib_command_and_returns_true(self):
        """send_advert() awaits mc.commands.send_advert(flood=True) and returns True."""
        t, mc, _ = _transport_with_mock_mc()
        try:
            mc.commands.send_advert = AsyncMock(return_value=None)
            result = t.send_advert()
            assert result is True
            mc.commands.send_advert.assert_awaited_once_with(flood=True)
        finally:
            _cleanup(t)

    def test_not_connected_returns_false(self):
        """send_advert() returns False when _mc is None (transport not connected)."""
        t = MeshCoreTransport(_mc_config())
        assert t.send_advert() is False

    def test_connected_but_flag_false_returns_false(self):
        """send_advert() returns False when _connected is False."""
        t = MeshCoreTransport(_mc_config())
        t._mc = MagicMock()   # mc set but _connected remains False
        assert t.send_advert() is False

    def test_updates_last_advert_sent_on_success(self):
        """send_advert() sets _last_advert_sent to current epoch on success."""
        import time
        t, mc, _ = _transport_with_mock_mc()
        try:
            mc.commands.send_advert = AsyncMock(return_value=None)
            before = time.time()
            t.send_advert()
            assert t._last_advert_sent is not None
            assert t._last_advert_sent >= before
        finally:
            _cleanup(t)

    def test_exception_returns_false_and_does_not_raise(self):
        """send_advert() returns False (never raises) when the lib command raises."""
        t, mc, _ = _transport_with_mock_mc()
        try:
            mc.commands.send_advert = AsyncMock(side_effect=Exception("timeout"))
            result = t.send_advert()
            assert result is False
        finally:
            _cleanup(t)

    def test_does_not_update_last_advert_sent_on_failure(self):
        """_last_advert_sent stays None when the lib command raises."""
        t, mc, _ = _transport_with_mock_mc()
        try:
            mc.commands.send_advert = AsyncMock(side_effect=Exception("timeout"))
            t.send_advert()
            assert t._last_advert_sent is None
        finally:
            _cleanup(t)


# ---------------------------------------------------------------------------
# 10. advert-on-connect
# ---------------------------------------------------------------------------

class TestAdvertOnConnect:
    def test_advert_sent_after_connect(self):
        """connect() calls send_advert() once after _enumerate_channels()."""
        from unittest.mock import patch
        cfg = _mc_config()
        # Disable periodic advert so we only check the one-shot on-connect call.
        cfg.meshcore_advert_interval_seconds = 0
        t = MeshCoreTransport(cfg)
        advert_calls = []

        original_send_advert = MeshCoreTransport.send_advert

        def _spy_send_advert(self_inner):
            advert_calls.append(True)
            return True

        with patch.object(MeshCoreTransport, "send_advert", _spy_send_advert):
            t.connect()

        try:
            assert len(advert_calls) == 1, (
                f"expected 1 send_advert call on connect, got {len(advert_calls)}"
            )
        finally:
            t.disconnect()


# ---------------------------------------------------------------------------
# 11. Periodic advert scheduler
# ---------------------------------------------------------------------------

class TestPeriodicAdvertScheduler:
    def test_task_armed_when_interval_nonzero(self):
        """connect() with meshcore_advert_interval_seconds > 0 arms _advert_task."""
        import time
        cfg = _mc_config(meshcore_advert_interval_seconds=3600)
        t = MeshCoreTransport(cfg)
        try:
            t.connect()
            # Give the event loop a moment to execute the call_soon_threadsafe callback.
            time.sleep(0.1)
            assert t._advert_task is not None, "_advert_task should be set after connect"
        finally:
            t.disconnect()

    def test_task_not_armed_when_interval_zero(self):
        """connect() with meshcore_advert_interval_seconds=0 leaves _advert_task None."""
        import time
        cfg = _mc_config(meshcore_advert_interval_seconds=0)
        t = MeshCoreTransport(cfg)
        try:
            t.connect()
            time.sleep(0.1)
            assert t._advert_task is None, "_advert_task should not be set when interval=0"
        finally:
            t.disconnect()

    def test_task_cleared_after_disconnect(self):
        """disconnect() cancels and clears _advert_task."""
        import time
        cfg = _mc_config(meshcore_advert_interval_seconds=3600)
        t = MeshCoreTransport(cfg)
        t.connect()
        time.sleep(0.1)
        assert t._advert_task is not None
        t.disconnect()
        assert t._advert_task is None, "_advert_task should be None after disconnect"


# ---------------------------------------------------------------------------
# 12. Auto-add contacts (CMD 58 + NEW_CONTACT roster refresh)
# ---------------------------------------------------------------------------

class TestAutoAddContacts:
    """set_autoadd_config called (or not) at connect; _on_new_contact refreshes roster."""

    def _run_subscriptions(self, t, mc):
        """Drive _setup_subscriptions on the transport's dedicated loop."""
        mc.ensure_contacts = AsyncMock(return_value=True)
        mc.start_auto_message_fetching = AsyncMock()
        t._run_coro(t._setup_subscriptions())

    def test_set_autoadd_config_called_when_enabled(self):
        """set_autoadd_config(1) is called during _setup_subscriptions when toggle is True."""
        t, mc, _ = _transport_with_mock_mc()
        try:
            mc.commands.set_autoadd_config = AsyncMock(return_value=MagicMock())
            # Default config has meshcore_auto_add_contacts=True (default from ConnectionConfig).
            t.config.meshcore_auto_add_contacts = True
            self._run_subscriptions(t, mc)
            mc.commands.set_autoadd_config.assert_awaited_once_with(1)
        finally:
            _cleanup(t)

    def test_set_autoadd_config_not_called_when_disabled(self):
        """set_autoadd_config is NOT called during _setup_subscriptions when toggle is False."""
        cfg = _mc_config()
        cfg.meshcore_auto_add_contacts = False
        t = MeshCoreTransport(cfg)
        mc = MagicMock()
        mc.get_contact_by_key_prefix.return_value = None
        _install_channel_table(mc)
        mc.commands.set_autoadd_config = AsyncMock(return_value=MagicMock())
        t._mc = mc
        t._connected = True
        loop = asyncio.new_event_loop()
        t._loop = loop
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        t._loop_thread = thread
        try:
            mc.ensure_contacts = AsyncMock(return_value=True)
            mc.start_auto_message_fetching = AsyncMock()
            t._run_coro(t._setup_subscriptions())
            mc.commands.set_autoadd_config.assert_not_awaited()
        finally:
            _cleanup(t)

    def test_on_new_contact_does_not_raise(self):
        """_on_new_contact handles a well-formed event payload without raising."""
        t, mc, _ = _transport_with_mock_mc()
        try:
            event = MagicMock()
            event.payload = {"adv_name": "K7ZVX-Node", "public_key": "abc123deadbeef"}
            t._on_new_contact(event)  # must not raise
        finally:
            _cleanup(t)

    def test_on_new_contact_triggers_roster_refresh(self):
        """_on_new_contact schedules ensure_contacts on the transport loop."""
        import time
        t, mc, loop = _transport_with_mock_mc()
        try:
            mc.ensure_contacts = AsyncMock(return_value=True)
            event = MagicMock()
            event.payload = {"adv_name": "NewNode", "public_key": "cafef00d"}
            t._on_new_contact(event)
            # Drain the dedicated loop so the fire-and-forget coroutine runs.
            drain = asyncio.run_coroutine_threadsafe(asyncio.sleep(0.05), loop)
            drain.result(timeout=2.0)
            mc.ensure_contacts.assert_called()
        finally:
            _cleanup(t)

    def test_on_new_contact_handles_missing_payload_gracefully(self):
        """_on_new_contact survives when event.payload is None or absent."""
        t, mc, _ = _transport_with_mock_mc()
        try:
            event = MagicMock()
            event.payload = None
            t._on_new_contact(event)  # must not raise
        finally:
            _cleanup(t)


# ---------------------------------------------------------------------------
# 13. _resolve_contact — cache-miss refetch behavior
# ---------------------------------------------------------------------------

class TestResolveContact:
    """_resolve_contact: fast path (hit) and miss → refetch → retry behavior."""

    _FAKE_CONTACT = {"public_key": "a" * 64, "adv_name": "RemoteNode", "out_path_len": -1}

    def test_miss_triggers_refetch_and_returns_contact_on_retry(self):
        """On a cache miss, get_contacts(lastmod=0) is called exactly once and
        the contact returned on the subsequent retry is passed back to the caller."""
        t, mc, _ = _transport_with_mock_mc()
        try:
            # First call returns None (miss); second call (after refetch) finds it.
            mc.get_contact_by_key_prefix.side_effect = [None, self._FAKE_CONTACT]
            mc.commands.get_contacts = AsyncMock(return_value=None)
            result = t._resolve_contact("7d4e07237294")
            assert result == self._FAKE_CONTACT
            mc.commands.get_contacts.assert_awaited_once_with(lastmod=0)
        finally:
            _cleanup(t)

    def test_hit_skips_refetch(self):
        """When the first lookup returns a contact, get_contacts is NOT called
        (fast path — no unnecessary serial round-trip to the radio)."""
        t, mc, _ = _transport_with_mock_mc()
        try:
            mc.get_contact_by_key_prefix.return_value = self._FAKE_CONTACT
            mc.commands.get_contacts = AsyncMock(return_value=None)
            result = t._resolve_contact("7d4e07237294")
            assert result == self._FAKE_CONTACT
            mc.commands.get_contacts.assert_not_awaited()
        finally:
            _cleanup(t)
