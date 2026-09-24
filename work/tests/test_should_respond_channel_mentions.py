"""Matrix tests for MessageRouter.should_respond()'s channel-@mention path
(BotConfig.respond_to_channel_mentions -- opt-in, OFF by default).

Covers: disabled-by-default, enabled+mention in an allowed channel -> yes,
other channel -> no, no mention -> no, a Meshtastic reply_id pointing at a
packet AIDA sent -> yes (mention-free), own message -> no, per-(sender,
channel) cooldown -> no, and that the always-on DM path is completely
unaffected by any of this (same behavior with the feature on or off).
"""
import sys
import types
from unittest.mock import MagicMock

import pytest

try:
    import pydantic  # noqa: F401
    _NEEDS_SDK_STUBS = False
except Exception:
    _NEEDS_SDK_STUBS = True

if _NEEDS_SDK_STUBS:
    if "openai" not in sys.modules:
        _openai_stub = types.ModuleType("openai")

        class _StubAsyncOpenAI:
            def __init__(self, api_key=None, base_url=None):
                self.api_key = api_key
                self.base_url = base_url
                self.chat = types.SimpleNamespace(
                    completions=types.SimpleNamespace(create=None)
                )

            async def close(self):
                pass

        _openai_stub.AsyncOpenAI = _StubAsyncOpenAI
        sys.modules["openai"] = _openai_stub

    if "anthropic" not in sys.modules:
        _anthropic_stub = types.ModuleType("anthropic")
        _anthropic_stub.AsyncAnthropic = object
        sys.modules["anthropic"] = _anthropic_stub

    if "google.genai" not in sys.modules:
        import google  # real namespace package; importable without pydantic

        _genai_stub = types.ModuleType("google.genai")
        _genai_stub.types = types.ModuleType("google.genai.types")
        _genai_stub.Client = object
        sys.modules["google.genai"] = _genai_stub
        google.genai = _genai_stub

    if "httpx" not in sys.modules:
        _httpx_stub = types.ModuleType("httpx")

        class _StubAsyncClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        class _ConnectError(Exception):
            pass

        class _TimeoutException(Exception):
            pass

        _httpx_stub.AsyncClient = _StubAsyncClient
        _httpx_stub.ConnectError = _ConnectError
        _httpx_stub.TimeoutException = _TimeoutException
        sys.modules["httpx"] = _httpx_stub

from meshai.config import Config
from meshai.connector import MeshMessage
from meshai.dedupe import InboundDedupeCache
from meshai.router import MessageRouter


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeMeshtasticChild:
    def __init__(self):
        self._owned: set[int] = set()

    def owns_packet_id(self, packet_id) -> bool:
        return packet_id in self._owned

    def note_own_packet_id(self, packet_id: int) -> None:
        self._owned.add(packet_id)


class FakeConnector:
    """Bare (non-composite) connector stand-in: owns_packet_id lives on the
    connector itself, no meshtastic_child()."""

    def __init__(self, my_node_id="!aida0000"):
        self.my_node_id = my_node_id
        self.max_chars = 200
        self._owned: set[int] = set()

    def owns_packet_id(self, packet_id) -> bool:
        return packet_id in self._owned

    def note_own_packet_id(self, packet_id: int) -> None:
        self._owned.add(packet_id)

    def self_info(self) -> dict:
        return {"connected": False}


class FakeCompositeConnector:
    """Composite-style connector: owns_packet_id lives on the meshtastic
    child; self_info() reports the MeshCore device's own name."""

    def __init__(self, my_node_id="!aida0000", mc_self_name="AIDA"):
        self.my_node_id = my_node_id
        self.max_chars = 200
        self._mt_child = FakeMeshtasticChild()
        self._mc_self_name = mc_self_name

    def meshtastic_child(self):
        return self._mt_child

    def self_info(self) -> dict:
        return {"name": self._mc_self_name, "connected": True}


