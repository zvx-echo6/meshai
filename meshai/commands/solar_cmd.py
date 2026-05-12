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

        # HF section
        s = self._env_store.get_swpc_status()
        if s:
            assessment = s.get("band_assessment", "Unknown")
            kp = s.get("kp_current", "?")
            sfi = s.get("sfi", "?")
            r = s.get("r_scale", 0)
            s_sc = s.get("s_scale", 0)
            g = s.get("g_scale", 0)

            lines.append(f"HF: {assessment} -- SFI {sfi}, Kp {kp}")
            lines.append(f"  R{r}/S{s_sc}/G{g} scales")

            if assessment in ("Excellent", "Good"):
                lines.append("  10m-20m open, solid DX")
            elif assessment == "Fair":
                lines.append("  20m-40m usable, upper bands marginal")
            else:
                lines.append("  Degraded -- lower bands only")

            warnings = s.get("active_warnings", [])
            for w in warnings[:2]:
                lines.append(f"  Warning: {w[:100]}")
        else:
            lines.append("HF: Data not available")

        # UHF ducting section
        d = self._env_store.get_ducting_status()
        if d:
            cond = d.get("condition", "unknown")
            if cond == "normal":
                lines.append("UHF: Normal propagation (906 MHz)")
            else:
                gradient = d.get("min_gradient", "?")
                lines.append(f"UHF: {cond.replace('_', ' ').title()} (906 MHz)")
                lines.append(f"  dM/dz: {gradient} M-units/km")
                lines.append("  Extended range -- expect distant nodes")
        else:
            lines.append("UHF: Ducting data not available")

        return "\n".join(lines)
