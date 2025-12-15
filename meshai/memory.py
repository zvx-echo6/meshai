"""Lightweight rolling summary memory manager for conversation context optimization."""

import logging
import time
from dataclasses import dataclass
from typing import Optional

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


@dataclass
class ConversationSummary:
    """Summary of conversation history."""

    summary: str
    last_updated: float
    message_count: int


class RollingSummaryMemory:
    """Manages conversation summaries with recent message window.

    Strategy:
    - Keep last N message pairs (window_size) in full
    - Summarize everything before the window
    - Update summary when old messages accumulate

    Example (window_size=4):
        Messages 1-10: Summarized to "User discussed weather and plans"
        Messages 11-18: Kept in full (last 4 pairs)
        Context sent: [Summary] + [Messages 11-18]

    This achieves ~70-80% token reduction for long conversations
    while preserving both long-term context (via summary) and
    recent context (via raw messages).
    """

    def __init__(
        self,
        client: AsyncOpenAI,
        model: str,
        window_size: int = 4,
        summarize_threshold: int = 8,
    ):
        """Initialize rolling summary memory.

        Args:
            client: AsyncOpenAI client for generating summaries
            model: Model name to use for summarization
            window_size: Number of recent message pairs to keep in full
            summarize_threshold: Messages to accumulate before re-summarizing
        """
        self._client = client
        self._model = model
        self._window_size = window_size
        self._summarize_threshold = summarize_threshold

        # In-memory cache of summaries (loaded from DB on startup)
        self._summaries: dict[str, ConversationSummary] = {}

    async def get_context_messages(
        self,
        user_id: str,
        full_history: list[dict],
    ) -> tuple[Optional[str], list[dict]]:
        """Get optimized context: summary + recent messages.

        Args:
            user_id: User identifier
            full_history: Full message history from database

        Returns:
            Tuple of (summary_text, recent_messages)
            summary_text is None if conversation is short
        """
        # Short conversation - no summary needed
        if len(full_history) <= self._window_size * 2:
            return None, full_history

        # Split into old (to summarize) and recent (keep raw)
        split_point = -(self._window_size * 2)
        old_messages = full_history[:split_point]
        recent_messages = full_history[split_point:]

        # Get or create summary
        summary = await self._get_or_create_summary(user_id, old_messages)

        return summary.summary, recent_messages

    async def _get_or_create_summary(
        self,
        user_id: str,
        messages: list[dict],
    ) -> ConversationSummary:
        """Get cached summary or create new one."""
        # Check cache
        if user_id in self._summaries:
            cached = self._summaries[user_id]

            # Reuse if message count is close (within threshold)
            if abs(cached.message_count - len(messages)) < self._summarize_threshold:
                return cached

        # Generate new summary
        logger.debug(f"Generating summary for {user_id} ({len(messages)} messages)")
        summary_text = await self._summarize(messages)

        summary = ConversationSummary(
            summary=summary_text,
            last_updated=time.time(),
            message_count=len(messages),
        )

        self._summaries[user_id] = summary
        return summary

    async def _summarize(self, messages: list[dict]) -> str:
        """Generate summary using LLM."""
        if not messages:
            return "No previous conversation."

        # Format conversation
        conversation = "\n".join(
            [f"{msg['role'].upper()}: {msg['content']}" for msg in messages]
        )

        prompt = f"""Summarize this conversation in 2-3 concise sentences. Focus on:
- Main topics discussed
- Important context or user preferences
- Key information to remember

Conversation:
{conversation}

Summary (2-3 sentences):"""

        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=150,
                temperature=0.3,
            )

            content = response.choices[0].message.content
            return content.strip() if content else f"Previous conversation: {len(messages)} messages."

        except Exception as e:
            logger.warning(f"Failed to generate summary: {e}")
            # Fallback - provide basic context
            return f"Previous conversation: {len(messages)} messages about various topics."

    def load_summary(self, user_id: str, summary: ConversationSummary) -> None:
        """Load summary from database into cache."""
        self._summaries[user_id] = summary

    def clear_summary(self, user_id: str) -> None:
        """Clear cached summary for user."""
        self._summaries.pop(user_id, None)

    def get_cached_summary(self, user_id: str) -> Optional[ConversationSummary]:
        """Get cached summary for user (for persistence)."""
        return self._summaries.get(user_id)
