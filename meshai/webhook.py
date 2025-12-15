"""Webhook integration for MeshAI."""

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Optional

import httpx

from .config import WebhookConfig

logger = logging.getLogger(__name__)


class WebhookClient:
    """Client for sending webhook notifications."""

    def __init__(self, config: WebhookConfig):
        self.config = config
        self._client: Optional[httpx.AsyncClient] = None
        self._queue: asyncio.Queue = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None

    async def start(self):
        """Start the webhook client."""
        if not self.config.enabled or not self.config.url:
            logger.debug("Webhooks disabled or no URL configured")
            return

        self._client = httpx.AsyncClient(timeout=10.0)
        self._task = asyncio.create_task(self._process_queue())
        logger.info(f"Webhook client started: {self.config.url}")

    async def stop(self):
        """Stop the webhook client."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        if self._client:
            await self._client.aclose()
            self._client = None

        logger.info("Webhook client stopped")

    def _should_send(self, event_type: str) -> bool:
        """Check if this event type should be sent."""
        if not self.config.enabled:
            return False
        return event_type in self.config.events

    async def send_event(
        self,
        event_type: str,
        data: dict[str, Any],
        immediate: bool = False,
    ):
        """Send a webhook event.

        Args:
            event_type: Type of event (message_received, response_sent, error)
            data: Event data
            immediate: If True, send immediately instead of queuing
        """
        if not self._should_send(event_type):
            return

        payload = {
            "event": event_type,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "data": data,
        }

        if immediate:
            await self._send_payload(payload)
        else:
            await self._queue.put(payload)

    async def _process_queue(self):
        """Process queued webhook payloads."""
        while True:
            try:
                payload = await self._queue.get()
                await self._send_payload(payload)
                self._queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error processing webhook queue: {e}")

    async def _send_payload(self, payload: dict):
        """Send a webhook payload."""
        if not self._client:
            return

        try:
            response = await self._client.post(
                self.config.url,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            if response.status_code >= 400:
                logger.warning(
                    f"Webhook returned {response.status_code}: {response.text[:100]}"
                )
        except Exception as e:
            logger.error(f"Failed to send webhook: {e}")

    # Convenience methods for common events

    async def on_message_received(
        self,
        sender_id: str,
        sender_name: str,
        text: str,
        channel: int,
        is_dm: bool,
    ):
        """Send message_received event."""
        await self.send_event(
            "message_received",
            {
                "sender_id": sender_id,
                "sender_name": sender_name,
                "text": text,
                "channel": channel,
                "is_dm": is_dm,
            },
        )

    async def on_response_sent(
        self,
        recipient_id: Optional[str],
        text: str,
        channel: int,
    ):
        """Send response_sent event."""
        await self.send_event(
            "response_sent",
            {
                "recipient_id": recipient_id,
                "text": text,
                "channel": channel,
            },
        )

    async def on_error(self, error: str, context: Optional[dict] = None):
        """Send error event."""
        await self.send_event(
            "error",
            {
                "error": error,
                "context": context or {},
            },
        )

    async def on_startup(self):
        """Send startup event."""
        await self.send_event(
            "startup",
            {"message": "MeshAI started"},
            immediate=True,
        )

    async def on_shutdown(self):
        """Send shutdown event."""
        await self.send_event(
            "shutdown",
            {"message": "MeshAI stopping"},
            immediate=True,
        )
