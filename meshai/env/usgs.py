"""USGS Water Services stream gauge adapter with NWS flood stage auto-lookup.

# TODO: Migrate to api.waterdata.usgs.gov OGC API before Q1 2027
# Legacy waterservices.usgs.gov will be decommissioned.
# See: https://www.usgs.gov/tools/usgs-water-data-apis
"""

import json
import logging
import time
from typing import TYPE_CHECKING, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlencode

if TYPE_CHECKING:
    from ..config import USGSConfig

logger = logging.getLogger(__name__)

# Minimum tick interval per USGS guidelines (do not fetch same data more than hourly)
MIN_TICK_SECONDS = 900  # 15 minutes

# Cache for NWS flood stages (rarely change)
_nwps_cache: dict[str, dict] = {}
_nwps_cache_time: dict[str, float] = {}
NWPS_CACHE_TTL = 86400 * 7  # 7 days


class USGSStreamsAdapter:
    """USGS instantaneous values for stream gauge readings with NWS flood stages."""

    BASE_URL = "https://waterservices.usgs.gov/nwis/iv/"
    NWPS_BASE_URL = "https://api.water.noaa.gov/nwps/v1/gauges"

    def __init__(self, config: "USGSConfig"):
        self._sites = config.sites or []
        self._tick_interval = max(config.tick_seconds or 900, MIN_TICK_SECONDS)
        self._flood_thresholds = getattr(config, "flood_thresholds", {}) or {}
        self._last_tick = 0.0
        self._events = []
        self._consecutive_errors = 0
        self._last_error = None
        self._is_loaded = False

        # Site metadata cache (name, flood stages from NWPS)
        self._site_metadata: dict[str, dict] = {}

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

    def _get_site_ids(self) -> list[str]:
        """Extract site IDs from config (handles both string and dict formats)."""
        site_ids = []
        for site in self._sites:
            if isinstance(site, str):
                site_ids.append(site)
            elif isinstance(site, dict):
                site_ids.append(site.get("id", ""))
            elif hasattr(site, "id"):
                site_ids.append(site.id)
        return [s for s in site_ids if s]

    def _lookup_nwps_stages(self, usgs_site_id: str) -> Optional[dict]:
        """Lookup flood stages from NWS National Water Prediction Service.

        The NWPS API uses NWS gauge IDs which may differ from USGS site IDs.
        We try a mapping lookup first, then fall back to direct lookup.

        Returns:
            dict with action_stage, flood_stage, moderate_flood_stage, major_flood_stage
            or None if not available
        """
        global _nwps_cache, _nwps_cache_time

        # Check cache
        now = time.time()
        if usgs_site_id in _nwps_cache:
            if now - _nwps_cache_time.get(usgs_site_id, 0) < NWPS_CACHE_TTL:
                return _nwps_cache[usgs_site_id]

        # Try to find NWS gauge ID from USGS site ID
        # First, query USGS site info to get the NWS ID crosswalk
        nws_gauge_id = self._usgs_to_nws_crosswalk(usgs_site_id)
        if not nws_gauge_id:
            # Fall back to using USGS ID directly (sometimes they match)
            nws_gauge_id = usgs_site_id

        # Query NWPS for flood stages
        url = f"{self.NWPS_BASE_URL}/{nws_gauge_id}"
        headers = {
            "User-Agent": "MeshAI/1.0 (stream gauge monitoring)",
            "Accept": "application/json",
        }

        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            # Extract flood stages
            stages = {}
            flood_info = data.get("flood", {})

            if "action" in flood_info:
                stages["action_stage"] = flood_info["action"].get("stage")
            if "minor" in flood_info:
                stages["flood_stage"] = flood_info["minor"].get("stage")
            if "moderate" in flood_info:
                stages["moderate_flood_stage"] = flood_info["moderate"].get("stage")
            if "major" in flood_info:
                stages["major_flood_stage"] = flood_info["major"].get("stage")

            # Also grab the official name if available
            stages["nws_name"] = data.get("name", "")
            stages["nws_gauge_id"] = nws_gauge_id

            # Cache result
            _nwps_cache[usgs_site_id] = stages
            _nwps_cache_time[usgs_site_id] = now

            logger.info(f"NWPS flood stages for {usgs_site_id}: {stages}")
            return stages

        except HTTPError as e:
            if e.code == 404:
                # No NWPS data for this gauge - cache the miss
                _nwps_cache[usgs_site_id] = {}
                _nwps_cache_time[usgs_site_id] = now
                logger.debug(f"No NWPS data for gauge {usgs_site_id}")
            else:
                logger.warning(f"NWPS lookup failed for {usgs_site_id}: HTTP {e.code}")
            return None

        except Exception as e:
            logger.warning(f"NWPS lookup error for {usgs_site_id}: {e}")
            return None

    def _usgs_to_nws_crosswalk(self, usgs_site_id: str) -> Optional[str]:
        """Try to find NWS gauge ID from USGS site ID.

        The USGS provides a crosswalk in their site metadata, but it's not
        always populated. This is a best-effort lookup.
        """
        # Try USGS site service for metadata including NWS ID
        url = f"https://waterservices.usgs.gov/nwis/site/?format=rdb&sites={usgs_site_id}&siteOutput=expanded"

        try:
            req = Request(url, headers={"User-Agent": "MeshAI/1.0"})
            with urlopen(req, timeout=10) as resp:
                content = resp.read().decode("utf-8")

            # Parse RDB format - look for NWS ID in the data
            # This is a simplified parser; full implementation would be more robust
            for line in content.split("\n"):
                if line.startswith(usgs_site_id):
                    # NWS station ID is typically in column ~30ish
                    # This varies by USGS response format
                    pass

        except Exception:
            pass

        return None

    def lookup_site(self, site_id: str) -> dict:
        """Lookup site metadata for config UI auto-populate.

        Returns:
            {
                "site_id": "13090500",
                "name": "Snake River nr Twin Falls ID",
                "lat": 42.xxx,
                "lon": -114.xxx,
                "flood_stages": {
                    "action_stage": 9.0,
                    "flood_stage": 10.5,
                    "moderate_flood_stage": 12.0,
                    "major_flood_stage": 14.0,
                } or None
            }
        """
        result = {"site_id": site_id, "name": None, "lat": None, "lon": None, "flood_stages": None}

        # Get USGS site info
        params = {
            "format": "json",
            "sites": site_id,
            "siteOutput": "expanded",
        }
        url = f"https://waterservices.usgs.gov/nwis/site/?{urlencode(params)}"

        try:
            req = Request(url, headers={"User-Agent": "MeshAI/1.0", "Accept": "application/json"})
            with urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            sites = data.get("value", {}).get("timeSeries", [])
            if not sites:
                # Try alternate format
                sites_list = data.get("value", {}).get("sites", [])
                if sites_list:
                    site_info = sites_list[0]
                    result["name"] = site_info.get("siteName", "")
                    result["lat"] = site_info.get("geoLocation", {}).get("geogLocation", {}).get("latitude")
                    result["lon"] = site_info.get("geoLocation", {}).get("geogLocation", {}).get("longitude")

        except Exception as e:
            logger.warning(f"USGS site lookup failed for {site_id}: {e}")

        # Get NWS flood stages
        stages = self._lookup_nwps_stages(site_id)
        if stages:
            result["flood_stages"] = {
                "action_stage": stages.get("action_stage"),
                "flood_stage": stages.get("flood_stage"),
                "moderate_flood_stage": stages.get("moderate_flood_stage"),
                "major_flood_stage": stages.get("major_flood_stage"),
            }
            if stages.get("nws_name") and not result["name"]:
                result["name"] = stages["nws_name"]

        return result

    def _fetch(self) -> bool:
        """Fetch instantaneous values from USGS Water Services.

        Returns:
            True if data changed
        """
        site_ids = self._get_site_ids()
        if not site_ids:
            return False

        params = {
            "format": "json",
            "sites": ",".join(site_ids),
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

                # Cache site name
                if site_id and site_id not in self._site_metadata:
                    self._site_metadata[site_id] = {"name": site_name}

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

                # Get flood stages for this site
                nwps_stages = self._lookup_nwps_stages(site_id)

                # Determine severity based on flood stages (for gage height)
                severity = "info"
                flood_status = None

                if param_type == "height" and nwps_stages:
                    major = nwps_stages.get("major_flood_stage")
                    moderate = nwps_stages.get("moderate_flood_stage")
                    minor = nwps_stages.get("flood_stage")
                    action = nwps_stages.get("action_stage")

                    if major and value >= major:
                        severity = "critical"
                        flood_status = "Major Flood"
                    elif moderate and value >= moderate:
                        severity = "warning"
                        flood_status = "Moderate Flood"
                    elif minor and value >= minor:
                        severity = "warning"
                        flood_status = "Minor Flood"
                    elif action and value >= action:
                        severity = "advisory"
                        flood_status = "Action Stage"

                # Fall back to legacy manual thresholds
                if severity == "info":
                    threshold = self._flood_thresholds.get(site_id, {}).get(param_type)
                    if threshold and value > threshold:
                        severity = "warning"

                # Format headline
                if param_type == "flow":
                    headline = f"{site_name}: {value:,.0f} {unit_code}"
                else:
                    headline = f"{site_name}: {value:.1f} {unit_code}"

                if flood_status:
                    headline += f" — {flood_status}"

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
                        "flood_status": flood_status,
                        "flood_stages": nwps_stages if nwps_stages else None,
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
            logger.info(f"USGS streams updated: {len(new_events)} readings from {len(site_ids)} sites")

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
            "site_count": len(self._get_site_ids()),
        }
