"""Tests for the "still thinking" interim notice sent during a slow LLM call.

When an LLM answer is taking a while, generate_llm_response() sends one
interim message (config.response.thinking_notice_text) to the same
destination/channel/transport the final reply will use, then keeps
awaiting the original LLM task (never cancels it). Covers:

- Slow LLM: exactly one notice, then the real answer, same destination.
- Fast LLM: no notice.
- Disabled (thinking_notice_seconds=0): no notice, regardless of latency.
- LLM error after the notice fired: notice + existing error/fallback reply
  (the notice path must not swallow the error).
- The notice is never written to conversation history.
"""
from __future__ import annotations

import asyncio
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
    # Same gap as test_openai_backend.py / test_router_sources_line.py: this
    # dev environment lacks `pydantic`, a transitive dependency of the real
    # openai/anthropic/google-genai SDKs, so importing meshai.router (which
    # imports meshai.backends, which eagerly imports all three backend
    # modules) fails before reaching the module under test. Nothing here
    # touches a real SDK client -- the LLM backend is a full AsyncMock --
    # so minimal stubs are enough. Production/CI environments have the
    # real packages.
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
        # Config() transitively imports meshai.notifications, which imports
        # meshai.notifications.channels for its email/webhook/etc. channel
        # implementations -- none of which are exercised by these tests, but
        # the `import httpx` at module load time still needs to succeed.
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Small, test-friendly notice window. Real default is 15s (config.py).
_NOTICE_SECONDS = 0.05
_SLOW_DELAY = 0.2  # comfortably past _NOTICE_SECONDS
_FAST_DELAY = 0.0  # AsyncMock default -- resolves at the next await point


def _make_router(
    llm_generate,
    thinking_notice_seconds: float = _NOTICE_SECONDS,
) -> tuple[MessageRouter, AsyncMock, AsyncMock, MagicMock]:
    """Build a MessageRouter with the minimum mocked collaborators needed to
    exercise generate_llm_response()'s thinking-notice path.

    `llm_generate` is an async callable used directly as
    llm_backend.generate's side_effect (so tests can control timing/errors).
    """
    config = Config()
    config.response.thinking_notice_seconds = thinking_notice_seconds

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
        config=config,
        connector=connector,
        history=history,
        dispatcher=dispatcher,
        llm_backend=llm_backend,
    )
    return router, llm_backend, history, connector


def _make_message(text: str = "hello") -> MeshMessage:
    return MeshMessage(
        sender_id="!testuser",
        sender_name="Tester",
        text=text,
        channel=0,
        is_dm=True,
        transport="meshtastic",
    )


async def _slow_answer(*_args, **_kwargs) -> str:
    await asyncio.sleep(_SLOW_DELAY)
    return "The real answer."


async def _fast_answer(*_args, **_kwargs) -> str:
    return "The real answer."


async def _slow_then_error(*_args, **_kwargs) -> str:
    await asyncio.sleep(_SLOW_DELAY)
    raise RuntimeError("boom")


# ---------------------------------------------------------------------------
# Slow LLM -> exactly one notice, then the answer, same destination
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slow_llm_sends_one_notice_then_answer():
    router, llm_backend, history, connector = _make_router(_slow_answer)
    message = _make_message()

    chunks = await router.generate_llm_response(message, "how's the mesh?")

    # Exactly one notice sent.
    assert connector.send_message_async.await_count == 1
    call = connector.send_message_async.await_args_list[0]
    assert call.kwargs["text"] == router.config.response.thinking_notice_text
    assert call.kwargs["destination"] == message.sender_id
    assert call.kwargs["channel"] == message.channel
    assert call.kwargs["transport"] == message.transport

    # The real answer still comes back for sending.
    assert " ".join(chunks) == "The real answer."


@pytest.mark.asyncio
async def test_notice_uses_same_destination_as_final_reply():
    """The notice's destination/channel/transport kwargs must match exactly
    what main.py's _on_message later passes to responder.send_response()
    for the real chunks: destination=message.sender_id, channel=message.
    channel, transport=<originating transport>."""
    router, llm_backend, history, connector = _make_router(_slow_answer)
    message = _make_message()
    message.channel = 3

    await router.generate_llm_response(message, "how's the mesh?")

    call = connector.send_message_async.await_args_list[0]
    assert call.kwargs["destination"] == "!testuser"
    assert call.kwargs["channel"] == 3
    assert call.kwargs["transport"] == "meshtastic"


# ---------------------------------------------------------------------------
# Fast LLM -> no notice
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fast_llm_sends_no_notice():
    router, llm_backend, history, connector = _make_router(_fast_answer)

    chunks = await router.generate_llm_response(_make_message(), "hi")

    assert connector.send_message_async.await_count == 0
    assert " ".join(chunks) == "The real answer."


# ---------------------------------------------------------------------------
# Disabled (thinking_notice_seconds=0) -> no notice regardless of latency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_notice_sends_nothing_even_when_slow():
    router, llm_backend, history, connector = _make_router(
        _slow_answer, thinking_notice_seconds=0
    )

    chunks = await router.generate_llm_response(_make_message(), "how's the mesh?")

    assert connector.send_message_async.await_count == 0
    assert " ".join(chunks) == "The real answer."


# ---------------------------------------------------------------------------
# LLM error after the notice fired -> notice + existing error reply
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_error_after_notice_still_returns_fallback():
    router, llm_backend, history, connector = _make_router(_slow_then_error)

    chunks = await router.generate_llm_response(_make_message(), "how's the mesh?")

    # Notice still went out exactly once.
    assert connector.send_message_async.await_count == 1

    # The existing error fallback text is still what gets sent -- the notice
    # path does not swallow the exception.
    assert " ".join(chunks) == "Sorry, I encountered an error. Please try again."


@pytest.mark.asyncio
async def test_llm_timeout_after_notice_still_returns_fallback():
    async def _slow_then_timeout(*_args, **_kwargs):
        await asyncio.sleep(_SLOW_DELAY)
        raise asyncio.TimeoutError()

    router, llm_backend, history, connector = _make_router(_slow_then_timeout)

    chunks = await router.generate_llm_response(_make_message(), "how's the mesh?")

    assert connector.send_message_async.await_count == 1
    assert " ".join(chunks) == "Sorry, request timed out. Try again."


# ---------------------------------------------------------------------------
# Notice is never written to conversation history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notice_not_stored_in_history():
    router, llm_backend, history, connector = _make_router(_slow_answer)

    await router.generate_llm_response(_make_message(), "how's the mesh?")

    # Only the user turn + assistant turn are recorded -- no third call for
    # the notice.
    assert history.add_message.await_count == 2
    for call in history.add_message.await_args_list:
        args, _kwargs = call
        content = args[2]
        assert router.config.response.thinking_notice_text not in content

    # The stored assistant turn is the real answer, not the notice.
    args, _kwargs = history.add_message.await_args_list[-1]
    assert args[1] == "assistant"
    assert args[2] == "The real answer."
