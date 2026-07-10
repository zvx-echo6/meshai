"""Tests for MeshCore room-server routing (backend capability).

Covers, end to end without a radio:
  * MeshCoreTransport.get_rooms()      — filter contacts to type==3, room shape
  * MeshCoreTransport.login_to_room()  — send_login_sync + LOGIN_SUCCESS/FAILED
  * MeshCoreTransport.send_to_room_async — login-if-password + addressed send
  * secrets_store room-password convention (derive / set / get / delete)
  * channels.parse_meshcore_room()     — room:<pubkey> vs bare channel name
  * MeshCoreBroadcastChannel routing   — room cell -> room send (NOT send_chan_msg),
                                         channel cell -> broadcast (regression),
                                         password room -> login before send.

The meshcore lib is mocked via sys.modules (same pattern as the existing
transport test module), so no lib or socket is required.
"""

import asyncio
import sys
import threading
import types
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Fake meshcore module (register before production imports)
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

    # Superset of the fake used by test_meshcore_transport.py: because test
    # modules share one interpreter and register via setdefault(), whichever
    # module is collected FIRST wins. This fake must therefore satisfy the
    # transport module's advert/connect tests too (send_advert, create_tcp,
    # auto-fetch, disconnect) — plus send_login_sync for room login here.
    class _FakeMeshCore:
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
                r = MagicMock()
                r.is_error.return_value = False
                return r

            @staticmethod
            async def send_msg(dst, text):
                r = MagicMock()
                r.is_error.return_value = False
                return r

            @staticmethod
            async def send_login_sync(dst, pwd):
                r = MagicMock()
                r.is_error.return_value = False
                return r

            @staticmethod
            async def send_advert(flood=False):
                pass

            @staticmethod
            async def set_autoadd_config(value):
                r = MagicMock()
                r.is_error.return_value = False
                return r

    mod.MeshCore = _FakeMeshCore
    return mod


sys.modules.setdefault("meshcore", _build_fake_meshcore())


# ---------------------------------------------------------------------------
# Production imports
# ---------------------------------------------------------------------------

from meshai.config import ConnectionConfig                       # noqa: E402
from meshai.transport.meshcore_transport import MeshCoreTransport  # noqa: E402
from meshai import secrets_store                                 # noqa: E402
from meshai.notifications import channels as ch                  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers (mirror test_meshcore_transport.py)
# ---------------------------------------------------------------------------

def _mc_config(**overrides):
    cfg = ConnectionConfig(meshcore_host="127.0.0.1", meshcore_port=5050)
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _transport_with_mock_mc():
    """MeshCoreTransport with a MagicMock _mc and a real dedicated loop thread."""
    t = MeshCoreTransport(_mc_config())
    mc = MagicMock()
    mc.get_contact_by_key_prefix.return_value = None
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


def _run(t, coro):
    """Run a coroutine on the transport's dedicated loop and return its result."""
    return asyncio.run_coroutine_threadsafe(coro, t._loop).result(timeout=5)


# A room server contact (type==3) and a couple of non-room contacts.
_ROOM_PUBKEY = "cc11" + "d" * 60          # 64-hex
_SAMPLE_CONTACTS = {
    "cc11": {
        "adv_name": "Boise Room", "public_key": _ROOM_PUBKEY,
        "type": 3, "last_advert": 3000, "adv_lat": 43.6, "adv_lon": -116.2,
        "out_path_len": 4,
    },
    "aa11": {
        "adv_name": "Repeater One", "public_key": "aa11" + "e" * 60,
        "type": 2, "last_advert": 1000, "out_path_len": 2,
    },
    "bb22": {
        "adv_name": "Chat Node", "public_key": "bb22" + "f" * 60,
        "type": 0, "last_advert": 2000, "out_path_len": -1,
    },
}


# ===========================================================================
# 1. get_rooms()
# ===========================================================================

