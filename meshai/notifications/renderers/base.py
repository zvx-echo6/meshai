"""Abstract base for channel-type-aware renderers.

Each renderer takes a NotificationPayload and produces a
channel-type-appropriate output (string, list, dict, etc.).
Renderers are pure functions of their input — no network, no
state, no side effects beyond logging. The channel that owns a
renderer calls render() and then handles delivery.
"""

from abc import ABC, abstractmethod
from typing import Any

from meshai.notifications.events import NotificationPayload


class Renderer(ABC):
    """Base class for all channel-type renderers."""

    @abstractmethod
    def render(self, payload: NotificationPayload) -> Any:
        """Produce the channel-type-appropriate output.

        Subclasses define the concrete return type:
          - MeshRenderer returns list[str]
          - EmailRenderer returns dict (subject, body)
          - WebhookRenderer returns dict (JSON-serializable)
        """
        raise NotImplementedError
