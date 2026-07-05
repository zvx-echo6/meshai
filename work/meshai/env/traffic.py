"""TomTom Traffic Flow adapter."""

import json
import logging
import os
import time
from typing import TYPE_CHECKING, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlencode

from meshai.notifications.events import Event, make_event

if TYPE_CHECKING:
    from ..config import TomTomConfig

logger = logging.getLogger(__name__)


class TomTomTrafficAdapter:
    """TomTom Traffic Flow Segment Data polling."""

    BASE_URL = "https://api.tomtom.com/traffic/services/4/flowSegmentData/relative0/10/json"

    def __init__(self, config: "TomTomConfig"):
        self._api_key = self._resolve_env(config.api_key or "")
        self._corridors = config.corridors or []
        self._tick_interval = config.tick_seconds or 300
        self._last_tick = 0.0
        self._events = []
        self._consecutive_errors = 0
        self._last_error = None
        self._is_loaded = False
        self._daily_requests = 0
        self._daily_reset = 0.0

        if not self._api_key:
            logger.warning("TomTom API key not configured, adapter disabled")

        if not self._corridors:
            logger.info("TomTom: No corridors configured, adapter idle")

    def _resolve_env(self, value: str) -> str:
        """Resolve ${ENV_VAR} references in value."""
        if value and value.startswith("${") and value.endswith("}"):
            env_var = value[2:-1]
            return os.environ.get(env_var, "")
        return value

    def tick(self) -> bool:
        """Execute one polling tick.

        Returns:
            True if data changed
        """
        now = time.time()

        # Reset daily counter at midnight
        if now - self._daily_reset > 86400:
            self._daily_requests = 0
            self._daily_reset = now

        # No API key or corridors
        if not self._api_key or not self._corridors:
            return False

        # Check tick interval
        if now - self._last_tick < self._tick_interval:
            return False

        self._last_tick = now
        return self._fetch_all()

    def _fetch_all(self) -> bool:
        """Fetch traffic flow for all configured corridors.

        Returns:
            True if data changed
        """
        new_events = []
        now = time.time()
        any_error = False

        for corridor in self._corridors:
            # Support both dict and object formats
            if isinstance(corridor, dict):
                name = corridor.get("name", "Unknown")
                lat = corridor.get("lat")
                lon = corridor.get("lon")
            else:
                name = getattr(corridor, "name", "Unknown")
                lat = getattr(corridor, "lat", None)
                lon = getattr(corridor, "lon", None)

            if lat is None or lon is None:
                continue

            event = self._fetch_point(name, lat, lon, now)
            if event:
                new_events.append(event)
            else:
                any_error = True

        if any_error and not new_events:
            return False

        # Check if data changed
        old_ids = {e["event_id"] for e in self._events}
        new_ids = {e["event_id"] for e in new_events}
        changed = old_ids != new_ids

        self._events = new_events
        if not any_error:
            self._consecutive_errors = 0
            self._last_error = None
        self._is_loaded = True

        if changed:
            logger.info(f"TomTom traffic updated: {len(new_events)} corridors")

        return changed

    def _fetch_point(self, name: str, lat: float, lon: float, now: float) -> dict:
        """Fetch traffic flow for a single point.

        Args:
            name: Corridor name
            lat: Latitude
            lon: Longitude
            now: Current timestamp

        Returns:
            Event dict or None on error
        """
        params = {
            "point": f"{lat},{lon}",
            "key": self._api_key,
            "unit": "MPH",
        }

        url = f"{self.BASE_URL}?{urlencode(params)}"

        headers = {
            "User-Agent": "MeshAI/1.0",
            "Accept": "application/json",
        }

        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            self._daily_requests += 1

        except HTTPError as e:
            if e.code == 401 or e.code == 403:
                logger.error(f"TomTom auth error: {e.code} - check API key")
                self._last_error = f"Auth error {e.code}"
            else:
                logger.warning(f"TomTom HTTP error for {name}: {e.code}")
                self._last_error = f"HTTP {e.code}"
            self._consecutive_errors += 1
            return None

        except URLError as e:
            logger.warning(f"TomTom connection error for {name}: {e.reason}")
            self._last_error = str(e.reason)
            self._consecutive_errors += 1
            return None

        except Exception as e:
            logger.warning(f"TomTom fetch error for {name}: {e}")
            self._last_error = str(e)
            self._consecutive_errors += 1
            return None

        # Parse response
        try:
            flow = data.get("flowSegmentData", {})
            current_speed = flow.get("currentSpeed", 0)
            free_flow_speed = flow.get("freeFlowSpeed", 0)
            current_time = flow.get("currentTravelTime", 0)
            free_flow_time = flow.get("freeFlowTravelTime", 0)
            confidence = flow.get("confidence", 0)
            road_closure = flow.get("roadClosure", False)

            # Calculate speed ratio for severity
            if free_flow_speed > 0:
                ratio = current_speed / free_flow_speed
            else:
                ratio = 1.0

            # Determine severity
            if road_closure:
                severity = "priority"
            elif ratio >= 0.8:
                severity = "routine"
            elif ratio >= 0.5:
                severity = "routine"
            else:
                severity = "priority"

            # Format headline
            if road_closure:
                headline = f"{name}: CLOSED"
            else:
                pct = int(ratio * 100)
                headline = f"{name}: {int(current_speed)}mph ({pct}% of free flow)"

            event = {
                "source": "traffic",
                "event_id": f"traffic_{name.replace(' ', '_').lower()}",
                "event_type": "Traffic Flow",
                "headline": headline,
                "severity": severity,
                "lat": lat,
                "lon": lon,
                "expires": now + 600,  # 10 min TTL
                "fetched_at": now,
                "properties": {
                    "corridor": name,
                    "currentSpeed": current_speed,
                    "freeFlowSpeed": free_flow_speed,
                    "speedRatio": ratio,
                    "currentTravelTime": current_time,
                    "freeFlowTravelTime": free_flow_time,
                    "confidence": confidence,
                    "roadClosure": road_closure,
                },
            }

            return event

        except Exception as e:
            logger.warning(f"TomTom parse error for {name}: {e}")
            self._last_error = f"Parse error: {e}"
            self._consecutive_errors += 1
            return None

    def to_event(self, evt: dict) -> Optional["Event"]:
        """Translate a stored traffic event dict into a pipeline Event.

        Args:
            evt: Internal event dict from get_events()

        Returns:
            Event instance ready for EventBus emission, or None if the
            dict is missing required fields (lat/lon or corridor identity).
        """
        try:
            lat = evt.get("lat")
            lon = evt.get("lon")
            if lat is None or lon is None:
                return None  # Can't make a useful Event without coords

            props = evt.get("properties", {}) or {}
            corridor = props.get("corridor")
            if not corridor:
                return None  # No stable identity to group/inhibit on

            severity = evt.get("severity", "routine")
            title = evt.get("headline", "") or f"Traffic: {corridor}"

            # Richer summary: speed vs free flow, closure, confidence
            summary_parts = [title]
            if props.get("roadClosure"):
                summary_parts.append("road closed")
            if props.get("currentSpeed") is not None and props.get("freeFlowSpeed"):
                summary_parts.append(
                    f"{int(props['currentSpeed'])}/{int(props['freeFlowSpeed'])} mph"
                )
            if props.get("speedRatio") is not None:
                summary_parts.append(f"{int(props['speedRatio'] * 100)}% free flow")
            if props.get("confidence") is not None:
                summary_parts.append(f"conf {props['confidence']}")
            summary = " | ".join(summary_parts)[:300]

            # Stable per-corridor key (matches the adapter's own event_id
            # derivation). Re-polls of the same corridor coalesce on this
            # group_key; using it as the sole inhibit_key lets the pipeline
            # Inhibitor suppress lower-severity re-emissions while a
            # higher-severity one is active for the same corridor.
            corridor_key = f"traffic_{str(corridor).replace(' ', '_').lower()}"

            # Canonical data for the Phase-2 formatter (incident path).
            # TomTom Flow lacks structured road/direction/county; degrade
            # gracefully (None fields) — formatter uses headline as comment.
            canonical_data = {
                "external_id":    None,   # native adapter, no Central dedup
                "source":         "traffic",
                "sub_type":       "road_closed" if props.get("roadClosure") else "jam",
                "road":           str(corridor).replace("_", " ") if corridor else None,
                "direction":      None,
                "from_loc":       None,
                "to_loc":         None,
                "mile_start":     None,
                "mile_end":       None,
                "mile_marker":    None,
                "lanes_affected": None,
                "cause":          None,
                "comment":        title,
                "impact":         "all lanes closed" if props.get("roadClosure") else None,
                "county":         None,
                "state":          None,
                "lat":            lat,
                "lon":            lon,
                "geocoder_city":  None,
                "landclass":      None,
                "start_at":       None,
                "end_at":         None,
                "magnitude":      None,
                "delay_seconds":  None,
                "icon_category":  "road_closed" if props.get("roadClosure") else "jam",
            }

            return make_event(
                source="traffic",
                category="traffic_congestion",
                severity=severity,
                title=title,
                summary=summary,
                timestamp=evt.get("fetched_at"),
                expires=evt.get("expires"),
                lat=lat,
                lon=lon,
                group_key=corridor_key,
                inhibit_keys=[corridor_key],
                data=canonical_data,
            )
        except Exception:
            logger.exception(f"Traffic to_event failed for evt: {evt.get('event_id')}")
            return None

    def get_events(self) -> list:
        """Get current traffic events."""
        return self._events

    @property
    def health_status(self) -> dict:
        """Get adapter health status."""
        return {
            "source": "traffic",
            "is_loaded": self._is_loaded,
            "last_error": str(self._last_error) if self._last_error else None,
            "consecutive_errors": self._consecutive_errors,
            "event_count": len(self._events),
            "last_fetch": self._last_tick,
            "corridor_count": len(self._corridors),
            "daily_requests": self._daily_requests,
        }
