"""511 Road Conditions adapter.

Polls a configurable 511 API for road events. The base URL is fully
configurable as each state has a different 511 system.
"""

import json
import logging
import os
import time
from typing import TYPE_CHECKING, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urljoin

from meshai.notifications.events import Event, make_event

if TYPE_CHECKING:
    from ..config import Roads511Config

logger = logging.getLogger(__name__)


class Roads511Adapter:
    """511 road conditions polling adapter."""

    def __init__(self, config: "Roads511Config"):
        self._api_key = self._resolve_env(config.api_key or "")
        self._base_url = (config.base_url or "").rstrip("/")
        self._endpoints = config.endpoints or ["/get/event"]
        self._bbox = config.bbox or []  # [west, south, east, north]
        self._tick_interval = config.tick_seconds or 300
        self._last_tick = 0.0
        self._events = []
        self._consecutive_errors = 0
        self._last_error = None
        self._is_loaded = False
        self._auth_failed = False  # Stop retrying on auth failures

        if not self._base_url:
            logger.info("511: No base URL configured, adapter disabled")

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

        # No base URL configured
        if not self._base_url:
            return False

        # Auth failed - don't keep retrying
        if self._auth_failed:
            return False

        # Check tick interval
        if now - self._last_tick < self._tick_interval:
            return False

        self._last_tick = now
        return self._fetch_all()

    def _fetch_all(self) -> bool:
        """Fetch events from all configured endpoints.

        Returns:
            True if data changed
        """
        new_events = []
        now = time.time()

        for endpoint in self._endpoints:
            events = self._fetch_endpoint(endpoint, now)
            if events:
                new_events.extend(events)

        # Apply bbox filter if configured
        if self._bbox and len(self._bbox) == 4:
            west, south, east, north = self._bbox
            new_events = [
                e for e in new_events
                if e.get("lat") is not None and e.get("lon") is not None
                and west <= e["lon"] <= east and south <= e["lat"] <= north
            ]

        # Check if data changed
        old_ids = {e["event_id"] for e in self._events}
        new_ids = {e["event_id"] for e in new_events}
        changed = old_ids != new_ids

        self._events = new_events
        self._is_loaded = True

        if changed:
            logger.info(f"511 road events updated: {len(new_events)} active")

        return changed

    def _fetch_endpoint(self, endpoint: str, now: float) -> list:
        """Fetch events from a single endpoint.

        Args:
            endpoint: API endpoint path
            now: Current timestamp

        Returns:
            List of event dicts
        """
        url = urljoin(self._base_url + "/", endpoint.lstrip("/"))

        # Add API key if configured
        if self._api_key:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}key={self._api_key}"

        headers = {
            "User-Agent": "MeshAI/1.0",
            "Accept": "application/json",
        }

        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))

        except HTTPError as e:
            if e.code == 401 or e.code == 403:
                logger.error(
                    f"511 auth error: {e.code} - check API key configuration for {self._base_url}"
                )
                self._last_error = f"Auth error {e.code} - check API key"
                self._auth_failed = True
                return []
            else:
                logger.warning(f"511 HTTP error for {endpoint}: {e.code}")
                self._last_error = f"HTTP {e.code}"
            self._consecutive_errors += 1
            return []

        except URLError as e:
            logger.warning(f"511 connection error for {endpoint}: {e.reason}")
            self._last_error = str(e.reason)
            self._consecutive_errors += 1
            return []

        except Exception as e:
            logger.warning(f"511 fetch error for {endpoint}: {e}")
            self._last_error = str(e)
            self._consecutive_errors += 1
            return []

        # Parse response - handle various 511 API formats
        return self._parse_response(data, now)

    def _parse_response(self, data, now: float) -> list:
        """Parse 511 API response.

        Different states use different formats. Try common patterns.

        Args:
            data: JSON response data
            now: Current timestamp

        Returns:
            List of event dicts
        """
        events = []

        # Handle array response
        if isinstance(data, list):
            items = data
        # Handle wrapped response
        elif isinstance(data, dict):
            # Try common wrapper keys
            items = (
                data.get("events") or
                data.get("items") or
                data.get("data") or
                data.get("results") or
                []
            )
            if not isinstance(items, list):
                items = [data] if self._looks_like_event(data) else []
        else:
            return []

        for item in items:
            event = self._parse_event(item, now)
            if event:
                events.append(event)

        self._consecutive_errors = 0
        self._last_error = None
        return events

    def _looks_like_event(self, item: dict) -> bool:
        """Check if dict looks like a 511 event."""
        return bool(
            item.get("id") or item.get("EventId") or item.get("event_id")
        )

    def _parse_event(self, item: dict, now: float) -> dict:
        """Parse a single 511 event.

        Args:
            item: Event dict from API
            now: Current timestamp

        Returns:
            Normalized event dict or None
        """
        try:
            # Try various ID field names
            event_id = (
                item.get("id") or
                item.get("EventId") or
                item.get("event_id") or
                item.get("ID") or
                str(hash(str(item)))[:12]
            )

            # Try various type field names
            event_type = (
                item.get("EventType") or
                item.get("event_type") or
                item.get("type") or
                item.get("Type") or
                item.get("category") or
                "Road Event"
            )

            # Try various road name fields
            roadway = (
                item.get("RoadwayName") or
                item.get("roadway_name") or
                item.get("roadway") or
                item.get("Roadway") or
                item.get("road") or
                item.get("route") or
                ""
            )

            # Try various description fields
            description = (
                item.get("Description") or
                item.get("description") or
                item.get("message") or
                item.get("Message") or
                item.get("details") or
                ""
            )

            # Try various location fields
            lat = (
                item.get("Latitude") or
                item.get("latitude") or
                item.get("lat") or
                item.get("StartLatitude") or
                None
            )
            lon = (
                item.get("Longitude") or
                item.get("longitude") or
                item.get("lon") or
                item.get("lng") or
                item.get("StartLongitude") or
                None
            )

            # Try to get coordinates from nested location object
            if lat is None and "location" in item:
                loc = item["location"]
                if isinstance(loc, dict):
                    lat = loc.get("latitude") or loc.get("lat")
                    lon = loc.get("longitude") or loc.get("lon") or loc.get("lng")

            # Check closure status
            is_closure = (
                item.get("IsFullClosure") or
                item.get("is_full_closure") or
                item.get("fullClosure") or
                item.get("closed") or
                "closure" in str(event_type).lower() or
                "closed" in str(description).lower()
            )

            # Determine severity
            if is_closure:
                severity = "priority"
            elif "construction" in str(event_type).lower():
                severity = "routine"
            elif "incident" in str(event_type).lower():
                severity = "routine"
            else:
                severity = "routine"

            # Format headline
            if roadway and description:
                headline = f"{roadway}: {description[:100]}"
            elif roadway:
                headline = f"{roadway}: {event_type}"
            elif description:
                headline = description[:120]
            else:
                headline = f"{event_type}"

            # Try to get timestamp for expiry
            last_updated = (
                item.get("LastUpdated") or
                item.get("last_updated") or
                item.get("updated") or
                item.get("timestamp") or
                None
            )

            # Default 6 hour TTL, refreshed every tick
            expires = now + 21600

            event = {
                "source": "511",
                "event_id": f"511_{event_id}",
                "event_type": event_type,
                "headline": headline,
                "description": description[:500] if description else "",
                "severity": severity,
                "lat": float(lat) if lat is not None else None,
                "lon": float(lon) if lon is not None else None,
                "expires": expires,
                "fetched_at": now,
                "properties": {
                    "roadway": roadway,
                    "is_closure": bool(is_closure),
                    "last_updated": last_updated,
                },
            }

            return event

        except Exception as e:
            logger.debug(f"511 event parse error: {e} - item: {item}")
            return None

    def to_event(self, evt: dict) -> Optional["Event"]:
        """Translate a stored 511 road event dict into a pipeline Event.

        Args:
            evt: Internal event dict from get_events()

        Returns:
            Event instance ready for EventBus emission, or None if the
            dict is missing required fields (lat/lon or event_id).
        """
        try:
            lat = evt.get("lat")
            lon = evt.get("lon")
            if lat is None or lon is None:
                return None  # Can't make a useful Event without coords

            event_id = evt.get("event_id")
            if not event_id:
                return None  # No stable identity to group/inhibit on

            props = evt.get("properties", {}) or {}
            severity = evt.get("severity", "routine")
            title = evt.get("headline", "") or evt.get("event_type", "") or "Road Event"

            # Richer summary: closure status, roadway, description
            summary_parts = [title]
            if props.get("is_closure"):
                summary_parts.append("road closed")
            roadway = props.get("roadway")
            if roadway and str(roadway) not in title:
                summary_parts.append(str(roadway))
            desc = evt.get("description")
            if desc and desc not in title:
                summary_parts.append(desc)
            summary = " | ".join(summary_parts)[:300]

            # The stored event_id is already the stable "511_{id}" key. Re-polls
            # of the same incident coalesce on this group_key; using it as the
            # sole inhibit_key lets the pipeline Inhibitor suppress
            # lower-severity re-emissions while a higher-severity one is active
            # for the same incident (severity tiering delegated to Inhibitor).

            # Canonical data for the Phase-2 formatter (incident path).
            # Native 511 adapter has a description-based format; road/direction
            # are parsed best-effort from the headline/description.
            _roadway = props.get("roadway")
            _desc = evt.get("description", "") or ""
            _is_closure = bool(props.get("is_closure"))
            canonical_data = {
                "external_id":    None,   # native adapter, no Central dedup
                "source":         "511",
                "sub_type":       "road_closed" if _is_closure else "incident",
                "road":           _roadway or None,
                "direction":      None,   # not structured in native 511 feed
                "from_loc":       None,
                "to_loc":         None,
                "mile_start":     None,
                "mile_end":       None,
                "mile_marker":    None,
                "lanes_affected": None,
                "cause":          None,
                "comment":        _desc[:200] if _desc else title,
                "impact":         "all lanes closed" if _is_closure else None,
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
                "icon_category":  "road_closed" if _is_closure else "incident",
            }

            return make_event(
                source="511",
                category="road_closure",
                severity=severity,
                title=title,
                summary=summary,
                timestamp=evt.get("fetched_at"),
                expires=evt.get("expires"),
                lat=lat,
                lon=lon,
                group_key=event_id,
                inhibit_keys=[event_id],
                data=canonical_data,
            )
        except Exception:
            logger.exception(f"511 to_event failed for evt: {evt.get('event_id')}")
            return None

    def get_events(self) -> list:
        """Get current road events."""
        return self._events

    @property
    def health_status(self) -> dict:
        """Get adapter health status."""
        return {
            "source": "511",
            "is_loaded": self._is_loaded,
            "last_error": str(self._last_error) if self._last_error else None,
            "consecutive_errors": self._consecutive_errors,
            "event_count": len(self._events),
            "last_fetch": self._last_tick,
            "auth_failed": self._auth_failed,
        }
