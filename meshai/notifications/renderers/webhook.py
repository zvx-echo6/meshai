"""Webhook channel renderer.

Produces a JSON-serializable dict with stable field names. Also
intended for reuse by the future MQTT event publisher (Phase
2.6.5), which wants the same structured shape on the wire.

Field names use snake_case. Optional fields are omitted from the
output when None, NOT set to null — keeps payloads compact and
avoids ambiguity between "field absent" and "field explicitly
null".
"""

import logging
from typing import Any

from meshai.notifications.events import NotificationPayload
from meshai.notifications.renderers.base import Renderer


# Schema version for the wire format. Bump when making
# breaking changes to the field shape. External consumers
# can use this to handle multiple versions.
WEBHOOK_SCHEMA_VERSION = "1.0"


class WebhookRenderer(Renderer):
    """Produce a JSON-serializable dict for a single payload."""

    def __init__(self):
        self._logger = logging.getLogger("meshai.renderers.webhook")

    def render(self, payload: NotificationPayload) -> dict:
        """Render the payload as a structured dict."""
        out: dict[str, Any] = {
            "schema_version": WEBHOOK_SCHEMA_VERSION,
            "message": payload.message,
            "severity": payload.severity or "routine",
            "timestamp": payload.timestamp,
        }

        # Optional fields — omit if None
        for src_attr, dst_key in (
            ("category", "category"),
            ("event_type", "event_type"),
            ("node_id", "node_id"),
            ("node_name", "node_name"),
            ("region", "region"),
            ("chunk_index", "chunk_index"),
            ("chunk_total", "chunk_total"),
        ):
            value = getattr(payload, src_attr, None)
            if value is not None:
                out[dst_key] = value

        # Optional source event detail
        if payload.source_event is not None:
            ev = payload.source_event
            source_event: dict[str, Any] = {}
            for attr in ("id", "source", "title", "expires", "group_key"):
                if hasattr(ev, attr):
                    value = getattr(ev, attr)
                    if value is not None:
                        source_event[attr] = value
            if source_event:
                out["source_event"] = source_event

        return out
