"""Master toggle filter.

Drops events whose category maps to a toggle that the operator has
disabled. Distinct from DigestAccumulator.include_toggles, which
only affects the digest recap — this filter drops events from the
entire pipeline, so disabled toggles produce no live mesh delivery,
no digest entry, nothing.
"""

import logging
from typing import Callable

from meshai.notifications.events import Event
from meshai.notifications.categories import get_toggle


class ToggleFilter:
    """Drop events whose toggle isn't in the enabled set."""

    def __init__(
        self,
        next_handler: Callable[[Event], None],
        enabled_toggles: set[str] | None = None,
    ):
        """Initialize.

        Args:
            next_handler: Callable that receives non-dropped events.
            enabled_toggles: Set of toggle names that are enabled.
                If None, all toggles are enabled (filter is a no-op).
        """
        self._next = next_handler
        self._enabled = enabled_toggles  # None = no-op
        self._logger = logging.getLogger("meshai.pipeline.toggle_filter")

    def refresh(self, config=None) -> None:
        """v0.6-6: rebuild the enabled set from the current config.

        Wired into /api/config PUT so toggle changes take effect on the
        next event with no container restart. If `config` is None, the
        caller is expected to have already mutated whatever object the
        filter was constructed from; the safer pattern is to pass the
        live Config in explicitly.
        """
        if config is None:
            # Nothing to read from; keep the current set.
            return
        toggles_cfg = getattr(config.notifications, "toggles", None) or {}
        enabled = set()
        if isinstance(toggles_cfg, dict):
            for fam_name, tog in toggles_cfg.items():
                if getattr(tog, "enabled", False):
                    enabled.add(str(fam_name))
        self._enabled = enabled if enabled else None
        self._logger.info("ToggleFilter refreshed: enabled families=%s",
                            sorted(self._enabled or []))

    def handle(self, event: Event) -> None:
        """Pass the event through, or drop it if its toggle is disabled."""
        if self._enabled is None:
            self._next(event)
            return

        toggle = get_toggle(event.category) or "other"
        if toggle not in self._enabled:
            self._logger.debug(
                f"DROPPED event {event.id} — toggle {toggle!r} not enabled"
            )
            return
        self._next(event)
