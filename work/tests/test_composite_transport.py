"""Hermetic tests for CompositeTransport (Phase 4).

All tests use fake child transports — no real sockets, threads, or meshcore
lib required.  The helpers below replicate the MeshMessage dataclass so the
tests run without importing the full connector chain.
"""

import asyncio
import types
from dataclasses import dataclass, field
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from meshai.transport.composite_transport import CompositeTransport, _child_name
from meshai.connector import MeshMessage


# ---------------------------------------------------------------------------
# Fake child transport helpers
# ---------------------------------------------------------------------------

class FakeChild:
    """Minimal MeshTransport stand-in for unit tests.

    Tracks calls and exposes enough surface for CompositeTransport to work.
    """

    def __init__(
        self,
        name: str,
        node_id: str = "!aabbccdd",
        max_chars_val: int = 200,
        connected_val: bool = True,
        known_nodes: Optional[dict] = None,
    ) -> None:
        self.transport_name = name
        self._connected = connected_val
        self._node_id = node_id
        self._max_chars = max_chars_val
        self._known_nodes: dict[str, str] = known_nodes or {}

        # Call-tracking
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.send_calls: list[dict] = []
        self._callback = None
        self._callback_loop = None

    # --- MeshTransport interface ---

    def connect(self) -> None:
        self.connect_calls += 1
        self._connected = True

    def disconnect(self) -> None:
        self.disconnect_calls += 1
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def my_node_id(self) -> Optional[str]:
        return self._node_id

    @property
    def max_chars(self) -> int:
        return self._max_chars

    def send_message(
        self,
        text: str,
        destination: Optional[str] = None,
        channel: int = 0,
        transport: Optional[str] = None,
    ) -> bool:
        self.send_calls.append(
            {"text": text, "destination": destination, "channel": channel, "transport": transport}
        )
        return True

    def set_message_callback(self, callback, loop) -> None:
        self._callback = callback
        self._callback_loop = loop

    def get_node_name(self, node_id: str) -> str:
        return self._known_nodes.get(node_id, node_id)

    def get_node_position(self, node_id: str) -> Optional[tuple]:
        return None

    # --- Test helper: simulate inbound message ---

    async def _simulate_inbound(self, msg: MeshMessage) -> None:
        """Call the registered callback directly (simulates an inbound packet)."""
        if self._callback:
            await self._callback(msg)


# ---------------------------------------------------------------------------
# Factory-level test
# ---------------------------------------------------------------------------

class TestFactory:
    def test_both_returns_composite(self) -> None:
        """factory transport='both' → CompositeTransport with 2 children."""
        from meshai.transport.factory import build_transport

        cfg = types.SimpleNamespace(
            transport="both",
            type="tcp",
            tcp_host="127.0.0.1",
            tcp_port=4403,
            meshcore_host="127.0.0.1",
            meshcore_port=5050,
        )
        t = build_transport(cfg)
        assert isinstance(t, CompositeTransport)
        assert len(t.children) == 2
        # Child order: Meshtastic first, MeshCore second.
        from meshai.connector import MeshtasticTransport
        from meshai.transport.meshcore_transport import MeshCoreTransport
        assert isinstance(t.children[0], MeshtasticTransport)
        assert isinstance(t.children[1], MeshCoreTransport)


# ---------------------------------------------------------------------------
# max_chars
# ---------------------------------------------------------------------------

