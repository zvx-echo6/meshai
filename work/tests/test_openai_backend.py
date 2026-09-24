"""Tests for meshai.backends.openai_backend.

Covers:
- RAG filter citation tags ("[DOMAIN_KNOWLEDGE:1]", "[LOCAL_WIKI:1, 4]", ...)
  are stripped from generate() output, with leftover whitespace/punctuation
  spacing tidied up.
- Ordinary bracketed text ("[see note]", "[1]") is left untouched.
- <think>...</think> reasoning blocks are stripped from generate() output.
- A response that hit the output-token cap (finish_reason == "length") or
  came back with no real content (e.g. only a <think> block) raises
  LLMTruncatedError instead of returning partial/empty text.
"""
from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

try:
    import pydantic  # noqa: F401
    _NEEDS_SDK_STUBS = False
except Exception:
    _NEEDS_SDK_STUBS = True

if _NEEDS_SDK_STUBS:
    # This dev environment is missing `pydantic`, a transitive dependency of
    # the real `openai`/`anthropic`/`google-genai` SDKs, so importing
    # `meshai.backends` (which eagerly imports all three backend modules)
    # fails before we ever reach the module under test. openai_backend.py
    # only uses AsyncOpenAI as a thin client holder -- every test below
    # replaces `client.chat.completions.create` with an AsyncMock anyway --
    # so minimal stubs are enough to exercise the stripping logic in
    # generate(). Production/CI environments have the real packages
    # installed; these stubs only cover the gap in this local environment.
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

from meshai.backends.base import LLMTruncatedError
from meshai.backends.openai_backend import (
    OpenAIBackend,
    _strip_rag_citations,
    _strip_think_blocks,
)
from meshai.config import LLMConfig


def _make_backend() -> OpenAIBackend:
    return OpenAIBackend(config=LLMConfig(), api_key="test-key")


def _mock_response(content: str, finish_reason: str = "stop") -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content),
                finish_reason=finish_reason,
            )
        ]
    )


# ---------------------------------------------------------------------------
# _strip_rag_citations unit tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("The sky is blue [DOMAIN_KNOWLEDGE:1].", "The sky is blue."),
        ("Node is offline [RECON:2]. Try again.", "Node is offline. Try again."),
        ("Per the weather feed [WEB:3], expect rain.", "Per the weather feed, expect rain."),
        (
            "Combined sources say so [LOCAL_WIKI:1, 4].",
            "Combined sources say so.",
        ),
        (
            "Multiple tags[DOMAIN_KNOWLEDGE:1][RECON:2] in a row.",
            "Multiple tags in a row.",
        ),
    ],
)
def test_strip_rag_citations_removes_tags(raw: str, expected: str) -> None:
    assert _strip_rag_citations(raw) == expected


def test_strip_rag_citations_leaves_ordinary_brackets_alone() -> None:
    assert _strip_rag_citations("See the details [see note].") == "See the details [see note]."
    assert _strip_rag_citations("Reference [1] applies here.") == "Reference [1] applies here."


def test_strip_rag_citations_collapses_double_spaces() -> None:
    assert _strip_rag_citations("two  spaces  here") == "two spaces here"


# ---------------------------------------------------------------------------
# _strip_think_blocks unit tests
# ---------------------------------------------------------------------------


def test_strip_think_blocks_removes_reasoning() -> None:
    raw = "<think>internal reasoning here</think>The actual answer."
    assert _strip_think_blocks(raw) == "The actual answer."


def test_strip_think_blocks_multiline_and_case_insensitive() -> None:
    raw = "<THINK>\nline one\nline two\n</THINK>Final answer"
    assert _strip_think_blocks(raw) == "Final answer"


def test_strip_think_blocks_noop_when_absent() -> None:
    assert _strip_think_blocks("No reasoning block here.") == "No reasoning block here."


