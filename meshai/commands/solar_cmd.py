"""Solar/RF propagation command handler."""

from .base import CommandContext, CommandHandler


class SolarCommand(CommandHandler):
    """Space weather & RF propagation."""

    name = "solar"
    description = "Space weather & RF propagation"
    usage = "!solar"

    def __init__(self, env_store):
        self._env_store = env_store

    async def execute(self, args: str, context: CommandContext) -> str:
        """Execute the solar command."""
        if not self._env_store:
            return "Environmental feeds not enabled."

        lines = []

        # Space weather indices (raw data - no band conclusions)
        s = self._env_store.get_swpc_status()
        if s:
            kp = s.get("kp_current", "?")
            sfi = s.get("sfi", "?")
            r = s.get("r_scale", 0)
            s_sc = s.get("s_scale", 0)
            g = s.get("g_scale", 0)

            lines.append(f"Solar: SFI {sfi}, Kp {kp}")
            lines.append(f"  R{r}/S{s_sc}/G{g} scales")

            warnings = s.get("active_warnings", [])
            for w in warnings[:2]:
                lines.append(f"  Warning: {w[:100]}")
        else:
            lines.append("Solar: Data not available")

        # Tropospheric ducting (raw data - no frequency conclusions)
        d = self._env_store.get_ducting_status()
        if d:
            cond = d.get("condition", "unknown")
            gradient = d.get("min_gradient", "?")
            if cond == "normal":
                lines.append(f"Ducting: Normal (dM/dz {gradient})")
            else:
                thickness = d.get("duct_thickness_m", "?")
                lines.append(f"Ducting: {cond.replace('_', ' ').title()}")
                lines.append(f"  dM/dz: {gradient} M-units/km, ~{thickness}m thick")
        else:
            lines.append("Ducting: Data not available")

        return "\n".join(lines)