class TestMaxChars:
    def test_fixed_universal_budget_no_config(self) -> None:
        """Without a config, CompositeTransport falls back to the 140 constant."""
        mt = FakeChild("meshtastic", max_chars_val=200)
        mc = FakeChild("meshcore", max_chars_val=140)
        comp = CompositeTransport([mt, mc])
        assert comp.max_chars == 140

    def test_fixed_universal_budget_from_config(self) -> None:
        """With a config, CompositeTransport reads mesh_max_chars directly."""
        from meshai.config import ConnectionConfig
        cfg = ConnectionConfig(transport="both", mesh_max_chars=140)
        mt = FakeChild("meshtastic", max_chars_val=200)
        mc = FakeChild("meshcore", max_chars_val=140)
        comp = CompositeTransport([mt, mc], config=cfg)
        assert comp.max_chars == 140

    def test_fixed_universal_budget_ignores_child_values(self) -> None:
        """CompositeTransport must NOT take min(children); it sources config."""
        from meshai.config import ConnectionConfig
        cfg = ConnectionConfig(transport="both", mesh_max_chars=140)
        # Even if a child would report 230, the composite must return 140.
        child = FakeChild("meshtastic", max_chars_val=230)
        comp = CompositeTransport([child], config=cfg)
        assert comp.max_chars == 140


# ---------------------------------------------------------------------------
# connect / disconnect
# ---------------------------------------------------------------------------

class TestLifecycle:
    def test_connect_fans_to_all(self) -> None:
        a = FakeChild("meshtastic", connected_val=False)
        b = FakeChild("meshcore", connected_val=False)
        comp = CompositeTransport([a, b])
        comp.connect()
        assert a.connect_calls == 1
        assert b.connect_calls == 1

    def test_connect_continues_after_failure(self) -> None:
        """One child failing to connect must not stop the other."""
        bad = FakeChild("meshtastic", connected_val=False)
        bad.connect = MagicMock(side_effect=RuntimeError("boom"))
        good = FakeChild("meshcore", connected_val=False)
        comp = CompositeTransport([bad, good])
        comp.connect()  # must not raise
        assert good.connect_calls == 1
        assert comp.connected  # good child is up

    def test_connected_true_if_any(self) -> None:
        down = FakeChild("meshtastic", connected_val=False)
        up = FakeChild("meshcore", connected_val=True)
        comp = CompositeTransport([down, up])
        assert comp.connected is True

    def test_connected_false_if_none(self) -> None:
        a = FakeChild("meshtastic", connected_val=False)
        b = FakeChild("meshcore", connected_val=False)
        comp = CompositeTransport([a, b])
        assert comp.connected is False

    def test_disconnect_fans_to_all(self) -> None:
        a = FakeChild("meshtastic")
        b = FakeChild("meshcore")
        comp = CompositeTransport([a, b])
        comp.disconnect()
        assert a.disconnect_calls == 1
        assert b.disconnect_calls == 1

    def test_disconnect_guarded(self) -> None:
        """disconnect() must not raise even if a child raises."""
        bad = FakeChild("meshtastic")
        bad.disconnect = MagicMock(side_effect=RuntimeError("oops"))
        good = FakeChild("meshcore")
        comp = CompositeTransport([bad, good])
        comp.disconnect()  # must not raise
        assert good.disconnect_calls == 1


# ---------------------------------------------------------------------------
# send_message — broadcast
# ---------------------------------------------------------------------------

