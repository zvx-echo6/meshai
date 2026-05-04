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

# Phrases that trigger continuation of a previous response
CONTINUE_PHRASES = {
    "yes", "yeah", "yep", "yea", "sure", "ok", "okay", "go on",
    "keep going", "continue", "more", "go ahead", "tell me more",
    "yes please", "y",
}

CONTINUATION_PROMPT = "Want me to keep going?"


def split_sentences(text: str) -> list[str]:
    """Split text into sentences, preserving abbreviations and decimals."""
    # Split on . ! ? followed by space or end of string
    # But not on decimals (4.8) or common abbreviations (e.g. Dr. Mr. etc.)
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    # Filter empty strings
    return [s.strip() for s in sentences if s.strip()]


def chunk_response(
    text: str,
    max_chars: int = 200,
    max_messages: int = 3,
) -> tuple[list[str], str]:
    """Split a response into sentence-aligned messages.

    Args:
        text: Full LLM response text
        max_chars: Maximum characters per message
        max_messages: Maximum messages to send before prompting

    Returns:
        Tuple of (messages_to_send, remaining_text)
        If remaining_text is non-empty, the last message includes
        a continuation prompt.
    """
    sentences = split_sentences(text)
    if not sentences:
        return [text[:max_chars]], ""

    messages = []
    current_msg = []
    current_len = 0
    sentence_idx = 0

    while sentence_idx < len(sentences) and len(messages) < max_messages:
        sentence = sentences[sentence_idx]

        # Would this sentence fit in the current message?
        added_len = len(sentence) + (1 if current_msg else 0)  # +1 for space

        if current_len + added_len <= max_chars:
            current_msg.append(sentence)
            current_len += added_len
            sentence_idx += 1
        else:
            # Sentence doesn't fit
            if current_msg:
                # Flush current message, start new one with this sentence
                messages.append(" ".join(current_msg))
                current_msg = []
                current_len = 0
                # Don't increment sentence_idx — retry this sentence in next message
            else:
                # Single sentence exceeds max_chars — truncate it
                messages.append(sentence[:max_chars])
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
        if len(last_msg) + 1 + len(prompt) <= max_chars:
            messages[-1] = last_msg + " " + prompt
        else:
            # Need to shorten the last message to fit the prompt
            # Remove sentences from the end until it fits
            last_sentences = split_sentences(last_msg)
            while last_sentences:
                test = " ".join(last_sentences) + " " + prompt
                if len(test) <= max_chars:
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
