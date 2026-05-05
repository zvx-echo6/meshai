"""Subscription commands for scheduled reports and alerts."""

from typing import TYPE_CHECKING

from .base import CommandContext, CommandHandler

if TYPE_CHECKING:
    from ..mesh_data_store import MeshDataStore
    from ..mesh_reporter import MeshReporter
    from ..subscriptions import SubscriptionManager


class SubCommand(CommandHandler):
    """Subscribe to scheduled reports or alerts."""

    name = "sub"
    description = "Subscribe to reports or alerts"
    usage = "!sub daily|weekly|alerts [time] [day] [scope]"
    aliases = ["subscribe"]

    def __init__(
        self,
        subscription_manager: "SubscriptionManager" = None,
        mesh_reporter: "MeshReporter" = None,
        data_store: "MeshDataStore" = None,
    ):
        self._sub_manager = subscription_manager
        self._reporter = mesh_reporter
        self._data_store = data_store

    async def execute(self, args: str, context: CommandContext) -> str:
        """Handle subscription command."""
        if not self._sub_manager:
            return "Subscriptions not available."

        parts = args.strip().split()
        if not parts:
            return self._usage_help()

        sub_type = parts[0].lower()
        if sub_type not in ("daily", "weekly", "alerts"):
            return f"Invalid type '{sub_type}'. Use: daily, weekly, or alerts"

        try:
            if sub_type == "daily":
                return self._handle_daily(parts[1:], context)
            elif sub_type == "weekly":
                return self._handle_weekly(parts[1:], context)
            else:  # alerts
                return self._handle_alerts(parts[1:], context)
        except ValueError as e:
            return f"Error: {e}"

    def _usage_help(self) -> str:
        """Return usage help."""
        return """Usage:
!sub daily 1830              - daily mesh report at 6:30 PM
!sub daily 1830 region SCID  - daily region report
!sub daily 1830 node MHR     - daily node report
!sub weekly 0800 sun         - weekly digest Sunday 8 AM
!sub alerts                  - mesh-wide alerts
!sub alerts region SCID      - alerts for a region"""

    def _handle_daily(self, args: list, context: CommandContext) -> str:
        """Handle daily subscription."""
        if not args:
            raise ValueError("Time required. Example: !sub daily 1830")

        schedule_time = args[0]
        scope_type, scope_value = self._parse_scope(args[1:])

        # Validate scope
        scope_value = self._validate_scope(scope_type, scope_value)

        result = self._sub_manager.add(
            user_id=self._get_user_id(context),
            sub_type="daily",
            schedule_time=schedule_time,
            scope_type=scope_type,
            scope_value=scope_value,
        )

        time_fmt = self._format_time(schedule_time)
        scope_desc = self._format_scope(scope_type, scope_value)
        return f"Subscribed: daily {scope_desc}report at {time_fmt}"

    def _handle_weekly(self, args: list, context: CommandContext) -> str:
        """Handle weekly subscription."""
        if len(args) < 2:
            raise ValueError("Time and day required. Example: !sub weekly 0800 sun")

        schedule_time = args[0]
        schedule_day = args[1].lower()
        scope_type, scope_value = self._parse_scope(args[2:])

        # Validate scope
        scope_value = self._validate_scope(scope_type, scope_value)

        result = self._sub_manager.add(
            user_id=self._get_user_id(context),
            sub_type="weekly",
            schedule_time=schedule_time,
            schedule_day=schedule_day,
            scope_type=scope_type,
            scope_value=scope_value,
        )

        time_fmt = self._format_time(schedule_time)
        day_fmt = schedule_day.capitalize()
        scope_desc = self._format_scope(scope_type, scope_value)
        return f"Subscribed: weekly {scope_desc}report at {time_fmt} {day_fmt}"

    def _handle_alerts(self, args: list, context: CommandContext) -> str:
        """Handle alerts subscription."""
        scope_type, scope_value = self._parse_scope(args)

        # Validate scope
        scope_value = self._validate_scope(scope_type, scope_value)

        result = self._sub_manager.add(
            user_id=self._get_user_id(context),
            sub_type="alerts",
            scope_type=scope_type,
            scope_value=scope_value,
        )

        scope_desc = self._format_scope(scope_type, scope_value)
        return f"Subscribed: alerts for {scope_desc.strip() or 'mesh'}"

    def _parse_scope(self, args: list) -> tuple[str, str]:
        """Parse scope from remaining args.

        Returns:
            (scope_type, scope_value) tuple
        """
        if not args:
            return "mesh", None

        # Look for 'region' or 'node' keyword
        scope_type = "mesh"
        scope_value = None

        for i, arg in enumerate(args):
            arg_lower = arg.lower()
            if arg_lower == "region":
                scope_type = "region"
                # Everything after 'region' is the region name
                scope_value = " ".join(args[i + 1:]) if i + 1 < len(args) else None
                break
            elif arg_lower == "node":
                scope_type = "node"
                # Next arg is the node identifier
                scope_value = args[i + 1] if i + 1 < len(args) else None
                break

        return scope_type, scope_value

    def _validate_scope(self, scope_type: str, scope_value: str) -> str:
        """Validate and resolve scope value.

        Returns:
            Resolved scope_value (e.g., full region name)

        Raises:
            ValueError: If scope not found
        """
        if scope_type == "mesh":
            return None

        if not scope_value:
            raise ValueError(f"Missing {scope_type} name")

        if scope_type == "region" and self._reporter:
            region = self._reporter._find_region(scope_value)
            if not region:
                # List available regions
                health = self._reporter.health_engine.mesh_health
                if health:
                    available = [r.name for r in health.regions if r.node_ids]
                    return scope_value  # Use as-is, will fail at delivery if invalid
                raise ValueError(f"Region '{scope_value}' not found")
            return region.name  # Return canonical name

        if scope_type == "node" and self._reporter:
            node = self._reporter._find_node(scope_value)
            if not node:
                raise ValueError(f"Node '{scope_value}' not found")
            return node.short_name or str(node.node_num)

        return scope_value

    def _get_user_id(self, context: CommandContext) -> str:
        """Extract user ID from context."""
        # sender_id is like "!abcd1234" - convert to node_num
        sender_id = context.sender_id
        if sender_id.startswith("!"):
            return str(int(sender_id[1:], 16))
        return sender_id

    def _format_time(self, hhmm: str) -> str:
        """Format HHMM as readable time."""
        hours = int(hhmm[:2])
        minutes = int(hhmm[2:])
        period = "AM" if hours < 12 else "PM"
        display_hour = hours % 12 or 12
        return f"{display_hour}:{minutes:02d} {period}"

    def _format_scope(self, scope_type: str, scope_value: str) -> str:
        """Format scope for display."""
        if scope_type == "mesh" or not scope_value:
            return "mesh "
        return f"{scope_type} {scope_value} "


