"""Sentence-aware message chunker for Meshtastic's character limits.

Splits LLM responses into messages that:
- Never exceed max_chars per message (default 200)
- Never split a sentence across messages
- Send at most max_messages per response (default 3)
- If more content remains, replace the last sentence with a continuation prompt
- Support up to max_continuations follow-ups (default 3)
"""

import logging
import re

logger = logging.getLogger(__name__)

# Hard ceiling on interactive LLM replies — protects LoRa airtime.
# Broadcast/notification chunking is NOT subject to this cap.
MAX_REPLY_PACKETS = 3

_TRUNCATION_INDICATOR = " …(ask for more)"


def cap_reply_chunks(chunks: list[str], max_packets: int, max_chars: int) -> list[str]:
    """Cap LLM reply chunks to max_packets; append truncation indicator if cut.

    Args:
        chunks: List of message chunks produced by chunk_response.
        max_packets: Maximum number of packets to deliver (e.g. MAX_REPLY_PACKETS).
        max_chars: Per-packet character budget (connector.max_chars).

    Returns:
        Original list when len(chunks) <= max_packets; otherwise a capped
        copy of length max_packets with the truncation indicator appended
        (and the last chunk trimmed if necessary to stay within max_chars).
    """
    if len(chunks) <= max_packets:
        return chunks
    capped = list(chunks[:max_packets])
    last = capped[-1]
    if len(last) + len(_TRUNCATION_INDICATOR) > max_chars:
        last = last[: max_chars - len(_TRUNCATION_INDICATOR)]
    capped[-1] = last + _TRUNCATION_INDICATOR
    return capped


def strip_markdown(text: str) -> str:
    """Remove markdown formatting from LLM output.

    LLMs often ignore 'no markdown' instructions.
    This strips it before sending over LoRa.
    """
    # Remove bold **text**
    text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
    # Remove italic *text*
    text = re.sub(r'\*(.*?)\*', r'\1', text)
    # Remove headers (## Header)
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # Remove bullet points at line start (- item or * item)
    text = re.sub(r'^\s*[-*]\s+', '', text, flags=re.MULTILINE)
    # Remove numbered lists at line start (1. item)
    text = re.sub(r'^\s*\d+\.\s+', '', text, flags=re.MULTILINE)
    # Remove code blocks
    text = re.sub(r'```.*?```', '', text, flags=re.DOTALL)
    # Remove inline code
    text = re.sub(r'`(.*?)`', r'\1', text)
    return text.strip()


# Phrases that trigger continuation of a previous response
CONTINUE_PHRASES = {
    "yes", "yeah", "yep", "yea", "sure", "ok", "okay", "go on",
    "keep going", "continue", "more", "go ahead", "tell me more",
    "yes please", "y",
}

CONTINUATION_PROMPT = "Want me to keep going?"


def split_sentences(text: str) -> list[str]:
    """Split text into sentences on periods, newlines, or question marks."""
    # First split on newlines (each line is a chunk candidate)
    lines = text.strip().split('\n')

    sentences = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # Then split on sentence boundaries within each line
        parts = re.split(r'(?<=[.!?])\s+', line)
        sentences.extend(p.strip() for p in parts if p.strip())

    return sentences


def _byte_len(s: str) -> int:
    """Get UTF-8 byte length of a string."""
    return len(s.encode('utf-8'))


