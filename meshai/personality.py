"""Personality and prompt template handling for MeshAI."""

import re
from datetime import datetime
from typing import Optional

from .config import PersonalityConfig


class PersonalityManager:
    """Manages personality switching and prompt templating."""

    def __init__(self, config: PersonalityConfig):
        self.config = config
        self._current_persona: Optional[str] = None
        self._persona_prompts: dict[str, str] = {}

        # Parse personas from config
        for name, persona_data in config.personas.items():
            if isinstance(persona_data, dict):
                self._persona_prompts[name] = persona_data.get("prompt", "")
            else:
                self._persona_prompts[name] = str(persona_data)

    def get_system_prompt(
        self,
        sender_name: str = "",
        channel: int = 0,
        extra_context: Optional[dict] = None,
    ) -> str:
        """Get the current system prompt with context injection.

        Args:
            sender_name: Name of the message sender
            channel: Channel number
            extra_context: Additional context variables

        Returns:
            Formatted system prompt
        """
        # Start with base prompt or persona prompt
        if self._current_persona and self._current_persona in self._persona_prompts:
            base_prompt = self._persona_prompts[self._current_persona]
        else:
            base_prompt = self.config.system_prompt

        # Apply context injection if configured
        if self.config.context_injection:
            context_vars = {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "sender_name": sender_name,
                "channel": str(channel),
                "persona": self._current_persona or "default",
            }
            if extra_context:
                context_vars.update(extra_context)

            try:
                context = self.config.context_injection.format(**context_vars)
                base_prompt = f"{base_prompt}\n\n{context}"
            except KeyError as e:
                # Ignore missing context variables
                pass

        return base_prompt

    def check_persona_trigger(self, text: str) -> Optional[str]:
        """Check if text contains a persona switch trigger.

        Args:
            text: Message text to check

        Returns:
            Persona name if triggered, None otherwise
        """
        text_lower = text.lower().strip()

        for name, persona_data in self.config.personas.items():
            trigger = None
            if isinstance(persona_data, dict):
                trigger = persona_data.get("trigger", f"!{name}")
            else:
                trigger = f"!{name}"

            if trigger and text_lower.startswith(trigger.lower()):
                return name

        return None

    def switch_persona(self, persona_name: Optional[str]) -> bool:
        """Switch to a different persona.

        Args:
            persona_name: Name of persona to switch to, or None for default

        Returns:
            True if switch was successful
        """
        if persona_name is None:
            self._current_persona = None
            return True

        if persona_name in self._persona_prompts:
            self._current_persona = persona_name
            return True

        return False

    def get_current_persona(self) -> Optional[str]:
        """Get the name of the current persona."""
        return self._current_persona

    def list_personas(self) -> list[str]:
        """List available persona names."""
        return list(self._persona_prompts.keys())

    def reset(self) -> None:
        """Reset to default persona."""
        self._current_persona = None
