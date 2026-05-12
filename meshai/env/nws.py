"""NWS Active Alerts adapter."""

import json
import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

if TYPE_CHECKING:
    from ..config import NWSConfig

logger = logging.getLogger(__name__)


class NWSAlertsAdapter:
    """NWS Active Alerts -- polls api.weather.gov"""

    def __init__(self, config: "NWSConfig"):
        self._areas = config.areas or ["ID"]
        self._user_agent = config.user_agent or "(meshai, ops@example.com)"
        self._severity_min = config.severity_min or "moderate"
        self._tick_interval = config.tick_seconds or 60
        self._last_tick = 0.0
        self._events = []
        self._consecutive_errors = 0
        self._last_error = None
        self._backoff_until = 0.0
        self._is_loaded = False

    def tick(self) -> bool:
        """Execute one polling tick.

        Returns:
            True if data changed
        """
        now = time.time()

        # Rate limit backoff
        if now < self._backoff_until:
            return False

        # Check tick interval
        if now - self._last_tick < self._tick_interval:
            return False

        self._last_tick = now
        return self._fetch()

    def _fetch(self) -> bool:
        """Fetch alerts from NWS API.

        Returns:
            True if data changed
        """
        areas = ",".join(self._areas)
        url = f"https://api.weather.gov/alerts/active?area={areas}"

        headers = {
            "User-Agent": self._user_agent,
            "Accept": "application/geo+json",
        }

        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))

        except HTTPError as e:
            if e.code == 429:
                self._backoff_until = time.time() + 5
                logger.warning("NWS rate limited, backing off 5s")
            else:
                logger.warning(f"NWS HTTP error: {e.code}")
            self._last_error = f"HTTP {e.code}"
            self._consecutive_errors += 1
            return False

        except URLError as e:
            logger.warning(f"NWS connection error: {e.reason}")
            self._last_error = str(e.reason)
            self._consecutive_errors += 1
            return False

        except Exception as e:
            logger.warning(f"NWS fetch error: {e}")
            self._last_error = str(e)
            self._consecutive_errors += 1
            return False

        # Parse response
        features = data.get("features", [])
        new_events = []

        # Severity levels for filtering
        severity_levels = ["unknown", "minor", "moderate", "severe", "extreme"]
        try:
            min_idx = severity_levels.index(self._severity_min.lower())
        except ValueError:
            min_idx = 2  # default to moderate

        for feature in features:
            props = feature.get("properties", {})

            # Severity filtering
            severity = (props.get("severity") or "Unknown").lower()
            try:
                sev_idx = severity_levels.index(severity)
            except ValueError:
                sev_idx = 0

            if sev_idx < min_idx:
                continue

            # Parse timestamps
            onset = self._parse_iso(props.get("onset"))
            expires = self._parse_iso(props.get("expires"))

            event = {
                "source": "nws",
                "event_id": props.get("id", ""),
                "event_type": props.get("event", "Unknown"),
                "severity": severity,
                "headline": props.get("headline", ""),
                "description": (props.get("description") or "")[:500],
                "onset": onset,
                "expires": expires,
                "areas": props.get("geocode", {}).get("UGC", []),
                "area_desc": props.get("areaDesc", ""),
                "fetched_at": time.time(),
            }

            # Try to get centroid from geometry
            geom = feature.get("geometry")
            if geom and geom.get("coordinates"):
                try:
                    coords = geom["coordinates"]
                    if geom.get("type") == "Polygon" and coords:
                        # Compute centroid of first ring
                        ring = coords[0]
                        lat_sum = sum(c[1] for c in ring)
                        lon_sum = sum(c[0] for c in ring)
                        event["lat"] = lat_sum / len(ring)
                        event["lon"] = lon_sum / len(ring)
                except Exception:
                    pass

            new_events.append(event)

        # Check if data changed
        old_ids = {e["event_id"] for e in self._events}
        new_ids = {e["event_id"] for e in new_events}
        changed = old_ids != new_ids

        self._events = new_events
        self._consecutive_errors = 0
        self._last_error = None
        self._is_loaded = True

        if changed:
            logger.info(f"NWS alerts updated: {len(new_events)} active")

        return changed

    def _parse_iso(self, iso_str: str) -> float:
        """Parse ISO timestamp to epoch float."""
        if not iso_str:
            return 0.0
        try:
            # Handle various ISO formats
            if iso_str.endswith("Z"):
                iso_str = iso_str[:-1] + "+00:00"
            dt = datetime.fromisoformat(iso_str)
            return dt.timestamp()
        except Exception:
            return 0.0

    def get_events(self) -> list:
        """Get current events."""
        return self._events

    @property
    def health_status(self) -> dict:
        """Get adapter health status."""
        return {
            "source": "nws",
            "is_loaded": self._is_loaded,
            "last_error": str(self._last_error) if self._last_error else None,
            "consecutive_errors": self._consecutive_errors,
            "event_count": len(self._events),
            "last_fetch": self._last_tick,
        }
