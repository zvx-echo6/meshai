"""Google Gemini LLM backend with rolling summary memory."""

import logging
from typing import Optional

import google.generativeai as genai

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

        # Initialize rolling summary memory with Gemini summarize function
        self._memory = RollingSummaryMemory(
            summarize_fn=self._summarize_messages,
            window_size=window_size,
            summarize_threshold=summarize_threshold,
        )

    async def _summarize_messages(self, messages: list[dict]) -> str:
        """Summarize messages using Google Gemini API."""
        if not messages:
            return "No previous conversation."

        conversation = "\n".join(
            [f"{msg['role'].upper()}: {msg['content']}" for msg in messages]
        )
        prompt = _SUMMARIZE_PROMPT.format(conversation=conversation)

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
            # Create model with system instruction for persistent system prompt
            model = genai.GenerativeModel(
                self.config.model,
                system_instruction=enhanced_system if enhanced_system else None,
            )

            # Convert messages to Gemini format
            # Gemini uses "user" and "model" roles
            history = []
            for msg in final_messages[:-1]:  # All but last message
                role = "model" if msg["role"] == "assistant" else "user"
                history.append({"role": role, "parts": [msg["content"]]})

            # Start chat with history
            chat = model.start_chat(history=history)

            # Get the last user message
            last_message = final_messages[-1]["content"] if final_messages else ""

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

    def get_memory(self) -> RollingSummaryMemory:
        """Get the memory manager instance."""
        return self._memory

    async def close(self) -> None:
        """Clean up - nothing to close for Google client."""
        pass
