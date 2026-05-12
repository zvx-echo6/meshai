"""Avalanche command handler."""

from .base import CommandContext, CommandHandler


class AvalancheCommand(CommandHandler):
    """Avalanche advisory information."""

    name = "avy"
    description = "Avalanche advisories"
    usage = "!avy"
    aliases = ["avalanche"]

    def __init__(self, env_store):
        self._env_store = env_store

    async def execute(self, args: str, context: CommandContext) -> str:
        """Execute the avalanche command."""
        if not self._env_store:
            return "Environmental feeds not enabled."

        # Check if any avalanche adapter is off season
        adapters = getattr(self._env_store, "_adapters", {})
        avy_adapter = adapters.get("avalanche")
        if avy_adapter and avy_adapter.is_off_season():
            return "Avalanche season ended -- check back in December."

        advisories = self._env_store.get_active(source="avalanche")

        if not advisories:
            return "No avalanche advisories available."

        lines = [f"Avalanche Advisories ({len(advisories)}):"]

        for a in advisories[:5]:
            zone = a.get("zone_name", "Unknown")
            danger_name = a.get("danger_name", "Unknown")
            center = a.get("center", "")
            link = a.get("forecast_link", "")

            line = f"* {zone}: {danger_name}"
            if center:
                line += f" ({center})"
            lines.append(line)

            # Add travel advice if present
            advice = a.get("travel_advice", "")
            if advice:
                lines.append(f"  {advice[:100]}")

        # Add link to first advisory
        if advisories and advisories[0].get("center_link"):
            lines.append(f"\nMore: {advisories[0]['center_link']}")

        return "\n".join(lines)
