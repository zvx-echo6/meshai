"""Dynamic MeshMonitor trigger sync for MeshAI.

Reads MeshMonitor auto-responder triggers from a JSON file and converts
them to compiled regex patterns. The router uses these patterns to ignore
messages that MeshMonitor will handle.

Trigger file format (triggers.json):
    {"triggers": ["weather {location}", "trivia", "ask {question}"]}

Or simple list:
    ["weather {location}", "trivia", "ask {question}"]
"""

import json
import logging
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _trigger_to_regex(trigger: str) -> re.Pattern:
    """Convert a MeshMonitor trigger pattern to a compiled regex.

    MeshMonitor trigger format:
        - "weather {location}" -> matches "weather miami"
        - "w {city},{state}" -> matches "w parkland,fl"
        - "trivia" -> matches "trivia" exactly
        - "ask {question}" -> matches "ask what is mesh"
        - "{name:regex}" -> custom regex per param

    Args:
        trigger: MeshMonitor trigger pattern string

    Returns:
        Compiled regex pattern (case insensitive)
    """
    pattern = trigger.strip()

    # Temporarily replace {param} and {param:regex} blocks
    param_blocks = []
    def _save_param(m):
        param_blocks.append(m.group(0))
        return f"__PARAM_{len(param_blocks) - 1}__"

    pattern = re.sub(r"\{[^}]+\}", _save_param, pattern)

    # Escape remaining special chars
    pattern = re.escape(pattern)

    # Restore param blocks as regex groups
    for i, block in enumerate(param_blocks):
        placeholder = re.escape(f"__PARAM_{i}__")
        # Check for custom regex: {name:regex}
        custom_match = re.match(r"\{(\w+):(.+)\}", block)
        if custom_match:
            _name, custom_regex = custom_match.groups()
            replacement = f"({custom_regex})"
        else:
            # Default: match one or more characters
            replacement = r"(.+)"
        pattern = pattern.replace(placeholder, replacement)

    return re.compile(f"^{pattern}$", re.IGNORECASE)


class MeshMonitorSync:
    """Syncs MeshMonitor auto-responder triggers for MeshAI to ignore.

    Reads trigger patterns from a JSON file and compiles them to regex.
    Watches the file mtime so edits are picked up without restart.
    """

    def __init__(self, triggers_file: str):
        self._triggers_file = Path(triggers_file)
        self._patterns: list[re.Pattern] = []
        self._raw_triggers: list[str] = []
        self._file_mtime: float = 0.0

    @property
    def trigger_list(self) -> list[str]:
        """Get raw trigger strings (for system prompt injection)."""
        return self._raw_triggers

    def load(self) -> int:
        """Load triggers from JSON file.

        Returns:
            Number of trigger patterns loaded
        """
        if not self._triggers_file.exists():
            logger.warning(f"Triggers file not found: {self._triggers_file}")
            return 0

        try:
            self._file_mtime = self._triggers_file.stat().st_mtime

            with open(self._triggers_file) as f:
                data = json.load(f)

            if isinstance(data, list):
                raw_triggers = data
            elif isinstance(data, dict):
                raw_triggers = data.get("triggers", [])
            else:
                logger.warning(f"Unexpected triggers file format: {type(data)}")
                return 0

            self._raw_triggers = raw_triggers
            self._compile_patterns(raw_triggers)

            logger.info(
                f"Loaded {len(self._patterns)} MeshMonitor trigger patterns "
                f"from {self._triggers_file}"
            )
            return len(self._patterns)

        except Exception as e:
            logger.error(f"Failed to load triggers file: {e}")
            return 0

    def maybe_refresh(self) -> bool:
        """Reload triggers if the file has changed on disk.

        Returns:
            True if triggers were refreshed
        """
        if not self._triggers_file.exists():
            return False

        mtime = self._triggers_file.stat().st_mtime
        if mtime > self._file_mtime:
            self.load()
            return True

        return False

    def matches(self, text: str) -> bool:
        """Check if text matches any MeshMonitor trigger pattern.

        Args:
            text: Incoming message text (stripped)

        Returns:
            True if the message matches a MeshMonitor trigger
        """
        for pattern in self._patterns:
            if pattern.match(text):
                logger.debug(
                    f"Message matches MeshMonitor trigger: "
                    f"{text[:40]}... -> {pattern.pattern}"
                )
                return True
        return False

    def _compile_patterns(self, raw_triggers: list[str]) -> None:
        """Compile trigger strings into regex patterns.

        Handles comma-separated multi-patterns per trigger.
        """
        patterns = []

        for trigger in raw_triggers:
            # Split multi-pattern triggers on comma
            sub_patterns = [t.strip() for t in trigger.split(",")]

            for sub in sub_patterns:
                if not sub:
                    continue
                try:
                    compiled = _trigger_to_regex(sub)
                    patterns.append(compiled)
                except Exception as e:
                    logger.warning(
                        f"Failed to compile trigger pattern: {e}"
                    )

        self._patterns = patterns
