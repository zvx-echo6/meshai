"""NOAA Space Weather Prediction Center adapter."""

import json
import logging
import time
from typing import TYPE_CHECKING, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from meshai.notifications.events import Event, make_event

if TYPE_CHECKING:
    from ..config import SWPCConfig

logger = logging.getLogger(__name__)


class SWPCAdapter:
    """NOAA Space Weather -- multi-endpoint with staggered ticks."""

    # Endpoint definitions: (url, interval_seconds)
    ENDPOINTS = {
        "scales": ("https://services.swpc.noaa.gov/products/noaa-scales.json", 300),
        "kp": ("https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json", 600),
        "alerts": ("https://services.swpc.noaa.gov/products/alerts.json", 120),
        "f107": ("https://services.swpc.noaa.gov/json/f107_cm_flux.json", 86400),
    }

    def __init__(self, config: "SWPCConfig"):
        self._last_tick = {}  # endpoint -> last_tick timestamp
        self._status = {}
        self._events = []
        self._consecutive_errors = 0
        self._last_error = None
        self._is_loaded = False

        # Initialize tick times to 0
        for endpoint in self.ENDPOINTS:
            self._last_tick[endpoint] = 0.0

    def tick(self) -> bool:
        """Execute one polling tick.

        Returns:
            True if data changed
        """
        changed = False
        now = time.time()

        for endpoint, (url, interval) in self.ENDPOINTS.items():
            if now - self._last_tick[endpoint] >= interval:
                self._last_tick[endpoint] = now
                if self._fetch_endpoint(endpoint, url):
                    changed = True

        if changed:
            self._update_events()

        return changed

    def _fetch_endpoint(self, endpoint: str, url: str) -> bool:
        """Fetch a single endpoint.

        Returns:
            True on success
        """
        headers = {
            "User-Agent": "MeshAI/1.0",
            "Accept": "application/json",
        }

        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))

        except HTTPError as e:
            logger.warning(f"SWPC {endpoint} HTTP error: {e.code}")
            self._last_error = f"{endpoint}: HTTP {e.code}"
            self._consecutive_errors += 1
            return False

        except URLError as e:
            logger.warning(f"SWPC {endpoint} connection error: {e.reason}")
            self._last_error = f"{endpoint}: {e.reason}"
            self._consecutive_errors += 1
            return False

        except Exception as e:
            logger.warning(f"SWPC {endpoint} error: {e}")
            self._last_error = f"{endpoint}: {e}"
            self._consecutive_errors += 1
            return False

        # Parse based on endpoint
        try:
            if endpoint == "scales":
                self._parse_scales(data)
            elif endpoint == "kp":
                self._parse_kp(data)
            elif endpoint == "alerts":
                self._parse_alerts(data)
            elif endpoint == "f107":
                self._parse_f107(data)

            self._consecutive_errors = 0
            self._last_error = None
            self._is_loaded = True
            return True

        except Exception as e:
            logger.warning(f"SWPC {endpoint} parse error: {e}")
            self._last_error = f"{endpoint}: parse error"
            return False

    def _parse_scales(self, data):
        """Parse noaa-scales.json.

        Data format: {""-1": {...}, "0": {...}, "1": {...}, ...}
        "0" is current.
        """
        current = data.get("0", {})

        r_data = current.get("R", {})
        s_data = current.get("S", {})
        g_data = current.get("G", {})

        # Handle empty string or None Scale values
        def parse_scale(val):
            if val is None or val == "":
                return 0
            try:
                return int(val)
            except (ValueError, TypeError):
                return 0

        self._status["r_scale"] = parse_scale(r_data.get("Scale"))
        self._status["s_scale"] = parse_scale(s_data.get("Scale"))
        self._status["g_scale"] = parse_scale(g_data.get("Scale"))

    def _parse_kp(self, data):
        """Parse noaa-planetary-k-index.json.

        Data format: array of objects with time_tag, Kp, a_running, station_count
        Last entry is most recent. Store full history for charting.
        """
        if not data:
            return

        # Store full history (last 24-48 hours of readings)
        kp_history = []
        for entry in data:
            if isinstance(entry, dict):
                try:
                    kp_history.append({
                        "time": entry.get("time_tag", ""),
                        "value": float(entry.get("Kp", 0)),
                    })
                except (ValueError, TypeError):
                    continue
            elif isinstance(entry, list) and len(entry) > 1:
                # Legacy array format fallback
                try:
                    kp_history.append({
                        "time": entry[0] if len(entry) > 0 else "",
                        "value": float(entry[1]),
                    })
                except (ValueError, TypeError):
                    continue

        self._status["kp_history"] = kp_history

        # Get last entry (most recent) for current value
        last_entry = data[-1]
        if isinstance(last_entry, dict):
            try:
                self._status["kp_current"] = float(last_entry.get("Kp", 0))
            except (ValueError, TypeError):
                pass
            self._status["kp_timestamp"] = last_entry.get("time_tag", "")
        elif isinstance(last_entry, list) and len(last_entry) > 1:
            # Legacy array format fallback
            try:
                self._status["kp_current"] = float(last_entry[1])
            except (ValueError, TypeError):
                pass
            if len(last_entry) > 0:
                self._status["kp_timestamp"] = last_entry[0]

    def _parse_alerts(self, data):
        """Parse alerts.json.

        Data format: array of objects with product_id, issue_datetime, message
        """
        warnings = []
        if isinstance(data, list):
            for alert in data[:5]:  # Keep most recent 5
                message = alert.get("message", "")
                # Extract first line as headline
                headline = message.split("\n")[0].strip()
                if headline:
                    warnings.append(headline)

        self._status["active_warnings"] = warnings

    def _parse_f107(self, data):
        """Parse f107_cm_flux.json.

        Data format: array of objects with time_tag, flux
        Store history for potential charting.
        """
        if not data:
            return

        # Store SFI history (last 30 days of readings)
        sfi_history = []
        if isinstance(data, list):
            for entry in data[-30:]:  # Last 30 entries
                if isinstance(entry, dict):
                    try:
                        sfi_history.append({
                            "time": entry.get("time_tag", ""),
                            "value": float(entry.get("flux", 0)),
                        })
                    except (ValueError, TypeError):
                        continue

        self._status["sfi_history"] = sfi_history

        # Get most recent entry (last in list)
        if isinstance(data, list) and data:
            last = data[-1]
            if isinstance(last, dict):
                try:
                    self._status["sfi"] = float(last.get("flux", 0))
                except (ValueError, TypeError):
                    pass

    def _update_events(self):
        """Generate events for active space weather conditions (R/S/G scales).

        One event per active NOAA scale (Radio blackout / Solar radiation /
        Geomagnetic storm) at level >= 1. The event_id is a STABLE
        "swpc_{scale}{level}" key (no timestamp), so a sustained condition
        dedups across ticks and only an escalation to a new level re-notifies.
        """
        self._events = []
        now = time.time()

        scale_defs = [
            ("r", "r_scale", "Radio Blackout"),
            ("s", "s_scale", "Solar Radiation Storm"),
            ("g", "g_scale", "Geomagnetic Storm"),
        ]

        for code, status_key, label in scale_defs:
            level = self._status.get(status_key, 0) or 0
            if level < 1:
                continue

            if level >= 5:
                severity = "immediate"
            elif level >= 3:
                severity = "priority"
            else:
                severity = "routine"

            scale_letter = code.upper()
            self._events.append({
                "source": "swpc",
                "event_id": f"swpc_{code}{level}",  # STABLE: scale+level, no timestamp
                "event_type": f"{scale_letter}{level} {label}",
                "scale": scale_letter,
                "level": level,
                "severity": severity,
                "headline": f"{scale_letter}{level} {label} in progress",
                "expires": now + 3600,  # 1hr TTL
                "areas": [],
                "fetched_at": now,
            })

    def to_event(self, evt: dict) -> Optional["Event"]:
        """Translate a stored SWPC scale condition into a pipeline Event.

        Category is chosen from the NOAA scale; severity (level-tiered) is
        passed through unchanged. SWPC conditions are global, so the Event
        carries no lat/lon and is tagged region="global".

        Args:
            evt: Internal event dict from get_events()

        Returns:
            Event instance ready for EventBus emission, or None if the dict is
            missing its scale/level (or level < 1) or event_id.
        """
        try:
            scale = evt.get("scale")
            if not scale:
                return None  # No scale discriminator

            level = evt.get("level")
            if level is None or level < 1:
                return None  # Quiet/baseline -- not actionable

            event_id = evt.get("event_id")
            if not event_id:
                return None  # No stable identity to group/inhibit on

            category = {
                "R": "rf_propagation_alert",
                "S": "solar_radiation_storm",
                "G": "geomagnetic_storm",
            }.get(scale)
            if category is None:
                return None  # Unknown scale

            severity = evt.get("severity", "routine")
            title = evt.get("headline") or evt.get("event_type") or f"{scale}{level} space weather"

            # event_id is the stable "swpc_{scale}{level}" key. A sustained
            # condition coalesces on this group_key (re-polls dedup); an
            # escalation to a higher level yields a new key and re-notifies.
            # Single inhibit key; severity tiering delegated to the Inhibitor.
            return make_event(
                source="swpc",
                category=category,
                severity=severity,
                title=title,
                summary=title,
                timestamp=evt.get("fetched_at"),
                expires=evt.get("expires"),
                lat=None,
                lon=None,
                region="global",
                group_key=event_id,
                inhibit_keys=[event_id],
            )
        except Exception:
            logger.exception(f"SWPC to_event failed for evt: {evt.get('event_id')}")
            return None

    def get_status(self) -> dict:
        """Get current SWPC status."""
        return self._status

    def get_events(self) -> list:
        """Get current alert events."""
        return self._events

    @property
    def health_status(self) -> dict:
        """Get adapter health status."""
        return {
            "source": "swpc",
            "is_loaded": self._is_loaded,
            "last_error": str(self._last_error) if self._last_error else None,
            "consecutive_errors": self._consecutive_errors,
            "event_count": len(self._events),
            "last_fetch": max(self._last_tick.values()) if self._last_tick else 0,
        }
