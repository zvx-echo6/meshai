"""Response filtering and safety for MeshAI."""

import re
from typing import Optional

from .config import SafetyConfig

# Basic profanity list (expand as needed)
PROFANITY_PATTERNS = [
    r"\bf+u+c+k+\w*\b",
    r"\bs+h+i+t+\w*\b",
    r"\ba+s+s+h+o+l+e+\w*\b",
    r"\bb+i+t+c+h+\w*\b",
    r"\bc+u+n+t+\w*\b",
    r"\bd+a+m+n+\w*\b",
]


class SafetyFilter:
    """Filter for response safety and content moderation."""

    def __init__(self, config: SafetyConfig):
        self.config = config
        self._profanity_regex = None
        if config.filter_profanity:
            self._profanity_regex = re.compile(
                "|".join(PROFANITY_PATTERNS), re.IGNORECASE
            )

    def filter_response(self, text: str) -> str:
        """Filter a response for safety.

        Args:
            text: The response text to filter

        Returns:
            Filtered text
        """
        # Truncate to max length
        if len(text) > self.config.max_response_length:
            text = text[: self.config.max_response_length - 3] + "..."

        # Filter profanity
        if self._profanity_regex:
            text = self._profanity_regex.sub("***", text)

        # Filter blocked phrases
        for phrase in self.config.blocked_phrases:
            text = text.replace(phrase, "[filtered]")
            text = text.replace(phrase.lower(), "[filtered]")
            text = text.replace(phrase.upper(), "[filtered]")

        return text

    def should_respond(
        self,
        text: str,
        sender_id: str,
        own_id: str,
        is_mentioned: bool,
        is_dm: bool,
    ) -> tuple[bool, Optional[str]]:
        """Check if we should respond to this message.

        Args:
            text: Message text
            sender_id: Sender's node ID
            own_id: Our own node ID
            is_mentioned: Whether our name is mentioned
            is_dm: Whether this is a direct message

        Returns:
            Tuple of (should_respond, reason). Reason is None if we should respond.
        """
        # Never respond to self
        if self.config.ignore_self and sender_id == own_id:
            return False, "Self message"

        # Check for emergency keywords (always respond)
        text_lower = text.lower()
        for keyword in self.config.emergency_keywords:
            if keyword.lower() in text_lower:
                return True, None

        # Check mention requirement
        if self.config.require_mention and not is_mentioned and not is_dm:
            return False, "Not mentioned"

        return True, None

    def contains_emergency(self, text: str) -> bool:
        """Check if text contains emergency keywords."""
        text_lower = text.lower()
        return any(kw.lower() in text_lower for kw in self.config.emergency_keywords)


class UserFilter:
    """Filter for user access control."""

    def __init__(
        self,
        blocklist: list[str],
        allowlist: list[str],
        allowlist_only: bool,
        admin_nodes: list[str],
    ):
        self.blocklist = set(blocklist)
        self.allowlist = set(allowlist)
        self.allowlist_only = allowlist_only
        self.admin_nodes = set(admin_nodes)

    def is_allowed(self, user_id: str) -> tuple[bool, Optional[str]]:
        """Check if user is allowed to interact.

        Args:
            user_id: The user's node ID

        Returns:
            Tuple of (allowed, reason)
        """
        # Check blocklist first
        if user_id in self.blocklist:
            return False, "User is blocked"

        # If allowlist_only mode, check allowlist
        if self.allowlist_only:
            if user_id not in self.allowlist:
                return False, "User not in allowlist"

        return True, None

    def is_admin(self, user_id: str) -> bool:
        """Check if user is an admin."""
        return user_id in self.admin_nodes

    def add_to_blocklist(self, user_id: str) -> None:
        """Add a user to the blocklist."""
        self.blocklist.add(user_id)

    def remove_from_blocklist(self, user_id: str) -> None:
        """Remove a user from the blocklist."""
        self.blocklist.discard(user_id)
