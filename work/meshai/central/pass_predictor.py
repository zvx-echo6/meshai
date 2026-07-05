"""SGP4-based satellite pass predictor.

Propagates a satellite at 30-second steps, converts ECI positions to
topocentric look angles (elevation/azimuth), and groups contiguous
above-horizon samples into discrete passes.

The ECI→topocentric conversion is implemented locally because sgp4
only provides ECI (TEME) position vectors.

Coordinate transform pipeline:
    1. SGP4 → satellite position in TEME (True Equator Mean Equinox) km
    2. Observer geodetic (lat, lon, alt) → ECEF position
    3. ECEF → TEME using GMST rotation
    4. Topocentric vector = sat_teme - obs_teme
    5. Rotate to SEZ (South-East-Zenith) local frame
    6. Elevation = arctan(Z / sqrt(S² + E²))
    7. Azimuth = arctan2(E, -S)  (clockwise from north)
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

from sgp4.api import Satrec, jday

# WGS-84 constants
_A_EARTH_KM = 6378.137       # equatorial radius
_F_EARTH = 1.0 / 298.257223563  # flattening
_E2 = 2 * _F_EARTH - _F_EARTH ** 2  # eccentricity squared
_TWOPI = 2 * math.pi
_DEG2RAD = math.pi / 180.0
_RAD2DEG = 180.0 / math.pi

# Propagation step size (seconds)
_STEP_S = 30


@dataclass
class PassInfo:
    """A single satellite pass over the observer."""
    aos_time: datetime       # Acquisition of Signal (rise above min_el)
    los_time: datetime       # Loss of Signal (drop below min_el)
    peak_time: datetime      # Time of maximum elevation
    max_elevation: float     # Degrees
    azimuth_at_aos: float    # Degrees, clockwise from north
    azimuth_at_los: float    # Degrees, clockwise from north
    azimuth_at_peak: float   # Degrees, clockwise from north (at max elevation)


def compute_passes(line1: str, line2: str,
                   obs_lat: float, obs_lon: float,
                   obs_alt_m: float = 0.0,
                   window_h: int = 24,
                   min_el: float = 10.0,
                   now: Optional[datetime] = None) -> list[PassInfo]:
    """Compute satellite passes visible from an observer location.

    Args:
        line1, line2: TLE lines
        obs_lat, obs_lon: Observer geodetic coordinates (degrees)
        obs_alt_m: Observer altitude above WGS-84 ellipsoid (meters)
        window_h: Prediction window in hours
        min_el: Minimum elevation to consider (degrees)
        now: Start time (default: UTC now)

    Returns:
        List of PassInfo sorted by AOS time.
    """
    sat = Satrec.twoline2rv(line1, line2)

    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    end = now + timedelta(hours=window_h)

    # Observer ECEF → TEME helper (computed once per GMST, but GMST changes
    # each step — we recompute per step for accuracy)
    obs_lat_rad = obs_lat * _DEG2RAD
    obs_lon_rad = obs_lon * _DEG2RAD
    obs_alt_km = obs_alt_m / 1000.0

    # Pre-compute observer ECEF (doesn't change with time)
    obs_ecef = _geodetic_to_ecef(obs_lat_rad, obs_lon_rad, obs_alt_km)

    # Propagate at _STEP_S intervals
    samples: list[tuple[datetime, float, float]] = []  # (time, el, az)
    t = now
    while t <= end:
        jd, fr = _datetime_to_jday(t)
        e, r, v = sat.sgp4(jd, fr)
        if e != 0:
            t += timedelta(seconds=_STEP_S)
            continue

        # r is TEME position in km
        gmst = _gmst(jd, fr)
        obs_teme = _ecef_to_teme(obs_ecef, gmst)

        # Topocentric vector in TEME
        dx = r[0] - obs_teme[0]
        dy = r[1] - obs_teme[1]
        dz = r[2] - obs_teme[2]

        # Rotate to SEZ (South-East-Zenith) at observer location
        el, az = _teme_to_look_angles(dx, dy, dz, obs_lat_rad, gmst + obs_lon_rad)

        samples.append((t, el * _RAD2DEG, az * _RAD2DEG))
        t += timedelta(seconds=_STEP_S)

    # Group contiguous above-min_el samples into passes
    passes: list[PassInfo] = []
    in_pass = False
    pass_samples: list[tuple[datetime, float, float]] = []

    for sample_time, el, az in samples:
        if el >= min_el:
            if not in_pass:
                in_pass = True
                pass_samples = []
            pass_samples.append((sample_time, el, az))
        else:
            if in_pass and pass_samples:
                passes.append(_build_pass(pass_samples))
                pass_samples = []
            in_pass = False

    # Close trailing pass
    if in_pass and pass_samples:
        passes.append(_build_pass(pass_samples))

    return sorted(passes, key=lambda p: p.aos_time)


def _build_pass(samples: list[tuple[datetime, float, float]]) -> PassInfo:
    """Build a PassInfo from a list of contiguous above-horizon samples."""
    peak_idx = max(range(len(samples)), key=lambda i: samples[i][1])
    return PassInfo(
        aos_time=samples[0][0],
        los_time=samples[-1][0],
        peak_time=samples[peak_idx][0],
        max_elevation=samples[peak_idx][1],
        azimuth_at_aos=samples[0][2] % 360,
        azimuth_at_los=samples[-1][2] % 360,
        azimuth_at_peak=samples[peak_idx][2] % 360,
    )


# ---------- coordinate transforms ----------------------------------------


def _geodetic_to_ecef(lat_rad: float, lon_rad: float, alt_km: float
                      ) -> tuple[float, float, float]:
    """WGS-84 geodetic (rad, rad, km) → ECEF (km)."""
    sin_lat = math.sin(lat_rad)
    cos_lat = math.cos(lat_rad)
    N = _A_EARTH_KM / math.sqrt(1 - _E2 * sin_lat ** 2)
    x = (N + alt_km) * cos_lat * math.cos(lon_rad)
    y = (N + alt_km) * cos_lat * math.sin(lon_rad)
    z = (N * (1 - _E2) + alt_km) * sin_lat
    return (x, y, z)


def _ecef_to_teme(ecef: tuple[float, float, float], gmst: float
                  ) -> tuple[float, float, float]:
    """Rotate ECEF → TEME by GMST (Earth rotation angle)."""
    cos_g = math.cos(gmst)
    sin_g = math.sin(gmst)
    x = cos_g * ecef[0] + sin_g * ecef[1]
    y = -sin_g * ecef[0] + cos_g * ecef[1]
    z = ecef[2]
    return (x, y, z)


def _teme_to_look_angles(dx: float, dy: float, dz: float,
                         obs_lat_rad: float, obs_theta: float
                         ) -> tuple[float, float]:
    """Convert TEME-frame topocentric vector to elevation and azimuth.

    obs_theta = GMST + observer_longitude (radians).
    Returns (elevation_rad, azimuth_rad) where azimuth is CW from north.
    """
    sin_lat = math.sin(obs_lat_rad)
    cos_lat = math.cos(obs_lat_rad)
    sin_theta = math.sin(obs_theta)
    cos_theta = math.cos(obs_theta)

    # Rotate topocentric TEME vector to SEZ (South, East, Zenith)
    top_s = (sin_lat * cos_theta * dx
             + sin_lat * sin_theta * dy
             - cos_lat * dz)
    top_e = (-sin_theta * dx + cos_theta * dy)
    top_z = (cos_lat * cos_theta * dx
             + cos_lat * sin_theta * dy
             + sin_lat * dz)

    range_sat = math.sqrt(top_s ** 2 + top_e ** 2 + top_z ** 2)
    if range_sat < 1e-6:
        return (0.0, 0.0)

    el = math.asin(top_z / range_sat)
    az = math.atan2(top_e, -top_s)
    if az < 0:
        az += _TWOPI

    return (el, az)


def _datetime_to_jday(dt: datetime) -> tuple[float, float]:
    """Convert datetime to Julian day + fraction for sgp4."""
    jd, fr = jday(dt.year, dt.month, dt.day,
                  dt.hour, dt.minute,
                  dt.second + dt.microsecond / 1e6)
    return jd, fr


def _gmst(jd: float, fr: float) -> float:
    """Greenwich Mean Sidereal Time in radians.

    Uses the IAU 1982 expression (same as SGP4's internal GSTIME).
    """
    # Julian centuries from J2000.0
    T = ((jd - 2451545.0) + fr) / 36525.0
    # GMST in seconds of time
    gmst_sec = (67310.54841
                + (876600.0 * 3600.0 + 8640184.812866) * T
                + 0.093104 * T ** 2
                - 6.2e-6 * T ** 3)
    # Convert to radians (86400 seconds per revolution)
    gmst_rad = (gmst_sec % 86400.0) / 86400.0 * _TWOPI
    if gmst_rad < 0:
        gmst_rad += _TWOPI
    return gmst_rad


def azimuth_to_compass(az_deg: float) -> str:
    """Convert azimuth in degrees to 8-point compass direction."""
    az = az_deg % 360
    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    idx = int((az + 22.5) / 45) % 8
    return dirs[idx]