def _make_router(connector, **bot_overrides) -> MessageRouter:
    config = Config()
    config.bot.mt_node = "!a1daa1da (AIDA-N2)"
    for k, v in bot_overrides.items():
        setattr(config.bot, k, v)

    history = MagicMock()
    dispatcher = MagicMock()
    llm_backend = MagicMock()

    return MessageRouter(
        config=config,
        connector=connector,
        history=history,
        dispatcher=dispatcher,
        llm_backend=llm_backend,
    )


def _mt_message(text="@AIDA status?", channel=1, sender_id="!bob00001", packet=None) -> MeshMessage:
    return MeshMessage(
        sender_id=sender_id,
        sender_name="Bob",
        text=text,
        channel=channel,
        is_dm=False,
        transport="meshtastic",
        packet=packet,
    )


def _mc_message(
    text="@AIDA status?", channel=3, channel_name="#aida", sender_id="mcname:Bob",
    sender_timestamp=None,
) -> MeshMessage:
    return MeshMessage(
        sender_id=sender_id,
        sender_name="Bob",
        text=text,
        channel=channel,
        is_dm=False,
        transport="meshcore",
        channel_name=channel_name,
        sender_timestamp=sender_timestamp,
    )


def _dm_message(
    text="hello", sender_id="!bob00001", transport="meshtastic", sender_timestamp=None,
) -> MeshMessage:
    return MeshMessage(
        sender_id=sender_id,
        sender_name="Bob",
        text=text,
        channel=0,
        is_dm=True,
        transport=transport,
        sender_timestamp=sender_timestamp,
    )


# ---------------------------------------------------------------------------
# Disabled by default
# ---------------------------------------------------------------------------


def test_disabled_by_default_ignores_mention():
    router = _make_router(FakeConnector())
    assert router.config.bot.respond_to_channel_mentions is False
    assert router.should_respond(_mt_message()) is False


# ---------------------------------------------------------------------------
# Enabled + mention in allowed channel -> yes
# ---------------------------------------------------------------------------


def test_enabled_mention_in_allowed_channel_yes():
    router = _make_router(
        FakeConnector(),
        respond_to_channel_mentions=True,
        mention_channels=[1],
        mention_names=["AIDA"],
    )
    assert router.should_respond(_mt_message(text="@AIDA status?", channel=1)) is True


def test_enabled_mention_in_other_channel_no():
    router = _make_router(
        FakeConnector(),
        respond_to_channel_mentions=True,
        mention_channels=[1],
        mention_names=["AIDA"],
    )
    assert router.should_respond(_mt_message(text="@AIDA status?", channel=7)) is False


def test_enabled_no_mention_no():
    router = _make_router(
        FakeConnector(),
        respond_to_channel_mentions=True,
        mention_channels=[1],
        mention_names=["AIDA"],
    )
    assert router.should_respond(_mt_message(text="just chatting, no mention", channel=1)) is False


# ---------------------------------------------------------------------------
# Meshtastic reply_id pointing at AIDA's own packet -> yes (mention-free)
# ---------------------------------------------------------------------------


def test_reply_id_to_aida_packet_yes_without_mention():
    connector = FakeCompositeConnector()
    connector.meshtastic_child().note_own_packet_id(999)
    router = _make_router(
        connector,
        respond_to_channel_mentions=True,
        mention_channels=[1],
        mention_names=["AIDA"],
    )
    msg = _mt_message(
        text="no mention here, just a threaded reply",
        channel=1,
        packet={"id": 12345, "decoded": {"replyId": 999}},
    )
    assert router.should_respond(msg) is True


def test_reply_id_not_matching_aida_packet_no():
    connector = FakeCompositeConnector()
    connector.meshtastic_child().note_own_packet_id(999)
    router = _make_router(
        connector,
        respond_to_channel_mentions=True,
        mention_channels=[1],
        mention_names=["AIDA"],
    )
    msg = _mt_message(
        text="no mention here",
        channel=1,
        packet={"id": 12345, "decoded": {"replyId": 111}},
    )
    assert router.should_respond(msg) is False


