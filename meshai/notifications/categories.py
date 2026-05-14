"""Alert category registry.

Defines all alertable conditions with human-readable names, descriptions,
and example messages showing what users will receive.

Severity levels (military/intelligence precedence):
  routine   - Informational, no time pressure
  priority  - Needs attention soon
  immediate - Act now, drop everything

Toggle categories (for v0.3 notification routing):
  mesh_health    - infrastructure, power, utilization, coverage, health-score
  weather        - NWS-sourced alerts, stream flooding
  fire           - NIFC perimeters, FIRMS hotspots
  rf_propagation - solar, geomagnetic, ducting, band conditions
  roads          - 511, TomTom traffic
  avalanche      - avalanche advisories
  seismic        - USGS quakes (Phase 3)
  tracking       - ADS-B, AIS, satellite passes (Phase 7)
"""

from typing import Optional


# Valid toggle values for v0.3 pipeline
VALID_TOGGLES = frozenset({
    "mesh_health",
    "weather",
    "fire",
    "rf_propagation",
    "roads",
    "avalanche",
    "seismic",
    "tracking",
})


ALERT_CATEGORIES = {
    # Infrastructure alerts
    "infra_offline": {
        "name": "Infrastructure Node Offline",
        "description": "An infrastructure node (router/repeater) stopped responding",
        "default_severity": "priority",
        "example_message": "⚠ Infrastructure Offline: MHR — Mountain Harrison Rptr has not been heard for 2 hours",
        "toggle": "mesh_health",
    },
    "critical_node_down": {
        "name": "Critical Node Down",
        "description": "A node you marked as critical went offline",
        "default_severity": "immediate",
        "example_message": "🚨 Critical Node Down: HPR — Hayden Peak Rptr offline for 1 hour",
        "toggle": "mesh_health",
    },
    "infra_recovery": {
        "name": "Infrastructure Recovery",
        "description": "An offline infrastructure node came back online",
        "default_severity": "routine",
        "example_message": "✅ Recovery: MHR — Mountain Harrison Rptr back online after 2h outage",
        "toggle": "mesh_health",
    },
    "new_router": {
        "name": "New Router",
        "description": "A new router appeared on the mesh",
        "default_severity": "routine",
        "example_message": "📡 New Router: Snake River Relay appeared in Wood River Valley",
        "toggle": "mesh_health",
    },

    # Power alerts
    "battery_warning": {
        "name": "Battery Warning",
        "description": "Infrastructure node battery below 30% (3.60V)",
        "default_severity": "routine",
        "example_message": "🔋 Battery Warning: BLD-MTN at 28% (3.58V), solar not charging",
        "toggle": "mesh_health",
    },
    "battery_critical": {
        "name": "Battery Critical",
        "description": "Infrastructure node battery below 15% (3.50V)",
        "default_severity": "priority",
        "example_message": "🔋 Battery Critical: BLD-MTN at 12% (3.48V) — shutdown in hours",
        "toggle": "mesh_health",
    },
    "battery_emergency": {
        "name": "Battery Emergency",
        "description": "Infrastructure node battery below 5% (3.40V) — shutdown imminent",
        "default_severity": "immediate",
        "example_message": "🚨 Battery Emergency: BLD-MTN at 4% (3.38V) — shutdown imminent",
        "toggle": "mesh_health",
    },
    "battery_trend": {
        "name": "Battery Declining",
        "description": "Battery showing declining trend over 7 days — possible solar or charging issue",
        "default_severity": "routine",
        "example_message": "🔋 Battery Trend: HPR declining 85% → 62% over 7 days (-3.3%/day)",
        "toggle": "mesh_health",
    },
    "power_source_change": {
        "name": "Power Source Change",
        "description": "Node switched from USB to battery — possible power outage at site",
        "default_severity": "priority",
        "example_message": "⚡ Power Source: MHR switched from USB to battery — possible outage",
        "toggle": "mesh_health",
    },
    "solar_not_charging": {
        "name": "Solar Not Charging",
        "description": "Solar panel not charging during daylight hours — panel issue or obstruction",
        "default_severity": "priority",
        "example_message": "☀️ Solar Issue: BLD-MTN not charging during daylight (12:00 MDT)",
        "toggle": "mesh_health",
    },

    # Utilization alerts
    "high_utilization": {
        "name": "Channel Airtime High",
        "description": "LoRa channel airtime exceeding threshold — mesh congestion",
        "default_severity": "routine",
        "example_message": "📊 Channel Airtime: 47% utilization (threshold: 40%). Reliability may degrade.",
        "toggle": "mesh_health",
    },
    "sustained_high_util": {
        "name": "Sustained High Utilization",
        "description": "Channel airtime elevated for extended period — ongoing congestion",
        "default_severity": "priority",
        "example_message": "📊 Sustained Congestion: 45% channel utilization for 2+ hours. Consider reducing telemetry.",
        "toggle": "mesh_health",
    },
    "packet_flood": {
        "name": "Packet Flood",
        "description": "A single node sending excessive radio packets (NOT water flooding) — possible firmware bug or stuck transmitter",
        "default_severity": "priority",
        "example_message": "📻 Packet Flood: Node 'BKBS' transmitting 42 packets/min (threshold: 10/min). Firmware bug?",
        "toggle": "mesh_health",
    },

    # Coverage alerts
    "infra_single_gateway": {
        "name": "Single Gateway",
        "description": "Infrastructure node dropped to single gateway coverage — reduced redundancy",
        "default_severity": "priority",
        "example_message": "📶 Reduced Coverage: HPR dropped to single gateway. Previously had 3 paths.",
        "toggle": "mesh_health",
    },
    "feeder_offline": {
        "name": "Feeder Offline",
        "description": "A feeder gateway stopped responding — coverage gap possible",
        "default_severity": "priority",
        "example_message": "📡 Feeder Offline: AIDA-N2 gateway not responding. 5 nodes may lose uplink.",
        "toggle": "mesh_health",
    },
    "region_total_blackout": {
        "name": "Region Blackout",
        "description": "All infrastructure in a region is offline — complete coverage loss",
        "default_severity": "immediate",
        "example_message": "🚨 REGION BLACKOUT: All infrastructure in Magic Valley offline!",
        "toggle": "mesh_health",
    },

    # Health score alerts
    "mesh_score_low": {
        "name": "Mesh Health Low",
        "description": "Overall mesh health score dropped below threshold — multiple issues likely",
        "default_severity": "priority",
        "example_message": "📉 Mesh Health: Score 62/100 (threshold: 65). Infrastructure: 71, Connectivity: 58.",
        "toggle": "mesh_health",
    },
    "region_score_low": {
        "name": "Region Health Low",
        "description": "A region's health score below threshold — localized issues",
        "default_severity": "priority",
        "example_message": "📉 Region Health: Magic Valley at 55/100 (threshold: 60). 2 nodes offline.",
        "toggle": "mesh_health",
    },

    # Environmental - Weather
    "weather_warning": {
        "name": "Severe Weather",
        "description": "NWS warning or advisory affecting your mesh area",
        "default_severity": "priority",
        "example_message": "⚠ Red Flag Warning — Twin Falls, Cassia counties. Gusty winds, low humidity. Until May 13 04:00Z",
        "toggle": "weather",
    },

    # Environmental - Space Weather
    "hf_blackout": {
        "name": "HF Radio Blackout",
        "description": "R3+ solar flare degrading HF propagation on sunlit side",
        "default_severity": "priority",
        "example_message": "⚠ R3 Strong Radio Blackout — X1.2 flare. Wide-area HF blackout ~1 hour on sunlit side.",
        "toggle": "rf_propagation",
    },
    "geomagnetic_storm": {
        "name": "Geomagnetic Storm",
        "description": "G2+ geomagnetic storm — HF degraded at higher latitudes, aurora possible",
        "default_severity": "priority",
        "example_message": "🌐 G2 Moderate Geomagnetic Storm — Kp=6. HF fades at high latitudes, aurora to ~55°.",
        "toggle": "rf_propagation",
    },

    # Environmental - Tropospheric
    "tropospheric_ducting": {
        "name": "Tropospheric Ducting",
        "description": "Atmospheric conditions trapping VHF/UHF signals — extended range",
        "default_severity": "routine",
        "example_message": "📡 Tropospheric Ducting: Surface duct detected, dM/dz -45 M-units/km, ~120m thick. VHF/UHF extended range.",
        "toggle": "rf_propagation",
    },

    # Environmental - Fire
    "fire_proximity": {
        "name": "Fire Near Mesh",
        "description": "Active wildfire within alert radius of mesh infrastructure",
        "default_severity": "priority",
        "example_message": "🔥 Fire Near Mesh: Rock Creek Fire — 1,240 ac, 15% contained, 12 km SSW of MHR. Monitor closely.",
        "toggle": "fire",
    },
    "wildfire_proximity": {
        "name": "Fire Near Mesh",
        "description": "Active wildfire within alert radius of mesh infrastructure",
        "default_severity": "priority",
        "example_message": "🔥 Fire Near Mesh: Rock Creek Fire — 1,240 ac, 15% contained, 12 km SSW of MHR.",
        "toggle": "fire",
    },
    "new_ignition": {
        "name": "New Fire Ignition",
        "description": "Satellite hotspot detected NOT near any known fire — potential new wildfire",
        "default_severity": "priority",
        "example_message": "🛰 New Ignition: Satellite fire at 42.32°N, 114.30°W — high confidence, 47 MW FRP. Not near any known fire.",
        "toggle": "fire",
    },

    # Environmental - Flood
    "stream_flood_warning": {
        "name": "Stream Flood Warning",
        "description": "River gauge exceeds NWS flood stage threshold",
        "default_severity": "priority",
        "example_message": "🌊 Stream Flood Warning: Snake River nr Twin Falls at 12.8 ft — Minor Flood Stage is 10.5 ft.",
        "toggle": "weather",
    },
    "stream_high_water": {
        "name": "Stream High Water",
        "description": "River gauge approaching flood stage — monitoring recommended",
        "default_severity": "routine",
        "example_message": "🌊 High Water: Snake River at 9.8 ft — Action Stage is 9.0 ft. Monitor conditions.",
        "toggle": "weather",
    },

    # Environmental - Roads
    "road_closure": {
        "name": "Road Closure",
        "description": "Full road closure on a monitored corridor",
        "default_severity": "priority",
        "example_message": "🚧 Road Closure: I-84 EB at MP 173 — full closure, construction. Detour via US-30.",
        "toggle": "roads",
    },
    "traffic_congestion": {
        "name": "Traffic Congestion",
        "description": "Traffic speed dropped below congestion threshold on a monitored corridor",
        "default_severity": "routine",
        "example_message": "🚗 Traffic Congestion: I-84 Twin Falls — 35 mph (free-flow 70 mph), 50% speed ratio",
        "toggle": "roads",
    },

    # Environmental - Avalanche
    "avalanche_warning": {
        "name": "Avalanche Danger High",
        "description": "Avalanche danger level 4 (High) or 5 (Extreme) in your area",
        "default_severity": "priority",
        "example_message": "⛷ Avalanche Danger HIGH: Sawtooth Zone — avoid avalanche terrain. Natural avalanches likely.",
        "toggle": "avalanche",
    },
    "avalanche_considerable": {
        "name": "Avalanche Danger Considerable",
        "description": "Avalanche danger level 3 (Considerable) — most fatalities occur at this level",
        "default_severity": "routine",
        "example_message": "⛷ Avalanche Danger CONSIDERABLE: Sawtooth Zone — dangerous conditions on steep slopes.",
        "toggle": "avalanche",
    },
}


