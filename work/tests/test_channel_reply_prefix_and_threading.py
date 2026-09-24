"""Tests for the channel-mention reply composition (item 6/7):

- Channel replies are prefixed "@<asker name> " (Meshtastic) or
  "@[<asker name>] " (MeshCore bracket form); DMs get no prefix.
- The prefix is reserved from the same per-packet chunk budget so the
  first chunk (with prefix) never exceeds max_chars.
- Only the first chunk gets the prefix.
- The "still thinking" notice gets the same prefix when it fires for a
  channel-mention reply.
- MeshtasticTransport threads replyId through to sendText() for a
  broadcast send and records its own outgoing packet id (owns_packet_id).
- main.py's MeshAI._send_reply() routes a channel-origin reply as a
  broadcast with reply_id/meshcore_channel set, and leaves the DM path
  byte-for-byte unchanged.
"""
import sys
import types
from unittest.mock import AsyncMock, MagicMock

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

import asyncio

from meshai.config import Config
from meshai.connector import MeshMessage
from meshai.router import MessageRouter


# ---------------------------------------------------------------------------
# Router-level: prefix + chunk budget + thinking notice prefix
# ---------------------------------------------------------------------------


def _make_router(llm_generate, max_length=200, max_messages=3, thinking_notice_seconds=0.05):
    config = Config()
    config.bot.mt_node = "!a1daa1da (AIDA-N2)"
    config.response.thinking_notice_seconds = thinking_notice_seconds
    config.response.max_length = max_length
    config.response.max_messages = max_messages

    connector = MagicMock()
    connector.max_chars = 200
    connector.send_message_async = AsyncMock(return_value=True)

    history = MagicMock()
    history.add_message = AsyncMock()
    history.get_history_for_llm = AsyncMock(return_value=[])

    dispatcher = MagicMock()
    dispatcher.get_commands = MagicMock(return_value=[])

    llm_backend = MagicMock()
    llm_backend.generate = AsyncMock(side_effect=llm_generate)
    llm_backend.get_memory = MagicMock(return_value=None)

    router = MessageRouter(
        config=config, connector=connector, history=history,
        dispatcher=dispatcher, llm_backend=llm_backend,
    )
    return router, connector


def _chan_message(transport="meshtastic", channel=1, sender_name="Bob",
                   channel_name=None) -> MeshMessage:
    return MeshMessage(
        sender_id="!bob00001" if transport == "meshtastic" else "mcname:Bob",
        sender_name=sender_name,
        text="@AIDA how's the weather",
        channel=channel,
        is_dm=False,
        transport=transport,
        channel_name=channel_name,
    )


def _dm_message(transport="meshtastic") -> MeshMessage:
    return MeshMessage(
        sender_id="!bob00001", sender_name="Bob", text="how's the weather",
        channel=0, is_dm=True, transport=transport,
    )


async def _fast_answer(*_a, **_kw) -> str:
    return "Sunny and clear all day today."


@pytest.mark.asyncio
async def test_meshtastic_channel_reply_gets_at_name_prefix():
    router, _ = _make_router(_fast_answer)
    messages = await router.generate_llm_response(_chan_message(), "how's the weather")
    assert messages[0].startswith("@Bob ")


@pytest.mark.asyncio
async def test_meshcore_channel_reply_gets_bracket_prefix():
    router, _ = _make_router(_fast_answer)
    messages = await router.generate_llm_response(
        _chan_message(transport="meshcore", channel_name="#aida"), "how's the weather"
    )
    assert messages[0].startswith("@[Bob] ")


@pytest.mark.asyncio
async def test_only_first_chunk_gets_the_prefix():
    long_answer = " ".join(f"Sentence number {i} is here." for i in range(1, 12))

    async def _long_answer(*_a, **_kw):
        return long_answer

    router, _ = _make_router(_long_answer, max_length=60, max_messages=3)
    messages = await router.generate_llm_response(_chan_message(), "tell me a lot")
    assert len(messages) > 1
    assert messages[0].startswith("@Bob ")
    for later in messages[1:]:
        assert not later.startswith("@Bob ")


