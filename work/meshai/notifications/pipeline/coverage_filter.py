"""Coverage area gate — geographic event filter.

Mirrors ToggleFilter's construction + integration pattern exactly:
same base interface (a callable ``.handle(event)``), constructed from config,
inserted in the pipeline chain adjacent to ToggleFilter.

Events that lie entirely outside ALL configured bounding boxes are dropped.
Events whose source is in ``excluded_adapters`` are always kept.

Unlocatable events (no parseable geometry/centroid) are handled by category:
weather-alert categories (``FAIL_CLOSED_CATEGORIES``) are DROPPED — fail-closed,
matching Central's NWS behaviour and closing the zone-only-advisory leak — while
all other categories are KEPT (fail-open, matching Central's choke-point
semantics; quake/fire events always carry a centroid anyway).

When coverage is disabled (``coverage.enabled`` is False) or no areas are
configured (``areas`` is empty), the filter is a no-op and passes everything.
"""

import logging
from typing import Callable

from meshai.notifications.events import Event
from meshai.coverage_area import MonitoringArea, classify_event_areas, event_region_names


# Weather-alert categories (from env/nws.py::_derive_category) for which an
# event that cannot be geographically located is DROPPED (fail-closed),
# matching Central's NWS behaviour. The real LA leak was a zone-only Heat
# Advisory with no polygon and no centroid — fail-open kept it; this drops it.
FAIL_CLOSED_CATEGORIES = {
    "weather_warning",
    "weather_watch",
    "weather_advisory",
    "weather_statement",
}


class CoverageFilter:
    """Gate events against one or more geographic bounding boxes (set union).

    Construction mirrors ToggleFilter:
        coverage_filter = CoverageFilter(
            next_handler=_tee,
            areas=areas_from_config(config.coverage),
            enabled=config.coverage.enabled,
            excluded_adapters=set(config.coverage.excluded_adapters),
        )

    Pipeline placement (inserted between ToggleFilter and _tee):
        inhibitor → grouper → toggle_filter → coverage_filter → _tee
    """

    def __init__(
        self,
        next_handler: Callable[[Event], None],
        areas: list[MonitoringArea] | None = None,
        enabled: bool = True,
        excluded_adapters: set[str] | None = None,
        region_tagging: bool = False,
    ):
        """Initialize.

        Args:
            next_handler: Callable that receives non-dropped events.
            areas: List of MonitoringArea bounding boxes.  Empty list or None
                means the filter is a no-op (keeps everything).
            enabled: Master switch; False makes the filter a no-op.
            excluded_adapters: Set of source/adapter names that bypass the gate.
                Matching is against event.source.
            region_tagging: P1 flag; when True (and areas non-empty), stamp
                event.region / event.regions from named areas BEFORE the gate.
                Never clobbers a preset regions list.
        """
        self._next = next_handler
        self._areas: list[MonitoringArea] = areas or []
        self._enabled = enabled
        self._excluded: set[str] = excluded_adapters or set()
        self._region_tagging = region_tagging
        self._logger = logging.getLogger("meshai.pipeline.coverage_filter")

    def handle(self, event: Event) -> None:
        """Pass the event through, or drop it if outside all coverage areas."""
        # No-op when disabled or no areas configured
        if not self._enabled or not self._areas:
            self._next(event)
            return

        # Bypass for excluded adapters (opt-out list from config)
        if event.source in self._excluded:
            self._next(event)
            return

        # Region tagging — stamp event.region / event.regions from named
        # coverage areas BEFORE the gate so the gate verdict is unchanged.
        # Runs whenever named areas exist and the event doesn't already carry
        # regions (never clobbers satpass/adapter-preset tags). Deliberately NOT
        # gated on a separate flag: the Coverage GUI save omits such a flag and
        # would reset it, silently turning tagging off. If you named your
        # coverage areas, events get tagged; region_routes.enabled gates ROUTING.
        if self._areas and not event.regions:
            names = event_region_names(event, self._areas)
            if names:
                event.regions = names
                event.region = names[0]
            # Measurement/observability: one DEBUG line per tagged event so the
            # real per-family region-coverage rate can be read from the logs.
            self._logger.debug(
                "region-tag: category=%s source=%s -> %s",
                event.category, event.source, names or "NONE",
            )

        verdict = classify_event_areas(event, self._areas)

        # Positively in-bounds → keep. 'no-area' can't occur here (guarded by
        # the `not self._areas` no-op above) but is treated as keep for safety.
        if verdict in ("in-bounds", "no-area"):
            self._next(event)
            return

        # Positively out-of-bounds → drop, regardless of category.
        if verdict == "out-of-bounds":
            self._logger.debug(
                "DROPPED event %s — out of all coverage areas "
                "(source=%s category=%s)",
                event.id,
                event.source,
                event.category,
            )
            return

        # Unlocatable ('null-geom' / 'invalid-geom'): fail CLOSED for weather
        # alerts (drop — matches Central), fail OPEN for everything else (keep).
        if event.category in FAIL_CLOSED_CATEGORIES:
            self._logger.debug(
                "DROPPED event %s — unlocatable weather alert (%s), failing "
                "closed (source=%s category=%s)",
                event.id,
                verdict,
                event.source,
                event.category,
            )
            return

        self._next(event)
