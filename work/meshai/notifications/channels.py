"""Notification channel implementations with connectivity testing."""

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
    from ..config import NotificationRuleConfig
    from .events import NotificationPayload

from meshai.notifications.renderers import MeshRenderer, EmailRenderer, WebhookRenderer

logger = logging.getLogger(__name__)


class NotificationChannel(ABC):
    """Base class for notification delivery channels."""

    channel_type: str = "base"

    @abstractmethod
    async def deliver(self, alert: "NotificationPayload", rule: "NotificationRuleConfig") -> bool:
        """Send alert. Returns True on success."""
        raise NotImplementedError

    @abstractmethod
    async def test_connection(self) -> dict:
        """Test channel connectivity without sending actual content.

        Returns:
            {
                "success": bool,
                "message": str,  # Human-readable result
                "error": str,    # Detailed error if failed
                "details": {}    # Channel-specific details
            }
        """
        raise NotImplementedError

    async def deliver_test(self, message: str) -> tuple[bool, str]:
        """Deliver a specific test message through the channel.

        Returns:
            (success, result_message)
        """
        raise NotImplementedError


class MeshBroadcastChannel(NotificationChannel):
    """Post alert to Meshtastic channel (explicit Meshtastic-only delivery)."""

    channel_type = "mesh_broadcast"

    def __init__(self, connector: "MeshConnector", channel_index: int = 0,
                 transport: Optional[str] = "meshtastic"):
        self._connector = connector
        self._channel = channel_index
        # Transport hint: "meshtastic" for mesh_broadcast; passed to CompositeTransport
        # so it routes only to the Meshtastic child. Single-transport implementations
        # accept and ignore this kwarg, so behavior is unchanged there.
        self._transport = transport
        _mc = getattr(connector, "max_chars", 200)
        self._renderer = MeshRenderer(char_limit=_mc if isinstance(_mc, int) else 200)

    async def deliver(self, alert: "NotificationPayload", rule: "NotificationRuleConfig") -> bool:
        """Send alert to Meshtastic channel."""
        if not self._connector:
            logger.warning("No mesh connector available")
            return False

        try:
            # If payload already has chunk metadata (from digest), use message directly
            if alert.chunk_index is not None:
                self._connector.send_message(
                    text=alert.message or "",
                    destination=None,
                    channel=self._channel,
                    transport=self._transport,
                )
                logger.info("Broadcast pre-chunked alert to channel %d", self._channel)
                return True

            # Render to chunks for single-event delivery
            chunks = self._renderer.render(alert)
            for chunk in chunks:
                self._connector.send_message(
                    text=chunk,
                    destination=None,
                    channel=self._channel,
                    transport=self._transport,
                )
            logger.info("Broadcast %d chunk(s) to channel %d", len(chunks), self._channel)
            return True
        except Exception as e:
            logger.error("Failed to broadcast alert: %s", e)
            return False

    async def test_connection(self) -> dict:
        """Test mesh radio connectivity."""
        if not self._connector:
            return {
                "success": False,
                "message": "Not connected to radio",
                "error": "MeshConnector not initialized. Check that meshtastic is connected.",
                "details": {"channel": self._channel}
            }

        try:
            # Check if interface is connected
            interface = getattr(self._connector, '_interface', None)
            if not interface:
                return {
                    "success": False,
                    "message": "Radio interface not available",
                    "error": "Meshtastic interface not initialized",
                    "details": {"channel": self._channel}
                }

            # Get channel info
            channels = getattr(interface, 'channels', [])
            channel_name = "Unknown"
            if self._channel < len(channels):
                ch = channels[self._channel]
                channel_name = getattr(ch, 'settings', {}).get('name', f'Channel {self._channel}')
                if hasattr(ch, 'settings') and hasattr(ch.settings, 'name'):
                    channel_name = ch.settings.name or f'Channel {self._channel}'

            # Send actual test message
            self._connector.send_message(
                text="MeshAI channel test - if you see this, delivery works",
                destination=None,
                channel=self._channel,
            )

            return {
                "success": True,
                "message": f"Sent to channel {self._channel}: {channel_name}",
                "error": "",
                "details": {
                    "channel": self._channel,
                    "channel_name": channel_name,
                }
            }
        except Exception as e:
            return {
                "success": False,
                "message": f"Failed to send to channel {self._channel}",
                "error": str(e),
                "details": {"channel": self._channel}
            }

    async def deliver_test(self, message: str) -> tuple[bool, str]:
        """Deliver a specific test message."""
        if not self._connector:
            return False, "Not connected to radio"

        try:
            self._connector.send_message(
                text=message,
                destination=None,
                channel=self._channel,
            )
            return True, f"Sent to mesh channel {self._channel}"
        except Exception as e:
            return False, f"Mesh broadcast failed: {e}"


