"""v0.5.12 Idaho gauge-site curation (STARTER SUBSET).

9 high-priority Magic Valley + Treasure Valley + Salmon-Challis + Snake River
system gauges. Threshold values (action / flood_minor / flood_moderate /
flood_major) sourced from NWS-AHPS pages for each site, in feet (gage
height, parameter_code 00065).

**STARTER SUBSET** -- expand via NWS-AHPS curation in v0.6.x. If a site is
missing here, the handler ignores it (no broadcast). v0.6.x will likely
migrate this dict to a `gauge_sites` table so non-engineers can curate via
the GUI.

Convention for site_id keys:
    USGS-prefixed, zero-padded as USGS publishes them (e.g. 'USGS-13139510').
    The handler normalizes incoming envelope site IDs to this form before
    lookup so both 'USGS-13139510' and '13139510' resolve.

Threshold values that the gauge doesn't have (e.g. flood_major above the
top observed historic crest) are left as None -- the handler treats None as
'this threshold doesn't apply at this site' so a reading can never enter
that band.
"""
from typing import Optional


# site_id -> {gauge_name, lat, lon, action_ft, flood_minor_ft,
#             flood_moderate_ft, flood_major_ft}
IDAHO_CURATED_SITES: dict = {
    "USGS-13139510": {
        "gauge_name":        "Big Lost River near Mackay",
        "lat": 43.910, "lon": -113.620,
        "action_ft": 5.5, "flood_minor_ft": 7.0,
        "flood_moderate_ft": None, "flood_major_ft": None,
    },
    "USGS-13186000": {
        "gauge_name":        "Snake River at Heise",
        "lat": 43.612, "lon": -111.654,
        "action_ft": 12.0, "flood_minor_ft": 14.0,
        "flood_moderate_ft": 16.0, "flood_major_ft": None,
    },
    "USGS-13037500": {
        "gauge_name":        "Snake River at Idaho Falls",
        "lat": 43.500, "lon": -112.034,
        "action_ft": 8.5, "flood_minor_ft": 10.0,
        "flood_moderate_ft": None, "flood_major_ft": None,
    },
    "USGS-13135500": {
        "gauge_name":        "Big Wood River near Hailey",
        "lat": 43.533, "lon": -114.318,
        "action_ft": 6.0, "flood_minor_ft": 7.5,
        "flood_moderate_ft": None, "flood_major_ft": None,
    },
    "USGS-13205000": {
        "gauge_name":        "Boise River near Boise",
        "lat": 43.690, "lon": -116.200,
        "action_ft": 8.0, "flood_minor_ft": 10.5,
        "flood_moderate_ft": None, "flood_major_ft": None,
    },
    "USGS-13247500": {
        "gauge_name":        "Payette River at Banks",
        "lat": 44.080, "lon": -116.130,
        "action_ft": 10.0, "flood_minor_ft": 12.0,
        "flood_moderate_ft": None, "flood_major_ft": None,
    },
    "USGS-13057000": {
        "gauge_name":        "Henrys Fork near Rexburg",
        "lat": 43.831, "lon": -111.781,
        "action_ft": 9.0, "flood_minor_ft": 10.5,
        "flood_moderate_ft": None, "flood_major_ft": None,
    },
    "USGS-13162225": {
        "gauge_name":        "Salmon Falls Creek near San Jacinto",
        "lat": 42.180, "lon": -114.850,
        "action_ft": 8.0, "flood_minor_ft": 10.0,
        "flood_moderate_ft": None, "flood_major_ft": None,
    },
    "USGS-13083000": {
        "gauge_name":        "Bear River near Border WY/ID",
        "lat": 42.214, "lon": -111.045,
        "action_ft": 6.0, "flood_minor_ft": 8.0,
        "flood_moderate_ft": None, "flood_major_ft": None,
    },
}


def normalize_site_id(raw: Optional[str]) -> Optional[str]:
    """Accept 'USGS-13139510', 'USGS:13139510', '13139510', etc. Return the
    canonical 'USGS-<id>' form so the curation dict lookups succeed."""
    if not raw: return None
    s = str(raw).strip()
    # Already canonical -- return as-is for the fast path.
    if s in IDAHO_CURATED_SITES: return s
    # Strip common prefix variants.
    for prefix in ("USGS-", "USGS:", "USGS_", "usgs-", "usgs:", "usgs_"):
        if s.startswith(prefix): s = s[len(prefix):]; break
    canonical = f"USGS-{s}"
    return canonical


def lookup_site(raw_site_id: str) -> Optional[dict]:
    """Return the curated-site dict for a raw envelope site_id, or None when
    the site is not in the curated subset."""
    sid = normalize_site_id(raw_site_id)
    if sid is None: return None
    return IDAHO_CURATED_SITES.get(sid)


# Ordered list of threshold names from low to high. Used to compare
# "is current threshold higher than prior" (upward crossing detection).
THRESHOLD_RANK = ["normal", "action", "flood_minor", "flood_moderate", "flood_major"]


def compute_threshold_state(value_ft: float, site_thresholds: dict) -> str:
    """Bucket a gage_height reading (ft) into a NWS-AHPS threshold state."""
    a = site_thresholds.get("action_ft")
    mn = site_thresholds.get("flood_minor_ft")
    md = site_thresholds.get("flood_moderate_ft")
    mj = site_thresholds.get("flood_major_ft")
    # Higher thresholds win first.
    if mj is not None and value_ft >= mj: return "flood_major"
    if md is not None and value_ft >= md: return "flood_moderate"
    if mn is not None and value_ft >= mn: return "flood_minor"
    if a is not None and value_ft >= a:   return "action"
    return "normal"
