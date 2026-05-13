"""Notification channel implementations."""

import asyncio
import logging
import smtplib
import ssl
import time
from abc import ABC, abstractmethod
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional, TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from ..connector import MeshConnector

logger = logging.getLogger(__name__)


class NotificationChannel(ABC):
    """Base class for notification delivery channels."""

    channel_type: str = "base"

    @abstractmethod
    async def deliver(self, alert: dict, rule: dict) -> bool:
        """Send alert. Returns True on success."""
        raise NotImplementedError

    @abstractmethod
    async def test(self) -> tuple[bool, str]:
        """Send test message. Returns (success, message)."""
        raise NotImplementedError


class MeshBroadcastChannel(NotificationChannel):
    """Post alert to mesh channel."""

    channel_type = "mesh_broadcast"

    def __init__(self, connector: "MeshConnector", channel_index: int = 0):
        self._connector = connector
        self._channel = channel_index

    async def deliver(self, alert: dict, rule: dict) -> bool:
        """Send alert to mesh channel."""
        if not self._connector:
            logger.warning("No mesh connector available")
            return False

        try:
            message = alert.get("message", "")
            self._connector.send_message(
                text=message,
                destination=None,
                channel=self._channel,
            )
            logger.info("Broadcast alert to channel %d", self._channel)
            return True
        except Exception as e:
            logger.error("Failed to broadcast alert: %s", e)
            return False

    async def test(self) -> tuple[bool, str]:
        """Send test broadcast."""
        try:
            self._connector.send_message(
                text="[TEST] MeshAI notification system test",
                destination=None,
                channel=self._channel,
            )
            return True, "Test message sent to channel %d" % self._channel
        except Exception as e:
            return False, "Failed to send test: %s" % e


class MeshDMChannel(NotificationChannel):
    """DM alert to specific node IDs."""

    channel_type = "mesh_dm"

    def __init__(self, connector: "MeshConnector", node_ids: list[str]):
        self._connector = connector
        self._node_ids = node_ids

    async def deliver(self, alert: dict, rule: dict) -> bool:
        """Send alert via DM to configured nodes."""
        if not self._connector:
            return False

        message = alert.get("message", "")
        success = True

        for node_id in self._node_ids:
            try:
                dest = int(node_id) if node_id.isdigit() else node_id
                self._connector.send_message(text=message, destination=dest, channel=0)
            except Exception as e:
                logger.error("Failed to DM %s: %s", node_id, e)
                success = False

        return success

    async def test(self) -> tuple[bool, str]:
        """Send test DM to all configured nodes."""
        if not self._node_ids:
            return False, "No node IDs configured"
        try:
            for node_id in self._node_ids:
                dest = int(node_id) if node_id.isdigit() else node_id
                self._connector.send_message(
                    text="[TEST] MeshAI notification test",
                    destination=dest,
                    channel=0,
                )
            return True, "Test DMs sent to %d nodes" % len(self._node_ids)
        except Exception as e:
            return False, "Failed to send test DMs: %s" % e


class EmailChannel(NotificationChannel):
    """Send alert via SMTP email."""

    channel_type = "email"

    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        smtp_user: str,
        smtp_password: str,
        smtp_tls: bool,
        from_address: str,
        recipients: list[str],
    ):
        self._host = smtp_host
        self._port = smtp_port
        self._user = smtp_user
        self._password = smtp_password
        self._tls = smtp_tls
        self._from = from_address
        self._recipients = recipients

    async def deliver(self, alert: dict, rule: dict) -> bool:
        """Send alert via email."""
        if not self._recipients:
            return False

        alert_type = alert.get("type", "alert")
        severity = alert.get("severity", "info").upper()
        message = alert.get("message", "")
        subject = "[MeshAI %s] %s" % (severity, alert_type.replace("_", " ").title())
        body = "MeshAI Alert\n\nType: %s\nSeverity: %s\nTime: %s\n\n%s\n\n---\nAutomated message from MeshAI." % (
            alert_type, severity, time.strftime("%Y-%m-%d %H:%M:%S"), message
        )

        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._send_email, subject, body)
            return True
        except Exception as e:
            logger.error("Failed to send email: %s", e)
            return False

    def _send_email(self, subject: str, body: str):
        msg = MIMEMultipart()
        msg["From"] = self._from
        msg["To"] = ", ".join(self._recipients)
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        if self._tls:
            context = ssl.create_default_context()
            with smtplib.SMTP(self._host, self._port) as server:
                server.starttls(context=context)
                if self._user and self._password:
                    server.login(self._user, self._password)
                server.sendmail(self._from, self._recipients, msg.as_string())
        else:
            with smtplib.SMTP(self._host, self._port) as server:
                if self._user and self._password:
                    server.login(self._user, self._password)
                server.sendmail(self._from, self._recipients, msg.as_string())

    async def test(self) -> tuple[bool, str]:
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                self._send_email,
                "[MeshAI TEST] Notification Test",
                "Test message from MeshAI.",
            )
            return True, "Test email sent to %d recipients" % len(self._recipients)
        except Exception as e:
            return False, "Failed to send test email: %s" % e