class MeshCoreBroadcastChannel(NotificationChannel):
    """Post alert to a MeshCore channel (explicit MeshCore-only delivery)."""

    channel_type = "meshcore_broadcast"

    def __init__(self, connector: "MeshConnector", meshcore_channel: Optional[str] = None):
        self._connector = connector
        # Channel NAME on the MeshCore companion (resolved to a slot at send time).
        self._meshcore_channel = meshcore_channel
        _mc = getattr(connector, "max_chars", 200)
        self._renderer = MeshRenderer(char_limit=_mc if isinstance(_mc, int) else 200)

    def _has_meshcore_capability(self) -> bool:
        """Return True if the connector can reach a MeshCore transport."""
        # CompositeTransport: check for a child named "meshcore".
        by_name = getattr(self._connector, "_by_name", None)
        if by_name is not None:
            return "meshcore" in by_name
        # Single-transport: check for an explicit transport_name tag.
        return getattr(self._connector, "transport_name", None) == "meshcore"

    async def deliver(self, alert: "NotificationPayload", rule: "NotificationRuleConfig") -> bool:
        """Send alert to MeshCore channel."""
        if not self._connector:
            logger.warning("No mesh connector available for meshcore_broadcast")
            return False

        if not self._meshcore_channel:
            logger.debug("meshcore_broadcast: meshcore_channel not set; skipping")
            return False

        if not self._has_meshcore_capability():
            logger.debug(
                "meshcore_broadcast: connector has no MeshCore transport; skipping"
            )
            return False

        try:
            # If payload already has chunk metadata (from digest), use message directly
            if alert.chunk_index is not None:
                self._connector.send_message(
                    text=alert.message or "",
                    destination=None,
                    meshcore_channel=self._meshcore_channel,
                    transport="meshcore",
                )
                logger.info(
                    "MeshCore broadcast pre-chunked alert to channel %r",
                    self._meshcore_channel,
                )
                return True

            # Render to chunks for single-event delivery
            chunks = self._renderer.render(alert)
            for chunk in chunks:
                self._connector.send_message(
                    text=chunk,
                    destination=None,
                    meshcore_channel=self._meshcore_channel,
                    transport="meshcore",
                )
            logger.info(
                "MeshCore broadcast %d chunk(s) to channel %r",
                len(chunks), self._meshcore_channel,
            )
            return True
        except Exception as e:
            logger.error("Failed to MeshCore broadcast alert: %s", e)
            return False

    async def test_connection(self) -> dict:
        """Test MeshCore channel connectivity."""
        if not self._has_meshcore_capability():
            return {
                "success": False,
                "message": "No MeshCore transport available",
                "error": "Set connection.meshcore_host to enable MeshCore",
                "details": {"meshcore_channel": self._meshcore_channel},
            }
        return {
            "success": True,
            "message": f"MeshCore channel: {self._meshcore_channel}",
            "error": "",
            "details": {"meshcore_channel": self._meshcore_channel},
        }

    async def deliver_test(self, message: str) -> tuple[bool, str]:
        """Deliver a specific test message to the MeshCore channel."""
        if not self._connector:
            return False, "Not connected"
        if not self._meshcore_channel:
            return False, "No MeshCore channel configured"
        try:
            self._connector.send_message(
                text=message,
                destination=None,
                meshcore_channel=self._meshcore_channel,
                transport="meshcore",
            )
            return True, f"Sent to MeshCore channel {self._meshcore_channel!r}"
        except Exception as e:
            return False, f"MeshCore broadcast failed: {e}"


