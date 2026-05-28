"""Tropospheric ducting assessment adapter using Open-Meteo GFS."""

import json
import logging
import math
import time
from datetime import datetime
from typing import TYPE_CHECKING, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from meshai.notifications.events import Event, make_event

if TYPE_CHECKING:
    from ..config import DuctingConfig

logger = logging.getLogger(__name__)


# Pressure levels and approximate heights (meters)
PRESSURE_LEVELS = {
    1000: 110,   # ~110m
    925: 760,    # ~760m
    850: 1500,   # ~1500m
    700: 3000,   # ~3000m
}


class DuctingAdapter:
    """Tropospheric ducting assessment from Open-Meteo GFS pressure levels."""

    def __init__(self, config: "DuctingConfig"):
        self._lat = config.latitude
        self._lon = config.longitude
        self._tick_interval = config.tick_seconds or 10800  # 3 hours
        self._last_tick = 0.0
        self._status = {}
        self._consecutive_errors = 0
        self._last_error = None
        self._is_loaded = False
        self._events = []
        self._last_tier = None

    def tick(self) -> bool:
        """Execute one polling tick.

        Returns:
            True if data changed
        """
        now = time.time()

        # Check tick interval
        if now - self._last_tick < self._tick_interval:
            return False

        self._last_tick = now
        return self._fetch()

    def _fetch(self) -> bool:
        """Fetch GFS data from Open-Meteo API.

        Returns:
            True on success
        """
        # Build API URL
        hourly_vars = [
            "temperature_1000hPa", "temperature_925hPa",
            "temperature_850hPa", "temperature_700hPa",
            "relative_humidity_1000hPa", "relative_humidity_925hPa",
            "relative_humidity_850hPa", "relative_humidity_700hPa",
            "surface_pressure",
        ]

        url = (
            f"https://api.open-meteo.com/v1/gfs"
            f"?latitude={self._lat}&longitude={self._lon}"
            f"&hourly={','.join(hourly_vars)}"
            f"&forecast_days=1&timezone=auto"
        )

        headers = {
            "User-Agent": "MeshAI/1.0",
            "Accept": "application/json",
        }

        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))

        except HTTPError as e:
            logger.warning(f"Ducting API HTTP error: {e.code}")
            self._last_error = f"HTTP {e.code}"
            self._consecutive_errors += 1
            return False

        except URLError as e:
            logger.warning(f"Ducting API connection error: {e.reason}")
            self._last_error = str(e.reason)
            self._consecutive_errors += 1
            return False

        except Exception as e:
            logger.warning(f"Ducting API error: {e}")
            self._last_error = str(e)
            self._consecutive_errors += 1
            return False

        # Parse response
        try:
            self._parse_response(data)
            self._consecutive_errors = 0
            self._last_error = None
            self._is_loaded = True
            logger.info(f"Ducting assessment updated: {self._status.get('condition', 'unknown')}")
            return True

        except Exception as e:
            logger.warning(f"Ducting parse error: {e}")
            self._last_error = f"parse error: {e}"
            return False

    def _parse_response(self, data):
        """Parse Open-Meteo response and compute ducting assessment."""
        hourly = data.get("hourly", {})
        times = hourly.get("time", [])

        if not times:
            raise ValueError("No time data in response")

        # Find index closest to current time
        now = datetime.now()
        idx = 0
        for i, t in enumerate(times):
            try:
                dt = datetime.fromisoformat(t)
                if dt <= now:
                    idx = i
            except Exception:
                pass

        # Extract values for current hour
        def get_val(key):
            vals = hourly.get(key, [])
            return vals[idx] if idx < len(vals) else None

        # Build profile for each pressure level
        profile = []
        gradients = []

        levels = sorted(PRESSURE_LEVELS.keys(), reverse=True)  # 1000, 925, 850, 700

        for i, pressure in enumerate(levels):
            temp_key = f"temperature_{pressure}hPa"
            rh_key = f"relative_humidity_{pressure}hPa"

            t_celsius = get_val(temp_key)
            rh = get_val(rh_key)

            if t_celsius is None or rh is None:
                continue

            height_m = PRESSURE_LEVELS[pressure]

            # Calculate radio refractivity N
            t_kelvin = t_celsius + 273.15

            # Saturation vapor pressure (Magnus formula)
            e_sat = 6.112 * math.exp(17.67 * t_celsius / (t_celsius + 243.5))
            # Actual vapor pressure
            e = (rh / 100.0) * e_sat

            # Radio refractivity
            n = 77.6 * (pressure / t_kelvin) + 3.73e5 * (e / t_kelvin**2)

            # Modified refractivity (accounts for Earth curvature)
            h_km = height_m / 1000.0
            m = n + 157.0 * h_km

            profile.append({
                "level_hPa": pressure,
                "height_m": height_m,
                "N": round(n, 1),
                "M": round(m, 1),
                "T_C": round(t_celsius, 1),
                "RH": round(rh, 1),
            })

        # Compute gradients between adjacent levels
        for i in range(len(profile) - 1):
            lower = profile[i]
            upper = profile[i + 1]

            dM = upper["M"] - lower["M"]
            dz = (upper["height_m"] - lower["height_m"]) / 1000.0  # km

            if dz > 0:
                gradient = dM / dz
                gradients.append({
                    "from_level": lower["level_hPa"],
                    "to_level": upper["level_hPa"],
                    "from_height_m": lower["height_m"],
                    "to_height_m": upper["height_m"],
                    "gradient": round(gradient, 1),
                })

        # Classify conditions based on minimum gradient
        # Standard atmosphere: ~118 M-units/km
        # Normal: > 79
        # Super-refraction: 0 to 79
        # Ducting: < 0 (negative = trapping layer)

        min_gradient = min((g["gradient"] for g in gradients), default=118)
        min_gradient_layer = None
        for g in gradients:
            if g["gradient"] == min_gradient:
                min_gradient_layer = g
                break

        if min_gradient < 0:
            # Ducting detected
            if min_gradient_layer and min_gradient_layer["from_level"] == 1000:
                condition = "surface_duct"
            else:
                condition = "elevated_duct"

            duct_base = min_gradient_layer["from_height_m"] if min_gradient_layer else 0
            duct_thickness = (
                min_gradient_layer["to_height_m"] - min_gradient_layer["from_height_m"]
                if min_gradient_layer else 0
            )
            assessment = "Ducting -- extended UHF range likely"

        elif min_gradient < 79:
            condition = "super_refraction"
            duct_base = None
            duct_thickness = None
            assessment = "Enhanced range possible"

        else:
            condition = "normal"
            duct_base = None
            duct_thickness = None
            assessment = "Normal propagation"

        # Update status
        self._status = {
            "condition": condition,
            "min_gradient": round(min_gradient, 1),
            "duct_thickness_m": duct_thickness,
            "duct_base_m": duct_base,
            "profile": profile,
            "gradients": gradients,
            "assessment": assessment,
            "last_update": times[idx] if idx < len(times) else None,
            "fetched_at": time.time(),
            "location": {
                "lat": self._lat,
                "lon": self._lon,
            },
        }

        self._update_events()

    # Tier model (enhancement strength, ascending):
    #   normal < super_refraction < duct < surface_duct
    _TIER_ORDER = ["normal", "super_refraction", "duct", "surface_duct"]
    # Gradient (M-units/km) at the top (more-enhanced side) of each tier:
    #   normal/super_refraction = 79 ; super_refraction/duct = 0 ; duct/surface_duct = -100
    _TIER_TOP_BOUNDARY = {0: 79.0, 1: 0.0, 2: -100.0}
    TIER_DEADBAND = 5.0  # M-units of hysteresis on gradient boundaries (anti-flap)

    _TIER_CATEGORY = {
        "super_refraction": "rf_anomalous_propagation",
        "duct": "rf_ducting_enhancement",
        "surface_duct": "rf_ducting_enhancement",
    }
    _TIER_SEVERITY = {
        "super_refraction": "routine",
        "duct": "priority",
        "surface_duct": "immediate",
    }
    _TIER_CODE = {
        "super_refraction": "superrefraction",
        "duct": "duct",
        "surface_duct": "surfaceduct",
    }
    _TIER_LABEL = {
        "super_refraction": "Super-refraction (enhanced range)",
        "duct": "Tropospheric ducting (elevated)",
        "surface_duct": "Tropospheric ducting (surface)",
    }

    def _raw_tier(self, min_gradient, condition) -> str:
        """Map the current min M-gradient + condition to a tier (no hysteresis)."""
        if min_gradient is None or min_gradient >= 79:
            return "normal"
        if min_gradient >= 0:
            return "super_refraction"
        # min_gradient < 0 -> ducting; strongest when it reaches the surface or is very steep
        if condition == "surface_duct" or min_gradient < -100:
            return "surface_duct"
        return "duct"

    def _apply_hysteresis(self, raw_tier, min_gradient, last_tier) -> str:
        """Commit a tier change only past a deadband, to avoid boundary flapping.

        The most-severe tier (surface/strong duct) is categorical (the duct
        reaches the ground) and is never held back or held onto by the
        gradient deadband -- it fires promptly and clears promptly.
        """
        if last_tier is None or raw_tier == last_tier or min_gradient is None:
            return raw_tier
        if raw_tier == "surface_duct" or last_tier == "surface_duct":
            return raw_tier
        idx_raw = self._TIER_ORDER.index(raw_tier)
        idx_last = self._TIER_ORDER.index(last_tier)
        if idx_raw > idx_last:
            # toward a more-enhanced tier (gradient dropped)
            boundary = self._TIER_TOP_BOUNDARY[idx_last]
            return raw_tier if min_gradient <= boundary - self.TIER_DEADBAND else last_tier
        # toward a less-enhanced tier (gradient rose)
        boundary = self._TIER_TOP_BOUNDARY[idx_last - 1]
        return raw_tier if min_gradient >= boundary + self.TIER_DEADBAND else last_tier

    def _update_events(self):
        """Derive the current propagation tier (with hysteresis) and stage events.

        Mirrors the SWPC pattern: get_events() returns the current notable tier
        every tick with a STABLE per-tier event_id, so the store dedups a
        sustained tier and only a tier change re-emits. A 'normal' tier stages
        nothing.
        """
        self._events = []
        if not self._status:
            return

        min_gradient = self._status.get("min_gradient")
        condition = self._status.get("condition")

        raw_tier = self._raw_tier(min_gradient, condition)
        tier = self._apply_hysteresis(raw_tier, min_gradient, self._last_tier)
        self._last_tier = tier
        self._status["tier"] = tier

        if tier == "normal":
            return  # no enhancement -- nothing to emit

        now = time.time()
        loc = f"{round(self._lat, 2)}_{round(self._lon, 2)}"
        label = self._TIER_LABEL[tier]
        assessment = self._status.get("assessment", "")
        headline = f"{label} -- {assessment}".strip(" -") if assessment else label

        self._events.append({
            "source": "ducting",
            "event_id": f"ducting_{self._TIER_CODE[tier]}_{loc}",  # STABLE per tier+location
            "event_type": label,
            "tier": tier,
            "severity": self._TIER_SEVERITY[tier],
            "headline": headline,
            "min_gradient": min_gradient,
            "duct_base_m": self._status.get("duct_base_m"),
            "duct_thickness_m": self._status.get("duct_thickness_m"),
            "surface_duct": (tier == "surface_duct"),
            "lat": self._lat,
            "lon": self._lon,
            "expires": now + 6 * 3600,  # outlast the 3h poll gap so a sustained tier dedups
            "fetched_at": now,
        })

    def get_events(self) -> list:
        """Get current propagation-enhancement events (tier-based)."""
        return self._events

    def to_event(self, evt: dict) -> Optional["Event"]:
        """Translate a stored ducting tier event into a pipeline Event.

        Category from tier; tier-mapped severity passed through. Ducting is
        geographic, so the Event carries the assessment location's lat/lon.
        The stable per-tier event_id is the group_key and sole inhibit_key;
        a tier escalation yields a new key and re-notifies.

        Args:
            evt: Internal event dict from get_events()

        Returns:
            Event instance, or None for a normal/missing tier, missing
            location, or missing gradient.
        """
        try:
            tier = evt.get("tier")
            if not tier or tier == "normal":
                return None  # no enhancement -- not actionable

            lat = evt.get("lat")
            lon = evt.get("lon")
            if lat is None or lon is None:
                return None  # missing location

            if evt.get("min_gradient") is None:
                return None  # missing the driving quantity

            event_id = evt.get("event_id")
            if not event_id:
                return None

            category = self._TIER_CATEGORY.get(tier)
            if category is None:
                return None  # unknown tier

            severity = evt.get("severity", "routine")
            title = evt.get("event_type") or "Tropospheric propagation enhancement"

            summary_parts = [title]
            mg = evt.get("min_gradient")
            if mg is not None:
                summary_parts.append(f"min M-gradient {mg}/km")
            base = evt.get("duct_base_m")
            thick = evt.get("duct_thickness_m")
            if base is not None and thick:
                summary_parts.append(f"duct base {int(base)} m, ~{int(thick)} m thick")
            if evt.get("surface_duct"):
                summary_parts.append("surface duct")
            summary = " | ".join(summary_parts)[:300]

            return make_event(
                source="ducting",
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
            )
        except Exception:
            logger.exception(f"Ducting to_event failed for evt: {evt.get('event_id')}")
            return None

    def get_status(self) -> dict:
        """Get current ducting status."""
        return self._status

    @property
    def health_status(self) -> dict:
        """Get adapter health status."""
        return {
            "source": "ducting",
            "is_loaded": self._is_loaded,
            "last_error": str(self._last_error) if self._last_error else None,
            "consecutive_errors": self._consecutive_errors,
            "event_count": 0,
            "last_fetch": self._last_tick,
        }
