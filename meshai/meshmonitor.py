"""MeshMonitor trigger sync via HTTP."""

import json
import logging
import re
import time
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


def _trigger_to_regex(trigger: str) -> re.Pattern:
    """Convert MeshMonitor trigger pattern to compiled regex.

    MeshMonitor patterns:
    - !weather {location:.+}  -> !weather (.+)
    - !ping                   -> !ping
    - !status                 -> !status

    Args:
        trigger: MeshMonitor trigger pattern

    Returns:
        Compiled regex pattern
    """
    # Extract just the command part (before any {param} placeholders)
    # Replace {name:pattern} with just the pattern for matching
    pattern = re.sub(r"\{[^:}]+:([^}]+)\}", r"(\1)", trigger)
    # Replace {name} (no pattern) with (.+)
    pattern = re.sub(r"\{[^}]+\}", r"(.+)", pattern)
    # Escape regex special chars except what we just inserted
    # Actually, we need to be careful - just anchor it
    pattern = "^" + pattern + "$"
    return re.compile(pattern, re.IGNORECASE)


class MeshMonitorSync:
    """Sync auto-responder triggers from MeshMonitor HTTP API."""

    def __init__(self, url: str, refresh_interval: int = 300):
        """Initialize MeshMonitor sync.

        Args:
            url: Base URL of MeshMonitor (e.g., http://100.64.0.11:3333)
            refresh_interval: Seconds between refresh checks (default 5 minutes)
        """
        self._url = url.rstrip("/")
        self._refresh_interval = refresh_interval
        self._patterns: list[re.Pattern] = []
        self._raw_triggers: list[str] = []
        self._last_refresh: float = 0.0
        self._last_error: Optional[str] = None

    @property
    def raw_triggers(self) -> list[str]:
        """Get raw trigger patterns (for display)."""
        return list(self._raw_triggers)

    @property
    def last_error(self) -> Optional[str]:
        """Get last error message if any."""
        return self._last_error

    def load(self) -> int:
        """Fetch triggers from MeshMonitor API.

        Returns:
            Number of triggers loaded
        """
        endpoint = f"{self._url}/api/settings"
        try:
            req = Request(endpoint, headers={"Accept": "application/json"})
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            # autoResponderTriggers is a JSON string inside the response
            triggers_json = data.get("autoResponderTriggers", "[]")
            if isinstance(triggers_json, str):
                triggers = json.loads(triggers_json)
            else:
                triggers = triggers_json

            # Extract trigger patterns
            self._raw_triggers = []
            self._patterns = []
            for item in triggers:
                if isinstance(item, dict):
                    trigger_value = item.get("trigger", "")
                else:
                    trigger_value = item

                # Handle both string and list of strings
                if isinstance(trigger_value, list):
                    trigger_list = trigger_value
                elif isinstance(trigger_value, str) and trigger_value:
                    trigger_list = [trigger_value]
                else:
                    continue

                for trigger in trigger_list:
                    if not trigger:
                        continue
                    self._raw_triggers.append(trigger)
                    try:
                        self._patterns.append(_trigger_to_regex(trigger))
                    except re.error as e:
                        logger.warning(f"Invalid trigger pattern '{trigger}': {e}")

            self._last_refresh = time.time()
            self._last_error = None
            logger.info(f"Loaded {len(self._patterns)} MeshMonitor triggers")
            return len(self._patterns)

        except HTTPError as e:
            self._last_error = f"HTTP {e.code}: {e.reason}"
            logger.error(f"Failed to fetch MeshMonitor triggers: {self._last_error}")
            return 0
        except URLError as e:
            self._last_error = f"Connection error: {e.reason}"
            logger.error(f"Failed to fetch MeshMonitor triggers: {self._last_error}")
            return 0
        except json.JSONDecodeError as e:
            self._last_error = f"Invalid JSON: {e}"
            logger.error(f"Failed to parse MeshMonitor response: {self._last_error}")
            return 0
        except Exception as e:
            self._last_error = str(e)
            logger.error(f"Failed to fetch MeshMonitor triggers: {self._last_error}")
            return 0

    def maybe_refresh(self) -> bool:
        """Refresh triggers if interval has passed.

        Returns:
            True if refresh was performed
        """
        if time.time() - self._last_refresh >= self._refresh_interval:
            self.load()
            return True
        return False

    def matches(self, text: str) -> bool:
        """Check if text matches any MeshMonitor trigger.

        Args:
            text: Message text to check

        Returns:
            True if MeshMonitor will handle this message
        """
        text = text.strip()
        for pattern in self._patterns:
            if pattern.match(text):
                return True
        return False

    def get_commands_summary(self) -> str:
        """Get a summary of MeshMonitor commands for prompt injection.

        Returns:
            Human-readable summary of available commands
        """
        if not self._raw_triggers:
            return ""

        lines = ["MeshMonitor handles these commands (do not respond to them):"]
        for trigger in self._raw_triggers:
            lines.append(f"  - {trigger}")
        return "\n".join(lines)
