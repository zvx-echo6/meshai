"""OpenAI-compatible LLM backend with rolling summary memory."""

import logging
from typing import Optional

from openai import AsyncOpenAI

from ..config import LLMConfig
from ..memory import ConversationSummary, RollingSummaryMemory
from .base import LLMBackend

logger = logging.getLogger(__name__)


class OpenAIBackend(LLMBackend):
    """OpenAI-compatible backend (works with OpenAI, LiteLLM, local models)."""

    def __init__(
        self,
        config: LLMConfig,
        api_key: str,
        window_size: int = 4,
        summarize_threshold: int = 8,
    ):
        """Initialize OpenAI backend.

        Args:
            config: LLM configuration
            api_key: API key to use
            window_size: Recent message pairs to keep in full
            summarize_threshold: Messages before re-summarizing
        """
        self.config = config
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=config.base_url,
        )

        # Initialize rolling summary memory for context optimization
        self._memory = RollingSummaryMemory(
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
        """Generate a response using OpenAI-compatible API.

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
                full_messages = [{"role": "system", "content": enhanced_system}]
                full_messages.extend(recent_messages)

                logger.debug(
                    f"Using summary + {len(recent_messages)} recent messages "
                    f"(total history: {len(messages)})"
                )
            else:
                # Short conversation: system + all messages
                full_messages = [{"role": "system", "content": system_prompt}]
                full_messages.extend(messages)
        else:
            # No user_id or short conversation - use full history
            full_messages = [{"role": "system", "content": system_prompt}]
            full_messages.extend(messages)

        try:
            response = await self._client.chat.completions.create(
                model=self.config.model,
                messages=full_messages,
                max_tokens=max_tokens,
                temperature=0.7,
            )

            content = response.choices[0].message.content
            return content.strip() if content else ""

        except Exception as e:
            logger.error(f"OpenAI API error: {e}")
            raise

    def get_memory(self) -> RollingSummaryMemory:
        """Get the memory manager instance."""
        return self._memory

    async def generate_with_search(
        self,
        query: str,
        system_prompt: Optional[str] = None,
    ) -> str:
        """Generate response - search depends on model/provider capabilities.

        Note: True web search requires the model/provider to support it
        (e.g., OpenAI with plugins, or a local setup with SearXNG).
        This implementation just passes the query as a regular message.
        """
        prompt = system_prompt or (
            "You are a helpful assistant. Answer the following question. "
            "If you have web search access, use it for current information."
        )

        messages = [{"role": "user", "content": query}]

        return await self.generate(messages, prompt, max_tokens=300)

    async def close(self) -> None:
        """Close the client."""
        await self._client.close()
