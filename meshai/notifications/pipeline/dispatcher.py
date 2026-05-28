"""Immediate event dispatcher.

The dispatcher routes immediate-severity events through the existing
NotificationRuleConfig rules and delivers via channels.py. This is the
transitional bridge between the new Event pipeline and the existing
channel implementations.

Phase 2.5a: dispatch() is now async, takes a connector at construction,
and properly awaits channel.deliver(payload, rule).
"""

import logging
from typing import Callable, Optional

from meshai.notifications.events import Event, make_payload_from_event
from meshai.notifications.categories import get_toggle


class Dispatcher:
    """Dispatches immediate events to channels matching configured rules."""

    SEVERITY_RANK = {"routine": 0, "priority": 1, "immediate": 2}

    def __init__(self, config, channel_factory: Callable, connector=None):
        """Initialize.

        Args:
            config: The full Config object (provides config.notifications.rules)
            channel_factory: Callable taking (rule, connector) and returning
                a NotificationChannel. This is create_channel from
                meshai/notifications/channels.py.
            connector: MeshConnector instance for mesh channel deliveries.
        """
        self._config = config
        self._channel_factory = channel_factory
        self._connector = connector
        self._logger = logging.getLogger("meshai.pipeline.dispatcher")

    async def dispatch(self, event: Event) -> None:
        """Deliver via matching rules AND enabled family toggles (parallel, v0.5)."""
        await self._dispatch_rules(event)
        await self._dispatch_toggles(event)

    async def _dispatch_rules(self, event: Event) -> None:
        """Deliver an immediate-severity event to all matching channels.
        
        This method is async and awaits each channel.deliver() call.
        """
        rules = self._matching_rules(event)
        if not rules:
            self._logger.debug(
                f"No matching rules for {event.source}/{event.category}, skipping"
            )
            return
        for rule in rules:
            try:
                channel = self._channel_factory(rule, self._connector)
                payload = make_payload_from_event(event)
                success = await channel.deliver(payload, rule)
                if success:
                    self._logger.info(
                        f"Dispatched event {event.id} via {rule.delivery_type}"
                    )
                else:
                    self._logger.warning(
                        f"Channel delivery returned False for rule {rule.name}"
                    )
            except Exception:
                self._logger.exception(
                    f"Channel delivery failed for rule {rule.name}"
                )

    async def _dispatch_toggles(self, event: Event) -> None:
        """Route an event through its family master-toggle (parallel to rules)."""
        toggles = getattr(self._config.notifications, "toggles", None)
        if not isinstance(toggles, dict) or not toggles:
            return
        fam = get_toggle(event.category)
        if not fam:
            return
        tog = toggles.get(fam)
        if tog is None or not getattr(tog, "enabled", False):
            return
        regions = getattr(tog, "regions", None) or []
        if regions:
            ev_regions = set(filter(None, [event.region, *(event.regions or [])]))
            if not (set(regions) & ev_regions):
                return
        event_rank = self.SEVERITY_RANK.get(event.severity, 0)
        if event_rank < self.SEVERITY_RANK.get(getattr(tog, "min_severity", "routine"), 0):
            return
        sev_channels = getattr(tog, "severity_channels", None) or {}
        for ch_type in sev_channels.get(event.severity, []):
            if ch_type == "digest":
                continue
            try:
                rule = self._toggle_to_rule(tog, ch_type, event)
                channel = self._channel_factory(rule, self._connector)
                payload = make_payload_from_event(event)
                success = await channel.deliver(payload, rule)
                if success:
                    self._logger.info(f"Dispatched event {event.id} via toggle {fam}/{ch_type}")
                else:
                    self._logger.warning(f"Toggle channel delivery returned False for {fam}/{ch_type}")
            except Exception:
                self._logger.exception(f"Toggle channel delivery failed for {fam}/{ch_type}")

    def _toggle_to_rule(self, tog, ch_type: str, event: Event):
        from meshai.config import NotificationRuleConfig
        return NotificationRuleConfig(
            name=f"toggle:{getattr(tog, 'name', '')}",
            enabled=True, trigger_type="condition", delivery_type=ch_type,
            broadcast_channel=(getattr(tog, "broadcast_channel", None) or 0),
            node_ids=list(getattr(tog, "node_ids", []) or []),
            smtp_host=getattr(tog, "smtp_host", ""), smtp_port=getattr(tog, "smtp_port", 587),
            smtp_user=getattr(tog, "smtp_user", ""), smtp_password=getattr(tog, "smtp_password", ""),
            smtp_tls=getattr(tog, "smtp_tls", True), from_address=getattr(tog, "from_address", ""),
            recipients=list(getattr(tog, "recipients", []) or []),
            webhook_url=getattr(tog, "webhook_url", ""),
            webhook_headers=dict(getattr(tog, "webhook_headers", {}) or {}),
            override_quiet=bool(getattr(tog, "quiet_hours_override", False) and event.severity == "immediate"),
        )

    def _matching_rules(self, event: Event) -> list:
        """Return enabled condition rules matching this event's category
        and severity threshold."""
        event_rank = self.SEVERITY_RANK.get(event.severity, 0)
        matches = []
        for rule in self._config.notifications.rules:
            if not rule.enabled:
                continue
            if rule.trigger_type != "condition":
                continue
            if rule.categories and event.category not in rule.categories:
                continue
            min_rank = self.SEVERITY_RANK.get(rule.min_severity, 0)
            if event_rank < min_rank:
                continue
            scope = getattr(rule, "region_scope", None) or []
            if scope:
                ev_regions = set(filter(None, [event.region, *(event.regions or [])]))
                if not (set(scope) & ev_regions):
                    continue
            matches.append(rule)
        return matches
