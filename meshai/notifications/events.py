"""Event dataclass for the v0.3 notification pipeline.

This module defines the unified Event shape that flows through the
notification routing pipeline. All adapters emit Events, and the
router consumes them.

Usage:
    from meshai.notifications.events import Event, make_event

    # Create an event
    event = make_event(
        source="nws",
        category="tornado_warning",
        severity="immediate",
        title="Tornado Warning for Ada County",
        summary="A tornado warning has been issued...",
        lat=43.615,
        lon=-116.2023,
    )

    # Serialize for storage/webhook
    data = event.to_dict()

    # Restore from storage
    event2 = Event.from_dict(data)
"""

import hashlib
import time
from dataclasses import dataclass, field, asdict
from typing import Optional, Any


# Valid severity levels
SEVERITY_LEVELS = frozenset({"routine", "priority", "immediate"})


@dataclass
class Event:
    """Unified event shape for the notification pipeline.

    All adapters (NWS, FIRMS, alert_engine, etc.) emit Events.
    The router consumes Events and dispatches them to channels.
    """

    # Identity
    id: str = ""  # stable hash for dedup, computed if not provided
    source: str = ""  # adapter name: "nws", "firms", "alert_engine", etc.
    category: str = ""  # specific event type within source

    # Severity
    severity: str = "routine"  # "routine" | "priority" | "immediate"

    # Geography
    region: Optional[str] = None  # primary region name, set by region tagger
    regions: list[str] = field(default_factory=list)  # all regions touched
    lat: Optional[float] = None
    lon: Optional[float] = None
    nws_zones: list[str] = field(default_factory=list)  # NWS zone codes

    # Content
    title: str = ""  # one-line summary for digest headers
    summary: str = ""  # 1-3 sentence summary for immediate/mesh delivery
    body: str = ""  # full content for email/webhook delivery

    # Affected entities (for mesh health events)
    node_ids: list[str] = field(default_factory=list)
    short_names: list[str] = field(default_factory=list)

    # Timing
    timestamp: float = 0.0  # event creation time
    effective: Optional[float] = None  # event start (NWS-style)
    expires: Optional[float] = None  # event end (NWS-style)

    # Routing hints
    group_key: Optional[str] = None  # events with same key get merged
    inhibit_keys: list[str] = field(default_factory=list)  # suppression keys

    # Raw adapter data (preserved for advanced rendering)
    data: dict = field(default_factory=dict)

    @staticmethod
    def compute_id(
        source: str,
        category: str,
        group_key: Optional[str] = None,
        lat: Optional[float] = None,
        lon: Optional[float] = None,
    ) -> str:
        """Compute a stable dedup ID for an event.

        Two events with the same source+category+group_key+location
        will have the same ID and can be deduplicated.

        Args:
            source: Adapter name
            category: Event category
            group_key: Optional grouping key
            lat: Optional latitude
            lon: Optional longitude

        Returns:
            16-character hex ID
        """
        key_parts = [
            source,
            category,
            group_key or "",
            str(lat) if lat is not None else "",
            str(lon) if lon is not None else "",
        ]
        key_string = ":".join(key_parts)
        return hashlib.sha1(key_string.encode()).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        """Serialize event to a dict for JSON storage/webhook.

        Returns:
            Dict representation of the event
        """
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Event":
        """Restore an Event from a dict.

        Args:
            d: Dict representation (from to_dict or JSON load)

        Returns:
            Event instance
        """
        return cls(**d)


def make_event(
    source: str,
    category: str,
    severity: str,
    **kwargs: Any,
) -> Event:
    """Create an Event with automatic ID and timestamp.

    This is the primary factory function for creating events.
    It auto-computes the ID if not provided and sets timestamp
    to the current time if not provided.

    Args:
        source: Adapter name (e.g., "nws", "firms", "alert_engine")
        category: Event category (e.g., "tornado_warning", "infra_offline")
        severity: One of "routine", "priority", "immediate"
        **kwargs: Additional Event fields

    Returns:
        Event instance

    Raises:
        ValueError: If severity is not valid
    """
    # Validate severity
    if severity not in SEVERITY_LEVELS:
        raise ValueError(
            f"Invalid severity '{severity}'. "
            f"Must be one of: {', '.join(sorted(SEVERITY_LEVELS))}"
        )

    # Auto-set timestamp if not provided
    if "timestamp" not in kwargs or kwargs["timestamp"] == 0.0:
        kwargs["timestamp"] = time.time()

    # Auto-compute ID if not provided
    if "id" not in kwargs or not kwargs["id"]:
        kwargs["id"] = Event.compute_id(
            source=source,
            category=category,
            group_key=kwargs.get("group_key"),
            lat=kwargs.get("lat"),
            lon=kwargs.get("lon"),
        )

    return Event(
        source=source,
        category=category,
        severity=severity,
        **kwargs,
    )