class TestBroadcast:
    def test_broadcast_sends_to_all(self) -> None:
        """With meshcore_channel set, broadcast fans to both children."""
        mt = FakeChild("meshtastic")
        mc = FakeChild("meshcore")
        comp = CompositeTransport([mt, mc])
        result = comp.send_message("hello mesh", meshcore_channel=0)
        assert result is True
        assert len(mt.send_calls) == 1
        assert len(mc.send_calls) == 1
        assert mt.send_calls[0]["destination"] is None
        assert mc.send_calls[0]["destination"] is None

    def test_broadcast_meshcore_skipped_when_channel_none(self) -> None:
        """meshcore_channel=None → MeshCore child silently skipped; Meshtastic still gets it."""
        mt = FakeChild("meshtastic")
        mc = FakeChild("meshcore")
        comp = CompositeTransport([mt, mc])
        result = comp.send_message("hello mesh", meshcore_channel=None)
        assert result is True  # Meshtastic succeeded
        assert len(mt.send_calls) == 1
        assert len(mc.send_calls) == 0  # MeshCore was skipped

    def test_broadcast_meshcore_uses_meshcore_channel(self) -> None:
        """MeshCore child receives meshcore_channel, Meshtastic receives channel."""
        mt = FakeChild("meshtastic")
        mc = FakeChild("meshcore")
        comp = CompositeTransport([mt, mc])
        result = comp.send_message("hello", channel=1, meshcore_channel=3)
        assert result is True
        assert len(mt.send_calls) == 1
        assert len(mc.send_calls) == 1
        assert mt.send_calls[0]["channel"] == 1   # Meshtastic gets `channel`
        assert mc.send_calls[0]["channel"] == 3   # MeshCore gets `meshcore_channel`

    def test_broadcast_meshtastic_only_when_no_meshcore_child(self) -> None:
        """When there is no MeshCore child, meshcore_channel is irrelevant."""
        mt = FakeChild("meshtastic")
        comp = CompositeTransport([mt])
        result = comp.send_message("hello", channel=2, meshcore_channel=5)
        assert result is True
        assert len(mt.send_calls) == 1
        assert mt.send_calls[0]["channel"] == 2

    def test_broadcast_skips_disconnected_child(self) -> None:
        """Disconnected Meshtastic child is skipped; MeshCore (with channel set) is sent."""
        mt = FakeChild("meshtastic", connected_val=False)
        mc = FakeChild("meshcore")
        comp = CompositeTransport([mt, mc])
        result = comp.send_message("hi", meshcore_channel=0)
        assert result is True
        assert len(mt.send_calls) == 0
        assert len(mc.send_calls) == 1

    def test_broadcast_true_if_at_least_one_ok(self) -> None:
        """True if any child succeeds; Meshtastic failing + MeshCore succeeding → True."""
        mt = FakeChild("meshtastic")
        mt.send_message = MagicMock(return_value=False)
        mc = FakeChild("meshcore")
        comp = CompositeTransport([mt, mc])
        result = comp.send_message("test", meshcore_channel=0)
        assert result is True


# ---------------------------------------------------------------------------
# send_message — hinted DM
# ---------------------------------------------------------------------------

class TestHintedDM:
    def test_hinted_meshcore_only(self) -> None:
        mt = FakeChild("meshtastic")
        mc = FakeChild("meshcore")
        comp = CompositeTransport([mt, mc])
        result = comp.send_message("reply", destination="abc123", transport="meshcore")
        assert result is True
        assert len(mc.send_calls) == 1
        assert len(mt.send_calls) == 0
        assert mc.send_calls[0]["destination"] == "abc123"

    def test_hinted_meshtastic_only(self) -> None:
        mt = FakeChild("meshtastic")
        mc = FakeChild("meshcore")
        comp = CompositeTransport([mt, mc])
        result = comp.send_message("reply", destination="!deadbeef", transport="meshtastic")
        assert result is True
        assert len(mt.send_calls) == 1
        assert len(mc.send_calls) == 0

    def test_unknown_hint_returns_false(self) -> None:
        mt = FakeChild("meshtastic")
        comp = CompositeTransport([mt])
        result = comp.send_message("x", destination="y", transport="nonexistent")
        assert result is False
        assert len(mt.send_calls) == 0

    def test_hinted_child_disconnected_returns_false(self) -> None:
        mt = FakeChild("meshtastic", connected_val=False)
        mc = FakeChild("meshcore")
        comp = CompositeTransport([mt, mc])
        result = comp.send_message("x", destination="y", transport="meshtastic")
        assert result is False


# ---------------------------------------------------------------------------
# send_message — unhinted DM
# ---------------------------------------------------------------------------

class TestUnhintedDM:
    def test_prefers_resolving_child(self) -> None:
        mt = FakeChild("meshtastic", known_nodes={"!aabbccdd": "NodeA"})
        mc = FakeChild("meshcore")
        comp = CompositeTransport([mt, mc])
        result = comp.send_message("hello", destination="!aabbccdd")
        assert result is True
        assert len(mt.send_calls) == 1
        assert len(mc.send_calls) == 0

    def test_fans_to_all_when_none_resolves(self) -> None:
        mt = FakeChild("meshtastic")
        mc = FakeChild("meshcore")
        comp = CompositeTransport([mt, mc])
        result = comp.send_message("hello", destination="!unknown")
        assert result is True
        assert len(mt.send_calls) == 1
        assert len(mc.send_calls) == 1


