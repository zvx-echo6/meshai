"""v0.6-3a.1 trimmed adapter_config defaults registry.

Per Matt's locked CONFIG-vs-CODE rule:

  CONFIG (lives here):
    where we send (channels), how often (cadences/schedules),
    thresholds (magnitude floors, severity gates, distance radius,
    cooldown durations, freshness windows), curation data (which
    sites/states/codes), toggles (enabled, include_in_llm_context,
    drop_zero_magnitude).

  CODE (stays in the handlers; not surfaced to the GUI):
    sentence templates, emoji choices, mapping/translation functions
    (TomTom icon_map, ITD sub_type_map, Central adapter_map and
    category_map), rendering logic (anchor priority order,
    expires-buckets formatting, threshold-state labels), heuristic
    logic (band_conditions Kp/SFI -> Good/Fair/Poor function).

Trimmed from the v0.6-3a draft of 77 keys down to 43. The 34 dropped
keys are removed from the live DB on first boot by prune_orphans(),
which logs each delete at INFO level so docker logs carry a paper trail.

Adding a new tunable:
    1. Add an entry to REGISTRY below with default + type + description.
    2. Confirm it matches the CONFIG rule (if you're tempted to add a
       sentence template, an emoji, or a translation map, STOP -- that's
       CODE).
    3. The next container restart calls seed_defaults() which
       INSERT OR IGNOREs the row.
    4. Wire the handler to read from adapter_config.<adapter>.<key>.
"""
from __future__ import annotations

from typing import Any