class MeshDMChannel(NotificationChannel):
    """DM alert to specific Meshtastic node IDs."""

    channel_type = "mesh_dm"

    def __init__(self, connector: "MeshConnector", node_ids: list[str],
                 transport_hint: Optional[str] = "meshtastic"):
        self._connector = connector
        self._node_ids = node_ids
        # Explicit transport hint so CompositeTransport routes only to the
        # Meshtastic child. Single-transport impls ignore this kwarg.
        self._transport_hint = transport_hint
        _mc = getattr(connector, "max_chars", 200)
        self._renderer = MeshRenderer(char_limit=_mc if isinstance(_mc, int) else 200)

    async def deliver(self, alert: "NotificationPayload", rule: "NotificationRuleConfig") -> bool:
        """Send alert via DM to configured Meshtastic nodes."""
        if not self._connector:
            return False

        # If payload already has chunk metadata (from digest), use message directly
        if alert.chunk_index is not None:
            messages = [alert.message or ""]
        else:
            # Render to chunks for single-event delivery
            messages = self._renderer.render(alert)

        success = True
        for node_id in self._node_ids:
            for message in messages:
                try:
                    node_id = str(node_id)
                    self._connector.send_message(
                        text=message,
                        destination=node_id,
                        channel=0,
                        transport=self._transport_hint,
                    )
                except Exception as e:
                    logger.error("Failed to DM %s: %s", node_id, e)
                    success = False

        return success

    async def test_connection(self) -> dict:
        """Test DM delivery to configured nodes."""
        if not self._connector:
            return {
                "success": False,
                "message": "Not connected to radio",
                "error": "MeshConnector not initialized",
                "details": {"node_ids": self._node_ids}
            }

        if not self._node_ids:
            return {
                "success": False,
                "message": "No recipient nodes configured",
                "error": "Add at least one node ID to receive DMs",
                "details": {"node_ids": []}
            }

        results = []
        all_success = True

        for node_id in self._node_ids:
            try:
                node_id = str(node_id)
                self._connector.send_message(
                    text="MeshAI DM test",
                    destination=node_id,
                    channel=0,
                )
                results.append({"node": node_id, "success": True})
            except Exception as e:
                results.append({"node": node_id, "success": False, "error": str(e)})
                all_success = False

        success_nodes = [r["node"] for r in results if r["success"]]
        failed_nodes = [r for r in results if not r["success"]]

        if all_success:
            node_names = ", ".join(self._node_ids)
            return {
                "success": True,
                "message": f"Sent DM to {node_names}",
                "error": "",
                "details": {"results": results}
            }
        elif success_nodes:
            return {
                "success": False,
                "message": f"Partial: sent to {len(success_nodes)}, failed {len(failed_nodes)}",
                "error": "; ".join([f"{r['node']}: {r.get('error', 'unknown')}" for r in failed_nodes]),
                "details": {"results": results}
            }
        else:
            return {
                "success": False,
                "message": "All DMs failed",
                "error": "; ".join([f"{r['node']}: {r.get('error', 'unknown')}" for r in failed_nodes]),
                "details": {"results": results}
            }

    async def deliver_test(self, message: str) -> tuple[bool, str]:
        """Deliver a specific test message via DM."""
        if not self._connector:
            return False, "Not connected to radio"

        if not self._node_ids:
            return False, "No recipient nodes configured"

        success_count = 0
        errors = []

        for node_id in self._node_ids:
            try:
                node_id = str(node_id)
                self._connector.send_message(text=message, destination=node_id, channel=0)
                success_count += 1
            except Exception as e:
                errors.append(f"{node_id}: {e}")

        if success_count == len(self._node_ids):
            return True, f"Sent DM to {success_count} node(s)"
        elif success_count > 0:
            return True, f"Sent to {success_count}/{len(self._node_ids)} nodes. Errors: {'; '.join(errors)}"
        else:
            return False, f"All DMs failed: {'; '.join(errors)}"


