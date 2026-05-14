"""Message summarizer for mesh delivery."""

import logging
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..backends import LLMBackend

logger = logging.getLogger(__name__)


class MessageSummarizer:
    """Summarizes long messages for mesh delivery.

    Only used when:
    - Delivering to mesh channels (broadcast or DM)
    - Message exceeds max_chars (default 200)
    - LLM backend is available

    Email and webhook channels receive full messages.
    """

    def __init__(self, llm_backend: Optional["LLMBackend"] = None):
        self._llm = llm_backend

    async def summarize(self, message: str, max_chars: int = 195) -> str:
        """Summarize a message to fit within max_chars.

        Args:
            message: Original message text
            max_chars: Maximum characters for summary

        Returns:
            Summarized message, or truncated original if LLM unavailable
        """
        if len(message) <= max_chars:
            return message

        if not self._llm:
            return message[:max_chars - 3] + "..."

        prompt = (
            "Summarize this alert in under %d characters. "
            "Keep severity, location, and key facts. No preamble, just the summary:\n\n%s"
            % (max_chars, message)
        )

        try:
            # Use the LLM to generate a summary
            response = await self._llm.generate(
                prompt,
                system_prompt="You are a concise alert summarizer. Output only the summary, no explanation.",
                
            )
            summary = response.strip()

            # Ensure it fits
            if len(summary) <= max_chars:
                return summary
            return summary[:max_chars - 3] + "..."

        except Exception as e:
            logger.debug("LLM summarization failed: %s", e)
            return message[:max_chars - 3] + "..."
