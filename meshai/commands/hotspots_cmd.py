"""Satellite fire hotspot command."""

from .base import CommandContext, CommandHandler


class HotspotsCommand(CommandHandler):
    """Show NASA FIRMS satellite fire hotspot data."""

    aliases = ["satellite", "ignitions"]

    def __init__(self, env_store):
        self._env_store = env_store
        self._name = "hotspots"

    @property
    def name(self) -> str:
        return self._name

    @name.setter
    def name(self, value: str):
        self._name = value

    @property
    def description(self) -> str:
        return "Show satellite fire hotspots"

    @property
    def usage(self) -> str:
        return "!hotspots [--new]"

    async def execute(self, args: str, context: CommandContext) -> str:
        if not self._env_store:
            return "Environmental feeds not configured."

        # Check for --new flag
        new_only = "--new" in args.lower() or "new" in args.lower().split()

        # Get FIRMS adapter
        firms_adapter = getattr(self._env_store, "_firms", None)

        if not firms_adapter:
            return "Satellite hotspot monitoring not configured."

        if not firms_adapter._is_loaded:
            return "Satellite data not yet loaded. Try again shortly."

        if firms_adapter._consecutive_errors >= 999:
            return "Satellite monitoring disabled (invalid API key)."

        # Get events
        if new_only:
            events = firms_adapter.get_new_ignitions()
            title = "NEW IGNITIONS"
        else:
            events = firms_adapter.get_events()
            title = "FIRE HOTSPOTS"

        if not events:
            if new_only:
                return "No new ignitions detected. All hotspots near known fires."
            return "No satellite fire hotspots detected in monitored area."

        # Build response
        lines = [f"{title} ({len(events)}):"]

        # Sort by severity (warning > watch > advisory) then by FRP
        severity_order = {"warning": 0, "watch": 1, "advisory": 2}
        sorted_events = sorted(
            events,
            key=lambda e: (
                severity_order.get(e.get("severity", "advisory"), 3),
                -(e.get("properties", {}).get("frp") or 0),
            ),
        )

        for event in sorted_events[:8]:  # Limit for mesh
            props = event.get("properties", {})
            severity = event.get("severity", "advisory").upper()[:1]  # W/A

            # Format line
            line = f"[{severity}] {event.get('headline', 'Unknown')}"

            # Add confidence and FRP if available
            details = []
            if props.get("confidence"):
                details.append(f"conf:{props['confidence']}")
            if props.get("frp"):
                details.append(f"{int(props['frp'])}MW")
            if props.get("acq_time"):
                details.append(f"@{props['acq_time']}Z")

            if details:
                line += f" ({', '.join(details)})"

            lines.append(line)

        if len(events) > 8:
            lines.append(f"...and {len(events) - 8} more")

        return "\n".join(lines)