# ---------------------------------------------------------------------------
# set_message_callback — inbound forwarding and self-filter
# ---------------------------------------------------------------------------

class TestInboundCallback:
    """Use asyncio.run() for each coroutine so the test always gets a fresh
    event loop regardless of what earlier tests in the suite may have done.
    FakeChild._simulate_inbound calls the wrapper directly (no
    call_soon_threadsafe), so we don't actually need the loop we pass to
    set_message_callback — we just need *some* AbstractEventLoop object to
    satisfy the call signature.
    """

    @staticmethod
    def _make_loop():
        """Return a new event loop used only as a placeholder for the callback
        registration (FakeChild doesn't schedule on it)."""
        return asyncio.new_event_loop()

    def test_inbound_forwarded_with_correct_transport(self) -> None:
        mt = FakeChild("meshtastic", node_id="!aaaaaaaa")
        mc = FakeChild("meshcore", node_id="!bbbbbbbb")
        comp = CompositeTransport([mt, mc])

        received: list[MeshMessage] = []

        async def run():
            async def cb(msg):
                received.append(msg)

            loop = self._make_loop()
            comp.set_message_callback(cb, loop)
            loop.close()

            inbound = MeshMessage(
                sender_id="!cccccccc",
                sender_name="Other",
                text="hello",
                channel=0,
                is_dm=True,
                transport="meshcore",
            )
            await mc._simulate_inbound(inbound)

        asyncio.run(run())
        assert len(received) == 1
        assert received[0].transport == "meshcore"

    def test_self_filter_drops_own_message(self) -> None:
        mt = FakeChild("meshtastic", node_id="!selfid1")
        comp = CompositeTransport([mt])

        received: list[MeshMessage] = []

        async def run():
            async def cb(msg):
                received.append(msg)

            loop = self._make_loop()
            comp.set_message_callback(cb, loop)
            loop.close()

            # sender_id matches the child's own node ID → should be dropped
            echo = MeshMessage(
                sender_id="!selfid1",
                sender_name="Self",
                text="echo",
                channel=0,
                is_dm=False,
            )
            await mt._simulate_inbound(echo)

        asyncio.run(run())
        assert len(received) == 0

    def test_non_self_not_dropped(self) -> None:
        mt = FakeChild("meshtastic", node_id="!selfid1")
        comp = CompositeTransport([mt])

        received: list[MeshMessage] = []

        async def run():
            async def cb(msg):
                received.append(msg)

            loop = self._make_loop()
            comp.set_message_callback(cb, loop)
            loop.close()

            msg = MeshMessage(
                sender_id="!otherid",
                sender_name="Friend",
                text="hi",
                channel=0,
                is_dm=True,
            )
            await mt._simulate_inbound(msg)

        asyncio.run(run())
        assert len(received) == 1

    def test_transport_tag_backfilled_when_missing(self) -> None:
        """If msg.transport is empty/falsy the wrapper sets it to the child name."""
        mc = FakeChild("meshcore", node_id="!mc00")
        comp = CompositeTransport([mc])

        received: list[MeshMessage] = []

        async def run():
            async def cb(msg):
                received.append(msg)

            loop = self._make_loop()
            comp.set_message_callback(cb, loop)
            loop.close()

            msg = MeshMessage(
                sender_id="!other",
                sender_name="X",
                text="ping",
                channel=0,
                is_dm=True,
                transport="",  # missing tag
            )
            await mc._simulate_inbound(msg)

        asyncio.run(run())
        assert len(received) == 1
        assert received[0].transport == "meshcore"


# ---------------------------------------------------------------------------
# Reply routing — responder seam
# ---------------------------------------------------------------------------

