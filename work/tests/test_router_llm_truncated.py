"""Tests for the router's handling of LLMTruncatedError.

`aida-mesh` (the Open WebUI-backed LLM) is getting a hard output-token cap
as a runaway guard. When that cap is hit, openai_backend.generate() raises
LLMTruncatedError instead of returning a cut-off/empty reply (see
test_openai_backend.py for the backend-side truncation detection). The
router must treat this exactly like its other LLM failure modes:

- The user gets a dedicated, distinct failure message (router.LLM_TRUNCATED_TEXT)
  -- never the partial/cut-off text.
- The existing timeout/generic-error messages for OTHER failures are
  unaffected.
- A "still thinking" notice already sent must still be followed by the
  truncation failure message (the notice path must not swallow it).
- Conversation history stores the failure message, not any partial content
  -- consistent with how the router already handles timeout/generic errors
  (generate() raises before returning anything, so only the fixed fallback
  text set in the except block ever reaches history.add_message()).
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
    # Same gap as test_openai_backend.py / test_router_sources_line.py /
    # test_router_thinking_notice.py: this dev environment lacks
    # `pydantic`, a transitive dependency of the real openai/anthropic/
    # google-genai SDKs, so importing meshai.router (which imports
    # meshai.backends, which eagerly imports all three backend modules)
    # fails before reaching the module under test. Nothing here touches a
    # real SDK client -- the LLM backend is a full MagicMock -- so minimal
    # stubs are enough. Production/CI environments have the real packages.
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

from meshai.backends.base import LLMTruncatedError
from meshai.config import Config
from meshai.connector import MeshMessage
from meshai.router import LLM_TRUNCATED_TEXT, MessageRouter


# Small, test-friendly notice window, matching test_router_thinking_notice.py.
_NOTICE_SECONDS = 0.05
_SLOW_DELAY = 0.2  # comfortably past _NOTICE_SECONDS


def _make_router(
    llm_generate,
    thinking_notice_seconds: float = 0,
    require_sources_line: bool = False,
) -> tuple[MessageRouter, AsyncMock, AsyncMock, MagicMock]:
    """Build a MessageRouter with the minimum mocked collaborators needed to
    exercise generate_llm_response()'s error-handling path.

    `llm_generate` is an async callable used directly as
    llm_backend.generate's side_effect.
    """
    config = Config()
    config.response.thinking_notice_seconds = thinking_notice_seconds
    config.llm.require_sources_line = require_sources_line

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


async def _immediate_truncated(*_args, **_kwargs) -> str:
    raise LLMTruncatedError("finish_reason='length' content_length=42")


async def _slow_then_truncated(*_args, **_kwargs) -> str:
    await asyncio.sleep(_SLOW_DELAY)
    raise LLMTruncatedError("finish_reason='length' content_length=0")


# ---------------------------------------------------------------------------
# LLMTruncatedError -> the dedicated user-facing text
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_truncated_error_returns_dedicated_message():
    router, llm_backend, history, connector = _make_router(_immediate_truncated)

    chunks = await router.generate_llm_response(_make_message(), "how's the mesh?")

    assert " ".join(chunks) == LLM_TRUNCATED_TEXT


@pytest.mark.asyncio
async def test_llm_truncated_message_is_distinct_from_other_failures():
    # Sanity check the new text really is different from the existing
    # timeout/generic-error fallbacks it sits alongside.
    assert LLM_TRUNCATED_TEXT != "Sorry, request timed out. Try again."
    assert LLM_TRUNCATED_TEXT != "Sorry, I encountered an error. Please try again."


@pytest.mark.asyncio
async def test_other_failures_are_unaffected_by_truncation_handling():
    """Existing timeout/generic-error messages for OTHER failure types must
    still come through unchanged -- the new except clause must not swallow
    or shadow them."""

    async def _timeout(*_args, **_kwargs):
        raise asyncio.TimeoutError()

    async def _generic_error(*_args, **_kwargs):
        raise RuntimeError("boom")

    router, _llm, _history, _connector = _make_router(_timeout)
    chunks = await router.generate_llm_response(_make_message(), "hi")
    assert " ".join(chunks) == "Sorry, request timed out. Try again."

    router2, _llm2, _history2, _connector2 = _make_router(_generic_error)
    chunks2 = await router2.generate_llm_response(_make_message(), "hi")
    assert " ".join(chunks2) == "Sorry, I encountered an error. Please try again."


# ---------------------------------------------------------------------------
# Thinking notice + truncation: notice, then the new failure text
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thinking_notice_then_truncated_error():
    router, llm_backend, history, connector = _make_router(
        _slow_then_truncated, thinking_notice_seconds=_NOTICE_SECONDS
    )

    chunks = await router.generate_llm_response(_make_message(), "how's the mesh?")

    # The notice still fired exactly once...
    assert connector.send_message_async.await_count == 1
    call = connector.send_message_async.await_args_list[0]
    assert call.kwargs["text"] == router.config.response.thinking_notice_text

    # ...followed by the truncation failure text, not the notice and not a
    # partial answer.
    assert " ".join(chunks) == LLM_TRUNCATED_TEXT


# ---------------------------------------------------------------------------
# History does not store the failed/cut reply
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_truncated_failure_stores_fallback_text_not_partial_content():
    """generate() raises before returning anything on this path, so the
    only thing that can reach history is the fixed fallback text set in the
    except block -- exactly like the existing timeout/generic-error paths.
    No partial/cut-off model output is ever stored."""
    router, llm_backend, history, connector = _make_router(_immediate_truncated)

    await router.generate_llm_response(_make_message(), "how's the mesh?")

    assert history.add_message.await_count == 2
    args, _kwargs = history.add_message.await_args_list[-1]
    assert args[1] == "assistant"
    assert args[2] == LLM_TRUNCATED_TEXT


# ---------------------------------------------------------------------------
# require_sources_line: missing trailing Sources line -> truncated path
# ---------------------------------------------------------------------------


async def _missing_sources_reply(*_args, **_kwargs) -> str:
    # Looks like a normal, complete reply -- but has no trailing "Sources:"
    # line, which the aida-mesh contract requires. finish_reason/content
    # checks in openai_backend.py already passed (this is what's returned
    # from LLMBackend.generate()); require_sources_line is the router-level
    # second line of defense against a signal the backend didn't catch.
    return "LiFePO4 batteries have a lower self-discharge rate than lead-acid."


async def _with_sources_reply(*_args, **_kwargs) -> str:
    return "LiFePO4 batteries have a lower self-discharge rate.\nSources: litime.com"


@pytest.mark.asyncio
async def test_require_sources_line_true_missing_line_is_truncated():
    router, llm_backend, history, connector = _make_router(
        _missing_sources_reply, require_sources_line=True
    )

    chunks = await router.generate_llm_response(_make_message(), "tell me about lifepo4")

    assert " ".join(chunks) == LLM_TRUNCATED_TEXT
    args, _kwargs = history.add_message.await_args_list[-1]
    assert args[2] == LLM_TRUNCATED_TEXT


@pytest.mark.asyncio
async def test_require_sources_line_true_with_line_is_normal():
    router, llm_backend, history, connector = _make_router(
        _with_sources_reply, require_sources_line=True
    )

    chunks = await router.generate_llm_response(_make_message(), "tell me about lifepo4")

    sent_text = " ".join(chunks)
    assert sent_text == "LiFePO4 batteries have a lower self-discharge rate."
    assert "Sources:" not in sent_text

    args, _kwargs = history.add_message.await_args_list[-1]
    assert args[2] == "LiFePO4 batteries have a lower self-discharge rate.\nSources: litime.com"


@pytest.mark.asyncio
async def test_require_sources_line_false_missing_line_is_unaffected():
    """Default (False) behavior: a reply with no Sources line is sent
    exactly as before -- require_sources_line off means no enforcement."""
    router, llm_backend, history, connector = _make_router(
        _missing_sources_reply, require_sources_line=False
    )

    chunks = await router.generate_llm_response(_make_message(), "tell me about lifepo4")

    sent_text = " ".join(chunks)
    assert sent_text == "LiFePO4 batteries have a lower self-discharge rate than lead-acid."