# ---------------------------------------------------------------------------
# generate() integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_strips_citation_tag_from_response() -> None:
    backend = _make_backend()
    backend._client.chat.completions.create = AsyncMock(
        return_value=_mock_response("The mesh is healthy [DOMAIN_KNOWLEDGE:1].")
    )

    result = await backend.generate(messages=[{"role": "user", "content": "status?"}], system_prompt="sys")

    assert result == "The mesh is healthy."
    await backend.close()


@pytest.mark.asyncio
async def test_generate_strips_multi_index_citation_tag() -> None:
    backend = _make_backend()
    backend._client.chat.completions.create = AsyncMock(
        return_value=_mock_response("Confirmed by two sources [LOCAL_WIKI:1, 4].")
    )

    result = await backend.generate(messages=[{"role": "user", "content": "confirm"}], system_prompt="sys")

    assert result == "Confirmed by two sources."
    await backend.close()


@pytest.mark.asyncio
async def test_generate_preserves_ordinary_brackets() -> None:
    backend = _make_backend()
    backend._client.chat.completions.create = AsyncMock(
        return_value=_mock_response("See node [1] for details [see note].")
    )

    result = await backend.generate(messages=[{"role": "user", "content": "hi"}], system_prompt="sys")

    assert result == "See node [1] for details [see note]."
    await backend.close()


@pytest.mark.asyncio
async def test_generate_strips_think_block_from_response() -> None:
    backend = _make_backend()
    backend._client.chat.completions.create = AsyncMock(
        return_value=_mock_response("<think>let me consider this</think>Here's the answer.")
    )

    result = await backend.generate(messages=[{"role": "user", "content": "hi"}], system_prompt="sys")

    assert result == "Here's the answer."
    await backend.close()


@pytest.mark.asyncio
async def test_generate_raises_truncated_error_for_no_content() -> None:
    backend = _make_backend()
    backend._client.chat.completions.create = AsyncMock(return_value=_mock_response(None))

    with pytest.raises(LLMTruncatedError):
        await backend.generate(messages=[{"role": "user", "content": "hi"}], system_prompt="sys")
    await backend.close()


# ---------------------------------------------------------------------------
# Truncated / empty generation -> LLMTruncatedError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_raises_truncated_error_when_finish_reason_is_length() -> None:
    """The output-token cap cut the model off mid-sentence -- finish_reason
    comes back "length" even though there's partial content. That content
    must never be returned; it's unreliable."""
    backend = _make_backend()
    backend._client.chat.completions.create = AsyncMock(
        return_value=_mock_response(
            "The mesh health looks good but there was an outage on node",
            finish_reason="length",
        )
    )

    with pytest.raises(LLMTruncatedError):
        await backend.generate(messages=[{"role": "user", "content": "status?"}], system_prompt="sys")
    await backend.close()


@pytest.mark.asyncio
async def test_generate_raises_truncated_error_when_only_think_block() -> None:
    """All the output-token budget went to reasoning -- finish_reason may
    still say "stop", but stripped content is empty. This must also be
    treated as a failed generation, not an empty-but-valid reply."""
    backend = _make_backend()
    backend._client.chat.completions.create = AsyncMock(
        return_value=_mock_response(
            "<think>spent the whole budget reasoning about this</think>",
            finish_reason="stop",
        )
    )

    with pytest.raises(LLMTruncatedError):
        await backend.generate(messages=[{"role": "user", "content": "status?"}], system_prompt="sys")
    await backend.close()


@pytest.mark.asyncio
async def test_generate_normal_stop_is_unaffected() -> None:
    """A normal, complete response (finish_reason == "stop") is returned as
    before -- the truncation check must not false-positive on it."""
    backend = _make_backend()
    backend._client.chat.completions.create = AsyncMock(
        return_value=_mock_response("The mesh is healthy.", finish_reason="stop")
    )

    result = await backend.generate(messages=[{"role": "user", "content": "status?"}], system_prompt="sys")

    assert result == "The mesh is healthy."
    await backend.close()