def test_reply_id_thread_does_not_apply_to_meshcore():
    """Item 7: MeshCore is mention-only -- a decoded replyId concept does
    not exist there, so a MeshCore message with no mention is always no,
    regardless of what packet ids anything owns."""
    router = _make_router(
        FakeCompositeConnector(),
        respond_to_channel_mentions=True,
        mention_channels=[1],
    )
    router.config.meshcore_context.mention_channels = ["#aida"]
    msg = _mc_message(text="no mention here", channel=3, channel_name="#aida")
    assert router.should_respond(msg) is False


# ---------------------------------------------------------------------------
# Own message -> no
# ---------------------------------------------------------------------------


def test_own_node_id_message_no():
    connector = FakeConnector(my_node_id="!a1daa1da")
    router = _make_router(
        connector,
        respond_to_channel_mentions=True,
        mention_channels=[1],
        mention_names=["AIDA"],
    )
    msg = _mt_message(text="@AIDA self echo?", channel=1, sender_id="!a1daa1da")
    assert router.should_respond(msg) is False


def test_meshcore_own_name_message_no():
    """The node-id self-filter can't catch this (MeshCore channel senders
    are identified by name, not the bot's my_node_id pubkey) -- the
    explicit own-name check in _is_own_meshcore_name must."""
    connector = FakeCompositeConnector(mc_self_name="AIDA")
    router = _make_router(
        connector,
        respond_to_channel_mentions=True,
        mention_channels=[1],
        mention_names=["AIDA"],
    )
    router.config.meshcore_context.mention_channels = ["#aida"]
    msg = _mc_message(text="@AIDA self echo?", channel=3, channel_name="#aida")
    msg.sender_name = "AIDA"
    assert router.should_respond(msg) is False


# ---------------------------------------------------------------------------
# Cooldown
# ---------------------------------------------------------------------------


def test_cooldown_blocks_second_reply_then_allows_after_clear():
    router = _make_router(
        FakeConnector(),
        respond_to_channel_mentions=True,
        mention_channels=[1],
        mention_names=["AIDA"],
        channel_reply_cooldown_seconds=30,
    )
    msg = _mt_message(text="@AIDA status?", channel=1, sender_id="!bob00001")

    assert router.should_respond(msg) is True
    # Same sender+channel, still within the 30s cooldown window.
    assert router.should_respond(msg) is False

    # Simulate cooldown expiry without sleeping in the test. This fixture's
    # `msg` has no packet id (packet=None), so the inbound de-dupe gate
    # falls back to its own short text+sender window (see
    # test_dm_fallback_dedupe_expires_after_window) -- reset it too so this
    # test simulates a later, independent occurrence of the mention rather
    # than the SAME wire resend arriving after the cooldown clears.
    router._channel_cooldowns.clear()
    router._inbound_dedupe = InboundDedupeCache()
    assert router.should_respond(msg) is True


def test_cooldown_is_per_sender_and_per_channel():
    router = _make_router(
        FakeConnector(),
        respond_to_channel_mentions=True,
        mention_channels=[1, 2],
        mention_names=["AIDA"],
        channel_reply_cooldown_seconds=30,
    )
    assert router.should_respond(_mt_message(channel=1, sender_id="!bob00001")) is True
    # Different sender, same channel -- not on cooldown.
    assert router.should_respond(_mt_message(channel=1, sender_id="!carol0001")) is True
    # Same sender, different channel -- not on cooldown.
    assert router.should_respond(_mt_message(channel=2, sender_id="!bob00001")) is True


# ---------------------------------------------------------------------------
# MeshCore channel-name gate
# ---------------------------------------------------------------------------


def test_meshcore_mention_channel_gate_by_name():
    router = _make_router(FakeCompositeConnector(), respond_to_channel_mentions=True)
    router.config.meshcore_context.mention_channels = ["#aida"]

    allowed = _mc_message(text="@AIDA status?", channel=3, channel_name="#aida")
    other = _mc_message(text="@AIDA status?", channel=5, channel_name="#general")

    assert router.should_respond(allowed) is True
    assert router.should_respond(other) is False


# ---------------------------------------------------------------------------
# DM path is completely unaffected (with the feature both off and on)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("channel_mentions_enabled", [False, True])
def test_dm_path_unaffected_by_channel_mention_feature(channel_mentions_enabled):
    router = _make_router(
        FakeConnector(),
        respond_to_channel_mentions=channel_mentions_enabled,
        mention_channels=[1],
        mention_names=["AIDA"],
    )
    assert router.should_respond(_dm_message(text="hello there")) is True