class TestReplyRouting:
    """Verify that a meshcore-tagged inbound message causes send_message to be
    called with transport='meshcore' via the Responder."""

    def test_responder_threads_transport(self) -> None:
        from meshai.responder import Responder
        from meshai.config import ResponseConfig

        mt = FakeChild("meshtastic")
        mc = FakeChild("meshcore")
        comp = CompositeTransport([mt, mc])

        cfg = ResponseConfig(delay_min=0.0, delay_max=0.0)
        responder = Responder(cfg, comp)

        asyncio.run(
            responder.send_response(
                "pong",
                destination="abc123",
                channel=0,
                transport="meshcore",
            )
        )

        # Only meshcore child should have been called.
        assert len(mc.send_calls) == 1
        assert mc.send_calls[0]["destination"] == "abc123"
        assert len(mt.send_calls) == 0


# ---------------------------------------------------------------------------
# meshtastic_child helper
# ---------------------------------------------------------------------------

class TestMeshtasticChild:
    def test_returns_meshtastic_transport(self) -> None:
        from meshai.connector import MeshtasticTransport
        from meshai.transport.meshcore_transport import MeshCoreTransport

        cfg = types.SimpleNamespace(
            type="tcp",
            tcp_host="127.0.0.1",
            tcp_port=4403,
            meshcore_host="127.0.0.1",
            meshcore_port=5050,
        )
        mt = MeshtasticTransport(cfg)
        mc = MeshCoreTransport(cfg)
        comp = CompositeTransport([mt, mc])
        child = comp.meshtastic_child()
        assert child is mt

    def test_returns_none_when_absent(self) -> None:
        from meshai.transport.meshcore_transport import MeshCoreTransport

        cfg = types.SimpleNamespace(
            meshcore_host="127.0.0.1",
            meshcore_port=5050,
        )
        mc = MeshCoreTransport(cfg)
        comp = CompositeTransport([mc])
        assert comp.meshtastic_child() is None


# ---------------------------------------------------------------------------
# _child_name helper
# ---------------------------------------------------------------------------

class TestChildName:
    def test_uses_transport_name_attr(self) -> None:
        child = FakeChild("foobar")
        assert _child_name(child) == "foobar"

    def test_derives_from_class_name(self) -> None:
        class MyTransport:
            pass

        obj = MyTransport()
        assert _child_name(obj) == "my"

    def test_strips_transport_suffix(self) -> None:
        class SomeTransport:
            pass

        assert _child_name(SomeTransport()) == "some"


# ---------------------------------------------------------------------------
# should_drop helper (unit test for routing logic)
# ---------------------------------------------------------------------------

class TestShouldDrop:
    def test_drops_own_sender_id(self) -> None:
        comp = CompositeTransport([FakeChild("x")])
        msg = MeshMessage("!abc", "Self", "hi", 0, False)
        assert comp._should_drop(msg, "!abc") is True

    def test_keeps_other_sender(self) -> None:
        comp = CompositeTransport([FakeChild("x")])
        msg = MeshMessage("!xyz", "Other", "hi", 0, False)
        assert comp._should_drop(msg, "!abc") is False

    def test_none_node_id_no_drop(self) -> None:
        comp = CompositeTransport([FakeChild("x")])
        msg = MeshMessage("!abc", "X", "hi", 0, False)
        assert comp._should_drop(msg, None) is False


# ---------------------------------------------------------------------------
# _resolve_child_for_hint (unit test for routing logic)
# ---------------------------------------------------------------------------

class TestResolveChildForHint:
    def test_resolves_known_name(self) -> None:
        mt = FakeChild("meshtastic")
        mc = FakeChild("meshcore")
        comp = CompositeTransport([mt, mc])
        assert comp._resolve_child_for_hint("meshcore") is mc
        assert comp._resolve_child_for_hint("meshtastic") is mt

    def test_returns_none_for_unknown(self) -> None:
        comp = CompositeTransport([FakeChild("meshtastic")])
        assert comp._resolve_child_for_hint("lora") is None
