"""Tests for the router's handling of aida-mesh's trailing "Sources:" line.

aida-mesh (the Open WebUI-backed LLM) always ends its replies with a final
line "Sources: <a>; <b>" or "Sources: none". That line must:
  - NEVER be sent to a mesh user (it's not part of the answer and it eats
    airtime), and
  - ALWAYS be stored in conversation history, so a later "where did you get
    that?" follow-up can be answered from what was actually cited.

Covers:
- _strip_sources_line() unit tests: stripped, case-insensitive, tolerates
  leading whitespace, only the last line counts, "Sources: none" handled,
  no-Sources replies are unchanged.
- generate_llm_response() integration: the chunks handed back for sending
  over the mesh have the Sources line stripped, while the message stored in
  conversation history via history.add_message() retains it verbatim.
"""
from __future__ import annotations

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
    # Same gap as test_openai_backend.py: this dev environment lacks
    # `pydantic`, a transitive dependency of the real openai/anthropic/
    # google-genai SDKs, so importing meshai.router (which imports
    # meshai.backends, which eagerly imports all three backend modules)
    # fails before reaching the module under test. Nothing here touches a
    # real SDK client -- the LLM backend is a full AsyncMock -- so minimal
    # stubs are enough. Production/CI environments have the real packages.
    if "openai" not in sys.modules:
        _openai_stub = types.ModuleType("openai")

        class _StubAsyncOpenAI:
            def __init__(self, api_key=None, base_url=None):
                self.api_key = api_key
                self.base_url = base_url
                # Mirror test_openai_backend.py's stub shape exactly so
                # whichever test file's module-level stub code runs first
                # in a shared pytest session still leaves `.chat.completions
                # .create` patchable for the other file's tests.
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
from meshai.router import (
    LLM_TRUNCATED_TEXT,
    MessageRouter,
    _extract_sources_as_answer,
    _has_sources_line,
    _strip_sources_line,
)


# ---------------------------------------------------------------------------
# _strip_sources_line unit tests
# ---------------------------------------------------------------------------


def test_strip_sources_line_basic():
    text = "The mesh looks healthy today.\nSources: recon_knowledge; wiki/mesh-health"
    assert _strip_sources_line(text) == "The mesh looks healthy today."


def test_strip_sources_line_none_value():
    text = "I don't have anything on that.\nSources: none"
    assert _strip_sources_line(text) == "I don't have anything on that."


def test_strip_sources_line_case_insensitive():
    text = "Answer here.\nSOURCES: some-doc"
    assert _strip_sources_line(text) == "Answer here."
    text2 = "Answer here.\nsources: some-doc"
    assert _strip_sources_line(text2) == "Answer here."


def test_strip_sources_line_tolerates_leading_whitespace():
    text = "Answer here.\n   Sources: some-doc"
    assert _strip_sources_line(text) == "Answer here."


def test_strip_sources_line_only_checks_last_line():
    # "Sources:" appearing mid-message (not the final line) is left alone --
    # only a trailing Sources line is treated specially.
    text = "Sources: not actually the citation line\nActual answer here."
    assert _strip_sources_line(text) == text


def test_strip_sources_line_noop_when_absent():
    text = "Just a normal reply with no citation line."
    assert _strip_sources_line(text) == text


def test_strip_sources_line_empty_string():
    assert _strip_sources_line("") == ""


def test_strip_sources_line_trims_trailing_blank_left_behind():
    text = "Answer here.\n\nSources: doc-a; doc-b"
    assert _strip_sources_line(text) == "Answer here."


# ---------------------------------------------------------------------------
# generate_llm_response() integration tests
# ---------------------------------------------------------------------------


def _make_router(llm_response: str) -> tuple[MessageRouter, AsyncMock, AsyncMock]:
    """Build a MessageRouter with the minimum mocked collaborators needed to
    exercise generate_llm_response()'s reply path."""
    config = Config()

    connector = MagicMock()
    connector.max_chars = 200

    history = MagicMock()
    history.add_message = AsyncMock()
    history.get_history_for_llm = AsyncMock(return_value=[])

    dispatcher = MagicMock()
    dispatcher.get_commands = MagicMock(return_value=[])

    llm_backend = MagicMock()
    llm_backend.generate = AsyncMock(return_value=llm_response)
    llm_backend.get_memory = MagicMock(return_value=None)

    router = MessageRouter(
        config=config,
        connector=connector,
        history=history,
        dispatcher=dispatcher,
        llm_backend=llm_backend,
    )
    return router, llm_backend, history


def _make_message(text: str = "hello") -> MeshMessage:
    return MeshMessage(
        sender_id="!testuser",
        sender_name="Tester",
        text=text,
        channel=0,
        is_dm=True,
        transport="meshtastic",
    )


@pytest.mark.asyncio
async def test_generate_llm_response_strips_sources_from_sent_chunks():
    raw = "The mesh looks healthy today.\nSources: recon_knowledge; wiki/mesh-health"
    router, llm_backend, history = _make_router(raw)

    chunks = await router.generate_llm_response(_make_message(), "how's the mesh?")

    sent_text = " ".join(chunks)
    assert "Sources:" not in sent_text
    assert "recon_knowledge" not in sent_text
    assert "The mesh looks healthy today." in sent_text


