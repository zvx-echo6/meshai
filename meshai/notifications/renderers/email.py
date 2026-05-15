"""Email channel renderer.

Produces a dict with subject and body for SMTP delivery. Plain
text body for now; HTML body is a future polish.

Subject format: "[MeshAI] <Severity> — <Event Type>"
Body format: multi-line, with the message as the lead, followed
by structured context fields (severity, region, node, timestamp,
source category).
"""

import logging
from datetime import datetime
from typing import Optional

from meshai.notifications.events import NotificationPayload
from meshai.notifications.renderers.base import Renderer


class EmailRenderer(Renderer):
    """Produce email subject and body for a single payload."""

    def __init__(self):
        self._logger = logging.getLogger("meshai.renderers.email")

    def render(self, payload: NotificationPayload) -> dict:
        """Render the payload as {subject, body}."""
        return {
            "subject": self._build_subject(payload),
            "body": self._build_body(payload),
        }

    def _build_subject(self, p: NotificationPayload) -> str:
        sev = (p.severity or "routine").upper()
        type_label = self._type_label(p.event_type) or "Alert"
        return f"[MeshAI] {sev} — {type_label}"

    def _build_body(self, p: NotificationPayload) -> str:
        lines: list[str] = []
        # Lead line
        lines.append(p.message or "(no message)")
        lines.append("")  # blank separator

        # Structured context
        lines.append(f"Severity: {p.severity or 'routine'}")
        if p.event_type:
            lines.append(f"Category: {p.event_type}")
        if p.region:
            lines.append(f"Region: {p.region}")
        if p.node_name:
            lines.append(f"Node: {p.node_name}")
        elif p.node_id:
            lines.append(f"Node: {p.node_id}")
        if p.timestamp:
            try:
                ts = datetime.fromtimestamp(p.timestamp)
                lines.append(f"Time: {ts.strftime('%Y-%m-%d %H:%M:%S')}")
            except (ValueError, OverflowError):
                pass

        # Optional source event detail (if present)
        if p.source_event is not None:
            ev = p.source_event
            if hasattr(ev, "source") and ev.source:
                lines.append(f"Source: {ev.source}")
            if hasattr(ev, "title") and ev.title and ev.title != p.message:
                lines.append(f"Title: {ev.title}")

        lines.append("")
        lines.append("--")
        lines.append("MeshAI notification")
        return "\n".join(lines)

    def _type_label(self, event_type: Optional[str]) -> Optional[str]:
        """Title-cased label for the event type (for subject line)."""
        if not event_type:
            return None
        return event_type.replace("_", " ").title()