def chunk_response(
    text: str,
    max_chars: int = 200,
    max_messages: int = 3,
) -> tuple[list[str], str]:
    """Split a response into sentence-aligned messages.

    Args:
        text: Full LLM response text
        max_chars: Maximum BYTES per message (LoRa limit, not characters)
        max_messages: Maximum messages to send before prompting

    Returns:
        Tuple of (messages_to_send, remaining_text)
        If remaining_text is non-empty, the last message includes
        a continuation prompt.
    """
    sentences = split_sentences(text)
    if not sentences:
        truncated = text[:max_chars]
        while _byte_len(truncated) > max_chars and truncated:
            truncated = truncated[:-1]
        return [truncated], ""

    messages = []
    current_msg = []
    current_bytes = 0
    sentence_idx = 0

    while sentence_idx < len(sentences) and len(messages) < max_messages:
        sentence = sentences[sentence_idx]
        sentence_bytes = _byte_len(sentence)

        # Would this sentence fit in the current message?
        # +1 byte for space between sentences
        added_bytes = sentence_bytes + (1 if current_msg else 0)

        if current_bytes + added_bytes <= max_chars:
            current_msg.append(sentence)
            current_bytes += added_bytes
            sentence_idx += 1
        else:
            # Sentence doesn't fit
            if current_msg:
                # Flush current message, start new one with this sentence
                messages.append(" ".join(current_msg))
                current_msg = []
                current_bytes = 0
                # Don't increment sentence_idx — retry this sentence in next message
            else:
                # Single sentence exceeds max_chars — split at last word boundary
                # Find break point that fits in byte budget
                words = sentence.split(' ')
                fit_words = []
                fit_bytes = 0
                for word in words:
                    word_bytes = _byte_len(word) + (1 if fit_words else 0)
                    if fit_bytes + word_bytes <= max_chars:
                        fit_words.append(word)
                        fit_bytes += word_bytes
                    else:
                        break

                if fit_words:
                    messages.append(" ".join(fit_words))
                    leftover = " ".join(words[len(fit_words):])
                    if leftover:
                        sentences.insert(sentence_idx + 1, leftover)
                else:
                    # Even first word doesn't fit — truncate it
                    truncated = sentence
                    while _byte_len(truncated) > max_chars and truncated:
                        truncated = truncated[:-1]
                    messages.append(truncated)
                    leftover = sentence[len(truncated):].lstrip()
                    if leftover:
                        sentences.insert(sentence_idx + 1, leftover)
                sentence_idx += 1

    # Flush any remaining buffered message
    if current_msg and len(messages) < max_messages:
        messages.append(" ".join(current_msg))

    # Determine remaining text
    remaining_sentences = sentences[sentence_idx:]

    # Also include any sentence that was in current_msg but didn't get flushed
    # because we hit max_messages
    if current_msg and len(messages) >= max_messages:
        remaining_sentences = [" ".join(current_msg)] + remaining_sentences

    remaining = " ".join(remaining_sentences)

    # If there's remaining content, replace the end of the last message
    # with a continuation prompt
    if remaining:
        prompt = CONTINUATION_PROMPT
        last_msg = messages[-1] if messages else ""

        # Check if we can append the prompt to the last message
        if _byte_len(last_msg) + 1 + _byte_len(prompt) <= max_chars:
            messages[-1] = last_msg + " " + prompt
        else:
            # Need to shorten the last message to fit the prompt
            # Remove sentences from the end until it fits
            last_sentences = split_sentences(last_msg)
            while last_sentences:
                test = " ".join(last_sentences) + " " + prompt
                if _byte_len(test) <= max_chars:
                    # Put removed sentences back into remaining
                    messages[-1] = test
                    break
                removed = last_sentences.pop()
                remaining = removed + " " + remaining
            else:
                # Couldn't fit — just use the prompt as the last message
                messages[-1] = prompt

    return messages, remaining


class ContinuationState:
    """Tracks continuation state per user."""

    def __init__(self, max_continuations: int = 3):
        self.max_continuations = max_continuations
        # user_id -> {"remaining": str, "count": int}
        self._state: dict[str, dict] = {}

    def has_pending(self, user_id: str) -> bool:
        """Check if user has pending continuation content."""
        return user_id in self._state and bool(self._state[user_id]["remaining"])

    def is_continuation_request(self, text: str) -> bool:
        """Check if the message is a request to continue."""
        return text.strip().lower().rstrip("!.,?") in CONTINUE_PHRASES

    def store(self, user_id: str, remaining: str) -> None:
        """Store remaining content for a user."""
        if remaining:
            existing = self._state.get(user_id, {"count": 0})
            self._state[user_id] = {
                "remaining": remaining,
                "count": existing.get("count", 0),
            }
        elif user_id in self._state:
            del self._state[user_id]

    def get_continuation(self, user_id: str) -> tuple[list[str], str] | None:
        """Get the next batch of messages for a continuation request.

        Returns None if no pending content or max continuations reached.
        """
        if user_id not in self._state:
            return None

        state = self._state[user_id]
        if state["count"] >= self.max_continuations:
            del self._state[user_id]
            return None

        remaining = state["remaining"]
        if not remaining:
            del self._state[user_id]
            return None

        messages, new_remaining = chunk_response(remaining)
        state["count"] += 1
        state["remaining"] = new_remaining

        if not new_remaining:
            del self._state[user_id]

        return messages, new_remaining

    def clear(self, user_id: str) -> None:
        """Clear continuation state for a user."""
        self._state.pop(user_id, None)
