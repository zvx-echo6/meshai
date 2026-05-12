"""Fire command handler."""

from .base import CommandContext, CommandHandler


class FireCommand(CommandHandler):
    """Active wildfire information."""

    name = "fire"
    description = "Active wildfires in the area"
    usage = "!fire"

    def __init__(self, env_store):
        self._env_store = env_store

    async def execute(self, args: str, context: CommandContext) -> str:
        """Execute the fire command."""
        if not self._env_store:
            return "Environmental feeds not enabled."

        fires = self._env_store.get_active(source="nifc")

        if not fires:
            return "No active wildfires in the area."

        lines = [f"Active Wildfires ({len(fires)}):"]

        for f in fires[:5]:
            name = f.get("name", "Unknown")
            acres = f.get("acres", 0)
            pct = f.get("pct_contained", 0)
            dist = f.get("distance_km")
            anchor = f.get("nearest_anchor")

            line = f"* {name} -- {int(acres):,} ac, {int(pct)}% contained"
            if dist is not None and anchor:
                line += f" ({int(dist)} km from {anchor})"
            lines.append(line)

        return "\n".join(lines)
