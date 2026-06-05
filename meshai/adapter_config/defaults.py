"""v0.6-3a single source-of-truth for adapter_config defaults.

Every module-level magic-number constant flagged by the audit doc
(/mnt/c/Users/mtthw/OneDrive/Documents/Claude/Projects/meshai/v0.6-phase1-audit.md)
Section A.1 through A.12 lives here. The migration runs first (v6.sql
creates the empty tables); init_db() then calls seed_defaults() which
INSERT OR IGNOREs one row per (adapter, key) entry below, copying
value_json = default_json.

**Behavior contract**: every default below MUST match the current
hardcoded value in the corresponding handler EXACTLY. v0.6-3b will
swap the handler's constant for an `adapter_config.<adapter>.<key>`
read. Until that wiring lands, this registry is the documentation of
intent; nothing reads from it at runtime yet.

Adding a new tunable in the future:
    1. Add an entry to REGISTRY below with default + type + description.
    2. The next container restart calls seed_defaults() which
       INSERT OR IGNOREs the row.
    3. Wire the handler to read from adapter_config.<adapter>.<key>.
"""
from __future__ import annotations

from typing import Any


# -------- REGISTRY --------------------------------------------------------
#
# Schema: REGISTRY[(adapter, key)] = {
#     "default":     <python value>,   # the seed value
#     "type":        "int" | "float" | "str" | "bool" | "json",
#     "description": "...",
# }
#
# `json` type covers list, dict, and nested structures. Decoded as
# json.loads(value_json) by the accessor.
#
# Citations in comments point at the current source-of-truth in the
# handler code -- these MUST stay in lock-step until v0.6-3b lands.

