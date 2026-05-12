"""Tropospheric ducting assessment adapter using Open-Meteo GFS."""

import json
import logging
import math
import time
from datetime import datetime
from typing import TYPE_CHECKING
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

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
        self._lat = config.latitude or 42.56
        self._lon = config.longitude or -114.47
        self._tick_interval = config.tick_seconds or 10800  # 3 hours
        self._last_tick = 0.0
        self._status = {}
        self._consecutive_errors = 0
        self._last_error = None
        self._is_loaded = False

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