class TestGetRooms:
    def test_filters_to_type_3_with_room_shape(self):
        t, mc, _ = _transport_with_mock_mc()
        try:
            mc.ensure_contacts = AsyncMock(return_value=None)
            mc.contacts = dict(_SAMPLE_CONTACTS)
            rooms = t.get_rooms()
            assert len(rooms) == 1, "only the type==3 contact is a room"
            room = rooms[0]
            assert room == {
                "name": "Boise Room",
                "pubkey": _ROOM_PUBKEY,
                "prefix": _ROOM_PUBKEY[:12],
                "path_established": True,     # out_path_len 4 >= 0
            }
        finally:
            _cleanup(t)

    def test_path_established_false_when_no_path(self):
        t, mc, _ = _transport_with_mock_mc()
        try:
            mc.ensure_contacts = AsyncMock(return_value=None)
            mc.contacts = {
                "cc11": {
                    "adv_name": "Flood Room", "public_key": _ROOM_PUBKEY,
                    "type": 3, "out_path_len": -1,
                }
            }
            rooms = t.get_rooms()
            assert rooms[0]["path_established"] is False
        finally:
            _cleanup(t)

    def test_no_rooms_when_none_are_type_3(self):
        t, mc, _ = _transport_with_mock_mc()
        try:
            mc.ensure_contacts = AsyncMock(return_value=None)
            mc.contacts = {k: v for k, v in _SAMPLE_CONTACTS.items() if k != "cc11"}
            assert t.get_rooms() == []
        finally:
            _cleanup(t)

    def test_returns_empty_when_not_connected(self):
        t = MeshCoreTransport(_mc_config())
        assert t.get_rooms() == []


# ===========================================================================
# 2. login_to_room() + send_to_room_async()
# ===========================================================================

class TestRoomLoginAndSend:
    def _wire_room(self, mc):
        room = dict(_SAMPLE_CONTACTS["cc11"])
        mc.get_contact_by_key_prefix.return_value = room
        # Fast path: send_msg carries an expected_ack, and the dispatcher
        # returns a matching ACK, so delivery succeeds with a SINGLE send_msg
        # (no discovery/resend leg) — keeps the login assertions clean.
        ok = MagicMock()
        ok.is_error.return_value = False
        ok.payload = {"type": 0, "expected_ack": b"\x01\x02\x03\x04"}
        mc.commands.send_msg = AsyncMock(return_value=ok)
        mc.dispatcher.wait_for_event = AsyncMock(return_value=MagicMock())  # ACK
        return room

    def test_open_room_send_no_login(self):
        """No password -> send_msg to the room pubkey, send_login_sync NOT called."""
        t, mc, _ = _transport_with_mock_mc()
        try:
            room = self._wire_room(mc)
            mc.commands.send_login_sync = AsyncMock()
            ok = _run(t, t.send_to_room_async(_ROOM_PUBKEY, "hi room", password=None))
            assert ok is True
            mc.commands.send_login_sync.assert_not_awaited()
            mc.commands.send_msg.assert_awaited_with(room, "hi room")
            assert _ROOM_PUBKEY not in t._logged_in_rooms
        finally:
            _cleanup(t)

    def test_password_room_logs_in_before_send(self):
        """A password -> send_login_sync (LOGIN_SUCCESS) THEN send_msg; room tracked."""
        t, mc, _ = _transport_with_mock_mc()
        try:
            room = self._wire_room(mc)
            login_ok = MagicMock()
            login_ok.is_error.return_value = False
            mc.commands.send_login_sync = AsyncMock(return_value=login_ok)

            ok = _run(t, t.send_to_room_async(_ROOM_PUBKEY, "secret hi", password="pw"))
            assert ok is True
            mc.commands.send_login_sync.assert_awaited_once_with(room, "pw")
            mc.commands.send_msg.assert_awaited_with(room, "secret hi")
            assert _ROOM_PUBKEY in t._logged_in_rooms
        finally:
            _cleanup(t)

    def test_login_reused_on_second_send(self):
        """Already-logged-in room -> no second login on the next send."""
        t, mc, _ = _transport_with_mock_mc()
        try:
            self._wire_room(mc)
            login_ok = MagicMock()
            login_ok.is_error.return_value = False
            mc.commands.send_login_sync = AsyncMock(return_value=login_ok)

            _run(t, t.send_to_room_async(_ROOM_PUBKEY, "one", password="pw"))
            _run(t, t.send_to_room_async(_ROOM_PUBKEY, "two", password="pw"))
            mc.commands.send_login_sync.assert_awaited_once()  # login only once
        finally:
            _cleanup(t)

    def test_login_failure_surfaces_and_no_send(self):
        """LOGIN_FAILED -> send_to_room_async returns False and does NOT send_msg."""
        t, mc, _ = _transport_with_mock_mc()
        try:
            self._wire_room(mc)
            login_err = MagicMock()
            login_err.is_error.return_value = True     # LOGIN_FAILED
            mc.commands.send_login_sync = AsyncMock(return_value=login_err)

            ok = _run(t, t.send_to_room_async(_ROOM_PUBKEY, "nope", password="bad"))
            assert ok is False, "login failure must surface as a failed send"
            mc.commands.send_msg.assert_not_awaited()
            assert _ROOM_PUBKEY not in t._logged_in_rooms
        finally:
            _cleanup(t)

    def test_login_failure_then_retry_relogins(self):
        """After a failed login the state is cleared, so the next send re-logins."""
        t, mc, _ = _transport_with_mock_mc()
        try:
            self._wire_room(mc)
            login_err = MagicMock(); login_err.is_error.return_value = True
            login_ok = MagicMock(); login_ok.is_error.return_value = False
            mc.commands.send_login_sync = AsyncMock(side_effect=[login_err, login_ok])

            first = _run(t, t.send_to_room_async(_ROOM_PUBKEY, "a", password="pw"))
            second = _run(t, t.send_to_room_async(_ROOM_PUBKEY, "b", password="pw"))
            assert first is False
            assert second is True
            assert mc.commands.send_login_sync.await_count == 2  # re-login attempted
        finally:
            _cleanup(t)


