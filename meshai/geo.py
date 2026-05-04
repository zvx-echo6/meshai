"""Geographic utilities for mesh clustering and naming."""

import logging
import math
from typing import Optional

logger = logging.getLogger(__name__)

# Earth radius in miles
EARTH_RADIUS_MILES = 3958.8

# Idaho/regional city lookup table for auto-naming
# Format: (lat, lon, city_name)
CITY_LOOKUP = [
    # Major Idaho cities
    (43.6150, -116.2023, "Boise"),
    (42.5558, -114.4701, "Twin Falls"),
    (43.4926, -114.3514, "Sun Valley"),
    (43.5263, -114.2742, "Ketchum"),
    (43.4666, -114.4110, "Hailey"),
    (43.8231, -111.7924, "Idaho Falls"),
    (42.8713, -112.4455, "Pocatello"),
    (46.7324, -117.0002, "Moscow"),
    (46.4165, -117.0012, "Lewiston"),
    (47.6777, -116.7805, "Coeur d'Alene"),
    (43.5826, -116.5635, "Nampa"),
    (43.5907, -116.3915, "Meridian"),
    (43.6629, -116.6874, "Caldwell"),
    (42.7257, -114.5178, "Jerome"),
    (42.5616, -113.7631, "Burley"),
    (42.1087, -113.8830, "Oakley"),
    (43.0766, -115.6932, "Mountain Home"),
    (44.0682, -114.9311, "Cascade"),
    (44.3761, -115.5606, "McCall"),
    (43.3493, -116.0553, "Kuna"),
    (43.3246, -115.9937, "Melba"),
    (43.1279, -115.6911, "Glenns Ferry"),
    (42.9088, -115.2598, "Gooding"),
    (42.7314, -114.8668, "Wendell"),
    (42.5554, -114.0782, "Rupert"),
    (42.5516, -113.5557, "Paul"),
    (42.7863, -115.0057, "Shoshone"),
    (43.1407, -114.4088, "Fairfield"),
    (43.9624, -116.5536, "Emmett"),
    (44.5429, -116.0489, "Donnelly"),

    # Oregon border
    (43.9404, -117.0264, "Ontario"),
    (44.3793, -117.2291, "Weiser"),

    # Utah border
    (42.0097, -111.9391, "Preston"),
    (42.1141, -112.0265, "Franklin"),

    # Nevada border
    (41.9942, -114.0836, "Jackpot"),

    # Montana border
    (46.8721, -114.9992, "Missoula"),

    # Wyoming border
    (43.4799, -110.7624, "Jackson"),
    (43.8554, -111.2227, "Driggs"),
    (43.7233, -111.1018, "Victor"),
]


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance between two GPS coordinates in miles.

    Args:
        lat1, lon1: First coordinate
        lat2, lon2: Second coordinate

    Returns:
        Distance in miles
    """
    # Convert to radians
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)

    # Haversine formula
    a = (math.sin(delta_lat / 2) ** 2 +
         math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lon / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    return EARTH_RADIUS_MILES * c


def nearest_city(lat: float, lon: float) -> tuple[str, float]:
    """Find the nearest city to a GPS coordinate.

    Args:
        lat: Latitude
        lon: Longitude

    Returns:
        Tuple of (city_name, distance_in_miles)
    """
    if not CITY_LOOKUP:
        return ("Unknown", 0.0)

    nearest = None
    min_dist = float("inf")

    for city_lat, city_lon, city_name in CITY_LOOKUP:
        dist = haversine_distance(lat, lon, city_lat, city_lon)
        if dist < min_dist:
            min_dist = dist
            nearest = city_name

    return (nearest or "Unknown", min_dist)


def cluster_by_distance(
    nodes: list[dict],
    radius_miles: float,
    lat_key: str = "latitude",
    lon_key: str = "longitude",
    id_key: str = "id",
) -> list[list[dict]]:
    """Cluster nodes by GPS proximity using simple agglomerative clustering.

    Args:
        nodes: List of node dicts with lat/lon coordinates
        radius_miles: Maximum distance to be in same cluster
        lat_key: Key for latitude in node dict
        lon_key: Key for longitude in node dict
        id_key: Key for node ID

    Returns:
        List of clusters, each cluster is a list of nodes
    """
    # Filter to nodes with valid GPS
    nodes_with_gps = []
    for node in nodes:
        lat = node.get(lat_key)
        lon = node.get(lon_key)
        if lat is not None and lon is not None and lat != 0 and lon != 0:
            nodes_with_gps.append(node)

    if not nodes_with_gps:
        return []

    # Track cluster assignments
    # Each node starts in its own cluster
    clusters: list[set[str]] = [{node[id_key]} for node in nodes_with_gps]
    node_map = {node[id_key]: node for node in nodes_with_gps}

    # Merge clusters that are within radius
    changed = True
    while changed:
        changed = False
        i = 0
        while i < len(clusters):
            j = i + 1
            while j < len(clusters):
                # Check if any node in cluster i is within radius of any node in cluster j
                should_merge = False
                for id_a in clusters[i]:
                    if should_merge:
                        break
                    node_a = node_map[id_a]
                    lat_a = node_a[lat_key]
                    lon_a = node_a[lon_key]
                    for id_b in clusters[j]:
                        node_b = node_map[id_b]
                        lat_b = node_b[lat_key]
                        lon_b = node_b[lon_key]
                        dist = haversine_distance(lat_a, lon_a, lat_b, lon_b)
                        if dist <= radius_miles:
                            should_merge = True
                            break

                if should_merge:
                    # Merge cluster j into cluster i
                    clusters[i] = clusters[i].union(clusters[j])
                    clusters.pop(j)
                    changed = True
                else:
                    j += 1
            i += 1

    # Convert sets back to node lists
    result = []
    for cluster_ids in clusters:
        cluster_nodes = [node_map[nid] for nid in cluster_ids]
        result.append(cluster_nodes)

    return result


def get_cluster_center(
    nodes: list[dict],
    lat_key: str = "latitude",
    lon_key: str = "longitude",
) -> tuple[float, float]:
    """Calculate the geographic center of a cluster of nodes.

    Args:
        nodes: List of node dicts with lat/lon
        lat_key: Key for latitude
        lon_key: Key for longitude

    Returns:
        Tuple of (center_lat, center_lon)
    """
    if not nodes:
        return (0.0, 0.0)

    total_lat = 0.0
    total_lon = 0.0
    count = 0

    for node in nodes:
        lat = node.get(lat_key)
        lon = node.get(lon_key)
        if lat is not None and lon is not None:
            total_lat += lat
            total_lon += lon
            count += 1

    if count == 0:
        return (0.0, 0.0)

    return (total_lat / count, total_lon / count)


def suggest_cluster_name(
    nodes: list[dict],
    lat_key: str = "latitude",
    lon_key: str = "longitude",
) -> str:
    """Suggest a name for a cluster based on nearest city.

    Args:
        nodes: List of nodes in the cluster
        lat_key: Key for latitude
        lon_key: Key for longitude

    Returns:
        Suggested name (nearest city)
    """
    center_lat, center_lon = get_cluster_center(nodes, lat_key, lon_key)
    if center_lat == 0.0 and center_lon == 0.0:
        return "Unknown"

    city, distance = nearest_city(center_lat, center_lon)

    # If very close to city center, just use city name
    # If farther away, add qualifier
    if distance < 5:
        return city
    elif distance < 15:
        return f"Greater {city}"
    else:
        return f"{city} Area"


def assign_to_nearest_cluster(
    node: dict,
    clusters: list[list[dict]],
    lat_key: str = "latitude",
    lon_key: str = "longitude",
) -> Optional[int]:
    """Find which cluster a node should belong to based on distance.

    Args:
        node: Node dict with lat/lon
        clusters: List of clusters (each a list of nodes)
        lat_key: Key for latitude
        lon_key: Key for longitude

    Returns:
        Index of nearest cluster, or None if node has no GPS
    """
    node_lat = node.get(lat_key)
    node_lon = node.get(lon_key)

    if node_lat is None or node_lon is None or (node_lat == 0 and node_lon == 0):
        return None

    min_dist = float("inf")
    nearest_idx = None

    for i, cluster in enumerate(clusters):
        center_lat, center_lon = get_cluster_center(cluster, lat_key, lon_key)
        if center_lat == 0 and center_lon == 0:
            continue
        dist = haversine_distance(node_lat, node_lon, center_lat, center_lon)
        if dist < min_dist:
            min_dist = dist
            nearest_idx = i

    return nearest_idx
