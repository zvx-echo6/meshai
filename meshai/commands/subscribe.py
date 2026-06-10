"""Subscription commands for scheduled reports and alerts."""

from typing import TYPE_CHECKING

from .base import CommandContext, CommandHandler

if TYPE_CHECKING:
    from ..mesh_data_store import MeshDataStore
    from ..mesh_reporter import MeshReporter
    from ..subscriptions import SubscriptionManager
    from ..notifications.router import NotificationRouter


class SubCommand(CommandHandler):
    """Subscribe to scheduled reports or alerts."""

    name = "sub"
    description = "Subscribe to reports or alerts"
    usage = "!sub daily|weekly|alerts|<category> [time] [day] [scope]"
    aliases = ["subscribe"]

    def __init__(
        self,
        subscription_manager: "SubscriptionManager" = None,
        mesh_reporter: "MeshReporter" = None,
        data_store: "MeshDataStore" = None,
        notification_router: "NotificationRouter" = None,
    ):
        self._sub_manager = subscription_manager
        self._reporter = mesh_reporter
        self._data_store = data_store
        self._notification_router = notification_router

    async def execute(self, args: str, context: CommandContext) -> str:
        """Handle subscription command."""
        parts = args.strip().split()

        # No args - show available alert categories
        if not parts:
            return self._show_categories()

        sub_type = parts[0].lower()

        # Check if it's a category subscription
        if self._notification_router:
            from ..notifications.categories import ALERT_CATEGORIES
            if sub_type in ALERT_CATEGORIES or sub_type == "all":
                return self._handle_category_subscription(sub_type, context)

        # Legacy subscription types
        if sub_type not in ("daily", "weekly", "alerts"):
            return self._show_categories()

        if not self._sub_manager:
            return "Subscriptions not available."

        try:
            if sub_type == "daily":
                return self._handle_daily(parts[1:], context)
            elif sub_type == "weekly":
                return self._handle_weekly(parts[1:], context)
            else:  # alerts
                return self._handle_alerts(parts[1:], context)
        except ValueError as e:
            return f"Error: {e}"

    def _show_categories(self) -> str:
        """Show available alert categories."""
        try:
            from ..notifications.categories import ALERT_CATEGORIES
        except ImportError:
            return self._usage_help()

        lines = ["Available alert categories:"]
        for cat_id, cat_info in ALERT_CATEGORIES.items():
            lines.append(f"  {cat_id} - {cat_info['description']}")
        lines.append("")
        lines.append("Usage:")
        lines.append("  !sub <category>  - subscribe to a category")
        lines.append("  !sub all         - subscribe to all alerts")
        lines.append("  !sub alerts      - legacy mesh-wide alerts")

        return "\n".join(lines)

    def _handle_category_subscription(self, category: str, context: CommandContext) -> str:
        """Handle category-based alert subscription."""
        node_id = self._get_user_id(context)

        if category == "all":
            categories = []  # Empty = all categories
        else:
            categories = [category]

        # Add subscription via notification router
        rule_name = self._notification_router.add_mesh_subscription(
            node_id=node_id,
            categories=categories,
        )

        if category == "all":
            return "Subscribed to all alert categories. Use !unsub to remove."
        else:
            from ..notifications.categories import get_category
            cat_info = get_category(category)
            return f"Subscribed to {cat_info['name']} alerts. Use !unsub {category} to remove."

    def _usage_help(self) -> str:
        """Return usage help."""
        return """Usage:
!sub daily 1830              - daily mesh report at 6:30 PM
!sub daily 1830 region SCID  - daily region report
!sub weekly 0800 sun         - weekly digest Sunday 8 AM
!sub alerts                  - mesh-wide alerts (legacy)
!sub <category>              - subscribe to alert category
!sub all                     - subscribe to all alerts"""

    def _handle_daily(self, args: list, context: CommandContext) -> str:
        """Handle daily subscription."""
        if not args:
            raise ValueError("Time required. Example: !sub daily 1830")

        schedule_time = args[0]
        scope_type, scope_value = self._parse_scope(args[1:])
        scope_value = self._validate_scope(scope_type, scope_value)

        self._sub_manager.add(
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
        scope_value = self._validate_scope(scope_type, scope_value)

        self._sub_manager.add(
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
        """Handle alerts subscription (legacy)."""
        scope_type, scope_value = self._parse_scope(args)
        scope_value = self._validate_scope(scope_type, scope_value)

        self._sub_manager.add(
            user_id=self._get_user_id(context),
            sub_type="alerts",
            scope_type=scope_type,
            scope_value=scope_value,
        )

        scope_desc = self._format_scope(scope_type, scope_value)
        return f"Subscribed: alerts for {scope_desc.strip() or 'mesh'}"

    def _parse_scope(self, args: list) -> tuple[str, str]:
        """Parse scope from remaining args."""
        if not args:
            return "mesh", None

        scope_type = "mesh"
        scope_value = None

        for i, arg in enumerate(args):
            arg_lower = arg.lower()
            if arg_lower == "region":
                scope_type = "region"
                scope_value = " ".join(args[i + 1:]) if i + 1 < len(args) else None
                break
            elif arg_lower == "node":
                scope_type = "node"
                scope_value = args[i + 1] if i + 1 < len(args) else None
                break

        return scope_type, scope_value

    def _validate_scope(self, scope_type: str, scope_value: str) -> str:
        """Validate and resolve scope value."""
        if scope_type == "mesh":
            return None

        if not scope_value:
            raise ValueError(f"Missing {scope_type} name")

        if scope_type == "region" and self._reporter:
            region = self._reporter._find_region(scope_value)
            if region:
                return region.name
            return scope_value

        if scope_type == "node" and self._reporter:
            node = self._reporter._find_node(scope_value)
            if not node:
                raise ValueError(f"Node '{scope_value}' not found")
            return node.short_name or str(node.node_num)

        return scope_value

    def _get_user_id(self, context: CommandContext) -> str:
        """Extract user ID from context."""
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
    usage = "!unsub daily|weekly|alerts|<category>|all"
    aliases = ["unsubscribe"]

    def __init__(
        self,
        subscription_manager: "SubscriptionManager" = None,
        notification_router: "NotificationRouter" = None,
    ):
        self._sub_manager = subscription_manager
        self._notification_router = notification_router

    async def execute(self, args: str, context: CommandContext) -> str:
        """Handle unsubscribe command."""
        sub_type = args.strip().lower() if args else None

        if not sub_type:
            return "Usage: !unsub daily|weekly|alerts|<category>|all"

        user_id = self._get_user_id(context)

        # Check if it's a category unsubscription
        if self._notification_router:
            from ..notifications.categories import ALERT_CATEGORIES
            if sub_type in ALERT_CATEGORIES or sub_type == "all":
                self._notification_router.remove_mesh_subscription(user_id)
                return "Removed alert subscriptions"

        # Legacy subscription types
        if not self._sub_manager:
            return "Subscriptions not available."

        if sub_type not in ("daily", "weekly", "alerts", "all"):
            return f"Invalid type '{sub_type}'. Use: daily, weekly, alerts, <category>, or all"

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
    aliases = ["subs", "subscriptions"]

    def __init__(
        self,
        subscription_manager: "SubscriptionManager" = None,
        notification_router: "NotificationRouter" = None,
    ):
        self._sub_manager = subscription_manager
        self._notification_router = notification_router

    async def execute(self, args: str, context: CommandContext) -> str:
        """List user's subscriptions."""
        user_id = self._get_user_id(context)
        lines = []

        # Check notification router subscriptions
        if self._notification_router:
            categories = self._notification_router.get_node_subscriptions(user_id)
            if categories:
                if categories == ["all"]:
                    lines.append("Alert subscriptions: all categories")
                else:
                    lines.append(f"Alert subscriptions: {', '.join(categories)}")

        # Check legacy subscriptions
        if self._sub_manager:
            subs = self._sub_manager.get_user_subs(user_id)
            if subs:
                if not lines:
                    lines.append("Your subscriptions:")
                else:
                    lines.append("\nScheduled reports:")
                for i, sub in enumerate(subs, 1):
                    lines.append(f"  {i}. {self._format_sub(sub)}")

        if not lines:
            return "No active subscriptions. Use !sub to subscribe."

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
        else:
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
