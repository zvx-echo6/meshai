"""Rate limiting for MeshAI."""

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from .config import RateLimitsConfig


@dataclass
class UserRateState:
    """Rate limit state for a single user."""

    message_times: list[float] = field(default_factory=list)
    last_response_time: float = 0.0
    burst_count: int = 0


class RateLimiter:
    """Rate limiter for message processing."""

    def __init__(self, config: RateLimitsConfig, vip_nodes: Optional[list[str]] = None):
        self.config = config
        self.vip_nodes = set(vip_nodes or [])
        self._user_states: dict[str, UserRateState] = defaultdict(UserRateState)
        self._global_times: list[float] = []

    def is_allowed(self, user_id: str) -> tuple[bool, Optional[str]]:
        """Check if a message from user is allowed.

        Args:
            user_id: The user's node ID

        Returns:
            Tuple of (allowed, reason). If not allowed, reason explains why.
        """
        # VIP users bypass rate limits
        if user_id in self.vip_nodes:
            return True, None

        now = time.time()
        state = self._user_states[user_id]

        # Clean old timestamps (older than 1 minute)
        cutoff = now - 60.0
        state.message_times = [t for t in state.message_times if t > cutoff]
        self._global_times = [t for t in self._global_times if t > cutoff]

        # Check cooldown (minimum time between responses to same user)
        if state.last_response_time > 0:
            elapsed = now - state.last_response_time
            if elapsed < self.config.cooldown_seconds:
                remaining = self.config.cooldown_seconds - elapsed
                return False, f"Cooldown: wait {remaining:.1f}s"

        # Check per-user rate limit
        if len(state.message_times) >= self.config.messages_per_minute:
            # Check burst allowance
            if state.burst_count >= self.config.burst_allowance:
                return False, "Rate limit exceeded (per-user)"
            state.burst_count += 1
        else:
            state.burst_count = 0

        # Check global rate limit
        if len(self._global_times) >= self.config.global_messages_per_minute:
            return False, "Rate limit exceeded (global)"

        return True, None

    def record_message(self, user_id: str) -> None:
        """Record that a message was processed for a user."""
        now = time.time()
        state = self._user_states[user_id]
        state.message_times.append(now)
        state.last_response_time = now
        self._global_times.append(now)

    def get_user_stats(self, user_id: str) -> dict:
        """Get rate limit stats for a user."""
        now = time.time()
        state = self._user_states[user_id]

        cutoff = now - 60.0
        recent_count = len([t for t in state.message_times if t > cutoff])

        return {
            "messages_last_minute": recent_count,
            "limit": self.config.messages_per_minute,
            "remaining": max(0, self.config.messages_per_minute - recent_count),
            "is_vip": user_id in self.vip_nodes,
        }

    def get_global_stats(self) -> dict:
        """Get global rate limit stats."""
        now = time.time()
        cutoff = now - 60.0
        recent_count = len([t for t in self._global_times if t > cutoff])

        return {
            "messages_last_minute": recent_count,
            "limit": self.config.global_messages_per_minute,
            "remaining": max(0, self.config.global_messages_per_minute - recent_count),
        }

    def reset_user(self, user_id: str) -> None:
        """Reset rate limit state for a user."""
        if user_id in self._user_states:
            del self._user_states[user_id]

    def reset_all(self) -> None:
        """Reset all rate limit state."""
        self._user_states.clear()
        self._global_times.clear()
