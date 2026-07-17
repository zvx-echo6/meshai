"""Gating/budgeting decision registry for the Phase-1+ dispatch path.

Phase-1: earthquake_event registered (quake.py).
All other categories retain their existing handler-module gating.

Usage (future phases):
    from meshai.notifications.gating import register
    from meshai.notifications.gating.base import GateResult

    @register("earthquake_event")
    def _gate_quake(event, *, now: float) -> GateResult:
        ...
"""

from typing import Callable, Optional

# Populated by per-category gating modules imported below.
DECIDERS: dict = {}


def register(category: str, fn: Callable) -> Callable:
    """Register a gating callable for a category (or family toggle name)."""
    DECIDERS[category] = fn
    return fn


def get_decider(category: str) -> Optional[Callable]:
    """Return the gating decider for *category*, or None if none is registered.

    Resolution order:
      1. Direct category match in DECIDERS.
      2. Family/toggle fallback: look up the category's toggle name via
         get_toggle(), then check DECIDERS for that toggle key.
      3. None — caller falls through to legacy per-handler gating logic.
    """
    fn = DECIDERS.get(category)
    if fn is not None:
        return fn
    # Family fallback — mirrors the toggle lookup pattern in composer.py.
    try:
        from meshai.notifications.categories import get_toggle
        tog = get_toggle(category)
        if tog:
            fn = DECIDERS.get(tog)
            if fn is not None:
                return fn
    except Exception:
        pass
    return None


# ── Phase-1 registrations ────────────────────────────────────────────────────
from meshai.notifications.gating import quake as _quake_gate_mod  # noqa: E402,F401
register("earthquake_event", _quake_gate_mod.decide)

from meshai.notifications.gating import avalanche as _avy_gate_mod  # noqa: E402,F401
register("avalanche_warning", _avy_gate_mod.decide)
register("avalanche_watch", _avy_gate_mod.decide)

# SWPC: geomagnetic_storm (swpc_kindex / native G-scale), rf_propagation_alert
# (swpc_alerts flare / native R-scale), and solar_radiation_storm (native
# S-scale proton events). One decider handles all three; the broadcast floor
# is G3+/R3+ for geomag/flare but S1+ for proton — see
# gating/swpc.py's _floor_for_scale() docstring for why S has a lower floor
# (it recovers central/swpc_handler.py's original proton threshold, not a
# new policy).
from meshai.notifications.gating import swpc as _swpc_gate_mod  # noqa: E402,F401
register("geomagnetic_storm", _swpc_gate_mod.decide)
register("rf_propagation_alert", _swpc_gate_mod.decide)
register("solar_radiation_storm", _swpc_gate_mod.decide)

# Phase-2: NWS weather alerts (weather_warning + weather_statement +
# weather_watch + weather_advisory).  All four categories share the same
# NWS gating decider; the severity override in _severity_override_for()
# correctly fires only for _warning categories.
from meshai.notifications.gating import nws as _nws_gate_mod  # noqa: E402,F401
register("weather_warning",  _nws_gate_mod.decide)
register("weather_statement", _nws_gate_mod.decide)
register("weather_watch",    _nws_gate_mod.decide)
register("weather_advisory", _nws_gate_mod.decide)

# Phase-2: incident / roads categories.
# One decider handles all four; it reads external_id to decide dedup path.
from meshai.notifications.gating import incident as _incident_gate_mod  # noqa: E402,F401
register("work_zone",          _incident_gate_mod.decide)
register("road_incident",      _incident_gate_mod.decide)
register("road_closure",       _incident_gate_mod.decide)
register("traffic_congestion", _incident_gate_mod.decide)

# Phase-3: USGS NWIS stream-gauge hydro. Registered under `stream_flow` — the
# flat category the Central nwis path produces for every `central.hydro.*`
# envelope (map_category "hydro." -> "stream_flow") — AND under the two
# threshold-classified categories env/usgs.py actually emits natively:
# stream_flood_warning (at/above flood stage) and stream_high_water (action
# stage). All three share the same hydro.decide() gate; store._emit_event
# forces the native two onto the live decider path unconditionally via
# cutover.NATIVE_ALWAYS_DECIDE (mirrors the native fire categories below),
# independent of the MESHAI_CUTOVER_CATEGORIES shadow-bake env var, since
# Central never emits those two category strings.
from meshai.notifications.gating import hydro as _hydro_gate_mod  # noqa: E402,F401
register("stream_flow", _hydro_gate_mod.decide)
register("stream_flood_warning", _hydro_gate_mod.decide)
register("stream_high_water", _hydro_gate_mod.decide)

# Phase-3b: WFIGS wildfire. Three explicit categories the wfigs_handler emits:
# `wildfire_declared` (New), `wildfire_incident` (growth Update), and
# `wildfire_closed` (tombstone all-clear). One decider handles all three; it
# keys off canonical `_kind` (wfigs_incident / wfigs_tombstone / wfigs_perimeter)
# to route New/Update/suppress vs the all-clear eligibility. Registered under
# explicit strings (not the "fire" toggle) so FIRMS categories are untouched —
# FIRMS + native env/fires.py are a deferred follow-up (env/fires.py lacks the
# IRWIN/FireCause/landclass fields and has no tombstone concept, and since fire
# is NOT cut over, store._emit_event's native decider hook won't run it).
from meshai.notifications.gating import fire as _fire_gate_mod  # noqa: E402,F401
register("wildfire_declared", _fire_gate_mod.decide)
register("wildfire_incident", _fire_gate_mod.decide)
register("wildfire_closed",   _fire_gate_mod.decide)

# Phase-3c: FIRMS fusion broadcasts (wildfire_growth / wildfire_spotting /
# wildfire_halted). One decider handles all three; it keys off the handler-
# stamped internal `_kind` (firms_growth / firms_spotting / firms_halt). The
# growth/spotting/halt DECISIONS move here; the eager latch writes
# (last_spotting_broadcast_at / halt_broadcast_at) move into the deferred commit
# closures (an intended tier-b change). All attribution / pass / centroid /
# perimeter plumbing stays INLINE in firms_handler. NOTE: wildfire_growth's
# FORMATTER is fire.py but its DECIDER is firms.py (own decision math). The dead
# unattributed_hotspot_cluster path and native env/fires.py hotspot broadcasts
# are NOT migrated. NOT cut over this phase.
from meshai.notifications.gating import firms as _firms_gate_mod  # noqa: E402,F401
register("wildfire_growth",   _firms_gate_mod.decide)
register("wildfire_spotting", _firms_gate_mod.decide)
register("wildfire_halted",   _firms_gate_mod.decide)

# IPAWS civil alerts (env/ipaws.py). Own dedup table (ipaws_alerts) — mirrors
# the NWS decider's first-sighting / Update / dedup-window / tombstone logic.
from meshai.notifications.gating import ipaws as _ipaws_gate_mod  # noqa: E402,F401
register("emergency_evacuation", _ipaws_gate_mod.decide)
register("emergency_civil",      _ipaws_gate_mod.decide)
register("emergency_amber",      _ipaws_gate_mod.decide)
register("emergency_law",        _ipaws_gate_mod.decide)
register("emergency_911_outage", _ipaws_gate_mod.decide)
register("emergency_hazmat",     _ipaws_gate_mod.decide)
