"""Region tagger for mapping coordinates and NWS zones to regions.

This module provides functions to:
- Map lat/lon coordinates to the nearest configured region
- Map NWS zone codes to matching regions

Usage:
    from meshai.notifications.region_tagger import tag_by_coordinates, tag_by_nws_zone
    from meshai.config import RegionAnchor

    regions = [
        RegionAnchor(name="South Western ID", lat=43.615, lon=-116.2023,
                     nws_zones=["IDZ016", "IDZ030"]),
        RegionAnchor(name="Magic Valley", lat=42.5558, lon=-114.4701,
                     nws_zones=["IDZ031"]),
    ]

    # Find region by coordinates
    region = tag_by_coordinates(43.6, -116.2, regions)
    # Returns: "South Western ID"

    # Find regions by NWS zone
    regions = tag_by_nws_zone("IDZ016", regions)
    # Returns: ["South Western ID"]
"""

import math
from typing import Optional

# Import RegionAnchor type for annotations
# Actual import happens at function call time to avoid circular imports
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from meshai.config import RegionAnchor


# Earth radius in miles (mean radius)
EARTH_RADIUS_MILES = 3958.8


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate the great-circle distance between two points on Earth.

    Uses the haversine formula for accuracy on a spherical Earth model.

    Args:
        lat1: Latitude of first point in degrees
        lon1: Longitude of first point in degrees
        lat2: Latitude of second point in degrees
        lon2: Longitude of second point in degrees

    Returns:
        Distance in miles
    """
    # Convert to radians
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    lon1_rad = math.radians(lon1)
    lon2_rad = math.radians(lon2)

    # Differences
    dlat = lat2_rad - lat1_rad
    dlon = lon2_rad - lon1_rad

    # Haversine formula
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon / 2) ** 2
    c = 2 * math.asin(math.sqrt(a))

    return EARTH_RADIUS_MILES * c


def tag_by_coordinates(
    lat: float,
    lon: float,
    regions: list,  # list[RegionAnchor]
    radius_miles: float = 25.0,
) -> Optional[str]:
    """Return the name of the nearest region within radius_miles.

    Finds the closest region anchor to the given coordinates. If the
    closest anchor is within radius_miles, returns its name. Otherwise
    returns None.

    Args:
        lat: Latitude of the point to tag
        lon: Longitude of the point to tag
        regions: List of RegionAnchor objects to search
        radius_miles: Maximum distance to consider (default 25 miles)

    Returns:
        Name of the nearest region within range, or None if no match
    """
    if not regions:
        return None

    closest_region = None
    closest_distance = float("inf")

    for region in regions:
        # Skip regions without valid coordinates
        region_lat = getattr(region, "lat", None)
        region_lon = getattr(region, "lon", None)

        if region_lat is None or region_lon is None:
            continue
        if region_lat == 0.0 and region_lon == 0.0:
            # Treat (0, 0) as unset coordinates
            continue

        distance = haversine_distance(lat, lon, region_lat, region_lon)

        if distance < closest_distance:
            closest_distance = distance
            closest_region = region

    # Check if closest is within radius
    if closest_region is not None and closest_distance <= radius_miles:
        return getattr(closest_region, "name", None)

    return None


def tag_by_nws_zone(
    zone_code: str,
    regions: list,  # list[RegionAnchor]
) -> list[str]:
    """Return all region names whose nws_zones list contains zone_code.

    Multiple regions can match the same zone (a zone may span multiple
    configured regions).

    Args:
        zone_code: NWS zone code to match (e.g., "IDZ016")
        regions: List of RegionAnchor objects to search

    Returns:
        List of region names that contain this zone, empty if no matches
    """
    if not zone_code or not regions:
        return []

    # Normalize zone code to uppercase for case-insensitive matching
    zone_upper = zone_code.upper().strip()

    matching_regions = []

    for region in regions:
        region_zones = getattr(region, "nws_zones", None)
        if not region_zones:
            continue

        # Check if zone matches any in this region's list (case-insensitive)
        for rz in region_zones:
            if rz.upper().strip() == zone_upper:
                region_name = getattr(region, "name", None)
                if region_name:
                    matching_regions.append(region_name)
                break  # Don't add same region twice

    return matching_regions
