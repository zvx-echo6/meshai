"""Avalanche.org advisory adapter."""

import json
import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from meshai.notifications.events import Event, make_event

if TYPE_CHECKING:
    from ..config import AvalancheConfig

logger = logging.getLogger(__name__)


class AvalancheAdapter:
    """Avalanche.org map layer polling."""

    BASE_URL = "https://api.avalanche.org/v2/public/products/map-layer"

    # Danger level mapping
    DANGER_LEVELS = {
        -1: ("no_rating", "No Rating"),
        0: ("no_rating", "No Rating"),
        1: ("low", "Low"),
        2: ("moderate", "Moderate"),
        3: ("considerable", "Considerable"),
        4: ("high", "High"),
        5: ("extreme", "Extreme"),
    }

    def __init__(self, config: "AvalancheConfig"):
        self._center_ids = config.center_ids
        self._tick_interval = config.tick_seconds or 1800
        self._season_months = config.season_months or [12, 1, 2, 3, 4]
        self._last_tick = 0.0
        self._events = []
        self._consecutive_errors = 0
        self._last_error = None
        self._is_loaded = False
        self._off_season = False

    def tick(self) -> bool:
        """Execute one polling tick.

        Returns:
            True if data changed
        """
        now = time.time()

        # Check if in season
        current_month = datetime.now().month
        if current_month not in self._season_months:
            self._off_season = True
            self._events = []
            return False

        self._off_season = False

        if now - self._last_tick < self._tick_interval:
            return False

        self._last_tick = now
        return self._fetch()

    def _fetch(self) -> bool:
        """Fetch avalanche advisories from all center IDs.

        Returns:
            True if data changed
        """
        new_events = []
        now = time.time()
        any_error = False

        for center_id in self._center_ids:
            url = f"{self.BASE_URL}/{center_id}"

            headers = {
                "User-Agent": "MeshAI/1.0",
                "Accept": "application/json",
            }

            try:
                req = Request(url, headers=headers)
                with urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode("utf-8"))

            except HTTPError as e:
                logger.warning(f"Avalanche {center_id} HTTP error: {e.code}")
                self._last_error = f"{center_id}: HTTP {e.code}"
                any_error = True
                continue

            except URLError as e:
                logger.warning(f"Avalanche {center_id} connection error: {e.reason}")
                self._last_error = f"{center_id}: {e.reason}"
                any_error = True
                continue

            except Exception as e:
                logger.warning(f"Avalanche {center_id} fetch error: {e}")
                self._last_error = f"{center_id}: {e}"
                any_error = True
                continue

            # Parse response - it's a FeatureCollection
            features = data.get("features", [])

            for feature in features:
                props = feature.get("properties", {})

                zone_name = props.get("name", "Unknown Zone")
                center_name = props.get("center", center_id)
                center_link = props.get("center_link", "")
                forecast_link = props.get("link", "")
                danger = props.get("danger", "no rating")
                danger_level = props.get("danger_level", -1)
                off_season = props.get("off_season", False)
                state = props.get("state", "")
                travel_advice = props.get("travel_advice", "")

                # Skip off-season zones
                if off_season:
                    continue

                # Map danger level to severity
                level_key, level_name = self.DANGER_LEVELS.get(danger_level, ("no_rating", "No Rating"))

                if danger_level >= 4:
                    severity = "priority"
                elif danger_level >= 3:
                    severity = "routine"
                elif danger_level >= 2:
                    severity = "routine"
                else:
                    severity = "routine"

                # Compute centroid
                geom = feature.get("geometry")
                lat, lon = self._compute_centroid(geom)

                # Format headline
                headline = f"{zone_name}: {level_name} avalanche danger"
                if travel_advice:
                    headline += f" -- {travel_advice[:100]}"

                # Expires at end of day (mountain time approximation)
                end_of_day = datetime.now().replace(hour=23, minute=59, second=59)
                expires = end_of_day.timestamp()

                event = {
                    "source": "avalanche",
                    "event_id": f"avy_{center_id}_{zone_name.replace(' ', '_').lower()}",
                    "event_type": "Avalanche Advisory",
                    "severity": severity,
                    "headline": headline,
                    "zone_name": zone_name,
                    "center": center_name,
                    "center_id": center_id,
                    "center_link": center_link,
                    "forecast_link": forecast_link,
                    "danger": danger,
                    "danger_level": danger_level,
                    "danger_name": level_name,
                    "travel_advice": travel_advice,
                    "state": state,
                    "lat": lat,
                    "lon": lon,
                    "expires": expires,
                    "fetched_at": now,
                }

                new_events.append(event)

        # Check if data changed
        old_ids = {e["event_id"] for e in self._events}
        new_ids = {e["event_id"] for e in new_events}
        changed = old_ids != new_ids

        self._events = new_events

        if not any_error:
            self._consecutive_errors = 0
            self._last_error = None
        else:
            self._consecutive_errors += 1

        self._is_loaded = True

        if changed:
            logger.info(f"Avalanche advisories updated: {len(new_events)} active zones")

        return changed

    def _compute_centroid(self, geom) -> tuple:
        """Compute centroid from GeoJSON geometry."""
        if not geom:
            return (None, None)

        try:
            coords = geom.get("coordinates", [])
            geom_type = geom.get("type")

            if geom_type == "Polygon" and coords:
                ring = coords[0]
                if ring:
                    lat_sum = sum(c[1] for c in ring)
                    lon_sum = sum(c[0] for c in ring)
                    return (lat_sum / len(ring), lon_sum / len(ring))

            elif geom_type == "MultiPolygon" and coords:
                all_lats = []
                all_lons = []
                for polygon in coords:
                    if polygon:
                        ring = polygon[0]
                        if ring:
                            all_lats.append(sum(c[1] for c in ring) / len(ring))
                            all_lons.append(sum(c[0] for c in ring) / len(ring))
                if all_lats and all_lons:
                    return (sum(all_lats) / len(all_lats), sum(all_lons) / len(all_lons))

        except Exception:
            pass

        return (None, None)

    def to_event(self, evt: dict) -> Optional["Event"]:
        """Translate a stored avalanche advisory into a pipeline Event.

        Only elevated danger is emitted: the category is chosen from
        danger_level, so a Low/Moderate/No-Rating advisory is intentionally
        NOT emitted (returns None). High/Extreme (4-5) -> avalanche_warning;
        Considerable (3) -> avalanche_watch.

        Multi-line wire format (matching Fire/Roads/Quake style):
            Line 1: emoji prefix zone — danger_name (level)
            Line 2: travel_advice (truncated, only if present)
            Line 3: center_id · valid today

        The _is_update flag is set by EnvironmentalStore when danger_level
        rises for an existing zone; New: for first sighting, Update: for rise.

        Args:
            evt: Internal event dict from get_events()

        Returns:
            Event instance ready for EventBus emission, or None if the dict
            is missing its centroid or event_id, or the danger is not elevated.
        """
        try:
            from meshai.adapter_config import adapter_config

            lat = evt.get("lat")
            lon = evt.get("lon")
            if lat is None or lon is None:
                return None  # No centroid -- can't make a useful Event

            event_id = evt.get("event_id")
            if not event_id:
                return None  # No stable identity to group/inhibit on

            danger_level = evt.get("danger_level")
            if danger_level is None:
                return None

            # Min danger level floor from adapter_config
            min_level = int(adapter_config.avalanche.min_danger_level)
            if danger_level < min_level:
                return None

            # Category from danger level: High/Extreme (4-5) is a warning,
            # Considerable (3) is a watch, anything below is not actionable.
            if danger_level >= 4:
                category = "avalanche_warning"
            elif danger_level == 3:
                category = "avalanche_watch"
            else:
                return None  # Below min_level but still < 3 -- do not emit

            severity = evt.get("severity", "routine")

            # New/Update prefix from store's danger-level-rise detection
            is_update = bool(evt.get("_is_update", False))
            prefix = "Update:" if is_update else "New:"

            # Line 1: emoji + prefix + zone + danger level
            emoji = "\u26f7"
            level_name = evt.get("danger_name", "Unknown")
            zone = evt.get("zone_name", "Unknown Zone")
            line1 = f"{emoji} {prefix} {zone} \u2014 {level_name} ({danger_level})"

            # Line 2: travel advice (truncated to 120 chars, only if present)
            travel = evt.get("travel_advice", "")
            line2 = travel[:120] if travel else None

            # Line 3: center ID + valid date
            center_id = evt.get("center_id", "")
            line3 = f"{center_id} \u00b7 valid today" if center_id else "valid today"

            summary = "\n".join(l for l in [line1, line2, line3] if l)
            title = line1  # first line only for title

            # event_id is already the stable "avy_{center}_{zone}" key. Re-polls
            # of the same zone coalesce on this group_key; using it as the sole
            # inhibit_key lets the pipeline Inhibitor suppress lower-severity
            # re-emissions while a higher-severity one is active (severity
            # tiering delegated to the Inhibitor).
            return make_event(
                source="avalanche",
                category=category,
                severity=severity,
                title=summary,
                summary=summary,
                data={"_meshai_precomposed": True},
                timestamp=evt.get("fetched_at"),
                expires=evt.get("expires"),
                lat=lat,
                lon=lon,
                group_key=event_id,
                inhibit_keys=[event_id],
            )
        except Exception:
            logger.exception(f"Avalanche to_event failed for evt: {evt.get('event_id')}")
            return None

    def is_off_season(self) -> bool:
        """Check if currently off season."""
        return self._off_season

    def get_events(self) -> list:
        """Get current avalanche events."""
        return self._events

    @property
    def health_status(self) -> dict:
        """Get adapter health status."""
        return {
            "source": "avalanche",
            "is_loaded": self._is_loaded,
            "last_error": str(self._last_error) if self._last_error else None,
            "consecutive_errors": self._consecutive_errors,
            "event_count": len(self._events),
            "last_fetch": self._last_tick,
            "off_season": self._off_season,
        }