class MeshCoreDMChannel(NotificationChannel):
    """DM alert to specific MeshCore contacts (names or pubkeys)."""

    channel_type = "meshcore_dm"

    def __init__(self, connector: "MeshConnector", contacts: list):
        self._connector = connector
        self._contacts = list(contacts)
        _mc = getattr(connector, "max_chars", 200)
        self._renderer = MeshRenderer(char_limit=_mc if isinstance(_mc, int) else 200)

    def _has_meshcore_capability(self) -> bool:
        """Return True if the connector can reach a MeshCore transport."""
        by_name = getattr(self._connector, "_by_name", None)
        if by_name is not None:
            return "meshcore" in by_name
        return getattr(self._connector, "transport_name", None) == "meshcore"

    async def deliver(self, alert: "NotificationPayload", rule: "NotificationRuleConfig") -> bool:
        """Send alert via DM to configured MeshCore contacts."""
        if not self._connector:
            return False

        if not self._contacts:
            logger.debug("meshcore_dm: no contacts configured; skipping")
            return False

        if not self._has_meshcore_capability():
            logger.debug(
                "meshcore_dm: connector has no MeshCore transport; skipping"
            )
            return False

        # If payload already has chunk metadata (from digest), use message directly
        if alert.chunk_index is not None:
            messages = [alert.message or ""]
        else:
            messages = self._renderer.render(alert)

        success = True
        for contact in self._contacts:
            for message in messages:
                try:
                    self._connector.send_message(
                        text=message,
                        destination=str(contact),
                        transport="meshcore",
                    )
                except Exception as e:
                    logger.error("Failed to MeshCore DM %s: %s", contact, e)
                    success = False

        return success

    async def test_connection(self) -> dict:
        """Test MeshCore DM connectivity."""
        if not self._has_meshcore_capability():
            return {
                "success": False,
                "message": "No MeshCore transport available",
                "error": "Set connection.meshcore_host to enable MeshCore",
                "details": {"contacts": self._contacts},
            }
        if not self._contacts:
            return {
                "success": False,
                "message": "No MeshCore DM contacts configured",
                "error": "Add at least one contact to meshcore_dm_contacts",
                "details": {"contacts": []},
            }
        return {
            "success": True,
            "message": f"MeshCore DM to {len(self._contacts)} contact(s)",
            "error": "",
            "details": {"contacts": self._contacts},
        }

    async def deliver_test(self, message: str) -> tuple[bool, str]:
        """Deliver a specific test message via MeshCore DM."""
        if not self._connector:
            return False, "Not connected"
        if not self._contacts:
            return False, "No MeshCore DM contacts configured"
        success_count = 0
        errors = []
        for contact in self._contacts:
            try:
                self._connector.send_message(
                    text=message,
                    destination=str(contact),
                    transport="meshcore",
                )
                success_count += 1
            except Exception as e:
                errors.append(f"{contact}: {e}")
        if success_count == len(self._contacts):
            return True, f"Sent MeshCore DM to {success_count} contact(s)"
        elif success_count > 0:
            return True, f"Sent to {success_count}/{len(self._contacts)} contacts. Errors: {'; '.join(errors)}"
        else:
            return False, f"All MeshCore DMs failed: {'; '.join(errors)}"


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
        self._renderer = EmailRenderer()

    async def deliver(self, alert: "NotificationPayload", rule: "NotificationRuleConfig") -> bool:
        """Send alert via email."""
        if not self._recipients:
            return False

        # Use renderer for subject and body
        rendered = self._renderer.render(alert)
        subject = rendered["subject"]
        body = rendered["body"]

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
            with smtplib.SMTP(self._host, self._port, timeout=15) as server:
                server.starttls(context=context)
                if self._user and self._password:
                    server.login(self._user, self._password)
                server.sendmail(self._from, self._recipients, msg.as_string())
        else:
            with smtplib.SMTP(self._host, self._port, timeout=15) as server:
                if self._user and self._password:
                    server.login(self._user, self._password)
                server.sendmail(self._from, self._recipients, msg.as_string())

    async def test_connection(self) -> dict:
        """Test SMTP connectivity and authentication."""
        if not self._host:
            return {
                "success": False,
                "message": "SMTP host not configured",
                "error": "Set smtp_host in email configuration",
                "details": {}
            }

        if not self._recipients:
            return {
                "success": False,
                "message": "No recipients configured",
                "error": "Add at least one email recipient",
                "details": {}
            }

        if not self._from:
            return {
                "success": False,
                "message": "From address not configured",
                "error": "Set from_address in email configuration",
                "details": {}
            }

        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, self._test_smtp_connection)
            return result
        except Exception as e:
            return {
                "success": False,
                "message": "SMTP test failed",
                "error": str(e),
                "details": {"host": self._host, "port": self._port}
            }

    def _test_smtp_connection(self) -> dict:
        """Actually test SMTP connection (blocking)."""
        try:
            if self._tls:
                context = ssl.create_default_context()
                with smtplib.SMTP(self._host, self._port, timeout=15) as server:
                    server.starttls(context=context)
                    if self._user and self._password:
                        server.login(self._user, self._password)

                    # Send actual test email
                    msg = MIMEMultipart()
                    msg["From"] = self._from
                    msg["To"] = ", ".join(self._recipients)
                    msg["Subject"] = "[MeshAI] Channel connectivity test"
                    msg.attach(MIMEText(
                        "This is a test message from MeshAI to verify email delivery is working.\n\n"
                        f"Sent: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                        f"SMTP: {self._host}:{self._port} (TLS)\n\n"
                        "If you received this, email delivery is working correctly.",
                        "plain"
                    ))
                    server.sendmail(self._from, self._recipients, msg.as_string())
            else:
                with smtplib.SMTP(self._host, self._port, timeout=15) as server:
                    if self._user and self._password:
                        server.login(self._user, self._password)

                    msg = MIMEMultipart()
                    msg["From"] = self._from
                    msg["To"] = ", ".join(self._recipients)
                    msg["Subject"] = "[MeshAI] Channel connectivity test"
                    msg.attach(MIMEText(
                        "This is a test message from MeshAI to verify email delivery is working.\n\n"
                        f"Sent: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                        f"SMTP: {self._host}:{self._port}\n\n"
                        "If you received this, email delivery is working correctly.",
                        "plain"
                    ))
                    server.sendmail(self._from, self._recipients, msg.as_string())

            recipient_str = self._recipients[0]
            if len(self._recipients) > 1:
                recipient_str += f" +{len(self._recipients) - 1} more"

            return {
                "success": True,
                "message": f"Email sent to {recipient_str} via {self._host}:{self._port}",
                "error": "",
                "details": {
                    "host": self._host,
                    "port": self._port,
                    "tls": self._tls,
                    "recipients": self._recipients,
                }
            }

        except smtplib.SMTPAuthenticationError as e:
            return {
                "success": False,
                "message": "SMTP authentication failed",
                "error": f"Authentication failed with username '{self._user}'. Check username/password. For Gmail, use an App Password.",
                "details": {"host": self._host, "port": self._port, "user": self._user}
            }
        except smtplib.SMTPConnectError as e:
            return {
                "success": False,
                "message": "Connection refused",
                "error": f"Could not connect to {self._host}:{self._port}. Check host and port.",
                "details": {"host": self._host, "port": self._port}
            }
        except smtplib.SMTPServerDisconnected as e:
            return {
                "success": False,
                "message": "Server disconnected",
                "error": f"Server {self._host} disconnected unexpectedly. May need TLS.",
                "details": {"host": self._host, "port": self._port, "tls": self._tls}
            }
        except ssl.SSLError as e:
            return {
                "success": False,
                "message": "SSL/TLS error",
                "error": f"SSL error connecting to {self._host}:{self._port}. Try toggling TLS setting.",
                "details": {"host": self._host, "port": self._port, "tls": self._tls}
            }
        except TimeoutError:
            return {
                "success": False,
                "message": "Connection timeout",
                "error": f"Connection to {self._host}:{self._port} timed out. Check host/port and firewall.",
                "details": {"host": self._host, "port": self._port}
            }
        except OSError as e:
            if "Name or service not known" in str(e) or "getaddrinfo failed" in str(e):
                return {
                    "success": False,
                    "message": "Host not found",
                    "error": f"Cannot resolve hostname '{self._host}'. Check the SMTP host.",
                    "details": {"host": self._host}
                }
            elif "Connection refused" in str(e):
                return {
                    "success": False,
                    "message": "Connection refused",
                    "error": f"Connection refused at {self._host}:{self._port}. Check port number.",
                    "details": {"host": self._host, "port": self._port}
                }
            else:
                return {
                    "success": False,
                    "message": "Network error",
                    "error": str(e),
                    "details": {"host": self._host, "port": self._port}
                }

    async def deliver_test(self, message: str) -> tuple[bool, str]:
        """Deliver a specific test message via email."""
        if not self._recipients:
            return False, "No recipients configured"

        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                self._send_email,
                "[MeshAI TEST] Notification Test",
                message,
            )
            recipient_str = self._recipients[0]
            if len(self._recipients) > 1:
                recipient_str += f" +{len(self._recipients) - 1}"
            return True, f"Email sent to {recipient_str}"
        except smtplib.SMTPAuthenticationError:
            return False, f"SMTP auth failed for {self._user}"
        except smtplib.SMTPConnectError:
            return False, f"Cannot connect to {self._host}:{self._port}"
        except Exception as e:
            return False, f"Email failed: {e}"


