"""USGS Water Services stream gauge adapter.

# TODO: Migrate to api.waterdata.usgs.gov OGC API before Q1 2027
# Legacy waterservices.usgs.gov will be decommissioned.
# See: https://www.usgs.gov/tools/usgs-water-data-apis
"""

import json
import logging
import time
from typing import TYPE_CHECKING
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlencode

if TYPE_CHECKING:
    from ..config import USGSConfig

logger = logging.getLogger(__name__)

# Minimum tick interval per USGS guidelines (do not fetch same data more than hourly)
MIN_TICK_SECONDS = 900  # 15 minutes


class USGSStreamsAdapter:
    """USGS instantaneous values for stream gauge readings."""

    BASE_URL = "https://waterservices.usgs.gov/nwis/iv/"

    def __init__(self, config: "USGSConfig"):
        self._sites = config.sites or []
        self._tick_interval = max(config.tick_seconds or 900, MIN_TICK_SECONDS)
        self._flood_thresholds = getattr(config, "flood_thresholds", {}) or {}
        self._last_tick = 0.0
        self._events = []
        self._consecutive_errors = 0
        self._last_error = None
        self._is_loaded = False

        if self._tick_interval < MIN_TICK_SECONDS:
            logger.warning(
                f"USGS tick_seconds {config.tick_seconds} below minimum, using {MIN_TICK_SECONDS}"
            )

    def tick(self) -> bool:
        """Execute one polling tick.

        Returns:
            True if data changed
        """
        now = time.time()

        # No sites configured
        if not self._sites:
            return False

        # Check tick interval
        if now - self._last_tick < self._tick_interval:
            return False

        self._last_tick = now
        return self._fetch()

    def _fetch(self) -> bool:
        """Fetch instantaneous values from USGS Water Services.

        Returns:
            True if data changed
        """
        params = {
            "format": "json",
            "sites": ",".join(self._sites),
            "parameterCd": "00060,00065",  # Streamflow (cfs) and Gage height (ft)
            "siteStatus": "active",
        }

        url = f"{self.BASE_URL}?{urlencode(params)}"

        headers = {
            "User-Agent": "MeshAI/1.0 (stream gauge monitoring)",
            "Accept": "application/json",
        }

        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))

        except HTTPError as e:
            logger.warning(f"USGS HTTP error: {e.code}")
            self._last_error = f"HTTP {e.code}"
            self._consecutive_errors += 1
            return False

        except URLError as e:
            logger.warning(f"USGS connection error: {e.reason}")
            self._last_error = str(e.reason)
            self._consecutive_errors += 1
            return False

        except Exception as e:
            logger.warning(f"USGS fetch error: {e}")
            self._last_error = str(e)
            self._consecutive_errors += 1
            return False

        # Parse response
        new_events = []
        now = time.time()

        try:
            time_series = data.get("value", {}).get("timeSeries", [])

            for ts in time_series:
                source_info = ts.get("sourceInfo", {})
                variable = ts.get("variable", {})
                values_list = ts.get("values", [])

                # Extract site info
                site_name = source_info.get("siteName", "Unknown Site")
                site_codes = source_info.get("siteCode", [])
                site_id = site_codes[0].get("value", "") if site_codes else ""

                # Extract location
                geo_loc = source_info.get("geoLocation", {}).get("geogLocation", {})
                lat = geo_loc.get("latitude")
                lon = geo_loc.get("longitude")

                # Extract variable info
                var_name = variable.get("variableName", "Unknown")
                unit_info = variable.get("unit", {})
                unit_code = unit_info.get("unitCode", "")

                # Determine parameter type
                if "Streamflow" in var_name or "00060" in str(variable.get("variableCode", [])):
                    param_type = "flow"
                    param_name = "Streamflow"
                elif "Gage height" in var_name or "00065" in str(variable.get("variableCode", [])):
                    param_type = "height"
                    param_name = "Gage height"
                else:
                    param_type = "other"
                    param_name = var_name

                # Get current value (most recent)
                if not values_list or not values_list[0].get("value"):
                    continue

                value_entries = values_list[0].get("value", [])
                if not value_entries:
                    continue

                latest = value_entries[-1]
                value_str = latest.get("value", "")
                timestamp_str = latest.get("dateTime", "")

                try:
                    value = float(value_str)
                except (ValueError, TypeError):
                    continue

                # Check flood threshold
                severity = "info"
                threshold = self._flood_thresholds.get(site_id, {}).get(param_type)
                if threshold and value > threshold:
                    severity = "warning"

                # Format headline
                if param_type == "flow":
                    headline = f"{site_name}: {value:,.0f} {unit_code}"
                else:
                    headline = f"{site_name}: {value:.1f} {unit_code}"

                event = {
                    "source": "usgs",
                    "event_id": f"{site_id}_{param_type}",
                    "event_type": "Stream Gauge",
                    "headline": headline,
                    "severity": severity,
                    "lat": lat,
                    "lon": lon,
                    "expires": now + 1800,  # 30 min TTL
                    "fetched_at": now,
                    "properties": {
                        "site_id": site_id,
                        "site_name": site_name,
                        "parameter": param_name,
                        "value": value,
                        "unit": unit_code,
                        "timestamp": timestamp_str,
                    },
                }

                new_events.append(event)

        except Exception as e:
            logger.warning(f"USGS parse error: {e}")
            self._last_error = f"Parse error: {e}"
            self._consecutive_errors += 1
            return False

        # Check if data changed
        old_ids = {e["event_id"] for e in self._events}
        new_ids = {e["event_id"] for e in new_events}
        changed = old_ids != new_ids or len(self._events) != len(new_events)

        self._events = new_events
        self._consecutive_errors = 0
        self._last_error = None
        self._is_loaded = True

        if changed:
            logger.info(f"USGS streams updated: {len(new_events)} readings from {len(self._sites)} sites")

        return changed

    def get_events(self) -> list:
        """Get current stream gauge events."""
        return self._events

    @property
    def health_status(self) -> dict:
        """Get adapter health status."""
        return {
            "source": "usgs",
            "is_loaded": self._is_loaded,
            "last_error": str(self._last_error) if self._last_error else None,
            "consecutive_errors": self._consecutive_errors,
            "event_count": len(self._events),
            "last_fetch": self._last_tick,
            "site_count": len(self._sites),
        }
