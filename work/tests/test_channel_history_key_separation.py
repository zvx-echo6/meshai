"""Tests for MessageRouter.history_key() and its use throughout
generate_llm_response()/check_continuation() (Matt's decision, item 5):

DMs keep the existing raw sender_id key (no migration). Channel turns are
keyed by (sender, transport, channel) so a person's DM history never
appears in a channel reply and vice versa, and different people never
share history on the same channel.
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

from meshai.config import Config
from meshai.connector import MeshMessage
from meshai.router import MessageRouter


def _make_router(llm_text: str = "answer", max_length: int = 200, max_messages: int = 3):
    config = Config()
    config.bot.mt_node = "!a1daa1da (AIDA-N2)"
    config.response.thinking_notice_seconds = 0
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
    llm_backend.generate = AsyncMock(return_value=llm_text)
    llm_backend.get_memory = MagicMock(return_value=None)

    router = MessageRouter(
        config=config, connector=connector, history=history,
        dispatcher=dispatcher, llm_backend=llm_backend,
    )
    return router, history


def _dm(sender_id="!bob00001", transport="meshtastic") -> MeshMessage:
    return MeshMessage(
        sender_id=sender_id, sender_name="Bob", text="hi", channel=0,
        is_dm=True, transport=transport,
    )


def _chan(sender_id="!bob00001", channel=1, transport="meshtastic",
          channel_name=None) -> MeshMessage:
    return MeshMessage(
        sender_id=sender_id, sender_name="Bob", text="@AIDA hi", channel=channel,
        is_dm=False, transport=transport, channel_name=channel_name,
    )


# ---------------------------------------------------------------------------
# history_key() itself
# ---------------------------------------------------------------------------


def test_dm_key_is_raw_sender_id():
    router, _ = _make_router()
    assert router.history_key(_dm(sender_id="!bob00001")) == "!bob00001"


def test_channel_key_includes_transport_and_channel():
    router, _ = _make_router()
    key = router.history_key(_chan(sender_id="!bob00001", channel=1, transport="meshtastic"))
    assert key == "!bob00001@meshtastic:1"


def test_meshcore_channel_key_uses_resolved_sender_id():
    router, _ = _make_router()
    msg = _chan(sender_id="mcname:Bob", channel=3, transport="meshcore", channel_name="#aida")
    assert router.history_key(msg) == "mcname:Bob@meshcore:3"


# ---------------------------------------------------------------------------
# Same person: DM history never leaks into channel reply and vice versa
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_person_dm_and_channel_use_different_history_keys():
    router, history = _make_router()

    await router.generate_llm_response(_dm(sender_id="!bob00001"), "hi there")
    await router.generate_llm_response(
        _chan(sender_id="!bob00001", channel=1, transport="meshtastic"), "hi there"
    )

    user_ids = {call.args[0] for call in history.add_message.await_args_list}
    assert "!bob00001" in user_ids
    assert "!bob00001@meshtastic:1" in user_ids
    assert len(user_ids) == 2


# ---------------------------------------------------------------------------
# Two different people on the same channel never share history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_people_same_channel_use_different_history_keys():
    router, history = _make_router()

    await router.generate_llm_response(
        _chan(sender_id="!bob00001", channel=1, transport="meshtastic"), "hi"
    )
    await router.generate_llm_response(
        _chan(sender_id="!carol0001", channel=1, transport="meshtastic"), "hi"
    )

    user_ids = {call.args[0] for call in history.add_message.await_args_list}
    assert user_ids == {"!bob00001@meshtastic:1", "!carol0001@meshtastic:1"}


# ---------------------------------------------------------------------------
# check_continuation() / ContinuationState.store() use the same key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_continuation_state_keyed_by_history_key_not_raw_sender_id():
    long_answer = "One. Two. Three. Four. Five. Six. Seven. Eight. Nine. Ten."
    router, _ = _make_router(llm_text=long_answer, max_length=15, max_messages=1)

    msg = _chan(sender_id="!bob00001", channel=1, transport="meshtastic")
    await router.generate_llm_response(msg, "give me a long list")

    expected_key = router.history_key(msg)
    assert router.continuations.has_pending(expected_key) is True
    # Never stored under the bare sender_id (would leak into that person's
    # DM continuation state).
    assert router.continuations.has_pending("!bob00001") is False


@pytest.mark.asyncio
async def test_check_continuation_reads_the_same_key_it_was_stored_under():
    long_answer = "One. Two. Three. Four. Five. Six. Seven. Eight. Nine. Ten."
    router, _ = _make_router(llm_text=long_answer, max_length=15, max_messages=1)

    msg = _chan(sender_id="!bob00001", channel=1, transport="meshtastic")
    await router.generate_llm_response(msg, "give me a long list")

    followup = _chan(sender_id="!bob00001", channel=1, transport="meshtastic")
    followup.text = "more"
    result = router.check_continuation(followup)
    assert result is not None
