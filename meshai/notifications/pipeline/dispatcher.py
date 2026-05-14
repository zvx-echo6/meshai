"""Immediate event dispatcher.

The dispatcher routes immediate-severity events through the existing
NotificationRuleConfig rules and delivers via channels.py. This is the
transitional bridge between the new Event pipeline and the existing
channel implementations.
"""

import logging
from typing import Callable

from meshai.notifications.events import Event


class Dispatcher:
    """Dispatches immediate events to channels matching configured rules."""

    SEVERITY_RANK = {"routine": 0, "priority": 1, "immediate": 2}

    def __init__(self, config, channel_factory: Callable):
        """Initialize.

        Args:
            config: The full Config object (provides config.notifications.rules)
            channel_factory: Callable taking a NotificationRuleConfig and
                returning a NotificationChannel. This is create_channel
                from meshai/notifications/channels.py.
        """
        self._config = config
        self._channel_factory = channel_factory
        self._logger = logging.getLogger("meshai.pipeline.dispatcher")

    def dispatch(self, event: Event) -> None:
        """Deliver an immediate-severity event to all matching channels."""
        rules = self._matching_rules(event)
        if not rules:
            self._logger.debug(
                f"No matching rules for {event.source}/{event.category}, skipping"
            )
            return
        for rule in rules:
            try:
                channel = self._channel_factory(rule)
                alert = {
                    "category": event.category,
                    "severity": event.severity,
                    "message": event.summary or event.title,
                    "node_id": event.node_ids[0] if event.node_ids else None,
                    "region": event.region,
                    "timestamp": event.timestamp,
                }
                channel.deliver(alert)
                self._logger.info(
                    f"Dispatched event {event.id} via {rule.delivery_type}"
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
