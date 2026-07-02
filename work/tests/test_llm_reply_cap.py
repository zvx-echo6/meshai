"""Tests for the interactive LLM reply cap (MAX_REPLY_PACKETS).

All tests are hermetic — no real LLM, no real socket.
"""

import pytest

from meshai.chunker import cap_reply_chunks, chunk_response, MAX_REPLY_PACKETS, _TRUNCATION_INDICATOR


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_chunks(n: int, chars_each: int = 50) -> list[str]:
    """Build n chunks of exactly chars_each ASCII characters."""
    return [f"chunk{i}" + "x" * (chars_each - len(f"chunk{i}")) for i in range(n)]


# ---------------------------------------------------------------------------
# Test 1 — long reply produces exactly 3 chunks with truncation indicator
# ---------------------------------------------------------------------------

def test_long_reply_capped_to_three_chunks():
    """A reply long enough to produce >3 chunks results in exactly 3 delivered
    chunks, and the truncation indicator is present in the last chunk."""
    max_chars = 140

    # Build a response that will produce >3 chunks at 140 chars each.
    # Each sentence is ~130 chars so 5 sentences → 5 chunks.
    sentence = "A" * 130 + "."
    text = " ".join([sentence] * 5)

    # chunk_response with a high max_messages so it happily returns 5 chunks
    raw_chunks, _ = chunk_response(text, max_chars=max_chars, max_messages=10)
    assert len(raw_chunks) > MAX_REPLY_PACKETS, (
        f"Precondition: expected >3 raw chunks, got {len(raw_chunks)}"
    )

    capped = cap_reply_chunks(raw_chunks, MAX_REPLY_PACKETS, max_chars)

    assert len(capped) == MAX_REPLY_PACKETS
    assert _TRUNCATION_INDICATOR in capped[-1], (
        f"Expected truncation indicator in last chunk, got: {capped[-1]!r}"
    )


# ---------------------------------------------------------------------------
# Test 2 — short reply is delivered unchanged, no indicator
# ---------------------------------------------------------------------------

def test_short_reply_unchanged():
    """A short reply (<=3 chunks) is delivered unchanged with no indicator."""
    max_chars = 140

    # One chunk well under the limit
    chunks_1 = ["Hello, this is a short reply."]
    result_1 = cap_reply_chunks(chunks_1, MAX_REPLY_PACKETS, max_chars)
    assert result_1 == chunks_1
    assert _TRUNCATION_INDICATOR not in result_1[-1]

    # Exactly 3 chunks — boundary condition
    chunks_3 = _make_chunks(3, chars_each=50)
    result_3 = cap_reply_chunks(chunks_3, MAX_REPLY_PACKETS, max_chars)
    assert result_3 == chunks_3
    assert _TRUNCATION_INDICATOR not in result_3[-1]


# ---------------------------------------------------------------------------
# Test 3 — cap respects connector.max_chars=140
# ---------------------------------------------------------------------------

def test_cap_respects_max_chars_140():
    """cap_reply_chunks uses the max_chars budget (140) from connector.max_chars."""
    max_chars = 140

    # 5 chunks of 80 chars each (well under max_chars individually)
    chunks = _make_chunks(5, chars_each=80)

    capped = cap_reply_chunks(chunks, MAX_REPLY_PACKETS, max_chars)

    assert len(capped) == 3
    # Last chunk must not exceed max_chars in total length
    assert len(capped[-1]) <= max_chars
    assert _TRUNCATION_INDICATOR in capped[-1]


# ---------------------------------------------------------------------------
# Test 4 — 3rd chunk trimmed correctly when at max_chars limit
# ---------------------------------------------------------------------------

def test_third_chunk_trimmed_to_fit_indicator():
    """When the 3rd chunk fills max_chars exactly, it is trimmed so that
    trimmed_chunk + indicator == max_chars exactly."""
    max_chars = 140
    indicator = _TRUNCATION_INDICATOR  # len 16 Python chars (" …(ask for more)")

    # 3rd chunk that is exactly max_chars long
    full_chunk = "B" * max_chars
    chunks = [
        "First chunk.",
        "Second chunk.",
        full_chunk,
        "Fourth chunk (should be dropped).",
    ]

    capped = cap_reply_chunks(chunks, MAX_REPLY_PACKETS, max_chars)

    assert len(capped) == MAX_REPLY_PACKETS
    last = capped[-1]
    assert last.endswith(indicator), f"Expected indicator at end, got: {last!r}"
    assert len(last) == max_chars, (
        f"Expected trimmed chunk + indicator == {max_chars}, got len={len(last)}"
    )
    # The trimmed body is max_chars - len(indicator) Bs
    expected_body = "B" * (max_chars - len(indicator))
    assert last == expected_body + indicator
