"""Tests for Responder.send_response's empty-chunk guard.

Matt's rule (13:10 UTC incident): an all-citations LLM reply whose Sources
line got stripped down to nothing chunked to [""], which Responder.
send_response then transmitted as a real, header-only, zero-length DM over
openhop. Responder must never hand an empty/whitespace-only chunk to the
connector, no matter what produced it -- this is the last-resort guard on
the send path itself, independent of router.py's own handling.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from meshai.config import ResponseConfig
from meshai.responder import Responder


def _make_responder() -> tuple[Responder, MagicMock]:
    connector = MagicMock()
    connector.send_message_async = AsyncMock(return_value=True)
    cfg = ResponseConfig(delay_min=0.0, delay_max=0.0)
    return Responder(cfg, connector), connector


@pytest.mark.asyncio
async def test_send_response_never_sends_empty_string():
    responder, connector = _make_responder()

    result = await responder.send_response("", destination="!abc", channel=0)

    connector.send_message_async.assert_not_awaited()
    assert result is True


@pytest.mark.asyncio
async def test_send_response_never_sends_whitespace_only_string():
    responder, connector = _make_responder()

    result = await responder.send_response("   \n  ", destination="!abc", channel=0)

    connector.send_message_async.assert_not_awaited()
    assert result is True


@pytest.mark.asyncio
async def test_send_response_drops_empty_chunk_from_list_sends_rest():
    responder, connector = _make_responder()

    result = await responder.send_response(
        ["Real answer.", "", "  ", "More answer."],
        destination="!abc",
        channel=0,
    )

    assert result is True
    sent_texts = [
        call.kwargs["text"] for call in connector.send_message_async.await_args_list
    ]
    assert sent_texts == ["Real answer.", "More answer."]
    assert "" not in sent_texts


@pytest.mark.asyncio
async def test_send_response_all_empty_list_sends_nothing():
    responder, connector = _make_responder()

    result = await responder.send_response(["", "   "], destination="!abc", channel=0)

    connector.send_message_async.assert_not_awaited()
    assert result is True


@pytest.mark.asyncio
async def test_send_response_normal_text_unaffected():
    """Sanity check the guard doesn't touch ordinary non-empty sends."""
    responder, connector = _make_responder()

    result = await responder.send_response("hello there", destination="!abc", channel=0)

    connector.send_message_async.assert_awaited_once()
    assert connector.send_message_async.await_args.kwargs["text"] == "hello there"
    assert result is True