@pytest.mark.asyncio
async def test_dm_reply_gets_no_prefix():
    router, _ = _make_router(_fast_answer)
    messages = await router.generate_llm_response(_dm_message(), "how's the weather")
    assert not messages[0].startswith("@Bob")


@pytest.mark.asyncio
async def test_prefixed_first_chunk_never_exceeds_the_packet_budget():
    long_answer = " ".join(f"Sentence number {i} is here for the test." for i in range(1, 12))

    async def _long_answer(*_a, **_kw):
        return long_answer

    max_length = 60
    router, _ = _make_router(_long_answer, max_length=max_length, max_messages=3)
    messages = await router.generate_llm_response(
        _chan_message(sender_name="LongNameBob"), "tell me a lot"
    )
    effective_max = min(max_length, 200)  # connector.max_chars == 200 in _make_router
    for msg in messages:
        assert len(msg.encode("utf-8")) <= effective_max


@pytest.mark.asyncio
async def test_thinking_notice_in_channel_carries_prefix():
    _SLOW_DELAY = 0.2

    async def _slow_answer(*_a, **_kw):
        await asyncio.sleep(_SLOW_DELAY)
        return "The real answer."

    router, connector = _make_router(_slow_answer, thinking_notice_seconds=0.05)
    msg = _chan_message()

    await router.generate_llm_response(msg, "how's the weather")

    assert connector.send_message_async.await_count == 1
    call = connector.send_message_async.await_args_list[0]
    assert call.kwargs["text"].startswith("@Bob ")
    assert router.config.response.thinking_notice_text in call.kwargs["text"]
    assert call.kwargs["destination"] is None
    assert call.kwargs["channel"] == msg.channel
    assert call.kwargs["transport"] == "meshtastic"


@pytest.mark.asyncio
async def test_thinking_notice_meshcore_channel_uses_channel_name():
    _SLOW_DELAY = 0.2

    async def _slow_answer(*_a, **_kw):
        await asyncio.sleep(_SLOW_DELAY)
        return "The real answer."

    router, connector = _make_router(_slow_answer, thinking_notice_seconds=0.05)
    msg = _chan_message(transport="meshcore", channel_name="#aida")

    await router.generate_llm_response(msg, "how's the weather")

    call = connector.send_message_async.await_args_list[0]
    assert call.kwargs["text"].startswith("@[Bob] ")
    assert call.kwargs["meshcore_channel"] == "#aida"
    assert call.kwargs["transport"] == "meshcore"


@pytest.mark.asyncio
async def test_dm_thinking_notice_unaffected_by_prefix_logic():
    """Existing DM thinking-notice behavior (test_router_thinking_notice.py)
    stays byte-for-byte the same after the channel-reply changes."""
    _SLOW_DELAY = 0.2

    async def _slow_answer(*_a, **_kw):
        await asyncio.sleep(_SLOW_DELAY)
        return "The real answer."

    router, connector = _make_router(_slow_answer, thinking_notice_seconds=0.05)
    msg = _dm_message()

    await router.generate_llm_response(msg, "how's the weather")

    call = connector.send_message_async.await_args_list[0]
    assert call.kwargs["text"] == router.config.response.thinking_notice_text
    assert call.kwargs["destination"] == msg.sender_id


# ---------------------------------------------------------------------------
# MeshtasticTransport: replyId threading + own-packet-id recording
# ---------------------------------------------------------------------------


def test_blocking_mt_send_threads_reply_id_and_records_own_packet_id():
    from meshai.config import ConnectionConfig
    from meshai.connector import MeshtasticTransport

    mt = MeshtasticTransport(ConnectionConfig())
    fake_sent = MagicMock()
    fake_sent.id = 555
    fake_interface = MagicMock()
    fake_interface.sendText.return_value = fake_sent
    mt._interface = fake_interface

    result = mt._blocking_mt_send("hello channel", destination=None, channel=1, reply_id=999)

    assert result is True
    kwargs = fake_interface.sendText.call_args.kwargs
    assert kwargs["replyId"] == 999
    assert kwargs["channelIndex"] == 1
    from meshtastic import BROADCAST_NUM
    assert kwargs["destinationId"] == BROADCAST_NUM

    # The reply we just sent (packet id 555) is now a known "own" outgoing id.
    assert mt.owns_packet_id(555) is True
    assert mt.owns_packet_id(12345) is False


