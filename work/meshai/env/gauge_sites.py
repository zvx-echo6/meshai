"""v0.6-4 gauge-site lookups (now backed by gauge_sites table).

The IDAHO_CURATED_SITES Python dict was migrated to the gauge_sites SQLite
table in v8.sql. seed_gauge_sites() (called from init_db on first boot)
populates the table with the original 9 sites. The lookup helpers below
read from the table via meshai.persistence.curation.

Module exports retained for backward-compat with existing tests:
    lookup_site, normalize_site_id, compute_threshold_state.
The IDAHO_CURATED_SITES name itself is gone -- new code should call
lookup_site() (DB-backed) or the curation accessor directly.

(Relocated from meshai/central/idaho_gauge_sites.py -- Central is gone;
this is pure hydro/gauge domain code, not a Central handler. The "idaho_"
prefix was dropped on the move: the data is Idaho-scoped but the code
itself is generic gauge/threshold logic. THRESHOLD_RANK, formerly exported
here, was inlined into its sole consumer, meshai.notifications.gating.hydro.)
"""
from typing import Optional


def normalize_site_id(raw: Optional[str]) -> Optional[str]:
    """Accept 'USGS-13139510', 'USGS:13139510', '13139510', etc. Return the
    canonical 'USGS-<id>' form so the curation table lookups succeed."""
    if not raw: return None
    s = str(raw).strip()
    for prefix in ("USGS-", "USGS:", "USGS_", "usgs-", "usgs:", "usgs_"):
        if s.startswith(prefix): s = s[len(prefix):]; break
    return f"USGS-{s}"


def lookup_site(raw_site_id: str) -> Optional[dict]:
    """Return the curated-site dict for a raw envelope site_id, or None when
    the site is not in the curated subset (or is disabled).

    v0.6-4: reads from the gauge_sites SQLite table via the curation accessor."""
    sid = normalize_site_id(raw_site_id)
    if sid is None: return None
    from meshai.persistence.curation import lookup_gauge_site
    return lookup_gauge_site(sid)


def compute_threshold_state(value_ft: float, site_thresholds: dict) -> str:
    """Bucket a gage_height reading (ft) into a NWS-AHPS threshold state."""
    a = site_thresholds.get("action_ft")
    mn = site_thresholds.get("flood_minor_ft")
    md = site_thresholds.get("flood_moderate_ft")
    mj = site_thresholds.get("flood_major_ft")
    if mj is not None and value_ft >= mj: return "flood_major"
    if md is not None and value_ft >= md: return "flood_moderate"
    if mn is not None and value_ft >= mn: return "flood_minor"
    if a is not None and value_ft >= a:   return "action"
    return "normal"