REGISTRY: dict[tuple[str, str], dict[str, Any]] = {

    # =================================================================
    # WFIGS -- central/wfigs_handler.py
    # =================================================================
    ("wfigs", "cooldown_seconds"): {
        "default": 28800,  # central/wfigs_handler.py:43 (8*60*60)
        "type": "int",
        "description": "Per-fire broadcast cooldown in seconds (forward-only Update gate).",
    },
    ("wfigs", "prefix_new"): {
        "default": "New",
        "type": "str",
        "description": "Wire-string prefix for the first broadcast of a fire.",
    },
    ("wfigs", "prefix_update"): {
        "default": "Update",
        "type": "str",
        "description": "Wire-string prefix for subsequent Update broadcasts.",
    },
    ("wfigs", "emoji"): {
        "default": "🔥",
        "type": "str",
        "description": "Wire-string lead emoji.",
    },
    ("wfigs", "anchor_priority"): {
        "default": ["geocoder_city", "nearest_town", "landclass", "county_state"],
        "type": "json",
        "description": "Ordered location-anchor fallback chain for the broadcast.",
    },
    ("wfigs", "anchor_max_mi"): {
        "default": 100.0,  # central/wfigs_handler.py:322 (nearest_town max_distance_mi)
        "type": "float",
        "description": "Max distance (mi) for the nearest_town anchor fallback.",
    },
    ("wfigs", "broadcast_on_acres"): {
        "default": True,
        "type": "bool",
        "description": "Re-broadcast when acres increase (forward-only).",
    },
    ("wfigs", "broadcast_on_contained"): {
        "default": True,
        "type": "bool",
        "description": "Re-broadcast when containment percent increases (forward-only).",
    },

    # =================================================================
    # NWS -- central/nws_handler.py
    # =================================================================
    ("nws", "broadcast_severities"): {
        "default": ["Extreme", "Severe"],  # nws_handler.py:43
        "type": "json",
        "description": "CAP severity strings allowed onto the mesh.",
    },
    ("nws", "tombstone_msgtypes"): {
        "default": ["Cancel", "Expire"],  # nws_handler.py:46
        "type": "json",
        "description": "CAP msgType values that mark an alert as gone.",
    },
    ("nws", "warning_suffix_promotes"): {
        "default": True,  # nws_handler.py:172
        "type": "bool",
        "description": "Promote category-name-ending-in-_warning to Severe when CAP severity is missing.",
    },
    ("nws", "event_emoji_map"): {
        "default": [   # nws_handler.py:49-68 (ordered substring matches)
            ["tornado", "🌪️"],
            ["severe thunderstorm", "🌩️"],
            ["thunderstorm", "🌩️"],
            ["flash flood", "🌊"],
            ["flood", "🌊"],
            ["winter storm", "❄️"],
            ["blizzard", "❄️"],
            ["ice storm", "❄️"],
            ["ice", "❄️"],
            ["excessive heat", "🌡️"],
            ["heat", "🌡️"],
            ["high wind", "🌬️"],
            ["wind", "🌬️"],
            ["fire weather", "🔥"],
            ["red flag", "🔥"],
            ["air quality", "😷"],
            ["freeze", "🥶"],
            ["frost", "🥶"],
        ],
        "type": "json",
        "description": "Ordered [substring, emoji] pairs; first case-insensitive substring match wins.",
    },
    ("nws", "expires_short_thresholds_s"): {
        "default": [21600, 604800],   # nws_handler.py:109-113  (6h, 7d)
        "type": "json",
        "description": "[under_threshold_s, weekly_threshold_s] for 'until <time>' rendering buckets.",
    },
    ("nws", "default_emoji"): {
        "default": "⚠️",  # nws_handler.py:86 fallback
        "type": "str",
        "description": "Wire emoji when no event_emoji_map substring matches.",
    },

    # =================================================================
    # USGS_QUAKE -- central/quake_handler.py
    # =================================================================
    ("usgs_quake", "regional_centroid"): {
        "default": [44.36, -114.61],   # quake_handler.py:36-37 (Idaho centroid)
        "type": "json",
        "description": "[lat, lon] of the regional gate origin; quakes within regional_radius_mi use regional_mag_floor.",
    },
    ("usgs_quake", "regional_radius_mi"): {
        "default": 250,                # quake_handler.py:38
        "type": "int",
        "description": "Radius (mi) of the regional gate around regional_centroid.",
    },
    ("usgs_quake", "broadcast_pager_alerts"): {
        "default": ["orange", "red"],  # quake_handler.py:40
        "type": "json",
        "description": "USGS PAGER alert levels that broadcast at any magnitude.",
    },
    ("usgs_quake", "global_mag_floor"): {
        "default": 3.0,                # quake_handler.py:69
        "type": "float",
        "description": "Global magnitude floor for unconditional broadcasts.",
    },
    ("usgs_quake", "regional_mag_floor"): {
        "default": 2.5,                # quake_handler.py:70
        "type": "float",
        "description": "Reduced magnitude floor for quakes within regional_radius_mi of centroid.",
    },
    ("usgs_quake", "escalate_mag_floor"): {
        "default": 5.0,                # quake_handler.py:76
        "type": "float",
        "description": "Magnitude floor for the visual ⚠️ escalation emoji.",
    },
    ("usgs_quake", "emoji_routine"): {
        "default": "🌐",
        "type": "str",
        "description": "Wire emoji for non-escalated quakes.",
    },
    ("usgs_quake", "emoji_escalate"): {
        "default": "⚠️",
        "type": "str",
        "description": "Wire emoji for quakes at or above escalate_mag_floor.",
    },
    ("usgs_quake", "emoji_tsunami"): {
        "default": "🚨",
        "type": "str",
        "description": "Wire emoji for any tsunami_warning, overrides other emoji.",
    },

    # =================================================================
    # SWPC -- central/swpc_handler.py
    # =================================================================
    ("swpc", "geomag_kp_floor"): {
        "default": 7.0,                 # swpc_handler.py:66-68 (Kp >= 7 = G3)
        "type": "float",
        "description": "Kp value at or above which geomagnetic storms broadcast.",
    },
    ("swpc", "flare_class_floor"): {
        "default": "X1",                # swpc_handler.py:40
        "type": "str",
        "description": "Minimum X-ray flare class to broadcast ('X1' = R3).",
    },
    ("swpc", "proton_pfu_floor"): {
        "default": 10.0,                # swpc_handler.py:48 (S1)
        "type": "float",
        "description": "Proton flux floor in pfu (>=10 = S1 minor radiation storm).",
    },
    ("swpc", "proton_energy_labels"): {
        "default": ["10", ">=10", ">10", ">=10 MeV", ">=10MeV",
                     "30", ">=30", ">=30 MeV", ">=50 MeV",
                     ">=100 MeV", ">=100"],   # swpc_handler.py:109-111
        "type": "json",
        "description": "Accepted energy-channel labels for proton flux validation.",
    },
    ("swpc", "emoji_geomag"): {"default": "🌌", "type": "str", "description": "Geomag-storm wire emoji."},
    ("swpc", "emoji_flare"):  {"default": "🔆", "type": "str", "description": "Solar-flare wire emoji."},
    ("swpc", "emoji_proton"): {"default": "☢️", "type": "str", "description": "Solar-radiation-storm wire emoji."},
    ("swpc", "wire_geomag_template"): {
        "default": "{emoji} {label} geomagnetic storm ({code}/{scalar}) -- HF degraded, aurora possible",
        "type": "str",
        "description": "Format template for geomag broadcasts. Available fields: emoji, code, label, scalar.",
    },
    ("swpc", "wire_flare_template"): {
        "default": "{emoji} Major solar flare ({code}/{scalar}) -- HF radio fading ~30 min, GPS may glitch",
        "type": "str",
        "description": "Format template for flare broadcasts.",
    },
    ("swpc", "wire_proton_template"): {
        "default": "{emoji} Solar radiation storm ({code}/{scalar}) -- polar HF radio affected",
        "type": "str",
        "description": "Format template for proton broadcasts.",
    },

    # =================================================================
    # USGS_NWIS -- central/nwis_handler.py + central/idaho_gauge_sites.py
    # (the 9-site curation table itself is commit #4 -- gauge_sites)
    # =================================================================
    ("usgs_nwis", "parameter_codes"): {
        "default": ["00060", "00065"],     # nwis_handler.py:57
        "type": "json",
        "description": "USGS parameter codes the handler processes (00060=discharge, 00065=gage height).",
    },
    ("usgs_nwis", "threshold_labels"): {
        "default": {                       # nwis_handler.py:60-65
            "action": "action stage",
            "flood_minor": "minor flooding",
            "flood_moderate": "moderate flooding",
            "flood_major": "major flooding",
        },
        "type": "json",
        "description": "Human-readable phrase per threshold_state band.",
    },
    ("usgs_nwis", "emoji"): {
        "default": "🌊",
        "type": "str",
        "description": "Wire emoji.",
    },
    ("usgs_nwis", "threshold_rank"): {
        "default": ["normal", "action", "flood_minor", "flood_moderate", "flood_major"],  # idaho_gauge_sites.py:110
        "type": "json",
        "description": "Threshold rank from low to high (upward-crossing detector).",
    },
    ("usgs_nwis", "broadcast_on_recede"): {
        "default": False,                  # nwis_handler.py:204-209 (silent on recede)
        "type": "bool",
        "description": "Broadcast when a gauge transitions DOWN through a threshold band.",
    },
    ("usgs_nwis", "prefix_new"): {
        "default": "New",
        "type": "str",
        "description": "Wire prefix for the first upward-crossing broadcast.",
    },

    # =================================================================
    # INCIDENT -- central/incident_handler.py (shared across 3 sources)
    # =================================================================
    ("incident", "freshness_seconds"): {
        "default": 1800,   # incident_handler.py:49 + central_normalizer.py:917 default
        "type": "int",
        "description": "Drop incidents older than this many seconds.",
    },
    ("incident", "broadcast_on_update"): {
        "default": False,  # incident_handler.py:594-602 (v0.5.9 REVISED: no Update)
        "type": "bool",
        "description": "Re-broadcast on magnitude bump / delay growth / icon flip after first New.",
    },

    # ---- TomTom-specific ----
    ("tomtom_incidents", "drop_zero_magnitude"): {
        "default": True,   # incident_handler.py:250
        "type": "bool",
        "description": "Drop envelopes with magnitude_of_delay==0.",
    },
    ("tomtom_incidents", "drop_non_present"): {
        "default": True,   # incident_handler.py:254
        "type": "bool",
        "description": "Drop envelopes whose time_validity != 'present'.",
    },
    ("tomtom_incidents", "icon_map"): {
        "default": {       # incident_handler.py:64-79 (int keys serialized as str via json)
            "0":  "incident", "1": "accident", "2": "fog", "3": "danger",
            "4":  "rain", "5": "ice", "6": "jam", "7": "lane_closed",
            "8":  "road_closed", "9": "road_works", "10": "wind",
            "11": "flooding", "12": "broken_down", "14": "incident",
        },
        "type": "json",
        "description": "TomTom icon_category int -> canonical sub_type mapping.",
    },

    # ---- state_511_atis specific ----
    ("state_511_atis", "skipped_states"): {
        "default": ["ID"],   # incident_handler.py:459-470 (v0.5.9 GAMMA cutover)
        "type": "json",
        "description": "States whose state_511_atis envelopes are silently skipped (handled by itd_511 instead).",
    },

    # ---- itd_511 specific ----
    ("itd_511", "sub_type_map"): {
        "default": {        # incident_handler.py:83-115 (shared with state_511)
            "crash": "accident",
            "incident": "incident",
            "debrisOnRoadway": "debris",
            "disabledVehicle": "disabled_vehicle",
            "vehicleOnFire": "vehicle_on_fire",
            "wildfire": "incident",
            "wildfireInArea": "incident",
            "leftLaneBlocked": "lane_closed",
            "onRampBlocked": "ramp_closed",
            "roadwayBlocked": "road_closed",
            "roadConstruction": "road_works",
            "pavementMarkingOperations": "road_works",
            "pavementMarkingOperations ": "road_works",
            "utilityWork": "road_works",
            "singleLineTraffic:AlternatingDirections": "lane_closed",
            "roadMaintenanceOperations": "road_works",
            "pavingOperations": "road_works",
            "bridgeConstruction": "road_works",
            "bridgeMaintenanceOperations": "road_works",
            "flaggingOperation": "lane_closed",
            "brushControl": "road_works",
            "constructionWork": "road_works",
            "guardrailRepairs": "road_works",
            "workOnTheShoulder": "road_works",
            "nightTimeConstructionWork": "road_works",
            "bridgeInspectionWork": "road_works",
            "longTermRoadConstruction": "road_works",
            "workOnUndergroundServices": "road_works",
            "roadsideCleanupCrew": "road_works",
            "RampRestriction": "lane_closed",
            "parade": "parade",
        },
        "type": "json",
        "description": "ITD/state_511 event_sub_type -> canonical sub_type mapping.",
    },
    ("itd_511", "sub_type_emoji"): {
        "default": {        # incident_handler.py:118-139
            "accident": "🚨", "jam": "🚗", "road_closed": "🚫", "closure": "🚫",
            "road_works": "🚧", "lane_closed": "🟠", "ramp_closed": "🟠",
            "debris": "⚠️", "vehicle_on_fire": "🔥", "disabled_vehicle": "🛑",
            "ice": "⚠️", "fog": "⚠️", "flooding": "🌊", "wind": "🌬️",
            "broken_down": "🛞", "danger": "⚠️", "rain": "⚠️",
            "incident": "⚠️", "special_event": "🎪", "parade": "🎪",
        },
        "type": "json",
        "description": "Canonical sub_type -> emoji.",
    },
    ("itd_511", "sub_type_phrase"): {
        "default": {        # incident_handler.py:142-163
            "accident": "crash", "jam": "jam", "road_closed": "road closed",
            "closure": "closure", "road_works": "road works",
            "lane_closed": "lane closed", "ramp_closed": "ramp closed",
            "debris": "debris on roadway", "vehicle_on_fire": "vehicle fire",
            "disabled_vehicle": "disabled vehicle", "ice": "icy conditions",
            "fog": "fog", "flooding": "flooding", "wind": "high wind",
            "broken_down": "broken-down vehicle", "danger": "dangerous conditions",
            "rain": "heavy rain", "incident": "incident",
            "special_event": "special event", "parade": "parade",
        },
        "type": "json",
        "description": "Canonical sub_type -> human-readable noun phrase.",
    },

    # =================================================================
    # CENTRAL consumer -- central/consumer.py
    # =================================================================
    ("central", "adapter_map"): {
        "default": {            # consumer.py:183-199 CENTRAL_ADAPTER_TO_SOURCE
            "wfigs_incidents": "fires",
            "wfigs_perimeters": "fires",
            "nwis": "usgs",
            "swpc_alerts": "swpc",
            "swpc_kindex": "swpc",
            "swpc_protons": "swpc",
            "wzdx": "traffic",
            "tomtom_incidents": "traffic",
            "state_511_atis": "roads511",
            "itd_511": "roads511",
            "firms": "firms",
        },
        "type": "json",
        "description": "Central adapter name -> meshai source name remap.",
    },
    ("central", "category_map"): {
        "default": [            # consumer.py:203-228 ordered prefix -> flat
            ["wx.alert", "weather_warning"],
            ["wx.", "weather_statement"],
            ["fire.hotspot", "wildfire_hotspot"],
            ["fire.incident", "wildfire_incident"],
            ["fire.perimeter", "wildfire_incident"],
            ["fire.", "wildfire_incident"],
            ["quake.", "earthquake_event"],
            ["hydro.", "stream_flow"],
            ["space.alert", "rf_propagation_alert"],
            ["space.kindex", "geomagnetic_storm"],
            ["space.proton", "solar_radiation_storm"],
            ["space.", "geomagnetic_storm"],
            ["disaster.", "disaster_event"],
            ["traffic_flow", "traffic_flow"],
            ["traffic_cameras", "traffic_camera"],
            ["work_zone", "work_zone"],
            ["incident", "road_incident"],
            ["closure", "road_closure"],
            ["traffic.", "traffic_congestion"],
        ],
        "type": "json",
        "description": "Ordered [prefix, flat_category] pairs; first prefix match wins.",
    },
    ("central", "severity_thresholds"): {
        "default": {            # consumer.py:264-288 (map_severity)
            "routine_max": 1,
            "priority_max": 2,
            "immediate_min": 3,
        },
        "type": "json",
        "description": "Central int severity buckets: 0..routine_max -> routine, priority_max -> priority, >= immediate_min -> immediate.",
    },

    # =================================================================
    # DISPATCHER -- notifications/pipeline/dispatcher.py
    # =================================================================
    ("dispatcher", "dedup_lru_max"): {
        "default": 10000,        # pipeline/dispatcher.py:28
        "type": "int",
        "description": "In-memory dedup OrderedDict cap. Disk has a 7-day window which may exceed this.",
    },
    ("dispatcher", "cooldown_prune_size"): {
        "default": 1024,         # pipeline/dispatcher.py:_COOLDOWN_INMEM_PRUNE_THRESHOLD
        "type": "int",
        "description": "In-memory cooldown map size that triggers a 2*cooldown_s prune.",
    },
    ("dispatcher", "cooldown_prune_multiplier"): {
        "default": 2,            # pipeline/dispatcher.py:184 (2 * cooldown_s)
        "type": "int",
        "description": "Cooldown-prune cutoff multiplier (rows older than N * cooldown_s deleted).",
    },
    ("dispatcher", "dedup_db_retention_days"): {
        "default": 7,            # pipeline/dispatcher.py:_DEDUP_DB_RETENTION_S = 7*86400
        "type": "int",
        "description": "Days a (source, event_id) dedup row stays on disk before the on-insert cleanup deletes it.",
    },

    # =================================================================
    # BAND_CONDITIONS -- notifications/scheduled/band_conditions.py
    # =================================================================
    ("band_conditions", "swpc_freshness_seconds"): {
        "default": 21600,        # band_conditions.py:45 (_SWPC_FRESHNESS_S)
        "type": "int",
        "description": "If swpc_events readings older than this, fall through to HamQSL.",
    },
    ("band_conditions", "slot_labels"): {
        "default": {             # band_conditions.py:54-58
            "06:00": ["☀️", "Day Propagation"],
            "14:00": ["🌞", "Day Propagation"],
            "22:00": ["🌙", "Night Propagation"],
        },
        "type": "json",
        "description": "Per-slot [emoji, headline] -- chosen by SLOT time, not actual fire time.",
    },
    ("band_conditions", "band_order"): {
        "default": ["80-40m", "30-20m", "17-15m", "12-10m"],   # band_conditions.py:62
        "type": "json",
        "description": "Wire-string band-row order. Matches HamQSL groupings.",
    },
    ("band_conditions", "hamqsl_url"): {
        "default": "https://www.hamqsl.com/solarxml.php",   # band_conditions.py:65
        "type": "str",
        "description": "HamQSL solarxml fallback URL (public, no auth).",
    },
    ("band_conditions", "hamqsl_timeout_s"): {
        "default": 5,            # band_conditions.py:66
        "type": "int",
        "description": "HamQSL fetch timeout.",
    },
    ("band_conditions", "rating_emoji"): {
        "default": {             # band_conditions.py:49
            "Good": "🟢", "Fair": "🟡", "Poor": "🔴",
        },
        "type": "json",
        "description": "Per-rating colour emoji.",
    },
    # Heuristic threshold blob -- one document so the GUI can render it as
    # a structured editor in v0.6-3c.
    ("band_conditions", "heuristic"): {
        "default": {             # band_conditions.py:194-240 (_heuristic_ratings)
            # All thresholds map to Good / Fair / Poor.
            # Format: {band: {day_or_night: {rule_name: value}}}
            "80-40m": {
                "night": {"good_kp_lt": 4,  "good_sfi_gt": 70, "fair_kp_lt": 5},
                "day":   {"fair_kp_lt": 4},
            },
            "30-20m": {
                "day":   {"good_sfi_gt": 120, "good_kp_lt": 4,
                          "fair_sfi_min": 80, "fair_sfi_max": 120, "fair_kp_lt": 6,
                          "poor_sfi_lt": 80,  "poor_kp_min": 6},
                "night": {"good_sfi_gt": 110, "good_kp_lt": 4, "fair_kp_lt": 5},
            },
            "17-15m": {
                "day":   {"good_sfi_gt": 120, "fair_sfi_min": 90, "fair_sfi_max": 120, "fair_kp_lt": 5},
            },
            "12-10m": {
                "day":   {"good_sfi_gt": 140, "fair_sfi_min": 110, "fair_sfi_max": 140},
            },
        },
        "type": "json",
        "description": "Per-band day/night Good/Fair/Poor thresholds. Edit by hand only.",
    },
    ("band_conditions", "fallback_kp"): {
        "default": 9.0,           # band_conditions.py:196 (state default when sfi missing)
        "type": "float",
        "description": "Default Kp when none persisted (worst-case).",
    },
    ("band_conditions", "fallback_sfi"): {
        "default": 50.0,          # band_conditions.py:197 (state default)
        "type": "float",
        "description": "Default SFI when none persisted (worst-case).",
    },

    # =================================================================
    # GEOCODER -- central_normalizer.py (Photon section)
    # =================================================================
    ("geocoder", "photon_url"): {
        "default": "http://100.64.0.24:2322",   # central_normalizer.py:282
        "type": "str",
        "description": "Photon base URL (Tailscale-internal Echo6 instance).",
    },
    ("geocoder", "photon_timeout_s"): {
        "default": 2.0,           # central_normalizer.py:284
        "type": "float",
        "description": "Photon HTTP timeout.",
    },
    ("geocoder", "photon_radius_km"): {
        "default": 80,            # central_normalizer.py:285
        "type": "int",
        "description": "Photon /reverse search radius (~50 mi default).",
    },
    ("geocoder", "photon_limit"): {
        "default": 10,            # central_normalizer.py:286
        "type": "int",
        "description": "Photon /reverse max features per call.",
    },
    ("geocoder", "town_osm_values"): {
        "default": ["city", "town", "village", "hamlet", "suburb", "locality"],   # central_normalizer.py:289
        "type": "json",
        "description": "OSM place classes we accept as 'town' for nearest_town.",
    },
    ("geocoder", "h3_resolution"): {
        "default": 7,             # central_normalizer.py:296
        "type": "int",
        "description": "H3 resolution for the Photon-cache hex grid (~5 km cells).",
    },
    ("geocoder", "h3_cache_max"): {
        "default": 10000,         # central_normalizer.py:297
        "type": "int",
        "description": "Max H3 cache entries before LRU eviction.",
    },

    # =================================================================
    # FIRMS -- central/firms_handler.py (v0.6-1)
    # =================================================================
    ("firms", "confidence_floor"): {
        "default": "low",          # firms_handler.py FIRMS_CONFIDENCE_FLOOR
        "type": "str",
        "description": "Min FIRMS confidence to store ('low' = store all).",
    },
    ("firms", "frp_floor"): {
        "default": 0.0,           # firms_handler.py FIRMS_FRP_FLOOR
        "type": "float",
        "description": "Min FRP (MW) to store; 0 = store every detection.",
    },
    ("firms", "bbox"): {
        "default": None,          # firms_handler.py FIRMS_BBOX_OPTIONAL
        "type": "json",
        "description": "Optional [min_lat, min_lon, max_lat, max_lon] spatial filter (null = no filter).",
    },
    ("firms", "dedup_lat_lon_decimals"): {
        "default": 5,             # firms_handler.py _DEDUP_LAT_LON_DECIMALS
        "type": "int",
        "description": "Decimal places for lat/lon dedup-key rounding (5 = ~1.1 m).",
    },

    # =================================================================
    # PIPELINE (inhibitor + grouper -- audit doc A.9)
    # =================================================================
    ("pipeline", "inhibitor_ttl_seconds"): {
        "default": 1800,         # pipeline/inhibitor.py:27 constructor default, never config-driven
        "type": "int",
        "description": "How long an inhibit_key remains active after the originating event.",
    },
    ("pipeline", "grouper_window_seconds"): {
        "default": 60,           # pipeline/grouper.py:27 constructor default
        "type": "int",
        "description": "How long to hold a group_key before emitting downstream.",
    },
}


