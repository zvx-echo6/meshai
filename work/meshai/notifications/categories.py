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
    "satpass",
    # Civil / public-safety alerts from FEMA IPAWS-OPEN (env/ipaws.py):
    # evacuation orders, Civil Emergency Messages, AMBER, 911 outages,
    # law-enforcement warnings, HazMat. NON-weather (weather stays on the
    # `weather` toggle via the nws adapter).
    "emergency",
})


# ---------------------------------------------------------------------------
# Dynamic category/family registry (Integration Phase A)
#
# ALERT_CATEGORIES / VALID_TOGGLES above enumerate the built-in families.
# Generic config-driven data sources (env/generic_http.py) carry an arbitrary
# `category` that is NOT in that static table, so historically get_toggle()
# returned None, the ToggleFilter treated the event as "other", and dropped it.
# These runtime registries let a generic source register its category as a
# first-class family with its own toggle, so its events resolve to that family
# and become routable. Both dicts are EMPTY until something registers, so
# behavior for every built-in category is byte-identical to before.
# ---------------------------------------------------------------------------

_DYNAMIC_CATEGORIES: dict[str, str] = {}   # category id -> family
_DYNAMIC_FAMILIES: dict[str, str] = {}     # family -> human label


def register_family(family: str, label: Optional[str] = None) -> None:
    """Register a family (toggle) at runtime. Idempotent.

    A re-registration with no label leaves any existing label untouched; a
    call with a label sets/updates it. When no label is supplied for a
    first-time family, a title-cased default is derived from the family id.
    """
    if not family:
        return
    if label is None and family in _DYNAMIC_FAMILIES:
        return
    _DYNAMIC_FAMILIES[family] = (
        label or _DYNAMIC_FAMILIES.get(family) or family.replace("_", " ").title()
    )


def register_category(category: str, family: str, name: Optional[str] = None) -> None:
    """Register a category -> family mapping at runtime.

    Records category->family and registers the family if not already known.
    `name` is an optional display hint for the category (reserved for Phase B
    surfacing; not required for routing).
    """
    if not category or not family:
        return
    _DYNAMIC_CATEGORIES[category] = family
    if family not in _DYNAMIC_FAMILIES:
        register_family(family)


def all_toggles() -> frozenset:
    """Static base VALID_TOGGLES plus any dynamically registered families."""
    return VALID_TOGGLES | set(_DYNAMIC_FAMILIES)


def registered_families() -> dict:
    """family -> label for ALL families (static + dynamic).

    Static families get a title-cased label; dynamic families use their
    registered label. For Phase B's API to enumerate routable families.
    """
    fams = {t: t.replace("_", " ").title() for t in VALID_TOGGLES}
    fams.update(_DYNAMIC_FAMILIES)
    return fams


