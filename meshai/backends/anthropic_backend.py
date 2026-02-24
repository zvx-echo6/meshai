"""Anthropic (Claude) LLM backend with rolling summary memory."""

import logging
from typing import Optional

from anthropic import AsyncAnthropic

from ..config import LLMConfig
from ..memory import RollingSummaryMemory
from .base import LLMBackend

logger = logging.getLogger(__name__)

_SUMMARIZE_PROMPT = """Summarize this conversation in 2-3 concise sentences. Focus on:
- Main topics discussed
- Important context or user preferences
- Key information to remember

Conversation:
{conversation}

Summary (2-3 sentences):"""


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

        # Initialize rolling summary memory with Anthropic summarize function
        self._memory = RollingSummaryMemory(
            summarize_fn=self._summarize_messages,
            window_size=window_size,
            summarize_threshold=summarize_threshold,
        )

    async def _summarize_messages(self, messages: list[dict]) -> str:
        """Summarize messages using Anthropic API."""
        if not messages:
            return "No previous conversation."

        conversation = "\n".join(
            [f"{msg['role'].upper()}: {msg['content']}" for msg in messages]
        )
        prompt = _SUMMARIZE_PROMPT.format(conversation=conversation)

        try:
            response = await self._client.messages.create(
                model=self.config.model,
                max_tokens=150,
                messages=[{"role": "user", "content": prompt}],
            )
            content = response.content[0].text if response.content else ""
            return content.strip() if content else f"Previous conversation: {len(messages)} messages."
        except Exception as e:
            logger.warning(f"Failed to generate summary: {e}")
            return f"Previous conversation: {len(messages)} messages about various topics."

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

    def get_memory(self) -> RollingSummaryMemory:
        """Get the memory manager instance."""
        return self._memory

    async def close(self) -> None:
        """Close the client."""
        await self._client.close()
