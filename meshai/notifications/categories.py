"""Alert category registry.

Defines all alertable conditions with human-readable names and descriptions.
"""

ALERT_CATEGORIES = {
    # Infrastructure alerts
    "infra_offline": {
        "name": "Infrastructure Offline",
        "description": "An infrastructure node stopped responding",
        "default_severity": "warning",
    },
    "critical_node_down": {
        "name": "Critical Node Down",
        "description": "A node marked as critical went offline",
        "default_severity": "critical",
    },
    "infra_recovery": {
        "name": "Infrastructure Recovery",
        "description": "An infrastructure node came back online",
        "default_severity": "info",
    },
    "new_router": {
        "name": "New Router",
        "description": "A new router appeared on the mesh",
        "default_severity": "info",
    },

    # Power alerts
    "battery_warning": {
        "name": "Battery Warning",
        "description": "Infrastructure node battery below warning threshold",
        "default_severity": "warning",
    },
    "battery_critical": {
        "name": "Battery Critical",
        "description": "Infrastructure node battery below critical threshold",
        "default_severity": "critical",
    },
    "battery_emergency": {
        "name": "Battery Emergency",
        "description": "Infrastructure node battery critically low",
        "default_severity": "emergency",
    },
    "battery_trend": {
        "name": "Battery Declining",
        "description": "Battery showing declining trend over 7 days",
        "default_severity": "warning",
    },
    "power_source_change": {
        "name": "Power Source Change",
        "description": "Node switched from USB to battery (possible outage)",
        "default_severity": "warning",
    },
    "solar_not_charging": {
        "name": "Solar Not Charging",
        "description": "Solar panel not charging during daylight hours",
        "default_severity": "warning",
    },

    # Utilization alerts
    "sustained_high_util": {
        "name": "High Utilization",
        "description": "Channel utilization elevated for extended period",
        "default_severity": "warning",
    },
    "packet_flood": {
        "name": "Packet Flood",
        "description": "Node sending excessive packets",
        "default_severity": "warning",
    },

    # Coverage alerts
    "infra_single_gateway": {
        "name": "Single Gateway",
        "description": "Infrastructure node dropped to single gateway coverage",
        "default_severity": "warning",
    },
    "feeder_offline": {
        "name": "Feeder Offline",
        "description": "A feeder gateway stopped responding",
        "default_severity": "warning",
    },
    "region_total_blackout": {
        "name": "Region Blackout",
        "description": "All infrastructure in a region is offline",
        "default_severity": "emergency",
    },

    # Health score alerts
    "mesh_score_low": {
        "name": "Mesh Health Low",
        "description": "Overall mesh health score below threshold",
        "default_severity": "warning",
    },
    "region_score_low": {
        "name": "Region Health Low",
        "description": "A region's health score below threshold",
        "default_severity": "warning",
    },

    # Environmental alerts
    "weather_warning": {
        "name": "Severe Weather",
        "description": "NWS warning or advisory for mesh area",
        "default_severity": "warning",
    },
    "hf_blackout": {
        "name": "HF Radio Blackout",
        "description": "R3+ solar event degrading HF propagation",
        "default_severity": "warning",
    },
    "tropospheric_ducting": {
        "name": "Tropospheric Ducting",
        "description": "Atmospheric conditions extending VHF/UHF range",
        "default_severity": "info",
    },
    "wildfire_proximity": {
        "name": "Fire Near Mesh",
        "description": "Wildfire detected within configured distance",
        "default_severity": "warning",
    },
    "new_ignition": {
        "name": "New Fire Ignition",
        "description": "Satellite hotspot not matching any known fire",
        "default_severity": "warning",
    },
    "flood_warning": {
        "name": "Flood Warning",
        "description": "Stream gauge exceeds flood threshold",
        "default_severity": "warning",
    },
    "road_closure": {
        "name": "Road Closure",
        "description": "Full road closure on monitored corridor",
        "default_severity": "warning",
    },
}


def get_category(category_id: str) -> dict:
    """Get category info by ID, with fallback for unknown categories."""
    if category_id in ALERT_CATEGORIES:
        return ALERT_CATEGORIES[category_id]
    return {
        "name": category_id.replace("_", " ").title(),
        "description": f"Alert type: {category_id}",
        "default_severity": "info",
    }


def list_categories() -> list[dict]:
    """List all categories with their IDs."""
    return [
        {"id": cat_id, **cat_info}
        for cat_id, cat_info in ALERT_CATEGORIES.items()
    ]
