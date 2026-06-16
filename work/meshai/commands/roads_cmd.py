"""Road conditions command."""

from .base import CommandContext, CommandHandler


class RoadsCommand(CommandHandler):
    """Show traffic flow and road conditions."""

    aliases = ["traffic", "highways"]

    def __init__(self, env_store):
        self._env_store = env_store
        self._name = "roads"

    @property
    def name(self) -> str:
        return self._name

    @name.setter
    def name(self, value: str):
        self._name = value

    @property
    def description(self) -> str:
        return "Show traffic flow and road conditions"

    @property
    def usage(self) -> str:
        return "!roads"

    async def execute(self, args: str, context: CommandContext) -> str:
        if not self._env_store:
            return "Environmental feeds not configured."

        traffic_events = self._env_store.get_active(source="traffic")
        road_events = self._env_store.get_active(source="511")

        if not traffic_events and not road_events:
            return "No traffic or road data available. Check if sources are configured."

        lines = []

        # Traffic flow from TomTom
        if traffic_events:
            lines.append("Traffic Flow:")
            for event in traffic_events:
                props = event.get("properties", {})
                corridor = props.get("corridor", "Unknown")
                current = props.get("currentSpeed", 0)
                free_flow = props.get("freeFlowSpeed", 0)
                ratio = props.get("speedRatio", 1.0)
                closure = props.get("roadClosure", False)

                if closure:
                    lines.append(f"  {corridor}: CLOSED")
                else:
                    pct = int(ratio * 100)
                    lines.append(f"  {corridor}: {int(current)}mph ({pct}% of {int(free_flow)}mph)")

        # 511 road events
        if road_events:
            if traffic_events:
                lines.append("")  # Separator
            lines.append("Road Events:")
            for event in road_events:
                event_type = event.get("event_type", "Event")
                headline = event.get("headline", "")[:80]
                props = event.get("properties", {})
                is_closure = props.get("is_closure", False)

                icon = "X" if is_closure else "-"
                lines.append(f"  {icon} {headline}")

        return "\n".join(lines) if lines else "No road conditions data."