# Prefix fallback for categories not enumerated in ALERT_CATEGORIES (resolves the
# v0.4 "category -> other" gap for phases 2.7-2.14 emitted categories).
_TOGGLE_PREFIX_FALLBACK = [
    ("weather", "weather"),
    # v0.5.2: stream_* (USGS hydro) belongs with Geohazards, not weather
    ("stream", "seismic"),
    ("wildfire", "fire"),
    ("fire", "fire"),
    ("earthquake", "seismic"),
    ("quake", "seismic"),
    ("traffic", "roads"),
    ("road", "roads"),
    ("geomagnetic", "rf_propagation"),
    ("solar_radiation", "rf_propagation"),
    ("rf_", "rf_propagation"),
    ("avalanche", "avalanche"),
    ("sat", "satpass"),
    # IPAWS civil-alert categories all share the emergency_ prefix.
    ("emergency", "emergency"),
]


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
    # v0.5.7-weather audit: nws.py._derive_category() emits exactly these four
    # category IDs (suffix dispatch on the NWS event_type: warning/watch/
    # advisory/{anything else -> statement}). The set is in lockstep —
    # test_alert_categories_weather_complete enforces parity if nws.py changes.
    # If a new branch is added there, add the matching entry here too.
    "weather_warning": {
        "name": "Severe Weather Warning",
        "description": "NWS Warning affecting your mesh area — highest urgency weather alert",
        "default_severity": "priority",
        "example_message": "⚠ Red Flag Warning — Twin Falls, Cassia counties. Gusty winds, low humidity. Until May 13 04:00Z",
        "toggle": "weather",
    },
    "weather_watch": {
        "name": "Weather Watch",
        "description": "NWS Watch affecting your mesh area — conditions favorable for hazardous weather",
        "default_severity": "routine",
        "example_message": "⏳ Winter Storm Watch — Wood River Valley. Heavy snow possible Thu night through Fri.",
        "toggle": "weather",
    },
    "weather_advisory": {
        "name": "Weather Advisory",
        "description": "NWS Advisory affecting your mesh area — weather may cause inconvenience",
        "default_severity": "routine",
        "example_message": "ℹ Wind Advisory — Magic Valley. SW winds 25-35 mph with gusts to 50 mph.",
        "toggle": "weather",
    },
    "weather_statement": {
        "name": "Weather Statement",
        "description": "NWS Special Weather Statement — general awareness, no specific hazard",
        "default_severity": "routine",
        "example_message": "📋 Special Weather Statement — Isolated thunderstorms possible this afternoon.",
        "toggle": "weather",
    },

    # Environmental - Space Weather / RF Propagation
    # v0.5.7-rf audit (test_alert_categories_rf_complete enforces parity):
    # Native: ducting.py -> rf_anomalous_propagation (super_refraction tier);
    #         ducting.py -> rf_ducting_enhancement (duct + surface_duct tiers).
    # Central path (via map_category): space.alert -> rf_propagation_alert
    #         (swpc_alerts); space.kindex -> geomagnetic_storm (swpc_kindex);
    #         space.proton_flux -> solar_radiation_storm (swpc_protons);
    #         catchall space.* -> geomagnetic_storm.
    # All three SWPC adapters publish severity=0 -> "routine" per guide
    # §swpc_alerts/§swpc_kindex/§swpc_protons live samples.
    #
    # Legacy entries kept: hf_blackout and tropospheric_ducting have no
    # current emitter (ducting.py was renamed to rf_ducting_enhancement,
    # and HF-blackout-specific parsing of swpc_alerts.message is deferred
    # to a future phase). They remain UI-selectable as forward-compatible
    # rule targets; queued for cleanup if no emitter materializes.
    "rf_anomalous_propagation": {
        "name": "RF Anomalous Propagation",
        "description": "Super-refractive atmospheric layer affecting VHF/UHF propagation — sub-standard refractive conditions, mostly affects line-of-sight links",
        "default_severity": "routine",
        "example_message": "📡 Anomalous Propagation: Super-refraction detected, dM/dz -45 M-units/km, ~80m thick layer. VHF/UHF links may show enhanced range.",
        "toggle": "rf_propagation",
    },
    "rf_ducting_enhancement": {
        "name": "RF Ducting Enhancement",
        "description": "Tropospheric duct trapping VHF/UHF signals — extended range, signals propagate well beyond the normal radio horizon",
        "default_severity": "priority",
        "example_message": "📡 Ducting Enhancement: Surface duct detected, base 0 m, ~120 m thick. VHF/UHF extended range, expect signals well beyond horizon.",
        "toggle": "rf_propagation",
    },
    "rf_propagation_alert": {
        "name": "Space Weather Alert",
        "description": "NOAA SWPC space weather alert/watch/warning — geomagnetic storm scales (G1-G5), radiation storms (S), radio blackouts (R), or summaries. Full operational text in event body.",
        "default_severity": "priority",
        "example_message": "⚠ Space Weather Alert (A20F): WATCH — Geomagnetic Storm Category G1 Predicted. Apr 25: G1 (Minor). Aurora possible at high latitudes.",
        "toggle": "rf_propagation",
    },
    "solar_radiation_storm": {
        "name": "Solar Radiation Storm",
        "description": "GOES proton flux above S-scale threshold — S1 ≥10 pfu at ≥10 MeV; S2 ≥100; S3 ≥1000; impacts HF over polar regions and satellite operations",
        "default_severity": "priority",
        "example_message": "🌐 Solar Radiation Storm: GOES-19 proton flux 12.5 pfu at ≥10 MeV (S1 threshold). HF over polar regions degraded.",
        "toggle": "rf_propagation",
    },
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
    # v0.5.7-fire audit (test_alert_categories_fire_complete enforces parity):
    # Native: firms.py -> {new_ignition, wildfire_hotspot};
    #         fires.py -> wildfire_incident.
    # Central path (via map_category): fire.hotspot.* -> wildfire_hotspot;
    #         fire.incident.* / fire.perimeter.* / fire.* -> wildfire_incident.
    #
    # REMOVED in v0.5.7-fire:
    #   - fire_proximity (Matt: "fire near mesh has its own set of parameters
    #     that I don't even know what they could be. like how far is near mesh?
    #     I don't know I can't set that.") -- parameterized distance_max_km on
    #     rules is queued for v0.5.8, not a registry entry.
    #   - wildfire_proximity (duplicate of fire_proximity, same parametric flaw)
    "new_ignition": {
        "name": "New Fire Ignition",
        "description": "Satellite hotspot detected NOT near any known fire — potential new wildfire",
        "default_severity": "priority",
        "example_message": "🛰 New Ignition: Satellite fire at 42.32°N, 114.30°W — high confidence, 47 MW FRP. Not near any known fire.",
        "toggle": "fire",
    },
    # v0.7-fire-tracker-1: WFIGS first-sight of a declared incident. The
    # WFIGS handler tags the data dict with this category on cases (i)
    # and (ii) -- INSERT or row-exists-but-never-broadcast. Subsequent
    # Update broadcasts (case iii) keep the existing wildfire_incident
    # category so the dispatcher can rule them separately.
    "wildfire_declared": {
        "name": "Wildfire Declared (WFIGS first sight)",
        "description": "WFIGS published a new IRWIN incident -- the first time meshai has seen this declared fire. Subsequent acreage/containment updates fall under wildfire_incident.",
        "default_severity": "priority",
        "example_message": "🔥 New: Cache Peak Fire (WF), 3 mi N of Almo: 250 ac, 0% contained, @ 42.118,-113.643",
        "toggle": "fire",
    },
    # v0.7-fire-tracker-1: 3+ FIRMS pixels cluster within 1 mi over a 60
    # min window with no WFIGS-declared fire to attribute to. This is the
    # "possible new fire" early-warning category -- FIRMS sees the heat
    # well before NIFC publishes the incident. Knobs in adapter_config:
    #   firms.cluster_min_pixels (3)
    #   firms.cluster_max_radius_mi (1.0)
    #   firms.cluster_time_window_minutes (60)
    "unattributed_hotspot_cluster": {
        "name": "Unattributed Hotspot Cluster (possible new fire)",
        "description": "3+ FIRMS satellite hotspots clustered within a small radius with no matching WFIGS incident. Possible new ignition; the cluster broadcast fires once per cluster (member pixels are marked covered so a 4th arrival does not re-trigger).",
        "default_severity": "priority",
        "example_message": "🔥 Possible new fire: 3 hotspots within 1 mi @ 42.93,-114.45 (combined 78 MW)",
        "toggle": "fire",
    },
    # v0.7-fire-tracker-2: per-satellite-pass centroid drift exceeds the
    # configured threshold. FIRMS handler computes drift on the boundary
    # of a new pass (different satellite/time-bucket than the fire's
    # last_pass_id). Each broadcast carries the drift direction (8-way)
    # and the speed in mi/h derived from pass_ended_at deltas.
    "wildfire_growth": {
        "name": "Wildfire Growth (centroid drift)",
        "description": "A tracked fire's per-pass centroid moved more than fires.growth_drift_threshold_mi between consecutive satellite passes. Drift direction is the 8-way compass bearing from the prior centroid to the current one.",
        "default_severity": "priority",
        "example_message": "🔥 Cache Peak Fire moving NE 1.2 mi/h, ~3 mi from Almo",
        "toggle": "fire",
    },
    # v0.7-fire-tracker-2: a tracked fire has had no new FIRMS pixels
    # attributed for >= fires.halt_minimum_seconds (default 12h). The
    # halt detector runs opportunistically on every FIRMS pixel arrival
    # (per-pixel check, cheap because tombstoned_at IS NULL is selective).
    # Latched via fires.halt_broadcast_at so the same idle fire does not
    # re-fire on every subsequent pixel.
    "wildfire_halted": {
        "name": "Wildfire Halted (no growth)",
        "description": "No FIRMS pixels attributed to this tracked fire for at least fires.halt_minimum_seconds. Broadcast fires once per halt event; a subsequent attributed pixel re-opens eligibility.",
        "default_severity": "routine",
        "example_message": "🔥 Cache Peak Fire no growth in 14h",
        "toggle": "fire",
    },
    # v0.7-fire-tracker-3: a FIRMS pixel attributed to a tracked fire
    # is at least fires.spotting_distance_threshold_mi from that fire's
    # previous-pass convex-hull perimeter. Spotting is the highest-
    # actionable signal -- fire spread BEYOND the existing perimeter
    # is what kills people during a major event -- hence immediate
    # severity. Cooldown via fires.last_spotting_broadcast_at keeps
    # rapid embers in the same area from spamming the mesh.
    "wildfire_spotting": {
        "name": "Wildfire Spotting (beyond perimeter)",
        "description": "FIRMS pixel attributed to a tracked fire but located outside the previous-pass perimeter by at least fires.spotting_distance_threshold_mi. Indicates ember spread or new ignition forward of the main fire.",
        "default_severity": "immediate",
        "example_message": "🔥 Possible spotting 2.1 mi NE of Cache Peak Fire perimeter",
        "toggle": "fire",
    },
    "wildfire_hotspot": {
        "name": "Wildfire Hotspot",
        "description": "Satellite thermal-anomaly detection (NASA FIRMS VIIRS/MODIS pixel) — not necessarily a new ignition",
        "default_severity": "routine",
        "example_message": "🔥 Wildfire Hotspot: VIIRS NOAA-20 pixel at 43.12°N, 114.85°W — high confidence, 22 MW FRP, daytime overpass.",
        "toggle": "fire",
    },
    "wildfire_incident": {
        "name": "Wildfire Incident",
        "description": "Active wildfire incident from NIFC WFIGS — official incident record with size, containment, cause",
        "default_severity": "priority",
        "example_message": "🔥 Wildfire Incident: Rochelle 2 — 1,240 ac, 15% contained, Custer County ID. WF, Natural cause.",
        "toggle": "fire",
    },

    # Environmental - Seismic (Geohazards family)
    # v0.5.7-seismic audit (test_alert_categories_seismic_complete enforces parity):
    # Native: usgs_quake.py -> earthquake_event.
    # Central path: map_category('quake.event.<tier>') -> earthquake_event
    # for any of the 6 tiers (minor/light/moderate/strong/major/great).
    # The hydro entries below also live under toggle='seismic' per the v0.5.2
    # USGS-water migration (see comment on stream_flood_warning) and are
    # OUT OF SCOPE for v0.5.7-seismic -- they belong to the water/geohazards
    # phase that follows. Verified-unchanged here.
    "earthquake_event": {
        "name": "Earthquake",
        "description": "USGS-catalogued earthquake — magnitude, depth, and PAGER alert level surfaced from the USGS earthquake feed",
        "default_severity": "routine",
        "example_message": "🌐 Earthquake: M 4.2 — 23 km ESE of Stanley, ID. Depth 8 km. USGS automatic.",
        "toggle": "seismic",
    },

    # Environmental - Flood / Water (Geohazards family per v0.5.2 migration)
    # v0.5.7-water audit (test_alert_categories_water_complete enforces parity):
    # Native: usgs.py applies NWPS flood-stage thresholds CLIENT-SIDE and emits
    #   - stream_flood_warning  (reading at/above flood stage)
    #   - stream_high_water     (reading at action stage)
    # Routine gauge readings below action stage are silently dropped on the
    # native path (no spam).
    # Central path: every NWIS reading arrives with category="hydro.<pcode>.
    # <agency>.<site>" at severity=0. consumer._CATEGORY_MAP maps `hydro.*` to
    # `stream_flow` (added below in v0.5.7-water so the rule editor can target
    # raw central readings). NOTE: meshai does NOT currently re-apply NWPS
    # threshold logic to central-delivered readings; future work to bring
    # the central path to parity with the native threshold-classification
    # is queued for v0.5.8+.
    "stream_flow": {
        "name": "Stream Gauge Reading",
        "description": "Raw USGS NWIS stream gauge reading (discharge, gage height, water temp) — no threshold classification. Use stream_flood_warning / stream_high_water for threshold-triggered alerts.",
        "default_severity": "routine",
        "example_message": "🌊 Stream Reading: Snake River nr Twin Falls — 7,420 ft³/s discharge at 2026-06-04T12:00Z (provisional).",
        "toggle": "seismic",
    },
    "stream_flood_warning": {
        "name": "Stream Flood Warning",
        "description": "River gauge exceeds NWS flood stage threshold",
        "default_severity": "priority",
        "example_message": "🌊 Stream Flood Warning: Snake River nr Twin Falls at 12.8 ft — Minor Flood Stage is 10.5 ft.",
        # v0.5.2: moved weather→seismic to match the GUI Geohazards family tab
        # (Environment.tsx FAMILIES key='geohazards' groups usgs_quake+usgs+avalanche).
        # 'seismic' is the canonical Geohazards toggle in VALID_TOGGLES; backend still
        # has separate avalanche/seismic toggles, but USGS hydro lives with USGS quake.
        "toggle": "seismic",
    },
    "stream_high_water": {
        "name": "Stream High Water",
        "description": "River gauge approaching flood stage — monitoring recommended",
        "default_severity": "routine",
        "example_message": "🌊 High Water: Snake River at 9.8 ft — Action Stage is 9.0 ft. Monitor conditions.",
        # v0.5.2: moved weather→seismic — see stream_flood_warning above
        "toggle": "seismic",
    },

    # Environmental - Roads
    # v0.5.7-traffic audit (test_alert_categories_roads_complete enforces parity):
    # Native: traffic.py -> traffic_congestion; roads511.py -> road_closure.
    # Central path (via map_category): work_zone (wzdx), road_incident
    # (tomtom_incidents + state_511_atis/itd_511 'incident'), road_closure
    # (state_511_atis/itd_511 'closure'), traffic_congestion (traffic. catchall).
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
    "work_zone": {
        "name": "Work Zone",
        "description": "Active construction or maintenance work zone affecting traffic — possible lane closures, reduced speed, or detour",
        "default_severity": "routine",
        "example_message": "🚧 Work Zone: I-84 EB MP 168-173 — right lane closed, 55 mph zone. Expect delays.",
        "toggle": "roads",
    },
    "road_incident": {
        "name": "Road Incident",
        "description": "Reported incident on a monitored corridor (crash, disabled vehicle, debris, hazard)",
        "default_severity": "priority",
        "example_message": "🚨 Road Incident: US-93 NB at MP 47 — crash blocking left lane, expect 30-min delay.",
        "toggle": "roads",
    },

    # Environmental - Avalanche
    # v0.5.7-avalanche audit (test_alert_categories_avalanche_complete enforces parity):
    # Central v0.10.0 does NOT ship an avalanche adapter (verified against the
    # guide TOC + producer src tree); avalanche is native-only in meshai. So
    # the audit is single-source: meshai/env/avalanche.py emits exactly two
    # categories based on the NWAC/CAIC danger level:
    #   danger_level >= 4 (High, Extreme)  -> avalanche_warning
    #   danger_level == 3 (Considerable)   -> avalanche_watch
    #   danger_level <= 2 (Low, Moderate)  -> silently dropped (not actionable)
    #
    # Legacy entry kept: avalanche_considerable has no current emitter -- the
    # Considerable-danger semantic now ships as avalanche_watch instead. The
    # legacy registry entry remains UI-selectable as a forward-compat target
    # (router.py source-attribution tables and composer.py emoji/label tables
    # still reference it). Queued for cleanup if no emitter materializes by
    # v0.6.
    "avalanche_warning": {
        "name": "Avalanche Danger High",
        "description": "Avalanche danger level 4 (High) or 5 (Extreme) in your area",
        "default_severity": "priority",
        "example_message": "⛷ Avalanche Danger HIGH: Sawtooth Zone — avoid avalanche terrain. Natural avalanches likely.",
        "toggle": "avalanche",
    },
    "avalanche_watch": {
        "name": "Avalanche Danger Considerable",
        "description": "Avalanche danger level 3 (Considerable) — dangerous conditions on steep slopes; most avalanche fatalities occur at this level. Travel with caution and conservative decision-making.",
        "default_severity": "routine",
        "example_message": "⛷ Avalanche Danger CONSIDERABLE: Sawtooth Zone — dangerous conditions on steep slopes. Cautious route-finding required.",
        "toggle": "avalanche",
    },
    "avalanche_considerable": {
        "name": "Avalanche Danger Considerable",
        "description": "Avalanche danger level 3 (Considerable) — most fatalities occur at this level",
        "default_severity": "routine",
        "example_message": "⛷ Avalanche Danger CONSIDERABLE: Sawtooth Zone — dangerous conditions on steep slopes.",
        "toggle": "avalanche",
    },
    # Satellite passes
    "sat_pass": {
        "name": "Satellite Pass",
        "description": "Notable satellite pass visible from your location (ISS, amateur sats)",
        "default_severity": "routine",
        "example_message": "🛰️ ISS Pass — 75° max\nAOS 19:32 · LOS 19:38\nBoise · NW→SE",
        "toggle": "satpass",
    },

    # Civil / public-safety alerts (FEMA IPAWS-OPEN EAS, env/ipaws.py).
    # NON-weather only: weather CAP is dropped by the adapter's weather-sender
    # gate and stays on the `weather` toggle via the nws adapter.
    "emergency_evacuation": {
        "name": "Evacuation Order",
        "description": "IPAWS evacuation order/warning (SAME EVI/EVA) — leave the area now or prepare to.",
        "default_severity": "immediate",
        "example_message": "🚨 Evacuation Immediate: Blaine County — leave now via ID-75 north. Wildfire threat to structures.",
        "toggle": "emergency",
    },
    "emergency_civil": {
        "name": "Civil Emergency",
        "description": "IPAWS Civil Emergency Message / Civil Danger Warning (SAME CEM/CDW) — significant in-progress or imminent threat to safety.",
        "default_severity": "priority",
        "example_message": "⚠️ Civil Emergency: Ada County — boil-water order in effect for the city of Kuna until further notice.",
        "toggle": "emergency",
    },
    "emergency_amber": {
        "name": "AMBER Alert",
        "description": "IPAWS Child Abduction Emergency / AMBER alert (SAME CAE/LAE).",
        "default_severity": "immediate",
        "example_message": "🚨 AMBER Alert: Canyon County — silver Honda Civic, plate 1A-23456. Call 911 with sightings.",
        "toggle": "emergency",
    },
    "emergency_law": {
        "name": "Law Enforcement / Shelter-in-Place",
        "description": "IPAWS Law Enforcement Warning or Shelter in Place Warning (SAME LEW/SPW).",
        "default_severity": "priority",
        "example_message": "⚠️ Shelter in Place: Twin Falls — active police incident near Blue Lakes Blvd. Stay indoors, lock doors.",
        "toggle": "emergency",
    },
    "emergency_911_outage": {
        "name": "911 Outage",
        "description": "IPAWS 911 Telephone Outage Emergency (SAME TOE) — 911 unreachable; use the published alternate number.",
        "default_severity": "priority",
        "example_message": "⚠️ 911 Outage: Jerome County — 911 down. For emergencies call (208) 555-0111 until restored.",
        "toggle": "emergency",
    },
    "emergency_hazmat": {
        "name": "Hazardous Materials",
        "description": "IPAWS Hazardous Materials / Radiological / Nuclear / Fire Warning (SAME HMW/RHW/NUW/FRW).",
        "default_severity": "immediate",
        "example_message": "🚨 HazMat Warning: Nampa — chlorine leak near rail yard. Evacuate 1-mile radius, avoid downwind areas.",
        "toggle": "emergency",
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
    if toggle not in all_toggles():
        return []

    result = [
        cat_id
        for cat_id, cat_info in ALERT_CATEGORIES.items()
        if cat_info.get("toggle") == toggle
    ]
    # Dynamically-registered categories that route to this family.
    result.extend(c for c, fam in _DYNAMIC_CATEGORIES.items() if fam == toggle)
    return result


def get_toggle(category_name: str) -> Optional[str]:
    """Return the toggle name for a category, or None if unknown.

    Args:
        category_name: Category ID (e.g., "infra_offline")

    Returns:
        Toggle name (e.g., "mesh_health") or None if category unknown
    """
    # Dynamic registry first: a registered generic category resolves to its
    # own family, NOT the ALERT_CATEGORIES entry or the mesh_health prefix
    # fallback. Empty until a generic source registers, so built-ins unchanged.
    dynamic = _DYNAMIC_CATEGORIES.get(category_name)
    if dynamic:
        return dynamic
    cat_info = ALERT_CATEGORIES.get(category_name)
    if cat_info:
        return cat_info.get("toggle")
    for prefix, toggle in _TOGGLE_PREFIX_FALLBACK:
        if category_name.startswith(prefix):
            return toggle
    return None