class UnsubCommand(CommandHandler):
    """Unsubscribe from reports or alerts."""

    name = "unsub"
    description = "Remove subscription(s)"
    usage = "!unsub daily|weekly|alerts|all"
    aliases = ["unsubscribe"]

    def __init__(self, subscription_manager: "SubscriptionManager" = None):
        self._sub_manager = subscription_manager

    async def execute(self, args: str, context: CommandContext) -> str:
        """Handle unsubscribe command."""
        if not self._sub_manager:
            return "Subscriptions not available."

        sub_type = args.strip().lower() if args else None

        if not sub_type:
            return "Usage: !unsub daily|weekly|alerts|all"

        if sub_type not in ("daily", "weekly", "alerts", "all"):
            return f"Invalid type '{sub_type}'. Use: daily, weekly, alerts, or all"

        user_id = self._get_user_id(context)
        removed = self._sub_manager.remove(user_id, sub_type if sub_type != "all" else None)

        if removed == 0:
            return "No subscriptions found to remove"
        elif sub_type == "all":
            return f"Removed all {removed} subscription(s)"
        else:
            return f"Removed {removed} {sub_type} subscription(s)"

    def _get_user_id(self, context: CommandContext) -> str:
        """Extract user ID from context."""
        sender_id = context.sender_id
        if sender_id.startswith("!"):
            return str(int(sender_id[1:], 16))
        return sender_id


class MySubsCommand(CommandHandler):
    """List active subscriptions."""

    name = "mysubs"
    description = "List your subscriptions"
    usage = "!mysubs"
    aliases = ["subs"]

    def __init__(self, subscription_manager: "SubscriptionManager" = None):
        self._sub_manager = subscription_manager

    async def execute(self, args: str, context: CommandContext) -> str:
        """List user's subscriptions."""
        if not self._sub_manager:
            return "Subscriptions not available."

        user_id = self._get_user_id(context)
        subs = self._sub_manager.get_user_subs(user_id)

        if not subs:
            return "No active subscriptions. Use !sub to subscribe."

        lines = ["Your subscriptions:"]
        for i, sub in enumerate(subs, 1):
            lines.append(f"  {i}. {self._format_sub(sub)}")

        return "\n".join(lines)

    def _format_sub(self, sub: dict) -> str:
        """Format a subscription for display."""
        sub_type = sub["sub_type"]
        scope_type = sub.get("scope_type", "mesh")
        scope_value = sub.get("scope_value")

        scope_desc = ""
        if scope_type == "region" and scope_value:
            scope_desc = f"region {scope_value} "
        elif scope_type == "node" and scope_value:
            scope_desc = f"node {scope_value} "

        if sub_type == "daily":
            time_str = self._format_time(sub.get("schedule_time", "0000"))
            return f"Daily {scope_desc}report at {time_str}"
        elif sub_type == "weekly":
            time_str = self._format_time(sub.get("schedule_time", "0000"))
            day_str = (sub.get("schedule_day") or "").capitalize()
            return f"Weekly {scope_desc}report at {time_str} {day_str}"
        else:  # alerts
            return f"Alerts for {scope_desc.strip() or 'mesh'}"

    def _format_time(self, hhmm: str) -> str:
        """Format HHMM as readable time."""
        if not hhmm or len(hhmm) != 4:
            return hhmm
        hours = int(hhmm[:2])
        minutes = int(hhmm[2:])
        period = "AM" if hours < 12 else "PM"
        display_hour = hours % 12 or 12
        return f"{display_hour}:{minutes:02d} {period}"

    def _get_user_id(self, context: CommandContext) -> str:
        """Extract user ID from context."""
        sender_id = context.sender_id
        if sender_id.startswith("!"):
            return str(int(sender_id[1:], 16))
        return sender_id
