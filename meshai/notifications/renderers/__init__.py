"""Channel-type-aware renderers.

Each renderer takes a NotificationPayload and produces a
channel-type-appropriate output: mesh chunks, email subject/body,
or webhook JSON dict.

Renderers are reusable beyond channels.py — the future MQTT event
publisher (Phase 2.6.5) uses WebhookRenderer for its on-the-wire
format.
"""

from meshai.notifications.renderers.base import Renderer
from meshai.notifications.renderers.mesh import MeshRenderer
from meshai.notifications.renderers.email import EmailRenderer
from meshai.notifications.renderers.webhook import WebhookRenderer

__all__ = [
    "Renderer",
    "MeshRenderer",
    "EmailRenderer",
    "WebhookRenderer",
]