class WebhookChannel(NotificationChannel):
    """POST alert JSON to a URL."""

    channel_type = "webhook"

    def __init__(self, url: str, headers: Optional[dict] = None):
        self._url = url
        self._headers = headers or {}
        self._renderer = WebhookRenderer()

    async def deliver(self, alert: "NotificationPayload", rule: "NotificationRuleConfig") -> bool:
        """POST alert to webhook URL."""
        # Use renderer for generic JSON payload
        payload = self._renderer.render(alert)

        # Discord/Slack format
        if "discord.com" in self._url or "slack.com" in self._url:
            severity = alert.severity or "routine"
            color = {
                "immediate": 0xFF0000,
                "priority": 0xFFAA00,
                "routine": 0x0099FF,
            }.get(severity, 0x888888)
            payload = {
                "embeds": [{
                    "title": "MeshAI: %s" % (alert.event_type or "unknown"),
                    "description": alert.message or "",
                    "color": color,
                }]
            }

        # ntfy format
        elif "ntfy" in self._url:
            headers = {
                **self._headers,
                "Title": "MeshAI: %s" % (alert.event_type or "alert"),
                "Priority": "3",
            }
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.post(
                        self._url,
                        content=alert.message or "",
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

    async def test_connection(self) -> dict:
        """Test webhook connectivity."""
        if not self._url:
            return {
                "success": False,
                "message": "Webhook URL not configured",
                "error": "Set webhook_url in configuration",
                "details": {}
            }

        # Validate URL format
        try:
            from urllib.parse import urlparse
            parsed = urlparse(self._url)
            if not parsed.scheme or not parsed.netloc:
                return {
                    "success": False,
                    "message": "Invalid URL format",
                    "error": f"URL must include scheme (https://) and host",
                    "details": {"url": self._url}
                }
        except Exception:
            return {
                "success": False,
                "message": "Invalid URL",
                "error": "Could not parse webhook URL",
                "details": {"url": self._url}
            }

        # Build test payload based on webhook type
        if "discord.com" in self._url:
            payload = {
                "embeds": [{
                    "title": "MeshAI: Channel Test",
                    "description": "This is a connectivity test from MeshAI. If you see this, webhook delivery is working.",
                    "color": 0x00FF00,
                }]
            }
        elif "slack.com" in self._url:
            payload = {
                "text": "MeshAI: Channel connectivity test - webhook delivery is working"
            }
        elif "ntfy" in self._url:
            # ntfy uses plain text body
            try:
                async with httpx.AsyncClient() as client:
                    headers = {
                        **self._headers,
                        "Title": "MeshAI Channel Test",
                        "Priority": "3",
                    }
                    resp = await client.post(
                        self._url,
                        content="Channel connectivity test - if you see this, webhook delivery works",
                        headers=headers,
                        timeout=10,
                    )
                    if resp.status_code < 400:
                        return {
                            "success": True,
                            "message": f"Webhook returned {resp.status_code} OK",
                            "error": "",
                            "details": {"url": self._url, "status": resp.status_code}
                        }
                    else:
                        return {
                            "success": False,
                            "message": f"Webhook returned {resp.status_code}",
                            "error": f"HTTP {resp.status_code}: {resp.text[:200]}",
                            "details": {"url": self._url, "status": resp.status_code}
                        }
            except httpx.ConnectError as e:
                return {
                    "success": False,
                    "message": "Connection failed",
                    "error": f"Cannot connect to {parsed.netloc}. Check URL.",
                    "details": {"url": self._url}
                }
            except httpx.TimeoutException:
                return {
                    "success": False,
                    "message": "Connection timeout",
                    "error": f"Request to {parsed.netloc} timed out",
                    "details": {"url": self._url}
                }
            except Exception as e:
                return {
                    "success": False,
                    "message": "Request failed",
                    "error": str(e),
                    "details": {"url": self._url}
                }
        else:
            payload = {
                "type": "test",
                "severity": "routine",
                "message": "MeshAI channel connectivity test",
                "timestamp": time.time(),
            }

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    self._url,
                    json=payload,
                    headers={"Content-Type": "application/json", **self._headers},
                    timeout=10,
                )

                if resp.status_code < 400:
                    return {
                        "success": True,
                        "message": f"Webhook returned {resp.status_code} OK",
                        "error": "",
                        "details": {"url": self._url, "status": resp.status_code}
                    }
                else:
                    # Try to get error details from response
                    error_body = ""
                    try:
                        error_body = resp.text[:200]
                    except Exception:
                        pass

                    return {
                        "success": False,
                        "message": f"Webhook returned {resp.status_code}",
                        "error": f"HTTP {resp.status_code}{': ' + error_body if error_body else ''}",
                        "details": {"url": self._url, "status": resp.status_code}
                    }

        except httpx.ConnectError as e:
            return {
                "success": False,
                "message": "Connection failed",
                "error": f"Cannot connect to {parsed.netloc}. Check URL and network.",
                "details": {"url": self._url}
            }
        except httpx.TimeoutException:
            return {
                "success": False,
                "message": "Connection timeout",
                "error": f"Request to {parsed.netloc} timed out after 10s",
                "details": {"url": self._url}
            }
        except Exception as e:
            return {
                "success": False,
                "message": "Request failed",
                "error": str(e),
                "details": {"url": self._url}
            }

    async def deliver_test(self, message: str) -> tuple[bool, str]:
        """Deliver a specific test message via webhook."""
        try:
            test_alert = {"type": "test", "severity": "routine", "message": message}
            success = await self.deliver(test_alert, {})
            if success:
                try:
                    from urllib.parse import urlparse
                    host = urlparse(self._url).netloc
                    return True, f"Sent to {host}"
                except Exception:
                    return True, "Webhook delivered"
            else:
                return False, "Webhook returned error"
        except Exception as e:
            return False, f"Webhook failed: {e}"