def test_blocking_mt_send_dm_does_not_record_own_packet_id():
    """owns_packet_id tracking is for broadcasts (channel replies/alerts)
    only -- a DM send must not pollute it."""
    from meshai.config import ConnectionConfig
    from meshai.connector import MeshtasticTransport

    mt = MeshtasticTransport(ConnectionConfig())
    fake_sent = MagicMock()
    fake_sent.id = 777
    fake_interface = MagicMock()
    fake_interface.sendText.return_value = fake_sent
    mt._interface = fake_interface

    mt._blocking_mt_send("hello", destination="!bob00001", channel=0, reply_id=None)

    assert mt.owns_packet_id(777) is False


def test_owns_packet_id_bounded_to_last_200():
    from meshai.config import ConnectionConfig
    from meshai.connector import MeshtasticTransport

    mt = MeshtasticTransport(ConnectionConfig())
    fake_interface = MagicMock()
    mt._interface = fake_interface

    for i in range(250):
        fake_sent = MagicMock()
        fake_sent.id = i
        fake_interface.sendText.return_value = fake_sent
        mt._blocking_mt_send(f"msg {i}", destination=None, channel=0)

    # Oldest ids fell off the bounded deque.
    assert mt.owns_packet_id(0) is False
    assert mt.owns_packet_id(49) is False
    # Most recent 200 (50..249) are retained.
    assert mt.owns_packet_id(249) is True
    assert mt.owns_packet_id(50) is True


# ---------------------------------------------------------------------------
# main.py: MeshAI._send_reply routes channel vs DM correctly
# ---------------------------------------------------------------------------


class _FakeResponder:
    def __init__(self):
        self.calls = []

    async def send_response(self, messages, destination=None, channel=0, transport=None,
                             meshcore_channel=None, reply_id=None):
        self.calls.append(dict(
            messages=messages, destination=destination, channel=channel,
            transport=transport, meshcore_channel=meshcore_channel, reply_id=reply_id,
        ))
        return True


def _bare_meshai(responder):
    import meshai.main as main_module
    obj = main_module.MeshAI.__new__(main_module.MeshAI)
    obj.responder = responder
    return obj


@pytest.mark.asyncio
async def test_send_reply_dm_is_unchanged():
    responder = _FakeResponder()
    obj = _bare_meshai(responder)
    msg = _dm_message()

    await obj._send_reply(msg, ["hello there"], "meshtastic")

    call = responder.calls[0]
    assert call["destination"] == "!bob00001"
    assert call["channel"] == 0
    assert call["transport"] == "meshtastic"
    assert call["reply_id"] is None
    assert call["meshcore_channel"] is None


@pytest.mark.asyncio
async def test_send_reply_meshtastic_channel_broadcasts_with_reply_id():
    responder = _FakeResponder()
    obj = _bare_meshai(responder)
    msg = _chan_message(channel=1)
    msg.packet = {"id": 4242, "decoded": {}}

    await obj._send_reply(msg, ["chunk1", "chunk2"], "meshtastic")

    call = responder.calls[0]
    assert call["destination"] is None
    assert call["channel"] == 1
    assert call["transport"] == "meshtastic"
    assert call["reply_id"] == 4242
    assert call["meshcore_channel"] is None
    assert call["messages"] == ["chunk1", "chunk2"]


@pytest.mark.asyncio
async def test_send_reply_meshcore_channel_uses_channel_name_no_reply_id():
    responder = _FakeResponder()
    obj = _bare_meshai(responder)
    msg = _chan_message(transport="meshcore", channel=3, channel_name="#aida")

    await obj._send_reply(msg, ["chunk1"], "meshcore")

    call = responder.calls[0]
    assert call["destination"] is None
    assert call["meshcore_channel"] == "#aida"
    assert call["reply_id"] is None


