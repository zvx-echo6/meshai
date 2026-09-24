"""Base class for LLM backends."""

from abc import ABC, abstractmethod
from typing import Optional


class LLMTruncatedError(Exception):
    """Raised when a backend's generate() call produced an unusable result:
    the model hit the output-token cap mid-answer (finish_reason == "length")
    or returned no real content at all (e.g. every token went to a stripped
    <think> block). Callers must treat this like any other failed
    generation -- never relay partial/cut-off text to the user."""


class LLMBackend(ABC):
    """Abstract base class for LLM backends."""

    @abstractmethod
    async def generate(
        self,
        messages: list[dict],
        system_prompt: str,
        max_tokens: int = 8192,
        user_id: Optional[str] = None,
    ) -> str:
        """Generate a response from the LLM.

        Args:
            messages: Conversation history as list of {"role": str, "content": str}
            system_prompt: System prompt to use
            max_tokens: Maximum tokens in response
            user_id: User identifier for memory optimization (optional)

        Returns:
            Generated response text
        """
        pass

    def get_memory(self):
        """Get the memory manager instance. Override in subclasses."""
        return None

    async def close(self) -> None:
        """Clean up resources. Override if needed."""
        pass
