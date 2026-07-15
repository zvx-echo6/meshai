"""NWS Active Alerts adapter."""

import json
import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from meshai.notifications.events import Event, make_event

if TYPE_CHECKING:
    from ..config import NWSConfig

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.weather.gov/alerts/active"


def _cfg_str(config, attr: str, default: str) -> str:
    """Read a string config field, falling back to `default` if absent,
    empty, or not a real string (e.g. an unconfigured test mock)."""
    value = getattr(config, attr, None)
    return value if isinstance(value, str) and value else default


class NWSAlertsAdapter:
    """NWS Active Alerts -- polls api.weather.gov"""

    def __init__(self, config: "NWSConfig", coverage: dict = None):
        if coverage is not None:
            derived_areas = coverage["areas"]
            if not derived_areas:
                # bbox overlaps no state — keep config areas so the API call
                # remains well-formed. (The pipeline coverage gate now does the
                # geometry filtering; this path is only a fetch-scope hint.)
                logger.debug(
                    "NWS coverage: derived area list is empty (bbox outside all states); "
                    "falling back to config areas for API query"
                )
                self._areas = config.areas or ["ID"]
            else:
                self._areas = derived_areas
        else:
            self._areas = config.areas or ["ID"]
        self._base_url = _cfg_str(config, "base_url", DEFAULT_BASE_URL)
        self._user_agent = config.user_agent or "(meshai, ops@example.com)"
        self._severity_min = config.severity_min or "moderate"
        self._tick_interval = config.tick_seconds or 60
        self._last_tick = 0.0
        self._events = []
        self._consecutive_errors = 0
        self._last_error = None
        self._backoff_until = 0.0
        self._is_loaded = False


    def _map_nws_severity(self, nws_severity: str) -> str:
        """Map NWS severity to 3-level system."""
        if nws_severity == "extreme":
            return "immediate"
        elif nws_severity in ("severe", "warning"):
            return "priority"
        else:  # moderate, minor, unknown
            return "routine"

    def _derive_category(self, event_type: str) -> str:
        """Derive notification category from NWS event type suffix.

        NWS event types like "Red Flag Warning", "Winter Storm Watch",
        "Wind Advisory" map to our fine-grained weather categories.

        Args:
            event_type: NWS event type string (e.g., "Tornado Warning")

        Returns:
            Category key: weather_warning, weather_watch, weather_advisory,
            or weather_statement
        """
        event_type_lower = event_type.lower()
        if event_type_lower.endswith("warning"):
            return "weather_warning"
        elif event_type_lower.endswith("watch"):
            return "weather_watch"
        elif event_type_lower.endswith("advisory"):
            return "weather_advisory"
        else:
            # Covers "Special Weather Statement", "Short Term Forecast", etc.
            return "weather_statement"

    def to_event(self, raw: dict) -> Event:
        """Convert internal event dict to pipeline Event.

        Phase-2: emits canonical event.data schema so the formatter+gater
        architecture (formatters/nws.py, gating/nws.py) can operate on
        native events identically to Central-sourced events.

        Args:
            raw: Internal event dict from get_events()

        Returns:
            Event instance ready for EventBus emission
        """
        event_type = raw.get("event_type", "Unknown")
        category = self._derive_category(event_type)
        nws_severity = raw.get("severity", "unknown")
        severity = self._map_nws_severity(nws_severity)

        # Build group_key for dedup: same alert ID should merge
        group_key = raw.get("event_id", "")

        # Build inhibit_keys: a Warning supersedes Watch/Advisory for same hazard
        inhibit_keys = []
        if category == "weather_warning":
            # Warning inhibits corresponding Watch/Advisory
            base = event_type.rsplit(" ", 1)[0] if " " in event_type else event_type
            inhibit_keys = [f"nws:{base} Watch", f"nws:{base} Advisory"]

        area_desc = raw.get("area_desc", "")

        # Build canonical data dict for the formatter+gater.
        # Keys match the Central-bridge canonical schema so both paths render
        # identically.  Native has no geocoder enrichment (city/state=None);
        # county falls back to areaDesc, mirroring the handler's:
        #     county = d.get("areaDesc") or ge.get("county")
        canonical = {
            "cap_id": raw.get("cap_id") or raw.get("event_id", ""),
            "event": event_type,
            "same_code": raw.get("same_code", ""),
            "cap_severity": raw.get("cap_severity") or raw.get("severity", "Unknown"),
            "certainty": raw.get("certainty", ""),
            "expires_at": raw.get("expires_at") or raw.get("expires") or None,
            "area_desc": area_desc,
            "geocoder": {
                "city": None,
                "county": area_desc,  # areaDesc as county fallback (no enrichment)
                "state": None,
            },
            "description": raw.get("description", ""),  # FULL — not truncated
            "parameters": raw.get("parameters") or {},
            "msgType": raw.get("msgType") or raw.get("messageType", "Alert"),
            "references": raw.get("references") or [],
            "category": category,
            "headline": raw.get("headline", ""),
            # RAW GeoJSON alert geometry (Polygon/MultiPolygon/None). Read by
            # the pipeline coverage gate as Event.data["geometry"].
            "geometry": raw.get("geometry"),
        }

        return make_event(
            source="nws",
            category=category,
            severity=severity,
            title=raw.get("headline", event_type),
            summary=raw.get("headline", ""),
            body=raw.get("description", ""),
            effective=raw.get("onset") or None,
            expires=raw.get("expires") or None,
            lat=raw.get("lat"),
            lon=raw.get("lon"),
            nws_zones=raw.get("areas", []),
            group_key=group_key,
            inhibit_keys=inhibit_keys,
            data=canonical,
        )

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
        url = f"{self._base_url}?area={areas}"

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
                "description": props.get("description") or "",  # FULL — not truncated
                "onset": onset,
                "expires": expires,
                "areas": props.get("geocode", {}).get("UGC", []),
                "area_desc": props.get("areaDesc", ""),
                "fetched_at": time.time(),
                # ── Canonical schema fields (Phase-2) ────────────────────────
                # These are read by to_event() to build the canonical data dict
                # for the formatter+gater architecture.
                "cap_id": props.get("id", ""),
                "same_code": ((props.get("eventCode") or {}).get("SAME") or [""])[0],
                "cap_severity": props.get("severity", "Unknown"),
                "certainty": props.get("certainty", "Unknown"),
                "expires_at": expires,
                "parameters": props.get("parameters") or {},
                "msgType": props.get("messageType", "Alert"),
                "references": props.get("references") or [],
            }

            # Attach the RAW GeoJSON alert geometry (Polygon / MultiPolygon /
            # None) — the authoritative field the coverage gate intersects
            # against configured areas. Zone-only NWS alerts have geometry=None;
            # resolve those from affectedZones (cached zone shapes) so
            # in-coverage zone alerts get placed + region-tagged instead of
            # dropped by the fail-closed gate.
            geom = feature.get("geometry")
            if geom is None:
                zurls = props.get("affectedZones") or []
                if zurls:
                    from meshai.env.nws_zones import resolve_zones_geometry
                    resolved = resolve_zones_geometry(zurls)
                    if resolved:
                        geom = resolved
            event["geometry"] = geom
            event["affected_zones"] = props.get("affectedZones", [])

            # Compute a best-effort centroid (fallback / nice-to-have; the
            # geometry above is authoritative). Handle Polygon and MultiPolygon.
            if geom and geom.get("coordinates"):
                try:
                    coords = geom["coordinates"]
                    gtype = geom.get("type")
                    ring = None
                    if gtype == "Polygon" and coords:
                        # Outer ring of the polygon
                        ring = coords[0]
                    elif gtype == "MultiPolygon" and coords:
                        # Outer ring of the first polygon
                        ring = coords[0][0]
                    if ring:
                        lat_sum = sum(c[1] for c in ring)
                        lon_sum = sum(c[0] for c in ring)
                        event["lat"] = lat_sum / len(ring)
                        event["lon"] = lon_sum / len(ring)
                    elif geom.get("type") == "MultiPolygon" and coords:
                        # Average all outer-ring vertices across all polygons
                        all_lats: list[float] = []
                        all_lons: list[float] = []
                        for poly_coords in coords:
                            if poly_coords:
                                ring = poly_coords[0]
                                all_lats.extend(c[1] for c in ring)
                                all_lons.extend(c[0] for c in ring)
                        if all_lats:
                            event["lat"] = sum(all_lats) / len(all_lats)
                            event["lon"] = sum(all_lons) / len(all_lons)
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
