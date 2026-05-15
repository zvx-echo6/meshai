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
            matches.append(rule)
        return matches