def test_dm_respond_to_dms_false_still_blocks_meshtastic_dm():
    router = _make_router(FakeConnector(), respond_to_dms=False)
    assert router.should_respond(_dm_message(text="hello", transport="meshtastic")) is False


def test_dm_meshcore_ignores_bot_respond_to_dms():
    """meshcore DMs are governed solely by meshcore_context.respond_to_dms
    -- unchanged existing behavior, verified still holds after the
    channel-mention restructuring of should_respond()."""
    router = _make_router(FakeConnector(), respond_to_dms=False)
    assert router.should_respond(_dm_message(text="hello", transport="meshcore")) is True


# ---------------------------------------------------------------------------
# Effective mention names: AIDA's own live MeshCore self-name (and
# bot.mc_mesh_name / bot.mt_mesh_name) are recognized as mentions in
# addition to bot.mention_names -- see MessageRouter._effective_mention_names().
# ---------------------------------------------------------------------------


def test_live_meshcore_self_name_is_recognized_as_mention():
    """MeshCore apps insert "@[AIDA-MC]" (bracket form of the device's own
    advertised name) when tap-mentioning it, even though only "AIDA" is
    configured in bot.mention_names -- self_info() sources the live name."""
    connector = FakeCompositeConnector(mc_self_name="AIDA-MC")
    router = _make_router(connector, respond_to_channel_mentions=True)
    router.config.meshcore_context.mention_channels = ["#aida"]

    for text in ("@[AIDA-MC] status?", "@AIDA-MC status?", "@[AIDA] status?", "@AIDA status?"):
        router._channel_cooldowns.clear()
        msg = _mc_message(text=text, channel=3, channel_name="#aida")
        assert router.should_respond(msg) is True, text


def test_live_meshcore_self_name_hyphen_suffix_does_not_match():
    connector = FakeCompositeConnector(mc_self_name="AIDA-MC")
    router = _make_router(connector, respond_to_channel_mentions=True)
    router.config.meshcore_context.mention_channels = ["#aida"]

    msg = _mc_message(text="@AIDA-MCX status?", channel=3, channel_name="#aida")
    assert router.should_respond(msg) is False


def test_mc_mesh_name_recognized_as_mention():
    connector = FakeCompositeConnector(mc_self_name="AIDA")
    router = _make_router(
        connector, respond_to_channel_mentions=True, mc_mesh_name="the MeshCore mesh",
    )
    router.config.meshcore_context.mention_channels = ["#aida"]

    msg = _mc_message(text="@the MeshCore mesh status?", channel=3, channel_name="#aida")
    assert router.should_respond(msg) is True


def test_mt_mesh_name_recognized_as_mention_on_meshtastic():
    router = _make_router(
        FakeConnector(), respond_to_channel_mentions=True, mention_channels=[1],
        mt_mesh_name="freq51 Meshtastic mesh",
    )
    msg = _mt_message(text="@freq51 Meshtastic mesh status?", channel=1)
    assert router.should_respond(msg) is True


def test_mt_mesh_name_not_used_on_meshcore():
    """mt_mesh_name is Meshtastic-only identity framing; it must not leak
    into MeshCore mention matching."""
    connector = FakeCompositeConnector(mc_self_name="AIDA")
    router = _make_router(
        connector, respond_to_channel_mentions=True, mt_mesh_name="freq51 Meshtastic mesh",
    )
    router.config.meshcore_context.mention_channels = ["#aida"]
    msg = _mc_message(text="@freq51 Meshtastic mesh status?", channel=3, channel_name="#aida")
    assert router.should_respond(msg) is False


# ---------------------------------------------------------------------------
# Inbound de-dupe (see meshai/dedupe.py): a client's automatic resend of the
# SAME message (same sender/text/sender_timestamp, or same Meshtastic packet
# id) is dropped before the LLM/history sees it a second time; a genuinely
# new message (new timestamp, or the same text well outside the fallback
# window) still gets answered.
# ---------------------------------------------------------------------------


