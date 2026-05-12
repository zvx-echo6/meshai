"""Stream gauge command."""

from .base import CommandContext, CommandHandler


class StreamsCommand(CommandHandler):
    """Show current stream gauge readings."""

    aliases = ["gauges", "rivers"]

    def __init__(self, env_store):
        self._env_store = env_store
        self._name = "streams"

    @property
    def name(self) -> str:
        return self._name

    @name.setter
    def name(self, value: str):
        self._name = value

    @property
    def description(self) -> str:
        return "Show stream gauge readings"

    @property
    def usage(self) -> str:
        return "!streams"

    async def execute(self, args: str, context: CommandContext) -> str:
        if not self._env_store:
            return "Environmental feeds not configured."

        events = self._env_store.get_active(source="usgs")

        if not events:
            return "No stream gauge data available. Check if USGS sites are configured."

        lines = []

        # Group by site
        sites = {}
        for event in events:
            props = event.get("properties", {})
            site_id = props.get("site_id", "")
            site_name = props.get("site_name", "Unknown")

            if site_id not in sites:
                sites[site_id] = {"name": site_name, "readings": []}

            param = props.get("parameter", "")
            value = props.get("value", 0)
            unit = props.get("unit", "")

            sites[site_id]["readings"].append((param, value, unit))

        for site_id, data in sites.items():
            name = data["name"]
            readings = data["readings"]

            # Format readings
            parts = []
            for param, value, unit in readings:
                if "flow" in param.lower() or unit == "ft3/s":
                    parts.append(f"{value:,.0f} {unit}")
                else:
                    parts.append(f"{value:.1f} {unit}")

            reading_str = ", ".join(parts)
            lines.append(f"{name}: {reading_str}")

        return "\n".join(lines) if lines else "No stream gauge readings."
