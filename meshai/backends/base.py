"""Base class for LLM backends."""

from abc import ABC, abstractmethod
from typing import Optional


class LLMBackend(ABC):
    """Abstract base class for LLM backends."""

    @abstractmethod
    async def generate(
        self,
        messages: list[dict],
        system_prompt: str,
        max_tokens: int = 300,
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