def test_dm_duplicate_resend_dropped_same_response_once():
    """Same DM 3x (same sender_timestamp) within the window -> only the
    first is answered."""
    router = _make_router(FakeConnector())
    msg = _dm_message(text="Thanks hows it going?", sender_id="!bob00001", sender_timestamp=1000)

    assert router.should_respond(msg) is True
    assert router.should_respond(msg) is False
    assert router.should_respond(msg) is False


def test_dm_duplicate_dropped_logs_at_info(caplog):
    import logging
    router = _make_router(FakeConnector())
    msg = _dm_message(sender_timestamp=1000)
    router.should_respond(msg)

    with caplog.at_level(logging.INFO):
        router.should_respond(msg)

    assert any("duplicate inbound DM dropped" in r.getMessage() for r in caplog.records)


def test_dm_same_text_new_timestamp_is_answered():
    """A genuine repeat with a NEW sender_timestamp (a new message, not a
    resend) must still be answered."""
    router = _make_router(FakeConnector())
    first = _dm_message(text="ping", sender_id="!bob00001", sender_timestamp=1000)
    second = _dm_message(text="ping", sender_id="!bob00001", sender_timestamp=2000)

    assert router.should_respond(first) is True
    assert router.should_respond(second) is True


def test_dm_fallback_dedupe_no_timestamp_within_window():
    """No sender_timestamp available -> falls back to a short text+sender
    window (still catches a fast resend)."""
    router = _make_router(FakeConnector())
    msg = _dm_message(text="hello there", sender_id="!bob00001")

    assert router.should_respond(msg) is True
    assert router.should_respond(msg) is False


def test_dm_fallback_dedupe_expires_after_window():
    """After the fallback window, the same text from the same sender is a
    genuinely new message and is answered again."""
    router = _make_router(FakeConnector())
    router._inbound_dedupe.fallback_window_seconds = 0.05
    msg = _dm_message(text="hello there", sender_id="!bob00001")

    assert router.should_respond(msg) is True
    import time as _time
    _time.sleep(0.1)
    assert router.should_respond(msg) is True


def test_dm_dedupe_expires_after_ttl():
    """An id-keyed (sender_timestamp) duplicate is answered again once the
    TTL has elapsed."""
    router = _make_router(FakeConnector())
    router._inbound_dedupe.ttl_seconds = 0.05
    msg = _dm_message(sender_timestamp=1000)

    assert router.should_respond(msg) is True
    import time as _time
    _time.sleep(0.1)
    assert router.should_respond(msg) is True


def test_meshtastic_packet_id_dedupe_for_dm():
    """Meshtastic DM de-dupe keys on the packet id when available."""
    router = _make_router(FakeConnector())
    msg1 = MeshMessage(
        sender_id="!bob00001", sender_name="Bob", text="hi", channel=0,
        is_dm=True, transport="meshtastic", packet={"id": 42},
    )
    msg2 = MeshMessage(
        sender_id="!bob00001", sender_name="Bob", text="hi", channel=0,
        is_dm=True, transport="meshtastic", packet={"id": 42},
    )
    msg3 = MeshMessage(
        sender_id="!bob00001", sender_name="Bob", text="hi", channel=0,
        is_dm=True, transport="meshtastic", packet={"id": 43},
    )

    assert router.should_respond(msg1) is True
    assert router.should_respond(msg2) is False  # same packet id -> resend
    assert router.should_respond(msg3) is True   # new packet id -> new message


def test_channel_mention_flood_repeat_triggers_one_reply():
    """A flood-repeated identical channel-mention message (same
    sender_timestamp) triggers only one reply."""
    router = _make_router(
        FakeCompositeConnector(), respond_to_channel_mentions=True,
        channel_reply_cooldown_seconds=0,
    )
    router.config.meshcore_context.mention_channels = ["#aida"]
    msg = _mc_message(text="@AIDA status?", channel=3, channel_name="#aida", sender_timestamp=500)

    assert router.should_respond(msg) is True
    assert router.should_respond(msg) is False
    assert router.should_respond(msg) is False