# ---------------------------------------------------------------------------
# Channel continuations ("more"/"continue") get the same prefix treatment
# as the first answer; DM continuations stay unaffected.
# ---------------------------------------------------------------------------


async def _long_list_answer(*_a, **_kw) -> str:
    return " ".join(f"Sentence number {i} is here for the test." for i in range(1, 20))


@pytest.mark.asyncio
async def test_channel_continuation_gets_prefix_and_fits_budget():
    max_length = 60
    router, _ = _make_router(_long_list_answer, max_length=max_length, max_messages=1)
    msg = _chan_message(sender_name="Bob")

    first_messages = await router.generate_llm_response(msg, "tell me a lot")
    assert router.continuations.has_pending(router.history_key(msg)) is True

    followup = _chan_message(sender_name="Bob")
    followup.text = "more"
    followup.packet = {"id": 9001, "decoded": {}}

    continuation_messages = router.check_continuation(followup)
    assert continuation_messages is not None
    assert continuation_messages[0].startswith("@Bob ")
    for later in continuation_messages[1:]:
        assert not later.startswith("@Bob ")

    effective_max = min(max_length, 200)  # connector.max_chars == 200 in _make_router
    for chunk in continuation_messages:
        assert len(chunk.encode("utf-8")) <= effective_max

    # Threading: main.py's _send_reply uses the "more" message itself, so the
    # continuation broadcast threads to the "more" packet id, same as a
    # first-answer channel reply threads to the question's packet id.
    responder = _FakeResponder()
    obj = _bare_meshai(responder)
    await obj._send_reply(followup, continuation_messages, "meshtastic")
    call = responder.calls[0]
    assert call["destination"] is None
    assert call["channel"] == followup.channel
    assert call["reply_id"] == 9001

    assert first_messages  # sanity: the first answer itself was produced


@pytest.mark.asyncio
async def test_meshcore_channel_continuation_gets_bracket_prefix():
    max_length = 60
    router, _ = _make_router(_long_list_answer, max_length=max_length, max_messages=1)
    msg = _chan_message(transport="meshcore", channel_name="#aida", sender_name="Bob")

    await router.generate_llm_response(msg, "tell me a lot")
    assert router.continuations.has_pending(router.history_key(msg)) is True

    followup = _chan_message(transport="meshcore", channel_name="#aida", sender_name="Bob")
    followup.text = "more"

    continuation_messages = router.check_continuation(followup)
    assert continuation_messages is not None
    assert continuation_messages[0].startswith("@[Bob] ")

    effective_max = min(max_length, 200)
    for chunk in continuation_messages:
        assert len(chunk.encode("utf-8")) <= effective_max


@pytest.mark.asyncio
async def test_dm_continuation_unaffected_by_channel_prefix_logic():
    """DM continuations carry no "@Name" prefix, but -- like every other
    reply -- must respect the mesh's real per-packet budget
    (min(config.response.max_length, connector.max_chars)) and
    config.response.max_messages, not chunk_response()'s bare 200-char
    default. Regression test for the pre-existing bug where a DM "more"
    chunk could exceed a small mesh's max_chars (e.g. 140)."""
    max_length = 60
    router, connector = _make_router(_long_list_answer, max_length=max_length, max_messages=1)
    msg = _dm_message()

    await router.generate_llm_response(msg, "tell me a lot")
    assert router.continuations.has_pending(router.history_key(msg)) is True

    followup = _dm_message()
    followup.text = "more"

    continuation_messages = router.check_continuation(followup)
    assert continuation_messages is not None
    for chunk in continuation_messages:
        assert not chunk.startswith("@Bob")
        assert not chunk.startswith("@[Bob]")

    effective_max = min(max_length, connector.max_chars)
    for chunk in continuation_messages:
        assert len(chunk.encode("utf-8")) <= effective_max