# ===========================================================================
# 3. send_message_async(meshcore_room=...) routes to room, not channel
# ===========================================================================

class TestSendMessageAsyncRoom:
    def test_meshcore_room_calls_send_msg_not_send_chan_msg(self):
        """A room target goes through the queue -> send_msg; send_chan_msg untouched."""
        t, mc, _ = _transport_with_mock_mc()
        try:
            room = dict(_SAMPLE_CONTACTS["cc11"])
            mc.get_contact_by_key_prefix.return_value = room
            ok = MagicMock(); ok.is_error.return_value = False
            ok.payload = {"type": 0, "expected_ack": b"\x01\x02\x03\x04"}
            mc.commands.send_msg = AsyncMock(return_value=ok)
            mc.commands.send_chan_msg = AsyncMock()
            mc.dispatcher.wait_for_event = AsyncMock(return_value=MagicMock())
            # Arm the send queue on the MC loop (send_message_async requires it).
            asyncio.run_coroutine_threadsafe(
                _arm_queue(t), t._loop
            ).result(timeout=5)

            result = _run(
                t, t.send_message_async("hi", destination=None, meshcore_room=_ROOM_PUBKEY)
            )
            assert result is True
            mc.commands.send_msg.assert_awaited_with(room, "hi")
            mc.commands.send_chan_msg.assert_not_awaited()
        finally:
            t._cancel_mc_queue()   # stop the drain task before the loop closes
            _cleanup(t)


async def _arm_queue(t):
    t._start_mc_queue()


# ===========================================================================
# 4. secrets_store room-password convention
# ===========================================================================

class TestRoomPasswordSecrets:
    def test_env_var_derivation_uses_12_hex_prefix_upper(self):
        var = secrets_store.room_pwd_env_var(_ROOM_PUBKEY)
        assert var == "MESHCORE_ROOM_" + _ROOM_PUBKEY[:12].upper() + "_PWD"

    def test_prefix_and_full_key_resolve_same_var(self):
        full = secrets_store.room_pwd_env_var(_ROOM_PUBKEY)
        prefix = secrets_store.room_pwd_env_var(_ROOM_PUBKEY[:12])
        assert full == prefix

    def test_empty_pubkey_raises(self):
        with pytest.raises(ValueError):
            secrets_store.room_pwd_env_var("")

    def test_set_get_delete_roundtrip(self, tmp_path):
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()
        assert secrets_store.get_room_password(_ROOM_PUBKEY, cfg_dir) is None
        assert secrets_store.room_password_is_set(_ROOM_PUBKEY, cfg_dir) is False

        secrets_store.set_room_password(_ROOM_PUBKEY, "topsecret", cfg_dir)
        assert secrets_store.get_room_password(_ROOM_PUBKEY, cfg_dir) == "topsecret"
        assert secrets_store.room_password_is_set(_ROOM_PUBKEY, cfg_dir) is True

        secrets_store.delete_room_password(_ROOM_PUBKEY, cfg_dir)
        assert secrets_store.get_room_password(_ROOM_PUBKEY, cfg_dir) is None

    def test_set_empty_value_clears(self, tmp_path):
        cfg_dir = tmp_path / "config"; cfg_dir.mkdir()
        secrets_store.set_room_password(_ROOM_PUBKEY, "x", cfg_dir)
        secrets_store.set_room_password(_ROOM_PUBKEY, "", cfg_dir)
        assert secrets_store.get_room_password(_ROOM_PUBKEY, cfg_dir) is None

    def test_get_missing_pubkey_none(self, tmp_path):
        cfg_dir = tmp_path / "config"; cfg_dir.mkdir()
        assert secrets_store.get_room_password("", cfg_dir) is None