def create_channel(rule: "NotificationRuleConfig", connector=None) -> NotificationChannel:
    """Create a channel instance from a NotificationRuleConfig.

    Delivery types and their per-mesh routing:
      mesh_broadcast    -> Meshtastic ONLY (broadcast_channel; transport="meshtastic")
      meshcore_broadcast-> MeshCore ONLY (meshcore_channel NAME; transport="meshcore")
      mesh_dm           -> Meshtastic DM (node_ids; transport="meshtastic")
      meshcore_dm       -> MeshCore DM (meshcore_dm_contacts; transport="meshcore")
      email             -> SMTP email
      webhook           -> HTTP POST

    Args:
        rule: NotificationRuleConfig with delivery_type and channel settings
        connector: MeshConnector instance (required for mesh channels)

    Returns:
        NotificationChannel instance
    """
    delivery_type = rule.delivery_type

    if delivery_type == "mesh_broadcast":
        # Meshtastic-only broadcast: explicit transport hint so CompositeTransport
        # routes only to the Meshtastic child and skips MeshCore.
        return MeshBroadcastChannel(
            connector=connector,
            channel_index=rule.broadcast_channel,
            transport="meshtastic",
        )
    elif delivery_type == "meshcore_broadcast":
        # MeshCore-only broadcast: routes to MeshCore child by channel NAME.
        return MeshCoreBroadcastChannel(
            connector=connector,
            meshcore_channel=getattr(rule, "meshcore_channel", None),
        )
    elif delivery_type == "mesh_dm":
        # Meshtastic-only DM: explicit transport hint so CompositeTransport
        # routes only to the Meshtastic child.
        return MeshDMChannel(
            connector=connector,
            node_ids=rule.node_ids,
            transport_hint="meshtastic",
        )
    elif delivery_type == "meshcore_dm":
        # MeshCore-only DM: routes to MeshCore child via contact name/pubkey.
        return MeshCoreDMChannel(
            connector=connector,
            contacts=list(getattr(rule, "meshcore_dm_contacts", []) or []),
        )
    elif delivery_type == "email":
        return EmailChannel(
            smtp_host=rule.smtp_host,
            smtp_port=rule.smtp_port,
            smtp_user=rule.smtp_user,
            smtp_password=rule.smtp_password,
            smtp_tls=rule.smtp_tls,
            from_address=rule.from_address,
            recipients=rule.recipients,
        )
    elif delivery_type == "webhook":
        return WebhookChannel(
            url=rule.webhook_url,
            headers=rule.webhook_headers,
        )
    else:
        raise ValueError("Unknown delivery type: %s" % delivery_type)


def create_channel_from_dict(config: dict, connector=None) -> NotificationChannel:
    """Create a channel instance from a dict config (legacy interface).

    Used by old router.py and test_channel API. Will be removed in Phase 2.7.
    """
    channel_type = config.get("type", "")

    if channel_type == "mesh_broadcast":
        # Legacy dict configs are Meshtastic-only; no auto-fan.
        return MeshBroadcastChannel(
            connector=connector,
            channel_index=config.get("channel_index", 0),
            transport="meshtastic",
        )
    elif channel_type == "mesh_dm":
        return MeshDMChannel(
            connector=connector,
            node_ids=config.get("node_ids", []),
            transport_hint="meshtastic",
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