def get_category(category_id: str) -> dict:
    """Get category info by ID, with fallback for unknown categories."""
    if category_id in ALERT_CATEGORIES:
        return ALERT_CATEGORIES[category_id]
    return {
        "name": category_id.replace("_", " ").title(),
        "description": f"Alert type: {category_id}",
        "default_severity": "routine",
        "example_message": f"Alert: {category_id}",
        "toggle": "mesh_health",  # Default unknown to mesh_health
    }


def list_categories() -> list[dict]:
    """List all categories with their IDs."""
    return [
        {"id": cat_id, **cat_info}
        for cat_id, cat_info in ALERT_CATEGORIES.items()
    ]


def categories_for_toggle(toggle: str) -> list[str]:
    """Return all category names that route to this toggle.

    Args:
        toggle: Toggle name (e.g., "mesh_health", "weather")

    Returns:
        List of category IDs that have this toggle assigned
    """
    if toggle not in VALID_TOGGLES:
        return []

    return [
        cat_id
        for cat_id, cat_info in ALERT_CATEGORIES.items()
        if cat_info.get("toggle") == toggle
    ]


def get_toggle(category_name: str) -> Optional[str]:
    """Return the toggle name for a category, or None if unknown.

    Args:
        category_name: Category ID (e.g., "infra_offline")

    Returns:
        Toggle name (e.g., "mesh_health") or None if category unknown
    """
    cat_info = ALERT_CATEGORIES.get(category_name)
    if cat_info:
        return cat_info.get("toggle")
    return None