@pytest.mark.asyncio
async def test_generate_llm_response_keeps_sources_in_history():
    raw = "The mesh looks healthy today.\nSources: recon_knowledge; wiki/mesh-health"
    router, llm_backend, history = _make_router(raw)

    await router.generate_llm_response(_make_message(), "how's the mesh?")

    # add_message(user_id, role, content) is called positionally -- content
    # (args[2]) must retain the full raw response, Sources line included.
    assert history.add_message.await_count == 2  # user turn + assistant turn
    args, _kwargs = history.add_message.await_args_list[-1]
    assert args[1] == "assistant"
    stored_content = args[2]
    assert stored_content == raw
    assert "Sources: recon_knowledge; wiki/mesh-health" in stored_content


@pytest.mark.asyncio
async def test_generate_llm_response_handles_sources_none():
    raw = "I don't have anything on that.\nSources: none"
    router, llm_backend, history = _make_router(raw)

    chunks = await router.generate_llm_response(_make_message(), "what's the secret handshake?")

    sent_text = " ".join(chunks)
    assert "Sources:" not in sent_text
    assert "I don't have anything on that." in sent_text

    args, _kwargs = history.add_message.await_args_list[-1]
    stored_content = args[2]
    assert stored_content == raw


@pytest.mark.asyncio
async def test_generate_llm_response_no_sources_line_unchanged():
    """A reply with no trailing Sources line behaves exactly as today: sent
    text and stored history content are the same (modulo markdown-strip/
    chunking of the sent text, which doesn't touch this plain reply)."""
    raw = "Just a normal reply with no citation line."
    router, llm_backend, history = _make_router(raw)

    chunks = await router.generate_llm_response(_make_message(), "hello")

    sent_text = " ".join(chunks)
    assert sent_text == raw

    args, _kwargs = history.add_message.await_args_list[-1]
    stored_content = args[2]
    assert stored_content == raw


# ---------------------------------------------------------------------------
# _has_sources_line / _extract_sources_as_answer unit tests
# ---------------------------------------------------------------------------


def test_has_sources_line_true_when_present():
    assert _has_sources_line("Answer here.\nSources: doc-a; doc-b") is True


def test_has_sources_line_false_when_absent():
    assert _has_sources_line("Just a normal reply.") is False


def test_has_sources_line_false_for_empty_string():
    assert _has_sources_line("") is False


def test_extract_sources_as_answer_rewrites_label():
    text = "Sources: litime.com; acebattery.com"
    assert _extract_sources_as_answer(text) == "Source: litime.com; acebattery.com"


def test_extract_sources_as_answer_uses_only_last_line():
    text = "Some earlier text.\nSources: doc-a; doc-b"
    assert _extract_sources_as_answer(text) == "Source: doc-a; doc-b"


def test_extract_sources_as_answer_none_when_no_sources_line():
    assert _extract_sources_as_answer("Just a normal reply.") is None


def test_extract_sources_as_answer_none_for_sources_none():
    assert _extract_sources_as_answer("Sources: none") is None


def test_extract_sources_as_answer_none_for_bare_label():
    assert _extract_sources_as_answer("Sources:") is None


def test_extract_sources_as_answer_none_for_empty_string():
    assert _extract_sources_as_answer("") is None


# ---------------------------------------------------------------------------
# generate_llm_response(): sources-only reply -> sources become the answer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sources_only_reply_sends_source_line_not_blank():
    """13:10 UTC incident: the model's whole reply was just the Sources
    line. After _strip_sources_line the body is empty -- the citations must
    become the visible answer instead of an empty/blank send."""
    raw = "Sources: LiFePO4 Temperature Range...(litime.com); ...(acebattery.com)"
    router, llm_backend, history = _make_router(raw)

    chunks = await router.generate_llm_response(_make_message(), "what temp range?")

    assert chunks, "must send something, never an empty list of chunks"
    sent_text = " ".join(chunks)
    assert sent_text.strip() != ""
    assert sent_text.startswith("Source:")
    assert "litime.com" in sent_text
    assert "acebattery.com" in sent_text
    # The label itself was singularized; the original plural "Sources:"
    # label is not part of what's sent.
    assert "Sources:" not in sent_text


@pytest.mark.asyncio
async def test_sources_only_reply_keeps_original_in_history():
    raw = "Sources: LiFePO4 Temperature Range...(litime.com); ...(acebattery.com)"
    router, llm_backend, history = _make_router(raw)

    await router.generate_llm_response(_make_message(), "what temp range?")

    args, _kwargs = history.add_message.await_args_list[-1]
    assert args[1] == "assistant"
    assert args[2] == raw


@pytest.mark.asyncio
async def test_empty_body_no_sources_falls_back_never_blank():
    """Body empty after stripping and no real citation content available ->
    treated like a failed generation, never an empty/blank send."""
    raw = "Sources: none"
    router, llm_backend, history = _make_router(raw)

    chunks = await router.generate_llm_response(_make_message(), "secret handshake?")

    assert chunks
    sent_text = " ".join(chunks)
    assert sent_text.strip() != ""
    assert sent_text == LLM_TRUNCATED_TEXT
