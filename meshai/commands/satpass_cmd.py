"""!satpass command — on-demand satellite pass predictions.

Three forms:
    !satpass            → default satellites from adapter_config
    !satpass <name|id>  → fuzzy name match or exact NORAD ID
    !satpass <zip>      → 5-digit ZIP code → ZCTA centroid as observer

Observer location chain:
    1. Requester node GPS position (from connector's node cache)
    2. ZIP code argument → ZCTA centroid
    3. Else: reply asking for "!satpass <zip>"

Reply: DM to requester only, max 3 messages, lines formatted:
    ISS 09:36–09:43 MDT max 64° SW→NE
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from .base import CommandContext, CommandHandler

logger = logging.getLogger(__name__)

# Mountain time for display
_TZ = ZoneInfo("America/Boise")

# Max messages per reply
_MAX_MESSAGES = 3
# Max characters per message (LoRa budget)
_MAX_CHARS = 175


class SatpassCommand(CommandHandler):
    """On-demand satellite pass predictions."""

    name = "satpass"
    description = "Satellite pass predictions"
    usage = "!satpass [name|norad_id|zip]"

    async def execute(self, args: str, context: CommandContext) -> str:
        args = args.strip()

        # Determine observer location
        obs_lat, obs_lon = None, None
        zip_used = None

        # Check if args is a 5-digit ZIP code
        zip_match = re.match(r"^(\d{5})$", args)
        if zip_match:
            zip_code = zip_match.group(1)
            centroid = _lookup_zip(zip_code)
            if centroid is not None:
                obs_lat, obs_lon = centroid
                zip_used = zip_code
                args = ""  # consumed the arg
            else:
                # Not a valid ZIP — might be a NORAD ID, fall through
                zip_match = None

        # Try requester's GPS position
        if obs_lat is None and context.position:
            obs_lat, obs_lon = context.position

        # If still no location, check if the arg itself is a zip
        if obs_lat is None and not args:
            return "No GPS position available. Try: !satpass <zip>"

        # Determine which satellites to predict
        norad_ids = None
        sat_name_query = None

        if args:
            # Check if it's a NORAD ID (all digits; 5-digit OK if ZIP failed)
            if args.isdigit():
                norad_ids = [int(args)]
            else:
                sat_name_query = args

        # Default satellites from config
        if norad_ids is None and sat_name_query is None:
            try:
                from meshai.adapter_config import adapter_config
                cfg_ids = getattr(adapter_config.satpass, "command_norad_ids", None)
                if cfg_ids:
                    import json
                    if isinstance(cfg_ids, str):
                        cfg_ids = json.loads(cfg_ids)
                    if isinstance(cfg_ids, list) and cfg_ids:
                        norad_ids = [int(x) for x in cfg_ids]
            except Exception:
                pass
            if not norad_ids:
                norad_ids = [25544]  # ISS default

        # Get TLEs
        try:
            from meshai.persistence import get_db
            conn = get_db()
        except Exception:
            return "Database unavailable."

        tles = []
        if norad_ids:
            from meshai.central.tle_handler import get_tle_by_norad
            for nid in norad_ids:
                tle = get_tle_by_norad(nid, conn=conn)
                if tle:
                    tles.append(tle)
            if not tles:
                id_str = ", ".join(str(n) for n in norad_ids)
                return f"No fresh TLE for NORAD {id_str}. TLE cache may be empty."
        elif sat_name_query:
            from meshai.central.tle_handler import search_tle_by_name
            # Try exact NORAD ID first
            try:
                exact_id = int(sat_name_query)
                from meshai.central.tle_handler import get_tle_by_norad
                tle = get_tle_by_norad(exact_id, conn=conn)
                if tle:
                    tles = [tle]
            except ValueError:
                pass

            if not tles:
                results = search_tle_by_name(sat_name_query, conn=conn, limit=5)
                if not results:
                    return f"No satellite matching '{sat_name_query}' in TLE cache."
                if len(results) == 1:
                    tles = results
                else:
                    # Multiple matches — list them
                    names = [f"{r['name']} ({r['norad_id']})" for r in results]
                    return f"Multiple matches: {', '.join(names)}"

        if not tles:
            return "No TLE data available."

        # Compute passes for each satellite
        try:
            from meshai.central.pass_predictor import compute_passes, azimuth_to_compass
        except ImportError:
            return "Pass predictor not available (sgp4 missing?)."

        all_lines = []
        for tle in tles:
            try:
                passes = compute_passes(
                    tle["line1"], tle["line2"],
                    obs_lat, obs_lon,
                    window_h=24, min_el=10.0,
                )
            except Exception as e:
                logger.exception("satpass: compute failed for %s", tle["name"])
                all_lines.append(f"{tle['name']}: prediction error")
                continue

            if not passes:
                all_lines.append(f"{tle['name']}: no passes in 24h")
                continue

            for p in passes:
                aos_local = p.aos_time.astimezone(_TZ)
                los_local = p.los_time.astimezone(_TZ)
                tz_abbr = aos_local.strftime("%Z")
                aos_str = aos_local.strftime("%H:%M")
                los_str = los_local.strftime("%H:%M")
                az_aos = azimuth_to_compass(p.azimuth_at_aos)
                az_los = azimuth_to_compass(p.azimuth_at_los)
                line = (f"{tle['name']} {aos_str}\u2013{los_str} {tz_abbr} "
                        f"max {int(p.max_elevation)}\u00B0 "
                        f"{az_aos}\u2192{az_los}")
                all_lines.append(line)

        if not all_lines:
            return "No passes found in the next 24 hours."

        # Format into max 3 messages
        return _format_reply(all_lines)


def _format_reply(lines: list[str]) -> str:
    """Format pass lines into a reply respecting message limits.

    Returns a single string. The connector/dispatcher will chunk it
    into multiple messages if needed.
    """
    if not lines:
        return "No passes found."

    # Join all lines; the connector handles chunking
    return "\n".join(lines)


def _lookup_zip(zip_code: str) -> Optional[tuple[float, float]]:
    """Look up a ZIP code in the vendored ZCTA centroid CSV.

    Returns (lat, lon) or None if not found.
    """
    global _ZCTA_CACHE
    if _ZCTA_CACHE is None:
        _ZCTA_CACHE = _load_zcta()
    return _ZCTA_CACHE.get(zip_code)


_ZCTA_CACHE: Optional[dict[str, tuple[float, float]]] = None


def _load_zcta() -> dict[str, tuple[float, float]]:
    """Load the vendored ZCTA centroid CSV into memory."""
    import csv
    import os

    # Look for the CSV relative to the meshai package
    candidates = [
        os.path.join(os.path.dirname(__file__), "..", "data", "zcta_centroids.csv"),
        "/app/meshai/data/zcta_centroids.csv",
    ]

    for path in candidates:
        path = os.path.normpath(path)
        if os.path.exists(path):
            result = {}
            with open(path, "r") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    zcta = row.get("zcta", "").strip()
                    lat = row.get("lat", "").strip()
                    lon = row.get("lon", "").strip()
                    if zcta and lat and lon:
                        try:
                            result[zcta] = (float(lat), float(lon))
                        except ValueError:
                            continue
            logger.info("satpass: loaded %d ZCTA centroids from %s", len(result), path)
            return result

    logger.warning("satpass: zcta_centroids.csv not found")
    return {}
