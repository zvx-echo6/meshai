"""Alert category registry.

Defines all alertable conditions with human-readable names, descriptions,
and example messages showing what users will receive.
"""

ALERT_CATEGORIES = {
    # Infrastructure alerts
    "infra_offline": {
        "name": "Infrastructure Offline",
        "description": "An infrastructure node stopped responding",
        "default_severity": "warning",
        "example_message": "❌ Mountain Harrison Rptr went offline in Magic Valley.",
    },
    "critical_node_down": {
        "name": "Critical Node Down",
        "description": "A node marked as critical went offline",
        "default_severity": "critical",
        "example_message": "🚨 MHR went offline in Magic Valley. (alert 1/4)",
    },
    "infra_recovery": {
        "name": "Infrastructure Recovery",
        "description": "An infrastructure node came back online",
        "default_severity": "info",
        "example_message": "✅ Mountain Harrison Rptr is back online in Magic Valley.",
    },
    "new_router": {
        "name": "New Router",
        "description": "A new router appeared on the mesh",
        "default_severity": "info",
        "example_message": "📡 New router appeared: Snake River Relay in Wood River Valley.",
    },

    # Power alerts
    "battery_warning": {
        "name": "Battery Warning",
        "description": "Infrastructure node battery below warning threshold",
        "default_severity": "warning",
        "example_message": "🔋 BLD-MTN battery low at 35% in Boise Foothills.",
    },
    "battery_critical": {
        "name": "Battery Critical",
        "description": "Infrastructure node battery below critical threshold",
        "default_severity": "critical",
        "example_message": "🔋 MHR battery critical at 18% in Magic Valley.",
    },
    "battery_emergency": {
        "name": "Battery Emergency",
        "description": "Infrastructure node battery critically low",
        "default_severity": "emergency",
        "example_message": "🚨 BLD-MTN battery EMERGENCY at 8% in Boise Foothills.",
    },
    "battery_trend": {
        "name": "Battery Declining",
        "description": "Battery showing declining trend over 7 days",
        "default_severity": "warning",
        "example_message": "🔋 HPR battery declining: 85% → 62% over 7 days (-3.3%/day) in Hagerman.",
    },
    "power_source_change": {
        "name": "Power Source Change",
        "description": "Node switched from USB to battery (possible outage)",
        "default_severity": "warning",
        "example_message": "⚡ MHR switched from USB to battery in Magic Valley. Possible power outage.",
    },
    "solar_not_charging": {
        "name": "Solar Not Charging",
        "description": "Solar panel not charging during daylight hours",
        "default_severity": "warning",
        "example_message": "☀️ BLD-MTN solar not charging in Boise Foothills.",
    },

    # Utilization alerts
    "sustained_high_util": {
        "name": "High Channel Utilization",
        "description": "Channel airtime elevated for extended period",
        "default_severity": "warning",
        "example_message": "🔥 MHR at 32% channel utilization for 6+ hours in Magic Valley.",
    },
    "packet_flood": {
        "name": "Packet Flood",
        "description": "Node sending excessive packets (possible firmware bug)",
        "default_severity": "warning",
        "example_message": "📡 BRKN-NODE sent 847 packets in 24h (threshold: 500) in Boise.",
    },

    # Coverage alerts
    "infra_single_gateway": {
        "name": "Single Gateway",
        "description": "Infrastructure node dropped to single gateway coverage",
        "default_severity": "warning",
        "example_message": "📶 HPR dropped to single gateway coverage in Hagerman.",
    },
    "feeder_offline": {
        "name": "Feeder Offline",
        "description": "A feeder gateway stopped responding",
        "default_severity": "warning",
        "example_message": "📡 Feeder gateway AIDA-N2 went offline.",
    },
    "region_total_blackout": {
        "name": "Region Blackout",
        "description": "All infrastructure in a region is offline",
        "default_severity": "emergency",
        "example_message": "🚨 TOTAL BLACKOUT: All infrastructure in Magic Valley is offline!",
    },

    # Health score alerts
    "mesh_score_low": {
        "name": "Mesh Health Low",
        "description": "Overall mesh health score below threshold",
        "default_severity": "warning",
        "example_message": "📉 Mesh Health: Score dropped to 62 (Warning threshold: 70).",
    },
    "region_score_low": {
        "name": "Region Health Low",
        "description": "A region's health score below threshold",
        "default_severity": "warning",
        "example_message": "📉 Magic Valley health score dropped to 55 (threshold: 60).",
    },

    # Environmental alerts
    "weather_warning": {
        "name": "Severe Weather",
        "description": "NWS warning or advisory for mesh area",
        "default_severity": "warning",
        "example_message": "⚠️ Red Flag Warning — Twin Falls, Jerome, Cassia counties until May 14 04:00 MDT.",
    },
    "hf_blackout": {
        "name": "HF Radio Blackout",
        "description": "R3+ solar event degrading HF propagation",
        "default_severity": "warning",
        "example_message": "📻 R3 HF Radio Blackout — HF propagation degraded for several hours.",
    },
    "tropospheric_ducting": {
        "name": "Tropospheric Ducting",
        "description": "Atmospheric conditions extending VHF/UHF range",
        "default_severity": "info",
        "example_message": "📡 Tropospheric Ducting: Surface duct detected, dM/dz -45 M-units/km. Extended VHF/UHF range possible.",
    },
    "wildfire_proximity": {
        "name": "Fire Near Mesh",
        "description": "Wildfire detected within configured distance of mesh infrastructure",
        "default_severity": "warning",
        "example_message": "🔥 Rock Creek Fire — 1,240 ac, 15% contained, 24 km SSW of MHR.",
    },
    "new_ignition": {
        "name": "New Fire Ignition",
        "description": "Satellite hotspot not matching any known fire perimeter",
        "default_severity": "warning",
        "example_message": "🛰️ New Ignition: Satellite fire detection at 42.32°N, 114.30°W — high confidence, not near any known fire.",
    },
    "stream_flood_warning": {
        "name": "Stream Flood Warning",
        "description": "River gauge exceeds flood stage threshold",
        "default_severity": "warning",
        "example_message": "🌊 Snake River nr Twin Falls at 12.8 ft (flood stage: 13.0 ft).",
    },
    "road_closure": {
        "name": "Road Closure",
        "description": "Full road closure on monitored corridor",
        "default_severity": "warning",
        "example_message": "🚧 I-84 EB closed at MP 173 — full closure due to wildfire smoke.",
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
        "example_message": f"Alert: {category_id}",
    }


def list_categories() -> list[dict]:
    """List all categories with their IDs."""
    return [
        {"id": cat_id, **cat_info}
        for cat_id, cat_info in ALERT_CATEGORIES.items()
    ]
