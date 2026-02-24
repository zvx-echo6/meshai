"""Google Gemini LLM backend with rolling summary memory and Google Search grounding."""

import logging
from typing import Optional

from google import genai
from google.genai import types

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
    """Google Gemini backend with rolling summary memory and optional grounding."""

    def __init__(
        self,
        config: LLMConfig,
        api_key: str,
        window_size: int = 4,
        summarize_threshold: int = 8,
    ):
        self.config = config
        self._client = genai.Client(api_key=api_key)

        self._memory = RollingSummaryMemory(
            summarize_fn=self._summarize_messages,
            window_size=window_size,
            summarize_threshold=summarize_threshold,
        )

    async def _summarize_messages(self, messages: list[dict]) -> str:
        """Summarize messages using Gemini."""
        if not messages:
            return "No previous conversation."

        conversation = "\n".join(
            [f"{msg['role'].upper()}: {msg['content']}" for msg in messages]
        )
        prompt = _SUMMARIZE_PROMPT.format(conversation=conversation)

        try:
            response = await self._client.aio.models.generate_content(
                model=self.config.model,
                contents=prompt,
                config=types.GenerateContentConfig(
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
        """Generate a response using Google Gemini with optional grounding."""
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
            contents = []
            for msg in final_messages:
                role = "model" if msg["role"] == "assistant" else "user"
                contents.append(
                    types.Content(
                        role=role,
                        parts=[types.Part.from_text(text=msg["content"])],
                    )
                )

            tools = []
            if self.config.google_grounding:
                tools.append(types.Tool(google_search=types.GoogleSearch()))

            config = types.GenerateContentConfig(
                system_instruction=enhanced_system if enhanced_system else None,
                max_output_tokens=max_tokens,
                temperature=0.7,
                tools=tools if tools else None,
            )

            response = await self._client.aio.models.generate_content(
                model=self.config.model,
                contents=contents,
                config=config,
            )

            return response.text.strip() if response.text else ""

        except Exception as e:
            logger.error(f"Google API error: {e}")
            raise

    def get_memory(self) -> RollingSummaryMemory:
        return self._memory

    async def close(self) -> None:
        pass
