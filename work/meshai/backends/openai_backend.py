"""OpenAI-compatible LLM backend with rolling summary memory."""

import asyncio
import logging
import re
from typing import Optional

from openai import AsyncOpenAI

from ..config import LLMConfig
from ..memory import RollingSummaryMemory
from .base import LLMBackend, LLMTruncatedError

logger = logging.getLogger(__name__)

_SUMMARIZE_PROMPT = """Summarize this conversation in 2-3 concise sentences. Focus on:
- Main topics discussed
- Important context or user preferences
- Key information to remember

Conversation:
{conversation}

Summary (2-3 sentences):"""

# Open WebUI's RAG filter has the model cite sources inline, e.g.
# "[DOMAIN_KNOWLEDGE:1]" or "[LOCAL_WIKI:1, 4]". These are an artifact of the
# filter, not something we want relayed to mesh users, so strip them before
# returning. Ordinary bracketed text like "[see note]" or "[1]" is left alone.
_CITATION_TAG_RE = re.compile(r"\[[A-Z][A-Z_ ]*:\s*\d+(?:\s*,\s*\d+)*\]")
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)


def _strip_rag_citations(text: str) -> str:
    """Remove RAG filter citation tags and tidy up the whitespace left behind."""
    text = _CITATION_TAG_RE.sub("", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"[ \t]+([.,!?;:])", r"\1", text)
    return text


def _strip_think_blocks(text: str) -> str:
    """Remove <think>...</think> reasoning blocks some models emit in content."""
    return _THINK_BLOCK_RE.sub("", text)


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

        # Initialize rolling summary memory with OpenAI summarize function
        self._memory = RollingSummaryMemory(
            summarize_fn=self._summarize_messages,
            window_size=window_size,
            summarize_threshold=summarize_threshold,
        )

    async def _summarize_messages(self, messages: list[dict]) -> str:
        """Summarize messages using OpenAI API."""
        if not messages:
            return "No previous conversation."

        conversation = "\n".join(
            [f"{msg['role'].upper()}: {msg['content']}" for msg in messages]
        )
        prompt = _SUMMARIZE_PROMPT.format(conversation=conversation)

        try:
            response = await self._client.chat.completions.create(
                model=self.config.model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=150,
                temperature=0.3,
            )
            content = response.choices[0].message.content
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
            # Build request kwargs
            request_kwargs = {
                "model": self.config.model,
                "messages": full_messages,
                "max_tokens": max_tokens,
                "temperature": 0.7,
            }

            # Enable web search if configured (Open WebUI feature)
            # Uses features.web_search parameter
            if getattr(self.config, 'web_search', False):
                request_kwargs["extra_body"] = {"features": {"web_search": True}}

            response = await asyncio.wait_for(
                self._client.chat.completions.create(**request_kwargs),
                timeout=self.config.timeout,
            )

            choice = response.choices[0]
            finish_reason = getattr(choice, "finish_reason", None)
            content = choice.message.content or ""
            content = _strip_think_blocks(content)
            content = _strip_rag_citations(content)
            content = content.strip()

            # Some Open WebUI configurations don't propagate the underlying
            # model's finish_reason=="length" through to us (see the
            # 13:22 UTC incident: a reply truncated mid-sentence at the
            # 1024-token cap came back with a finish_reason that wasn't
            # "length"). usage.completion_tokens is a second, independent
            # signal of the same thing -- if the model generated at or near
            # the requested cap, treat it as truncated even when
            # finish_reason claims otherwise.
            usage = getattr(response, "usage", None)
            completion_tokens = (
                getattr(usage, "completion_tokens", None) if usage is not None else None
            )
            at_token_cap = (
                completion_tokens is not None
                and max_tokens
                and completion_tokens >= max_tokens * 0.98
            )

            # Open WebUI's `aida-mesh` model has a hard output-token cap as a
            # runaway guard. When it's hit mid-answer, finish_reason comes
            # back "length" -- the content may be cut off mid-sentence, or
            # empty if every token went to a stripped <think> block. Either
            # way this is not a usable answer: never relay a partial/cut-off
            # reply to the mesh (Matt's rule -- "I don't have that
            # information" beats confidently wrong).
            if finish_reason == "length" or not content or at_token_cap:
                if finish_reason == "length":
                    signal = "finish_reason=length"
                elif not content:
                    signal = "empty_content"
                else:
                    signal = "completion_tokens_at_cap"
                logger.warning(
                    "LLM generation truncated (signal=%s): finish_reason=%r "
                    "content_length=%d completion_tokens=%r max_tokens=%r",
                    signal,
                    finish_reason,
                    len(content),
                    completion_tokens,
                    max_tokens,
                )
                raise LLMTruncatedError(
                    f"LLM generation truncated or empty (signal={signal}, "
                    f"finish_reason={finish_reason!r}, content_length={len(content)}, "
                    f"completion_tokens={completion_tokens!r}, max_tokens={max_tokens!r})"
                )

            return content

        except asyncio.TimeoutError:
            logger.error(f"OpenAI API timed out after {self.config.timeout}s")
            raise
        except LLMTruncatedError:
            # Already logged (with finish_reason/content_length) above --
            # just propagate so router.py's error handling can react to it
            # specifically, without also logging it as a generic API error.
            raise
        except Exception as e:
            logger.error(f"OpenAI API error: {e}")
            raise

    def get_memory(self) -> RollingSummaryMemory:
        """Get the memory manager instance."""
        return self._memory

    async def close(self) -> None:
        """Close the client."""
        await self._client.close()
