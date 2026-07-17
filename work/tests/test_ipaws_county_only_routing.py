"""County-only IPAWS routing gap — regression + fix proof (NO transmit).

The bug this proves fixed: a civil CAP alert carrying only SAME county
geocode(s) and NO <polygon> had no geometry, so the geometry-based region
tagger (CoverageFilter -> event_region_names -> matching_area_names) could not
place it. With no region it never matched the region-routing matrix and was
SILENTLY not broadcast. Many CEMs / 911 outages / some AMBER alerts are
county-only.

The fix gives such alerts a Census county internal-point (Point, or MultiPoint
for multi-county) so the EXISTING tagger locates them. These tests assert the
county-only alert goes from "no region / dropped" to "tagged + routable" against
the prod-shaped SW/SC/East Idaho coverage areas + emergency route cells.

Everything here is a pure in-process harness. Nothing is sent to any mesh.
"""
from __future__ import annotations

import pytest

from meshai.config import IPAWSConfig
from meshai.env.ipaws import IPAWSAlertsAdapter
from meshai.coverage_area import MonitoringArea, event_region_names, classify_event_areas
from meshai.notifications.pipeline.coverage_filter import CoverageFilter


# Prod coverage areas (from the live CT108 config, 2026-07-16): three named
# Idaho region boxes. Names match the emergency region_routes cell keys exactly.
PROD_AREAS = [
    MonitoringArea(name="SW Idaho", west=-117.993408, south=41.9, east=-115.389404, north=44.331707),
    MonitoringArea(name="SC Idaho", west=-115.389404, south=41.9, east=-112.8, north=44.331707),
    MonitoringArea(name="East Idaho", west=-112.8, south=41.9, east=-110.9, north=45.331707),
]

# Prod emergency region_routes cells (live CT108 config): every named region has
# an enabled cell on both transports. A tagged region that is a key here WOULD
# route (matrix Section 1.5 owns delivery); tagging is the gap we're closing.
EMERGENCY_CELLS = {
    "SW Idaho": {"enabled": True, "mt": 3, "mc": "#sw-id-aida", "min_severity": "routine"},
    "SC Idaho": {"enabled": True, "mt": 2, "mc": "#sc-id-aida", "min_severity": "routine"},
    "East Idaho": {"enabled": True, "mt": 5, "mc": "#e-id-aida", "min_severity": "routine"},
}


def _cap(same_areas, *, polygon: str | None = None) -> bytes:
    """Build a minimal CAP 1.2 civil alert.

    same_areas: list of (areaDesc, SAME_code) tuples -> one <area> each.
    polygon:    optional CAP polygon string ('lat,lon lat,lon ...'); when given
                it is attached to the FIRST area (real-geometry regression case).
    """
    areas_xml = []
    for i, (desc, same) in enumerate(same_areas):
        poly = f"<polygon>{polygon}</polygon>" if (polygon and i == 0) else ""
        areas_xml.append(
            f"<area><areaDesc>{desc}</areaDesc>"
            f"<geocode><valueName>SAME</valueName><value>{same}</value></geocode>"
            f"{poly}</area>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">'
        "<identifier>ID-COUNTY-ONLY-TEST</identifier>"
        "<sender>oem@example-county.id.gov</sender>"
        "<sent>2026-07-16T12:00:00-06:00</sent>"
        "<status>Actual</status><msgType>Alert</msgType>"
        "<info>"
        "<language>en-US</language>"
        "<event>Civil Emergency Message</event>"
        "<urgency>Immediate</urgency><severity>Extreme</severity>"
        "<certainty>Observed</certainty>"
        "<eventCode><valueName>SAME</valueName><value>CEM</value></eventCode>"
        "<headline>County-only civil emergency</headline>"
        "<description>Shelter in place until further notice.</description>"
        + "".join(areas_xml) +
        "</info></alert>"
    ).encode()


def _adapter() -> IPAWSAlertsAdapter:
    return IPAWSAlertsAdapter(IPAWSConfig())


def _tag_via_pipeline(event):
    """Run an event through CoverageFilter exactly as the live pipeline does and
    return (kept, event). Region tags are stamped onto the event in place."""
    received = []
    flt = CoverageFilter(next_handler=received.append, areas=PROD_AREAS, enabled=True)
    flt.handle(event)
    return (len(received) == 1), event


# ===========================================================================
# 1. County-only Idaho alert (the exact silent-drop case) -> tagged + routable
# ===========================================================================

def test_county_only_alert_resolves_to_point_geometry():
    """A SAME-only (no polygon) Ada County alert gets a Point geometry + lat/lon
    from the Census internal point instead of None."""
    a = _adapter()
    raw = a._parse_cap(_cap([("Ada County", "016001")]), "16")
    assert raw is not None
    assert raw["geometry"] is not None
    assert raw["geometry"]["type"] == "Point"
    lon, lat = raw["geometry"]["coordinates"]
    assert raw["lat"] == pytest.approx(lat) and raw["lon"] == pytest.approx(lon)
    # Ada County internal point is in the Boise area.
    assert 43.0 < raw["lat"] < 44.0 and -117.0 < raw["lon"] < -116.0


