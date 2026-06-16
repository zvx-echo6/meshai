"""Alerts command handler."""

import time
from datetime import datetime

from .base import CommandContext, CommandHandler


class AlertsCommand(CommandHandler):
    """Active weather alerts for mesh area."""

    name = "alerts"
    description = "Active weather alerts for mesh area"
    usage = "!alerts"

    def __init__(self, env_store):
        self._env_store = env_store

    async def execute(self, args: str, context: CommandContext) -> str:
        """Execute the alerts command."""
        if not self._env_store:
            return "Environmental feeds not enabled."

        zones = self._env_store._mesh_zones
        alerts = self._env_store.get_for_zones(zones)

        if not alerts:
            alerts = self._env_store.get_active(source="nws")

        if not alerts:
            return "No active weather alerts for the mesh area."

        lines = [f"Active Alerts ({len(alerts)}):"]
        for a in alerts[:5]:
            # Format expiry time
            expires = a.get("expires", 0)
            if expires:
                try:
                    dt = datetime.fromtimestamp(expires)
                    expires_str = dt.strftime("%b %d %H:%MZ")
                except Exception:
                    expires_str = "Unknown"
            else:
                expires_str = "Unknown"

            lines.append(f"* {a['event_type']} -- {a.get('area_desc', '')[:60]}")
            lines.append(f"  Until {expires_str}")

        return "\n".join(lines)