class WebhookChannel(NotificationChannel):
    """POST alert JSON to a URL."""

    channel_type = "webhook"

    def __init__(self, url: str, headers: Optional[dict] = None):
        self._url = url
        self._headers = headers or {}

    async def deliver(self, alert: dict, rule: dict) -> bool:
        """POST alert to webhook URL."""
        payload = {
            "type": alert.get("type"),
            "severity": alert.get("severity", "info"),
            "message": alert.get("message", ""),
            "timestamp": time.time(),
            "node_name": alert.get("node_name"),
            "region": alert.get("region"),
        }

        # Discord/Slack format
        if "discord.com" in self._url or "slack.com" in self._url:
            severity = alert.get("severity", "info")
            color = {
                "emergency": 0xFF0000,
                "critical": 0xFF4444,
                "warning": 0xFFAA00,
                "info": 0x0099FF,
            }.get(severity, 0x888888)
            payload = {
                "embeds": [{
                    "title": "MeshAI: %s" % alert.get("type", "unknown"),
                    "description": alert.get("message", ""),
                    "color": color,
                }]
            }

        # ntfy format
        elif "ntfy" in self._url:
            headers = {
                **self._headers,
                "Title": "MeshAI: %s" % alert.get("type", "alert"),
                "Priority": "3",
            }
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.post(
                        self._url,
                        content=alert.get("message", ""),
                        headers=headers,
                        timeout=10,
                    )
                    return resp.status_code < 400
            except Exception as e:
                logger.error("Webhook failed: %s", e)
                return False

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    self._url,
                    json=payload,
                    headers={"Content-Type": "application/json", **self._headers},
                    timeout=10,
                )
                return resp.status_code < 400
        except Exception as e:
            logger.error("Webhook failed: %s", e)
            return False

    async def test(self) -> tuple[bool, str]:
        test_alert = {"type": "test", "severity": "info", "message": "MeshAI test message"}
        success = await self.deliver(test_alert, {})
        if success:
            return True, "Test sent to %s" % self._url

    async def deliver_test(self, message: str) -> bool:
        """Deliver a specific test message via webhook."""
        try:
            test_alert = {"type": "test", "severity": "info", "message": message}
            return await self.deliver(test_alert, {})
        except Exception as e:
            logger.warning("Webhook test failed: %s", e)
            return False
        return False, "Webhook failed"


def create_channel(config: dict, connector=None) -> NotificationChannel:
    """Create a channel instance from config."""
    channel_type = config.get("type", "")

    if channel_type == "mesh_broadcast":
        return MeshBroadcastChannel(
            connector=connector,
            channel_index=config.get("channel_index", 0),
        )
    elif channel_type == "mesh_dm":
        return MeshDMChannel(
            connector=connector,
            node_ids=config.get("node_ids", []),
        )
    elif channel_type == "email":
        return EmailChannel(
            smtp_host=config.get("smtp_host", ""),
            smtp_port=config.get("smtp_port", 587),
            smtp_user=config.get("smtp_user", ""),
            smtp_password=config.get("smtp_password", ""),
            smtp_tls=config.get("smtp_tls", True),
            from_address=config.get("from_address", ""),
            recipients=config.get("recipients", []),
        )
    elif channel_type == "webhook":
        return WebhookChannel(
            url=config.get("url", ""),
            headers=config.get("headers", {}),
        )
    else:
        raise ValueError("Unknown channel type: %s" % channel_type)
