"""Fallback-aware LLM backend wrapper."""

import asyncio
import logging
from typing import Optional

from ..config import LLMConfig, LLMBackendConfig
from .base import LLMBackend
from .openai_backend import OpenAIBackend
from .anthropic_backend import AnthropicBackend
from .google_backend import GoogleBackend

logger = logging.getLogger(__name__)


def create_backend(
    backend_type: str,
    api_key: str,
    base_url: str,
    model: str,
    timeout: int,
    window_size: int = 0,
    summarize_threshold: int = 8,
) -> LLMBackend:
    """Create an LLM backend instance.

    Args:
        backend_type: Type of backend (openai, anthropic, google)
        api_key: API key for the backend
        base_url: Base URL for the API
        model: Model name to use
        timeout: Request timeout in seconds
        window_size: Memory window size
        summarize_threshold: When to summarize older messages

    Returns:
        Configured LLM backend instance
    """
    # Create a minimal config object for the backend
    from dataclasses import dataclass

    @dataclass
    class MinimalLLMConfig:
        backend: str
        api_key: str
        base_url: str
        model: str
        system_prompt: str = ""

    config = MinimalLLMConfig(
        backend=backend_type,
        api_key=api_key,
        base_url=base_url,
        model=model,
    )

    backend_type = backend_type.lower()
    if backend_type == "openai":
        return OpenAIBackend(config, api_key, window_size, summarize_threshold)
    elif backend_type == "anthropic":
        return AnthropicBackend(config, api_key, window_size, summarize_threshold)
    elif backend_type == "google":
        return GoogleBackend(config, api_key, window_size, summarize_threshold)
    else:
        logger.warning(f"Unknown backend '{backend_type}', defaulting to OpenAI")
        return OpenAIBackend(config, api_key, window_size, summarize_threshold)


class FallbackBackend(LLMBackend):
    """LLM backend with automatic fallback support."""

    def __init__(
        self,
        config: LLMConfig,
        api_key: str,
        window_size: int = 0,
        summarize_threshold: int = 8,
    ):
        self.config = config
        self.api_key = api_key
        self.window_size = window_size
        self.summarize_threshold = summarize_threshold

        # Create primary backend
        self.primary = create_backend(
            backend_type=config.backend,
            api_key=api_key,
            base_url=config.base_url,
            model=config.model,
            timeout=config.timeout,
            window_size=window_size,
            summarize_threshold=summarize_threshold,
        )

        # Create fallback backend if configured
        self.fallback: Optional[LLMBackend] = None
        if config.fallback:
            fb = config.fallback
            fb_api_key = fb.api_key or api_key  # Use primary key if not specified
            self.fallback = create_backend(
                backend_type=fb.backend,
                api_key=fb_api_key,
                base_url=fb.base_url,
                model=fb.model,
                timeout=fb.timeout,
                window_size=window_size,
                summarize_threshold=summarize_threshold,
            )

        self._using_fallback = False

    @property
    def using_fallback(self) -> bool:
        """Whether we're currently using the fallback backend."""
        return self._using_fallback

    def get_memory(self):
        """Get memory from the active backend."""
        if self._using_fallback and self.fallback:
            return self.fallback.get_memory()
        return self.primary.get_memory()

    async def generate(
        self,
        messages: list[dict],
        system_prompt: str,
        max_tokens: int = 300,
        user_id: Optional[str] = None,
    ) -> str:
        """Generate with automatic fallback."""
        last_error = None

        # Try primary
        for attempt in range(self.config.retry_attempts):
            try:
                result = await asyncio.wait_for(
                    self.primary.generate(messages, system_prompt, max_tokens, user_id),
                    timeout=self.config.timeout,
                )
                self._using_fallback = False
                return result
            except asyncio.TimeoutError as e:
                logger.warning(f"Primary backend timeout (attempt {attempt + 1})")
                last_error = e
                if not self.config.fallback_on_timeout:
                    raise
            except Exception as e:
                logger.warning(f"Primary backend error (attempt {attempt + 1}): {e}")
                last_error = e
                if not self.config.fallback_on_error:
                    raise

        # Try fallback if available
        if self.fallback:
            logger.info("Switching to fallback backend")
            try:
                result = await asyncio.wait_for(
                    self.fallback.generate(messages, system_prompt, max_tokens, user_id),
                    timeout=self.config.fallback.timeout if self.config.fallback else 30,
                )
                self._using_fallback = True
                return result
            except Exception as e:
                logger.error(f"Fallback backend also failed: {e}")
                raise

        # No fallback, raise the last error
        if last_error:
            raise last_error
        raise RuntimeError("All LLM backends failed")

    async def generate_with_search(
        self,
        query: str,
        system_prompt: Optional[str] = None,
    ) -> str:
        """Generate with search using automatic fallback."""
        last_error = None

        # Try primary
        try:
            result = await asyncio.wait_for(
                self.primary.generate_with_search(query, system_prompt),
                timeout=self.config.timeout,
            )
            self._using_fallback = False
            return result
        except asyncio.TimeoutError as e:
            logger.warning("Primary backend timeout for search")
            last_error = e
            if not self.config.fallback_on_timeout:
                raise
        except Exception as e:
            logger.warning(f"Primary backend search error: {e}")
            last_error = e
            if not self.config.fallback_on_error:
                raise

        # Try fallback
        if self.fallback:
            logger.info("Switching to fallback backend for search")
            try:
                result = await self.fallback.generate_with_search(query, system_prompt)
                self._using_fallback = True
                return result
            except Exception as e:
                logger.error(f"Fallback search also failed: {e}")
                raise

        if last_error:
            raise last_error
        raise RuntimeError("All LLM backends failed")

    async def close(self) -> None:
        """Close both backends."""
        await self.primary.close()
        if self.fallback:
            await self.fallback.close()
