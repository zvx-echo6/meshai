"""Anthropic (Claude) LLM backend with rolling summary memory."""

import logging
import time
from typing import Optional

from anthropic import AsyncAnthropic

from ..config import LLMConfig
from ..memory import ConversationSummary
from .base import LLMBackend

logger = logging.getLogger(__name__)


class AnthropicMemory:
    """Rolling summary memory for Anthropic backend."""

    def __init__(self, client: AsyncAnthropic, model: str, window_size: int = 4, summarize_threshold: int = 8):
        self._client = client
        self._model = model
        self._window_size = window_size
        self._summarize_threshold = summarize_threshold
        self._summaries: dict[str, ConversationSummary] = {}

    async def get_context_messages(
        self, user_id: str, full_history: list[dict]
    ) -> tuple[Optional[str], list[dict]]:
        """Get optimized context: summary + recent messages."""
        if len(full_history) <= self._window_size * 2:
            return None, full_history

        split_point = -(self._window_size * 2)
        old_messages = full_history[:split_point]
        recent_messages = full_history[split_point:]

        summary = await self._get_or_create_summary(user_id, old_messages)
        return summary.summary, recent_messages

    async def _get_or_create_summary(self, user_id: str, messages: list[dict]) -> ConversationSummary:
        """Get cached summary or create new one."""
        if user_id in self._summaries:
            cached = self._summaries[user_id]
            if abs(cached.message_count - len(messages)) < self._summarize_threshold:
                return cached

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
        """Generate summary using Anthropic."""
        if not messages:
            return "No previous conversation."

        conversation = "\n".join([f"{msg['role'].upper()}: {msg['content']}" for msg in messages])

        prompt = f"""Summarize this conversation in 2-3 concise sentences. Focus on:
- Main topics discussed
- Important context or user preferences
- Key information to remember

Conversation:
{conversation}

Summary (2-3 sentences):"""

        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=150,
                messages=[{"role": "user", "content": prompt}],
            )
            content = response.content[0].text if response.content else ""
            return content.strip() if content else f"Previous conversation: {len(messages)} messages."
        except Exception as e:
            logger.warning(f"Failed to generate summary: {e}")
            return f"Previous conversation: {len(messages)} messages about various topics."

    def load_summary(self, user_id: str, summary: ConversationSummary) -> None:
        """Load summary from database into cache."""
        self._summaries[user_id] = summary

    def clear_summary(self, user_id: str) -> None:
        """Clear cached summary for user."""
        self._summaries.pop(user_id, None)

    def get_cached_summary(self, user_id: str) -> Optional[ConversationSummary]:
        """Get cached summary for user."""
        return self._summaries.get(user_id)


class AnthropicBackend(LLMBackend):
    """Anthropic Claude backend with rolling summary memory."""

    def __init__(
        self,
        config: LLMConfig,
        api_key: str,
        window_size: int = 4,
        summarize_threshold: int = 8,
    ):
        """Initialize Anthropic backend.

        Args:
            config: LLM configuration
            api_key: Anthropic API key
            window_size: Recent message pairs to keep in full
            summarize_threshold: Messages before re-summarizing
        """
        self.config = config
        self._client = AsyncAnthropic(api_key=api_key)
        self._memory = AnthropicMemory(
            client=self._client,
            model=config.model,
            window_size=window_size,
            summarize_threshold=summarize_threshold,
        )

    async def generate(
        self,
        messages: list[dict],
        system_prompt: str,
        max_tokens: int = 300,
        user_id: Optional[str] = None,
    ) -> str:
        """Generate a response using Anthropic API.

        Args:
            messages: Conversation history
            system_prompt: System prompt
            max_tokens: Maximum tokens to generate
            user_id: User identifier (enables memory optimization)

        Returns:
            Generated response
        """
        # Use memory manager to optimize context if user_id provided
        if user_id and len(messages) > self._memory._window_size * 2:
            summary, recent_messages = await self._memory.get_context_messages(
                user_id=user_id,
                full_history=messages,
            )

            if summary:
                # Long conversation: system + summary + recent
                enhanced_system = f"{system_prompt}\n\nPrevious conversation summary: {summary}"
                final_messages = recent_messages

                logger.debug(
                    f"Using summary + {len(recent_messages)} recent messages "
                    f"(total history: {len(messages)})"
                )
            else:
                enhanced_system = system_prompt
                final_messages = messages
        else:
            enhanced_system = system_prompt
            final_messages = messages

        try:
            response = await self._client.messages.create(
                model=self.config.model,
                max_tokens=max_tokens,
                system=enhanced_system,
                messages=final_messages,
            )

            # Extract text from response
            content = response.content[0].text if response.content else ""
            return content.strip()

        except Exception as e:
            logger.error(f"Anthropic API error: {e}")
            raise

    def get_memory(self) -> AnthropicMemory:
        """Get the memory manager instance."""
        return self._memory

    async def generate_with_search(
        self,
        query: str,
        system_prompt: Optional[str] = None,
    ) -> str:
        """Generate response - Anthropic doesn't have built-in search."""
        prompt = system_prompt or (
            "You are a helpful assistant. Answer the following question "
            "based on your knowledge."
        )

        messages = [{"role": "user", "content": query}]

        return await self.generate(messages, prompt, max_tokens=300)

    async def close(self) -> None:
        """Close the client."""
        await self._client.close()
