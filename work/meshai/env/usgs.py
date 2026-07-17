"""USGS Water Services stream gauge adapter with NWS flood stage auto-lookup.

# TODO: Migrate to api.waterdata.usgs.gov OGC API before Q1 2027
# Legacy waterservices.usgs.gov will be decommissioned.
# See: https://www.usgs.gov/tools/usgs-water-data-apis
"""

import json
import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlencode

from meshai.notifications.events import Event, make_event

if TYPE_CHECKING:
    from ..config import USGSConfig

logger = logging.getLogger(__name__)

# Minimum tick interval per USGS guidelines (do not fetch same data more than hourly)
MIN_TICK_SECONDS = 900  # 15 minutes

# Cache for NWS flood stages (rarely change)
_nwps_cache: dict[str, dict] = {}
_nwps_cache_time: dict[str, float] = {}
NWPS_CACHE_TTL = 86400 * 7  # 7 days


def _cfg_str(config, attr: str, default: str) -> str:
    """Read a string config field, falling back to `default` if absent,
    empty, or not a real string (e.g. an unconfigured test mock)."""
    value = getattr(config, attr, None)
    return value if isinstance(value, str) and value else default


# Maps the flood_status strings _fetch() sets (see severity-classification
# block above) onto the ranked threshold_state vocabulary the shared hydro
# gating/formatter modules speak (meshai.central.idaho_gauge_sites.
# THRESHOLD_RANK / meshai.notifications.gating.hydro / formatters.hydro).
# Kept local rather than importing from central.idaho_gauge_sites so this
# adapter has no dependency on the (Central-only, reference-only) central
# package -- only the string vocabulary is shared, not the code.
_FLOOD_STATUS_TO_THRESHOLD_STATE = {
    "Action Stage": "action",
    "Minor Flood": "flood_minor",
    "Moderate Flood": "flood_moderate",
    "Major Flood": "flood_major",
}


def _parse_iso_epoch(s: Optional[str]) -> Optional[int]:
    """Parse a USGS instantaneous-values dateTime string to an epoch int.

    USGS IV timestamps carry a UTC offset (e.g. "2026-06-04T12:00:00.000-06:00");
    accept a trailing "Z" too for robustness. Returns None on any parse failure.
    """
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


