"""Google Gemini LLM backend with rolling summary memory."""

import logging
import time
from typing import Optional

import google.generativeai as genai

from ..config import LLMConfig
from ..memory import ConversationSummary
from .base import LLMBackend

logger = logging.getLogger(__name__)


class GoogleMemory:
    """Rolling summary memory for Google backend."""

    def __init__(self, model: genai.GenerativeModel, window_size: int = 4, summarize_threshold: int = 8):
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
        """Generate summary using Google Gemini."""
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
            response = await self._model.generate_content_async(
                prompt,
                generation_config=genai.types.GenerationConfig(
                    max_output_tokens=150,
                    temperature=0.3,
                ),
            )
            return response.text.strip() if response.text else f"Previous conversation: {len(messages)} messages."
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


class GoogleBackend(LLMBackend):
    """Google Gemini backend with rolling summary memory."""

    def __init__(
        self,
        config: LLMConfig,
        api_key: str,
        window_size: int = 4,
        summarize_threshold: int = 8,
    ):
        """Initialize Google backend.

        Args:
            config: LLM configuration
            api_key: Google API key
            window_size: Recent message pairs to keep in full
            summarize_threshold: Messages before re-summarizing
        """
        self.config = config
        genai.configure(api_key=api_key)
        self._model = genai.GenerativeModel(config.model)
        self._memory = GoogleMemory(
            model=self._model,
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
        """Generate a response using Google Gemini API.

        Args:
            messages: Conversation history
            system_prompt: System prompt
            max_tokens: Maximum tokens to generate
            user_id: User identifier (enables memory optimization)

        Returns:
            Generated response
        """
        # Use memory manager to optimize context if user_id provided
        enhanced_system = system_prompt
        final_messages = messages

        if user_id and len(messages) > self._memory._window_size * 2:
            summary, recent_messages = await self._memory.get_context_messages(
                user_id=user_id,
                full_history=messages,
            )

            if summary:
                enhanced_system = f"{system_prompt}\n\nPrevious conversation summary: {summary}"
                final_messages = recent_messages

                logger.debug(
                    f"Using summary + {len(recent_messages)} recent messages "
                    f"(total history: {len(messages)})"
                )

        try:
            # Convert messages to Gemini format
            # Gemini uses "user" and "model" roles
            history = []
            for msg in final_messages[:-1]:  # All but last message
                role = "model" if msg["role"] == "assistant" else "user"
                history.append({"role": role, "parts": [msg["content"]]})

            # Start chat with history
            chat = self._model.start_chat(history=history)

            # Get the last user message
            last_message = final_messages[-1]["content"] if final_messages else ""

            # Prepend system prompt to first message if needed
            if enhanced_system and not history:
                last_message = f"{enhanced_system}\n\n{last_message}"

            # Generate response
            response = await chat.send_message_async(
                last_message,
                generation_config=genai.types.GenerationConfig(
                    max_output_tokens=max_tokens,
                    temperature=0.7,
                ),
            )

            return response.text.strip() if response.text else ""

        except Exception as e:
            logger.error(f"Google API error: {e}")
            raise

    def get_memory(self) -> GoogleMemory:
        """Get the memory manager instance."""
        return self._memory

    async def generate_with_search(
        self,
        query: str,
        system_prompt: Optional[str] = None,
    ) -> str:
        """Generate response - uses Gemini's built-in grounding if available."""
        prompt = system_prompt or "You are a helpful assistant."

        messages = [{"role": "user", "content": query}]

        return await self.generate(messages, prompt, max_tokens=300)

    async def close(self) -> None:
        """Clean up - nothing to close for Google client."""
        pass