# REGISTRY[(adapter, key)] = {"default": ..., "type": ..., "description": ...}
# Type vocabulary: "int" | "float" | "str" | "bool" | "json"
REGISTRY: dict[tuple[str, str], dict[str, Any]] = {

    # =================================================================
    # WFIGS -- 4 settings (cooldown, anchor radius, two re-broadcast toggles)
    # =================================================================
    ("wfigs", "cooldown_seconds"): {
        "default": 28800,                   # central/wfigs_handler.py:43
        "type": "int",
        "description": "Per-fire broadcast cooldown in seconds (forward-only Update gate).",
    },
    ("wfigs", "anchor_max_mi"): {
        "default": 100.0,                   # central/wfigs_handler.py:322
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
    # NWS -- 3 settings (severity gate, tombstone msgTypes, suffix-promote toggle)
    # =================================================================
    ("nws", "broadcast_severities"): {
        "default": ["Extreme", "Severe"],   # nws_handler.py:43
        "type": "json",
        "description": "CAP severity strings allowed onto the mesh.",
    },
    ("nws", "tombstone_msgtypes"): {
        "default": ["Cancel", "Expire"],    # nws_handler.py:46
        "type": "json",
        "description": "CAP msgType values that mark an alert as gone.",
    },
    ("nws", "warning_suffix_promotes"): {
        "default": True,                    # nws_handler.py:172
        "type": "bool",
        "description": "Promote category-name-ending-in-_warning to Severe when CAP severity is missing.",
    },

    # =================================================================
    # USGS_QUAKE -- 6 settings (regional geography + 3 mag floors + PAGER set)
    # =================================================================
    ("usgs_quake", "regional_centroid"): {
        "default": [44.36, -114.61],        # quake_handler.py:36-37 (Idaho centroid)
        "type": "json",
        "description": "[lat, lon] of the regional gate origin; quakes within regional_radius_mi use regional_mag_floor.",
    },
    ("usgs_quake", "regional_radius_mi"): {
        "default": 250,                     # quake_handler.py:38
        "type": "int",
        "description": "Radius (mi) of the regional gate around regional_centroid.",
    },
    ("usgs_quake", "broadcast_pager_alerts"): {
        "default": ["orange", "red"],       # quake_handler.py:40
        "type": "json",
        "description": "USGS PAGER alert levels that broadcast at any magnitude.",
    },
    ("usgs_quake", "global_mag_floor"): {
        "default": 3.0,                     # quake_handler.py:69
        "type": "float",
        "description": "Global magnitude floor for unconditional broadcasts.",
    },
    ("usgs_quake", "regional_mag_floor"): {
        "default": 2.5,                     # quake_handler.py:70
        "type": "float",
        "description": "Reduced magnitude floor for quakes within regional_radius_mi of centroid.",
    },
    ("usgs_quake", "escalate_mag_floor"): {
        "default": 5.0,                     # quake_handler.py:76
        "type": "float",
        "description": "Magnitude floor for the visual escalation emoji.",
    },

    # =================================================================
    # SWPC -- 3 settings (three storm-tier broadcast floors)
    # =================================================================
    ("swpc", "geomag_kp_floor"): {
        "default": 7.0,                     # swpc_handler.py:66-68 (Kp >= 7 = G3)
        "type": "float",
        "description": "Kp value at or above which geomagnetic storms broadcast.",
    },
    ("swpc", "flare_class_floor"): {
        "default": "X1",                    # swpc_handler.py:40
        "type": "str",
        "description": "Minimum X-ray flare class to broadcast ('X1' = R3).",
    },
    ("swpc", "proton_pfu_floor"): {
        "default": 10.0,                    # swpc_handler.py:48 (S1)
        "type": "float",
        "description": "Proton flux floor in pfu (>=10 = S1 minor radiation storm).",
    },

    # =================================================================
    # USGS_NWIS -- 2 settings (parameter-code curation + recede toggle)
    # =================================================================
    ("usgs_nwis", "parameter_codes"): {
        "default": ["00060", "00065"],      # nwis_handler.py:57
        "type": "json",
        "description": "USGS parameter codes the handler processes (00060=discharge, 00065=gage height).",
    },
    ("usgs_nwis", "broadcast_on_recede"): {
        "default": False,                   # nwis_handler.py:204-209
        "type": "bool",
        "description": "Broadcast when a gauge transitions DOWN through a threshold band.",
    },

    # =================================================================
    # INCIDENT -- 2 settings (shared freshness gate + Update-after-New toggle)
    # =================================================================
    ("incident", "freshness_seconds"): {
        "default": 1800,                    # incident_handler.py:49 + central_normalizer.py:917
        "type": "int",
        "description": "Drop incidents older than this many seconds.",
    },
    ("incident", "broadcast_on_update"): {
        "default": False,                   # incident_handler.py:594-602 (v0.5.9 REVISED)
        "type": "bool",
        "description": "Re-broadcast on magnitude bump / delay growth / icon flip after first New.",
    },

    # =================================================================
    # TOMTOM_INCIDENTS -- 2 settings (per-source drop toggles)
    # =================================================================
    ("tomtom_incidents", "drop_zero_magnitude"): {
        "default": True,                    # incident_handler.py:250
        "type": "bool",
        "description": "Drop envelopes with magnitude_of_delay==0.",
    },
    ("tomtom_incidents", "drop_non_present"): {
        "default": True,                    # incident_handler.py:254
        "type": "bool",
        "description": "Drop envelopes whose time_validity != 'present'.",
    },

    # =================================================================
    # STATE_511_ATIS -- 1 setting (states to skip in favor of itd_511)
    # =================================================================
    ("state_511_atis", "skipped_states"): {
        "default": ["ID"],                  # incident_handler.py:459-470 (v0.5.9 GAMMA)
        "type": "json",
        "description": "States whose state_511_atis envelopes are silently skipped (handled by itd_511 instead).",
    },

    # =================================================================
    # ITD_511 -- 0 settings (its maps/emoji/phrases are CODE, not config)
    # =================================================================

    # =================================================================
    # CENTRAL consumer -- 1 setting (severity-int bucket boundaries)
    # =================================================================
    ("central", "severity_thresholds"): {
        "default": {"routine_max": 1, "priority_max": 2, "immediate_min": 3},
        "type": "json",
        "description": "Central int severity buckets: 0..routine_max -> routine, priority_max -> priority, >= immediate_min -> immediate.",
    },

    # =================================================================
    # DISPATCHER -- 4 settings (LRU cap + cooldown prune params + retention)
    # =================================================================
    ("dispatcher", "dedup_lru_max"): {
        "default": 10000,                   # pipeline/dispatcher.py:28
        "type": "int",
        "description": "In-memory dedup OrderedDict cap. Disk has a 7-day window which may exceed this.",
    },
    ("dispatcher", "cooldown_prune_size"): {
        "default": 1024,                    # _COOLDOWN_INMEM_PRUNE_THRESHOLD
        "type": "int",
        "description": "In-memory cooldown map size that triggers a 2*cooldown_s prune.",
    },
    ("dispatcher", "cooldown_prune_multiplier"): {
        "default": 2,                       # pipeline/dispatcher.py:184 (2*cooldown_s)
        "type": "int",
        "description": "Cooldown-prune cutoff multiplier (rows older than N*cooldown_s deleted).",
    },
    ("dispatcher", "dedup_db_retention_days"): {
        "default": 7,                       # _DEDUP_DB_RETENTION_S
        "type": "int",
        "description": "Days a (source, event_id) dedup row stays on disk before the on-insert cleanup deletes it.",
    },

    # =================================================================
    # BAND_CONDITIONS -- 3 settings (SWPC freshness + HamQSL endpoint config)
    # (schedule, tz, enabled stay in YAML config.notifications.band_conditions_*)
    # =================================================================
    ("band_conditions", "swpc_freshness_seconds"): {
        "default": 21600,                   # band_conditions.py:45
        "type": "int",
        "description": "If swpc_events readings older than this, fall through to HamQSL.",
    },
    ("band_conditions", "hamqsl_url"): {
        "default": "https://www.hamqsl.com/solarxml.php",
        "type": "str",
        "description": "HamQSL solarxml fallback URL.",
    },
    ("band_conditions", "hamqsl_timeout_s"): {
        "default": 5,
        "type": "int",
        "description": "HamQSL fetch timeout.",
    },

    # =================================================================
    # GEOCODER -- 6 settings (Photon endpoint + curation + cache size)
    # =================================================================
    ("geocoder", "photon_url"): {
        "default": "http://100.64.0.24:2322",
        "type": "str",
        "description": "Photon base URL (Tailscale-internal Echo6 instance).",
    },
    ("geocoder", "photon_timeout_s"): {
        "default": 2.0,
        "type": "float",
        "description": "Photon HTTP timeout.",
    },
    ("geocoder", "photon_radius_km"): {
        "default": 80,
        "type": "int",
        "description": "Photon /reverse search radius (~50 mi default).",
    },
    ("geocoder", "photon_limit"): {
        "default": 10,
        "type": "int",
        "description": "Photon /reverse max features per call.",
    },
    ("geocoder", "town_osm_values"): {
        "default": ["city", "town", "village", "hamlet", "suburb", "locality"],
        "type": "json",
        "description": "OSM place classes that count as a town for the nearest_town anchor.",
    },
    ("geocoder", "h3_cache_max"): {
        "default": 10000,                   # central_normalizer.py:297
        "type": "int",
        "description": "Max H3 cache entries before LRU eviction.",
    },

    # =================================================================
    # FIRES -- 1 setting (default attribution radius for FIRMS -> fire matching)
    # =================================================================
    # Per-fire override lives in the fires.spread_radius_mi column; this
    # is the global default used when that column is NULL. v0.7-fire-1
    # ships with 5 mi based on the design doc's open question #1
    # ("Spread radius default. Start with 5 mi per fire?"). Tune from
    # operations once we have a week of observed attribution rates.
    ("fires", "spread_radius_mi_default"): {
        "default": 5.0,
        "type": "float",
        "description": "Default attribution radius for FIRMS hotspot -> fire matching, miles. Per-fire override in fires.spread_radius_mi.",
    },

    # =================================================================
    # FIRMS -- 7 settings (storage floors + dedup + 3 v0.7 cluster knobs)
    # =================================================================
    ("firms", "confidence_floor"): {
        "default": "low",                   # firms_handler.py FIRMS_CONFIDENCE_FLOOR
        "type": "str",
        "description": "Min FIRMS confidence to store ('low' = store all).",
    },
    ("firms", "frp_floor"): {
        "default": 0.0,                     # firms_handler.py FIRMS_FRP_FLOOR
        "type": "float",
        "description": "Min FRP (MW) to store; 0 = store every detection.",
    },
    ("firms", "bbox"): {
        "default": None,                    # firms_handler.py FIRMS_BBOX_OPTIONAL
        "type": "json",
        "description": "Optional [min_lat, min_lon, max_lat, max_lon] spatial filter (null = no filter).",
    },
    ("firms", "dedup_distance_m"): {
        # v0.6-3a.1 (Matt's call): user-facing unit is METERS, not decimal
        # places. firms_handler internally translates this to a lat/lon
        # quantization step (1 deg ~ 111 km so step_deg = m / 111_000).
        # Default 5m is slightly coarser than the v0.6-1 implementation's
        # 1.1m (round(.,5)) -- the actual wire-up + index update lands in
        # v0.6-3b (firms handler wiring step).
        "default": 5,
        "type": "int",
        "description": "Distance in meters within which two FIRMS pixel observations from the same satellite + acquisition time are considered duplicates.",
    },

    # ---- v0.7-fire-tracker-1 unattributed-cluster knobs ----
    # On every FIRMS pixel that fails attribution to any known fire, the
    # handler asks: "are there enough other unattributed pixels nearby
    # right now to suggest a new ignition?" The three knobs below define
    # "enough", "nearby", and "right now". Defaults match design doc
    # open question #6 ("3 pixels within 1 mi") -- tune from ops once we
    # have false-positive data.
    ("firms", "cluster_min_pixels"): {
        "default": 3,
        "type": "int",
        "description": "Minimum unattributed pixels within cluster_max_radius_mi over cluster_time_window_minutes to fire an unattributed_hotspot_cluster broadcast.",
    },
    ("firms", "cluster_max_radius_mi"): {
        "default": 1.0,
        "type": "float",
        "description": "Spatial radius (miles) defining a candidate hotspot cluster.",
    },
    ("firms", "cluster_time_window_minutes"): {
        "default": 60,
        "type": "int",
        "description": "Temporal window (minutes); unattributed pixels older than this don't count toward a new cluster.",
    },

    # =================================================================
    # PIPELINE (Inhibitor + Grouper) -- 2 settings
    # =================================================================
    ("pipeline", "inhibitor_ttl_seconds"): {
        "default": 1800,                    # pipeline/inhibitor.py:27 default
        "type": "int",
        "description": "How long an inhibit_key remains active after the originating event.",
    },
    ("pipeline", "grouper_window_seconds"): {
        "default": 60,                      # pipeline/grouper.py:27 default
        "type": "int",
        "description": "How long to hold a group_key before emitting downstream.",
    },
    ("pipeline", "env_reporter_block_chars"): {
        "default": 3000,
        "type": "int",
        "description": "Max chars per env_reporter block injected into the LLM system prompt.",
    },
    # =================================================================
    # v0.6-phase3 reminders: per-adapter clock-driven re-broadcast config.
    # =================================================================
    ("reminders_wfigs", "cadence_kind"): {
        "default": "interval",
        "type": "str",
        "description": "Reminder cadence kind (interval | clock).",
    },
    ("reminders_wfigs", "cadence_value"): {
        "default": 28800,                  # 8h
        "type": "json",
        "description": "Cadence value: int seconds for interval, list of HH:MM strings for clock.",
    },
    ("reminders_wfigs", "channels"): {
        "default": ["mesh_broadcast"],
        "type": "json",
        "description": "Channel types for the reminder broadcast.",
    },
    ("reminders_wfigs", "terminate_when"): {
        "default": ["tombstone", "containment_100", "last_event_age_24h"],
        "type": "json",
        "description": "Stop reminding when any of these conditions is true.",
    },

    ("reminders_swpc", "cadence_kind"): {
        "default": "interval",
        "type": "str",
        "description": "Reminder cadence kind (interval | clock).",
    },
    ("reminders_swpc", "cadence_value"): {
        "default": 28800,                  # 8h
        "type": "json",
        "description": "Cadence value: int seconds for interval, list of HH:MM strings for clock.",
    },
    ("reminders_swpc", "channels"): {
        "default": ["mesh_broadcast"],
        "type": "json",
        "description": "Channel types for the reminder broadcast.",
    },
    ("reminders_swpc", "terminate_when"): {
        "default": ["tombstone", "end_date_passed"],
        "type": "json",
        "description": "Stop reminding when any of these conditions is true.",
    },

    ("reminders_itd_511_work_zone", "cadence_kind"): {
        "default": "clock",
        "type": "str",
        "description": "Reminder cadence kind (interval | clock).",
    },
    ("reminders_itd_511_work_zone", "cadence_value"): {
        "default": ["08:00"],
        "type": "json",
        "description": "List of HH:MM clock slots (local timezone) when reminders fire.",
    },
    ("reminders_itd_511_work_zone", "channels"): {
        "default": ["mesh_broadcast"],
        "type": "json",
        "description": "Channel types for the reminder broadcast.",
    },
    ("reminders_itd_511_work_zone", "dow_mask"): {
        "default": [True, True, True, True, True, True, True],
        "type": "json",
        "description": "Day-of-week enable mask (Mon..Sun).",
    },
    ("reminders_itd_511_work_zone", "timezone"): {
        "default": "America/Boise",
        "type": "str",
        "description": "Timezone for the clock slots.",
    },
    ("reminders_itd_511_work_zone", "terminate_when"): {
        "default": ["tombstone", "end_date_passed"],
        "type": "json",
        "description": "Stop reminding when any of these conditions is true.",
    },

    # NWS dedup-window relaxation (separate from reminders by design).
    ("nws", "duplicate_allowed_after_seconds"): {
        "default": 10800,                  # 3h
        "type": "int",
        "description": "Allow re-broadcast of the same CAP id after this many seconds (the nws_handler relaxes its dedup gate past this point and uses an Active: prefix).",
    },

}


# -------- ADAPTER_META ----------------------------------------------------
#
# Per-adapter metadata. One row per adapter the GUI surfaces; the row
# survives even when an adapter has zero config keys, because the
# include_in_llm_context toggle is still meaningful (the user wants the
# LLM to be able to see traffic_events from itd_511 even though all of
# its render-side stuff is now CODE).

ADAPTER_META: dict[str, dict[str, Any]] = {
    "wfigs": {
        "display_name": "WFIGS wildfire incidents",
        "include_in_llm_context": True,
        "reminder_enabled": True,
        "description": "NIFC-authoritative wildfire registry (named incidents, acres, containment).",
    },
    # v0.7-fire-tracker-1: "fires" is not a feed; it's the registry table
    # populated by WFIGS first-sight + FIRMS attribution. Surfacing it as
    # an adapter_meta family lets the GUI show "spread radius default"
    # alongside the per-feed knobs.
    "fires": {
        "display_name": "Fire registry",
        "include_in_llm_context": True,
        "description": "Cross-feed fire registry: WFIGS declares them; FIRMS pixels grow them. spread_radius_mi_default tunes the attribution gate.",
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
        "reminder_enabled": True,
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
        "description": "Adapter <-> source remap + severity buckets. Operational, not LLM-relevant.",
    },
    "dispatcher": {
        "display_name": "Dispatcher state",
        "include_in_llm_context": True,
        "description": "Cold-start anchor, cumulative drop counters, cooldown + dedup state. Useful for 'why did we drop X?' answers.",
    },
    "geocoder": {
        "display_name": "Geocoder (Photon)",
        "include_in_llm_context": False,
        "description": "Photon-reverse settings + town-class curation. Operational, not LLM-relevant.",
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

    # v0.6-phase3 reminder pseudo-adapters: each carries the per-adapter
    # ReminderScheduler config. Their adapter_meta rows exist so the GUI
    # surfaces them; include_in_llm_context is True so the LLM can answer
    # "are reminders firing for fires right now?".
    "reminders_wfigs": {
        "display_name": "Reminders (WFIGS fires)",
        "include_in_llm_context": True,
        "description": "Per-fire Active: reminders. 8h interval by default.",
    },
    "reminders_swpc": {
        "display_name": "Reminders (SWPC space weather)",
        "include_in_llm_context": True,
        "description": "Active: reminders for ongoing G-storm / R-flare / S-radiation events. 8h interval.",
    },
    "reminders_itd_511_work_zone": {
        "display_name": "Reminders (ITD 511 work zones)",
        "include_in_llm_context": True,
        "description": "Clock-driven daily reminders for active road-works zones (default 08:00 Mountain).",
    },
    "itd_511_work_zone": {
        "display_name": "ITD 511 (work zones, reminder-eligible)",
        "include_in_llm_context": True,
        "reminder_enabled": True,
        "description": "Subset of itd_511 traffic_events filtered to work-zone sub_type, used as the reminder target.",
    },
}


# Convenience views.

def all_adapters() -> set[str]:
    """Set of every adapter name referenced by REGISTRY or ADAPTER_META."""
    return {adapter for adapter, _ in REGISTRY} | set(ADAPTER_META)


def registry_for(adapter: str) -> dict[str, dict[str, Any]]:
    """Subset of REGISTRY for one adapter, keyed by key only."""
    return {k: v for (a, k), v in REGISTRY.items() if a == adapter}