# ===========================================================================
# 5. channels.parse_meshcore_room()
# ===========================================================================

class TestParseMeshcoreRoom:
    def test_room_prefix_extracts_pubkey(self):
        assert ch.parse_meshcore_room("room:" + _ROOM_PUBKEY) == _ROOM_PUBKEY

    def test_bare_channel_name_is_none(self):
        assert ch.parse_meshcore_room("AIDA") is None

    def test_empty_room_pubkey_is_none(self):
        assert ch.parse_meshcore_room("room:") is None
        assert ch.parse_meshcore_room("room:   ") is None

    def test_none_and_empty_are_none(self):
        assert ch.parse_meshcore_room(None) is None
        assert ch.parse_meshcore_room("") is None


# ===========================================================================
# 6. MeshCoreBroadcastChannel routing (room vs channel)
# ===========================================================================

class _RecConnector:
    """Records send_message_async kwargs; looks like a single meshcore transport."""
    transport_name = "meshcore"
    max_chars = 200

    def __init__(self):
        self.calls = []

    async def send_message_async(self, text=None, destination=None, channel=0,
                                 transport=None, meshcore_channel=None,
                                 meshcore_room=None, meshcore_room_password=None):
        self.calls.append({
            "text": text, "destination": destination,
            "meshcore_channel": meshcore_channel,
            "meshcore_room": meshcore_room,
            "meshcore_room_password": meshcore_room_password,
            "transport": transport,
        })
        return True


def _run_sync(coro):
    """Run a coroutine on a fresh loop, restoring the prior event-loop state.

    Creating+closing a loop without restoring leaves a CLOSED loop as this
    thread's default, which breaks sibling test modules that call
    asyncio.get_event_loop() (e.g. the advert-scheduler tests). We snapshot the
    current loop and put it back afterwards so there is no cross-module leak.
    """
    try:
        prev = asyncio.get_event_loop()
    except RuntimeError:
        prev = None
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(prev)


def _payload(msg="fire near you"):
    p = MagicMock()
    p.message = msg
    p.chunk_index = 0   # pre-chunked -> single send, no renderer indirection
    return p


class TestMeshCoreBroadcastChannelRouting:
    def test_room_cell_routes_to_room_send(self, monkeypatch):
        """A room:<pubkey> cell -> send_message_async(meshcore_room=pubkey),
        never a channel broadcast."""
        monkeypatch.setattr(
            "meshai.secrets_store.get_room_password", lambda pk: None
        )
        conn = _RecConnector()
        chan = ch.MeshCoreBroadcastChannel(conn, meshcore_channel="room:" + _ROOM_PUBKEY)
        ok = _run_sync(chan.deliver(_payload(), MagicMock()))
        assert ok is True
        assert len(conn.calls) == 1
        call = conn.calls[0]
        assert call["meshcore_room"] == _ROOM_PUBKEY
        assert call["meshcore_channel"] is None, "must NOT route as a channel broadcast"

    def test_room_cell_passes_configured_password(self, monkeypatch):
        monkeypatch.setattr(
            "meshai.secrets_store.get_room_password", lambda pk: "hunter2"
        )
        conn = _RecConnector()
        chan = ch.MeshCoreBroadcastChannel(conn, meshcore_channel="room:" + _ROOM_PUBKEY)
        _run_sync(chan.deliver(_payload(), MagicMock()))
        assert conn.calls[0]["meshcore_room_password"] == "hunter2"

    def test_channel_cell_still_broadcasts(self, monkeypatch):
        """Regression: a bare channel name -> meshcore_channel broadcast, no room."""
        conn = _RecConnector()
        chan = ch.MeshCoreBroadcastChannel(conn, meshcore_channel="AIDA")
        ok = _run_sync(chan.deliver(_payload(), MagicMock()))
        assert ok is True
        call = conn.calls[0]
        assert call["meshcore_channel"] == "AIDA"
        assert call["meshcore_room"] is None, "channel cell must NOT trigger a room send"

    def test_no_channel_configured_is_noop(self):
        """No cell set -> nothing sent (unchanged behavior)."""
        conn = _RecConnector()
        chan = ch.MeshCoreBroadcastChannel(conn, meshcore_channel=None)
        ok = _run_sync(chan.deliver(_payload(), MagicMock()))
        assert ok is False
        assert conn.calls == []
