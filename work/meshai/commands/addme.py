"""!addme command registration (registry/help visibility only).

The REAL !addme flow (MeshCore #aida channel self-service contact add) is
handled directly in the MeshCore channel ingest path -- see
``MeshCoreTransport._on_channel_event`` / ``meshai/meshcore_addme.py`` --
so it works independent of respond_to_channel_mentions and never reaches
this dispatcher for that case.

This handler exists only so ``!addme`` shows up in the command registry
and !help text. It deliberately does nothing: returning "" sends no reply
at all, so a Meshtastic (or MeshCore DM) invocation of !addme -- which DOES
reach this handler -- produces no noise on the wrong transport.
"""

from .base import CommandContext, CommandHandler


class AddMeCommand(CommandHandler):
    """MeshCore-only: added purely for !help discoverability."""

    name = "addme"
    description = "MeshCore: adds you as an AIDA contact and DMs you (use in #aida)"
    usage = "!addme"

    async def execute(self, args: str, context: CommandContext) -> str:
        # The real flow runs in the MeshCore channel ingest path before this
        # dispatcher is ever reached. If execution gets here at all (e.g. a
        # Meshtastic message, or a MeshCore DM), there's nothing to do --
        # an empty response sends no reply.
        return ""