def test_county_only_alert_tags_region_and_matches_route_cell():
    """THE proof: the county-only alert that silently dropped now tags SW Idaho
    (via the existing geometry tagger) AND that region is an emergency route
    cell -> it WOULD route. No send is performed."""
    a = _adapter()
    raw = a._parse_cap(_cap([("Ada County", "016001")]), "16")
    ev = a.to_event(raw)

    # Direct tagger check (what CoverageFilter calls).
    names = event_region_names(ev, PROD_AREAS)
    assert names == ["SW Idaho"]

    # Full pipeline gate: kept (in-bounds) and region stamped on the event.
    kept, ev = _tag_via_pipeline(ev)
    assert kept is True
    assert ev.regions == ["SW Idaho"]
    assert ev.region == "SW Idaho"

    # Routable: the tagged region resolves to an ENABLED emergency route cell.
    matched = [r for r in ([ev.region, *ev.regions]) if r in EMERGENCY_CELLS]
    assert matched, "county-only alert must now match an emergency route cell"
    assert EMERGENCY_CELLS[matched[0]]["enabled"] is True


def test_county_only_alert_was_dropped_before_fix():
    """Regression anchor: WITHOUT the centroid (simulating pre-fix state — no
    geometry, no lat/lon) the same event tags NO region, i.e. it had nowhere to
    route. This is the behaviour the fix changes."""
    a = _adapter()
    raw = a._parse_cap(_cap([("Ada County", "016001")]), "16")
    ev = a.to_event(raw)
    # Strip the fix's contribution to reproduce the old no-geometry event.
    ev.data["geometry"] = None
    ev.lat = None
    ev.lon = None
    assert event_region_names(ev, PROD_AREAS) == []


# ===========================================================================
# 2. Multi-county alert tags ALL its regions
# ===========================================================================

def test_multi_county_alert_tags_all_regions():
    """Ada County (SW Idaho) + Bannock County (East Idaho) in one alert -> a
    MultiPoint geometry -> BOTH regions tagged -> both route cells reachable."""
    a = _adapter()
    raw = a._parse_cap(
        _cap([("Ada County", "016001"), ("Bannock County", "016005")]), "16"
    )
    assert raw["geometry"]["type"] == "MultiPoint"
    assert len(raw["geometry"]["coordinates"]) == 2

    ev = a.to_event(raw)
    names = event_region_names(ev, PROD_AREAS)
    assert set(names) == {"SW Idaho", "East Idaho"}

    kept, ev = _tag_via_pipeline(ev)
    assert kept is True
    assert set(ev.regions) == {"SW Idaho", "East Idaho"}
    # Both tagged regions are enabled emergency route cells.
    assert all(r in EMERGENCY_CELLS for r in ev.regions)


# ===========================================================================
# 3. Polygon alert is UNCHANGED (regression guard)
# ===========================================================================

def test_polygon_alert_geometry_not_overridden():
    """When a real <polygon> is present the county-centroid fallback must NOT
    fire — the Polygon geometry wins unchanged."""
    a = _adapter()
    # A small polygon roughly over Ada County; SAME geocode also present.
    poly = "43.7,-116.4 43.7,-116.1 43.5,-116.1 43.5,-116.4"
    raw = a._parse_cap(_cap([("Ada County", "016001")], polygon=poly), "16")
    assert raw["geometry"]["type"] == "Polygon"
    ev = a.to_event(raw)
    assert ev.data["geometry"]["type"] == "Polygon"
    assert classify_event_areas(ev, PROD_AREAS) == "in-bounds"


def test_polygon_alert_matches_real_fixture(_ipaws_fixture_bytes):
    """The real captured Idaho CEM fixture (has a Boundary County polygon) still
    parses to a Polygon — the fallback path leaves it alone."""
    a = _adapter()
    raw = a._parse_cap(_ipaws_fixture_bytes, "16")
    assert raw["geometry"]["type"] == "Polygon"


@pytest.fixture
def _ipaws_fixture_bytes():
    import pathlib
    fx = pathlib.Path(__file__).parent / "fixtures" / "ipaws" / "eas_idaho_cem.xml"
    return fx.read_bytes()


# ===========================================================================
# 4. Unresolvable county (northern panhandle) — honest coverage-gap guard
# ===========================================================================

def test_panhandle_county_gets_geometry_but_no_region():
    """Boundary County (northern panhandle) DOES get a valid Point, but the prod
    coverage boxes stop at ~lat 44.3 (SW/SC), so it tags NO region. That is a
    coverage-DEFINITION gap (region boxes don't cover north Idaho), not a bug in
    this fix — documented so it isn't mistaken for a regression."""
    a = _adapter()
    raw = a._parse_cap(_cap([("Boundary County", "016021")]), "16")
    assert raw["geometry"]["type"] == "Point"       # located...
    assert raw["lat"] > 48.0                          # ...in the panhandle
    ev = a.to_event(raw)
    assert event_region_names(ev, PROD_AREAS) == []   # outside all region boxes
