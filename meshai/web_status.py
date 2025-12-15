"""Simple web status page for MeshAI."""

import asyncio
import json
import logging
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import Callable, Optional

from .config import WebStatusConfig

logger = logging.getLogger(__name__)


class StatusData:
    """Container for status information."""

    def __init__(self):
        self.start_time = time.time()
        self.message_count = 0
        self.response_count = 0
        self.error_count = 0
        self.connected_nodes: set[str] = set()
        self.recent_activity: list[dict] = []
        self.last_message_time: Optional[float] = None
        self.using_fallback = False

    def record_message(self, sender_id: str, sender_name: str):
        """Record an incoming message."""
        self.message_count += 1
        self.last_message_time = time.time()
        self.connected_nodes.add(sender_id)

        self.recent_activity.append({
            "type": "message",
            "time": datetime.now().isoformat(),
            "sender": sender_name,
        })
        # Keep only last 20 activities
        self.recent_activity = self.recent_activity[-20:]

    def record_response(self):
        """Record an outgoing response."""
        self.response_count += 1

    def record_error(self, error: str):
        """Record an error."""
        self.error_count += 1
        self.recent_activity.append({
            "type": "error",
            "time": datetime.now().isoformat(),
            "error": error[:100],
        })
        self.recent_activity = self.recent_activity[-20:]

    def get_uptime(self) -> str:
        """Get formatted uptime string."""
        elapsed = int(time.time() - self.start_time)
        days, remainder = divmod(elapsed, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)

        parts = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        if minutes:
            parts.append(f"{minutes}m")
        parts.append(f"{seconds}s")

        return " ".join(parts)

    def to_dict(self, include_activity: bool = False) -> dict:
        """Convert to dictionary for JSON response."""
        data = {
            "status": "online",
            "uptime": self.get_uptime(),
            "uptime_seconds": int(time.time() - self.start_time),
            "messages_received": self.message_count,
            "responses_sent": self.response_count,
            "errors": self.error_count,
            "connected_nodes": len(self.connected_nodes),
            "using_fallback": self.using_fallback,
        }

        if self.last_message_time:
            data["last_message_ago"] = int(time.time() - self.last_message_time)

        if include_activity:
            data["recent_activity"] = self.recent_activity

        return data


# Global status data instance
_status_data = StatusData()


def get_status_data() -> StatusData:
    """Get the global status data instance."""
    return _status_data


class StatusRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for status page."""

    config: WebStatusConfig = None

    def log_message(self, format, *args):
        """Suppress default logging."""
        pass

    def do_GET(self):
        """Handle GET requests."""
        if self.path == "/" or self.path == "/status":
            self._serve_status_page()
        elif self.path == "/api/status":
            self._serve_json_status()
        elif self.path == "/health":
            self._serve_health()
        else:
            self.send_error(404)

    def _check_auth(self) -> bool:
        """Check authentication if required."""
        if not self.config or not self.config.require_auth:
            return True

        auth_header = self.headers.get("Authorization", "")
        if auth_header.startswith("Basic "):
            import base64
            try:
                decoded = base64.b64decode(auth_header[6:]).decode()
                _, password = decoded.split(":", 1)
                return password == self.config.auth_password
            except Exception:
                pass

        return False

    def _serve_status_page(self):
        """Serve HTML status page."""
        if not self._check_auth():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="MeshAI Status"')
            self.end_headers()
            return

        status = _status_data.to_dict(
            include_activity=self.config.show_recent_activity if self.config else False
        )

        html = f"""<!DOCTYPE html>
<html>
<head>
    <title>MeshAI Status</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace;
            background: #0d1117;
            color: #c9d1d9;
            margin: 0;
            padding: 20px;
        }}
        .container {{ max-width: 600px; margin: 0 auto; }}
        h1 {{ color: #58a6ff; border-bottom: 1px solid #30363d; padding-bottom: 10px; }}
        .stat {{
            display: flex;
            justify-content: space-between;
            padding: 10px 0;
            border-bottom: 1px solid #21262d;
        }}
        .stat-label {{ color: #8b949e; }}
        .stat-value {{ color: #58a6ff; font-weight: bold; }}
        .status-online {{ color: #3fb950; }}
        .status-fallback {{ color: #d29922; }}
        .footer {{ margin-top: 20px; color: #484f58; font-size: 12px; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>MeshAI Status</h1>
        <div class="stat">
            <span class="stat-label">Status</span>
            <span class="stat-value {'status-fallback' if status.get('using_fallback') else 'status-online'}">
                {'ONLINE (Fallback)' if status.get('using_fallback') else 'ONLINE'}
            </span>
        </div>
        {'<div class="stat"><span class="stat-label">Uptime</span><span class="stat-value">' + status["uptime"] + '</span></div>' if self.config and self.config.show_uptime else ''}
        {'<div class="stat"><span class="stat-label">Messages</span><span class="stat-value">' + str(status["messages_received"]) + '</span></div>' if self.config and self.config.show_message_count else ''}
        {'<div class="stat"><span class="stat-label">Responses</span><span class="stat-value">' + str(status["responses_sent"]) + '</span></div>' if self.config and self.config.show_message_count else ''}
        {'<div class="stat"><span class="stat-label">Connected Nodes</span><span class="stat-value">' + str(status["connected_nodes"]) + '</span></div>' if self.config and self.config.show_connected_nodes else ''}
        <div class="stat">
            <span class="stat-label">Errors</span>
            <span class="stat-value">{status["errors"]}</span>
        </div>
        <div class="footer">Auto-refresh in 30s</div>
    </div>
    <script>setTimeout(() => location.reload(), 30000);</script>
</body>
</html>"""

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(html.encode())

    def _serve_json_status(self):
        """Serve JSON status."""
        if not self._check_auth():
            self.send_response(401)
            self.end_headers()
            return

        status = _status_data.to_dict(
            include_activity=self.config.show_recent_activity if self.config else False
        )

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(status, indent=2).encode())

    def _serve_health(self):
        """Serve simple health check."""
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")


class WebStatusServer:
    """Web status server manager."""

    def __init__(self, config: WebStatusConfig):
        self.config = config
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[Thread] = None

    def start(self):
        """Start the web status server."""
        if not self.config.enabled:
            return

        StatusRequestHandler.config = self.config

        try:
            self._server = HTTPServer(("0.0.0.0", self.config.port), StatusRequestHandler)
            self._thread = Thread(target=self._server.serve_forever, daemon=True)
            self._thread.start()
            logger.info(f"Web status server started on port {self.config.port}")
        except Exception as e:
            logger.error(f"Failed to start web status server: {e}")

    def stop(self):
        """Stop the web status server."""
        if self._server:
            self._server.shutdown()
            self._server = None
            self._thread = None
            logger.info("Web status server stopped")