# -------- ADAPTER_META ----------------------------------------------------
#
# One row per adapter the GUI surfaces. include_in_llm_context controls
# whether the env_reporter (commit #5) is allowed to read this adapter's
# tables when assembling LLM context for a DM. Defaults to True per
# Matt's locked refinement.

ADAPTER_META: dict[str, dict[str, Any]] = {
    "wfigs": {
        "display_name": "WFIGS wildfire incidents",
        "include_in_llm_context": True,
        "description": "NIFC-authoritative wildfire registry (named incidents, acres, containment).",
    },
    "firms": {
        "display_name": "FIRMS satellite hotspots",
        "include_in_llm_context": True,
        "description": "NASA VIIRS/MODIS heat-pixel feed. Storage-only (no broadcast).",
    },
    "nws": {
        "display_name": "NWS weather alerts",
        "include_in_llm_context": True,
        "description": "CAP-formatted severe-weather warnings/watches/advisories.",
    },
    "usgs_quake": {
        "display_name": "USGS earthquakes",
        "include_in_llm_context": True,
        "description": "Real-time earthquake feed with Idaho-regional + global tiers.",
    },
    "swpc": {
        "display_name": "SWPC space weather",
        "include_in_llm_context": True,
        "description": "Geomagnetic / flare / proton storm alerts (G/R/S scale).",
    },
    "usgs_nwis": {
        "display_name": "USGS NWIS stream gauges",
        "include_in_llm_context": True,
        "description": "Real-time stream-gauge readings (Idaho curated sites).",
    },
    "tomtom_incidents": {
        "display_name": "TomTom traffic incidents",
        "include_in_llm_context": True,
        "description": "Real-time crashes/jams/closures (TomTom feed).",
    },
    "state_511_atis": {
        "display_name": "Castle Rock state 511 ATIS",
        "include_in_llm_context": True,
        "description": "Multi-state ATIS feed (Idaho cutover to itd_511 in v0.5.9 GAMMA).",
    },
    "itd_511": {
        "display_name": "ITD 511 (Idaho)",
        "include_in_llm_context": True,
        "description": "Idaho Transportation Department incident/closure/work-zone feed.",
    },
    "band_conditions": {
        "display_name": "Band conditions (HF propagation)",
        "include_in_llm_context": True,
        "description": "3x/day scheduled broadcast of HF band ratings (SWPC-local + HamQSL fallback).",
    },
    "central": {
        "display_name": "Central consumer routing",
        "include_in_llm_context": False,
        "description": "Adapter <-> source remap, hierarchical category map, severity buckets. Operational, not LLM-relevant.",
    },
    "dispatcher": {
        "display_name": "Dispatcher state",
        "include_in_llm_context": True,
        "description": "Cold-start anchor, cumulative drop counters, cooldown + dedup state. Useful for 'why did we drop X?' answers.",
    },
    "geocoder": {
        "display_name": "Geocoder (Photon + H3)",
        "include_in_llm_context": False,
        "description": "Photon-reverse + H3 cache settings. Operational, not LLM-relevant.",
    },
    "incident": {
        "display_name": "Incident pipeline (shared settings)",
        "include_in_llm_context": True,
        "description": "Settings shared across tomtom_incidents / state_511_atis / itd_511.",
    },
    "pipeline": {
        "display_name": "Notification pipeline (Inhibitor + Grouper)",
        "include_in_llm_context": True,
        "description": "TTL + window tunables for the Inhibitor and Grouper stages.",
    },
}


# Convenience views.

def all_adapters() -> set[str]:
    """Set of every adapter name referenced by REGISTRY or ADAPTER_META."""
    return {adapter for adapter, _ in REGISTRY} | set(ADAPTER_META)


def registry_for(adapter: str) -> dict[str, dict[str, Any]]:
    """Subset of REGISTRY for one adapter, keyed by key only."""
    return {k: v for (a, k), v in REGISTRY.items() if a == adapter}