class USGSStreamsAdapter:
    """USGS instantaneous values for stream gauge readings with NWS flood stages."""

    BASE_URL = "https://waterservices.usgs.gov/nwis/iv/"
    NWPS_BASE_URL = "https://api.water.noaa.gov/nwps/v1/gauges"
    SITE_INFO_URL = "https://waterservices.usgs.gov/nwis/site/"

    def __init__(self, config: "USGSConfig", coverage: dict = None):
        self._base_url = _cfg_str(config, "base_url", self.BASE_URL)
        self._nwps_base_url = _cfg_str(config, "nwps_base_url", self.NWPS_BASE_URL)
        self._site_info_url = _cfg_str(config, "site_info_url", self.SITE_INFO_URL)
        self._sites = config.sites or []
        self._coverage_bbox = coverage["bbox"] if coverage is not None else None
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

        # No sites configured and no coverage bbox to discover by
        if not self._sites and self._coverage_bbox is None:
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
        url = f"{self._nwps_base_url}/{nws_gauge_id}"
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
        url = f"{self._site_info_url}?format=rdb&sites={usgs_site_id}&siteOutput=expanded"

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
        url = f"{self._site_info_url}?{urlencode(params)}"

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

    def _build_iv_params(self, site_ids: list) -> dict:
        """Build USGS IV API params — bBox-first when coverage is configured.

        When a coverage bbox is set, the query uses USGS IV bBox discovery
        (west,south,east,north) and ignores preset site IDs. When no bbox is
        configured, falls back to an explicit sites= list. Shared between
        _fetch() and tests so the param logic is directly verifiable.
        """
        if self._coverage_bbox is not None:
            west, south, east, north = self._coverage_bbox
            width = east - west
            height = north - south
            if width * height > 25:
                logger.warning(
                    "USGS bBox %s too large (>25 sq-deg); readings may be rejected",
                    self._coverage_bbox,
                )
            return {
                "format": "json",
                "bBox": ",".join(str(c) for c in self._coverage_bbox),
                "parameterCd": "00060,00065",  # Streamflow (cfs) and Gage height (ft)
                "siteStatus": "active",
            }
        return {
            "format": "json",
            "sites": ",".join(site_ids),
            "parameterCd": "00060,00065",  # Streamflow (cfs) and Gage height (ft)
            "siteStatus": "active",
        }

    def _fetch(self) -> bool:
        """Fetch instantaneous values from USGS Water Services.

        Returns:
            True if data changed
        """
        site_ids = self._get_site_ids()
        if not site_ids and self._coverage_bbox is None:
            return False

        params = self._build_iv_params(site_ids)

        url = f"{self._base_url}?{urlencode(params)}"

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
                severity = "routine"
                flood_status = None

                if param_type == "height" and nwps_stages:
                    major = nwps_stages.get("major_flood_stage")
                    moderate = nwps_stages.get("moderate_flood_stage")
                    minor = nwps_stages.get("flood_stage")
                    action = nwps_stages.get("action_stage")

                    if major and value >= major:
                        severity = "immediate"
                        flood_status = "Major Flood"
                    elif moderate and value >= moderate:
                        severity = "priority"
                        flood_status = "Moderate Flood"
                    elif minor and value >= minor:
                        severity = "priority"
                        flood_status = "Minor Flood"
                    elif action and value >= action:
                        severity = "routine"
                        flood_status = "Action Stage"

                # Fall back to legacy manual thresholds
                if severity == "routine":
                    threshold = self._flood_thresholds.get(site_id, {}).get(param_type)
                    if threshold and value > threshold:
                        severity = "priority"

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
            if self._coverage_bbox is not None:
                logger.info("USGS streams updated: %d readings (bbox query)", len(new_events))
            else:
                logger.info("USGS streams updated: %d readings from %d sites",
                            len(new_events), len(site_ids))

        return changed

    def to_event(self, evt: dict) -> Optional["Event"]:
        """Translate a stored USGS gauge reading into a pipeline Event.

        Only elevated readings are emitted: the category is chosen from
        flood_status, so a routine (below-action-stage) reading -- which has
        no flood_status -- is intentionally NOT emitted (returns None).

        Args:
            evt: Internal event dict from get_events()

        Returns:
            Event instance ready for EventBus emission, or None if the dict
            is missing lat/lon or event_id, or the reading is not elevated.
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
            flood_status = props.get("flood_status")
            if not flood_status:
                return None  # routine reading -- not actionable, do not emit

            # Category from flood_status: an exceeded stage is a flood warning;
            # "Action Stage" (approaching) is high water.
            if "Flood" in str(flood_status):
                category = "stream_flood_warning"
            else:  # "Action Stage"
                category = "stream_high_water"

            severity = evt.get("severity", "routine")
            title = evt.get("headline", "") or props.get("site_name") or "Stream Gauge"

            # Summary: reading value/unit and the flood status
            summary_parts = [title]
            value = props.get("value")
            unit = props.get("unit")
            if value is not None:
                summary_parts.append(f"{value} {unit}".strip())
            summary_parts.append(str(flood_status))
            summary = " | ".join(summary_parts)[:300]

            # Canonical hydro schema (event.data) — same shape the Central nwis
            # path writes (site_id, gauge_name, stage_ft, flow_cfs, unit,
            # threshold_state, reading_time, lat, lon, parameter_code), so the
            # shared gating.hydro.decide() / formatters.hydro.format() work
            # unmodified for both sources. flood_status is only ever computed
            # for gage-height (00065) readings above, so a reading that reaches
            # this point is always a stage reading -- parameter_code is fixed
            # to "00065" and value maps to stage_ft (never flow_cfs; the
            # companion discharge reading, if any, arrives as its own separate
            # evt with no flood_status and never reaches to_event()).
            reading_time = _parse_iso_epoch(props.get("timestamp"))
            if reading_time is None:
                fetched_at = evt.get("fetched_at")
                reading_time = int(fetched_at) if isinstance(fetched_at, (int, float)) else int(time.time())
            data = {
                "site_id": props.get("site_id"),
                "gauge_name": props.get("site_name") or title,
                "stage_ft": value,
                "flow_cfs": None,
                "unit": unit,
                "threshold_state": _FLOOD_STATUS_TO_THRESHOLD_STATE.get(str(flood_status), "normal"),
                "reading_time": reading_time,
                "lat": lat,
                "lon": lon,
                "parameter_code": "00065",
            }

            # event_id is already the stable "{site_id}_{param}" key. Re-polls of
            # the same gauge/parameter coalesce on this group_key; using it as the
            # sole inhibit_key lets the pipeline Inhibitor suppress lower-severity
            # re-emissions while a higher-severity one is active (severity tiering
            # delegated to the Inhibitor).
            return make_event(
                source="usgs",
                category=category,
                severity=severity,
                title=title,
                summary=summary,
                timestamp=evt.get("fetched_at"),
                expires=evt.get("expires"),
                lat=lat,
                lon=lon,
                group_key=event_id,
                inhibit_keys=[event_id],
                data=data,
            )
        except Exception:
            logger.exception(f"USGS to_event failed for evt: {evt.get('event_id')}")
            return None

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
