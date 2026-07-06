"""Tests for meshai/coverage.py — pure coverage-bbox derivation module.

Bbox convention: [west, south, east, north] = [min_lon, min_lat, max_lon, max_lat].
All functions are pure (no I/O) so no fixtures are needed.
"""

from __future__ import annotations

import pytest

from meshai.coverage import (
    AVALANCHE_CENTER_BBOXES,
    US_STATE_BBOXES,
    _significant_overlap,
    arcgis_envelope,
    avalanche_centers_for_bbox,
    bbox_intersects,
    bbox_is_valid,
    centroid,
    grid_points,
    point_in_bbox,
    resolve_adapter_coverage,
    states_for_bbox,
)

# ---------------------------------------------------------------------------
# Reference boxes used in multiple tests
# ---------------------------------------------------------------------------

# Magic Valley / south-central Idaho
IDAHO_BOX = [-116.5, 42.0, -112.0, 44.0]

# Straddles the ID/OR border (western edge is inside Oregon)
ID_OR_BOX = [-118.0, 43.5, -115.0, 46.0]

# A box clearly out in the Pacific — no US states
PACIFIC_BOX = [-170.0, 20.0, -160.0, 25.0]  # hits HI

# Very small Wyoming box (no avalanche centers overlap)
SMALL_WY_BOX = [-106.0, 41.5, -105.5, 42.0]


# ===========================================================================
# bbox_is_valid
# ===========================================================================


def test_valid_box_passes():
    assert bbox_is_valid([-116.5, 42.0, -112.0, 44.0]) is True


def test_valid_box_tuple_passes():
    assert bbox_is_valid((-116.5, 42.0, -112.0, 44.0)) is True


def test_invalid_wrong_length():
    assert bbox_is_valid([-116.5, 42.0, -112.0]) is False


def test_invalid_empty():
    assert bbox_is_valid([]) is False


def test_invalid_west_equals_east():
    assert bbox_is_valid([-112.0, 42.0, -112.0, 44.0]) is False


def test_invalid_west_greater_than_east():
    assert bbox_is_valid([-110.0, 42.0, -116.0, 44.0]) is False


def test_invalid_south_equals_north():
    assert bbox_is_valid([-116.5, 44.0, -112.0, 44.0]) is False


def test_invalid_south_greater_than_north():
    assert bbox_is_valid([-116.5, 46.0, -112.0, 42.0]) is False


def test_invalid_lat_out_of_range():
    assert bbox_is_valid([-116.5, -91.0, -112.0, 44.0]) is False
    assert bbox_is_valid([-116.5, 42.0, -112.0, 91.0]) is False


def test_invalid_lon_out_of_range():
    assert bbox_is_valid([-181.0, 42.0, -112.0, 44.0]) is False
    assert bbox_is_valid([-116.5, 42.0, 181.0, 44.0]) is False


def test_invalid_non_numeric():
    assert bbox_is_valid([-116.5, "bad", -112.0, 44.0]) is False


def test_invalid_none():
    assert bbox_is_valid(None) is False


# ===========================================================================
# point_in_bbox
# ===========================================================================


def test_point_inside_box():
    assert point_in_bbox(43.0, -114.0, IDAHO_BOX) is True


def test_point_on_south_edge():
    # On-edge counts as inside (inclusive)
    assert point_in_bbox(42.0, -114.0, IDAHO_BOX) is True


def test_point_on_north_edge():
    assert point_in_bbox(44.0, -114.0, IDAHO_BOX) is True


def test_point_outside_box_south():
    assert point_in_bbox(41.9, -114.0, IDAHO_BOX) is False


def test_point_outside_box_north():
    assert point_in_bbox(44.1, -114.0, IDAHO_BOX) is False


def test_point_outside_box_west():
    assert point_in_bbox(43.0, -117.0, IDAHO_BOX) is False


def test_point_outside_box_east():
    assert point_in_bbox(43.0, -111.0, IDAHO_BOX) is False


# ===========================================================================
# bbox_intersects
# ===========================================================================


def test_overlapping_boxes_intersect():
    # Boxes share a large area
    a = [-116.0, 42.0, -112.0, 44.0]
    b = [-114.0, 43.0, -110.0, 45.0]
    assert bbox_intersects(a, b) is True
    assert bbox_intersects(b, a) is True  # symmetric


def test_disjoint_boxes_do_not_intersect_east_west():
    a = [-120.0, 42.0, -116.0, 44.0]
    b = [-114.0, 42.0, -110.0, 44.0]
    assert bbox_intersects(a, b) is False


def test_disjoint_boxes_do_not_intersect_north_south():
    a = [-116.0, 42.0, -112.0, 44.0]
    b = [-116.0, 45.0, -112.0, 47.0]
    assert bbox_intersects(a, b) is False


def test_touching_edge_does_not_intersect():
    # Boxes share only an edge — strict intersection → False
    a = [-116.0, 42.0, -112.0, 44.0]
    b = [-112.0, 42.0, -108.0, 44.0]
    assert bbox_intersects(a, b) is False


def test_identical_boxes_intersect():
    a = [-116.0, 42.0, -112.0, 44.0]
    assert bbox_intersects(a, a) is True


def test_one_box_contained_in_other():
    outer = [-120.0, 40.0, -110.0, 50.0]
    inner = [-116.0, 42.0, -112.0, 44.0]
    assert bbox_intersects(outer, inner) is True
    assert bbox_intersects(inner, outer) is True


# ===========================================================================
# centroid
# ===========================================================================


def test_centroid_basic():
    lat, lon = centroid([-116.0, 42.0, -112.0, 44.0])
    assert lat == pytest.approx(43.0)
    assert lon == pytest.approx(-114.0)


def test_centroid_asymmetric():
    lat, lon = centroid([-116.5, 42.0, -112.0, 44.0])
    assert lat == pytest.approx(43.0)
    assert lon == pytest.approx(-114.25)


# ===========================================================================
# grid_points
# ===========================================================================


def test_grid_points_default_count():
    pts = grid_points(IDAHO_BOX)
    assert len(pts) == 9  # 3x3


def test_grid_points_custom_count():
    pts = grid_points(IDAHO_BOX, cols=4, rows=2)
    assert len(pts) == 8


def test_grid_points_all_inside_box():
    """All returned points must lie strictly inside the bbox (not on edges)."""
    west, south, east, north = IDAHO_BOX
    for lat, lon in grid_points(IDAHO_BOX, cols=3, rows=3):
        assert south < lat < north, f"lat {lat} not strictly inside [{south}, {north}]"
        assert west < lon < east, f"lon {lon} not strictly inside [{west}, {east}]"


def test_grid_points_1x1_is_centroid():
    lat, lon = grid_points(IDAHO_BOX, cols=1, rows=1)[0]
    clat, clon = centroid(IDAHO_BOX)
    assert lat == pytest.approx(clat)
    assert lon == pytest.approx(clon)


def test_grid_points_return_type():
    pts = grid_points(IDAHO_BOX)
    assert all(isinstance(p, tuple) and len(p) == 2 for p in pts)


# ===========================================================================
# arcgis_envelope
# ===========================================================================


def test_arcgis_envelope_keys():
    result = arcgis_envelope(IDAHO_BOX)
    assert set(result.keys()) == {"geometry", "geometryType", "spatialRel", "inSR"}


def test_arcgis_envelope_geometry_string():
    result = arcgis_envelope([-116.5, 42.0, -112.0, 44.0])
    assert result["geometry"] == "-116.5,42.0,-112.0,44.0"


def test_arcgis_envelope_static_fields():
    result = arcgis_envelope(IDAHO_BOX)
    assert result["geometryType"] == "esriGeometryEnvelope"
    assert result["spatialRel"] == "esriSpatialRelIntersects"
    assert result["inSR"] == "4326"


def test_arcgis_envelope_known_box():
    bbox = [-117.0, 43.0, -113.0, 46.0]
    env = arcgis_envelope(bbox)
    assert env["geometry"] == "-117.0,43.0,-113.0,46.0"


# ===========================================================================
# states_for_bbox
# ===========================================================================


def test_idaho_box_returns_id():
    states = states_for_bbox(IDAHO_BOX)
    assert "ID" in states


def test_idaho_box_does_not_return_far_states():
    """A south-central Idaho box should not pull in distant states."""
    states = states_for_bbox(IDAHO_BOX)
    for far in ("FL", "ME", "TX", "NY"):
        assert far not in states, f"unexpected far state {far} in {states}"


def test_id_or_straddling_box_returns_both():
    states = states_for_bbox(ID_OR_BOX)
    assert "ID" in states
    assert "OR" in states


def test_result_is_sorted():
    states = states_for_bbox(ID_OR_BOX)
    assert states == sorted(states)


def test_result_is_deduped():
    states = states_for_bbox(IDAHO_BOX)
    assert len(states) == len(set(states))


def test_pacific_box_returns_only_hi():
    states = states_for_bbox(PACIFIC_BOX)
    # Hawaii bbox is [-160.3, 18.9, -154.8, 22.3]; PACIFIC_BOX overlaps it.
    assert "HI" in states
    # Continental states should not appear.
    for continental in ("ID", "OR", "WA", "CA", "TX"):
        assert continental not in states


def test_state_bboxes_table_has_all_50_plus_dc():
    """The table must have at least 51 entries (50 states + DC)."""
    # We may also have territories; just require 50 states + DC minimum.
    required = {
        "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL",
        "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME",
        "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH",
        "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
        "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    }
    assert required.issubset(set(US_STATE_BBOXES.keys()))


# ===========================================================================
# avalanche_centers_for_bbox
# ===========================================================================


def test_idaho_box_includes_snfac():
    centers = avalanche_centers_for_bbox(IDAHO_BOX)
    assert "SNFAC" in centers


def test_result_is_sorted_deduped():
    centers = avalanche_centers_for_bbox(IDAHO_BOX)
    assert centers == sorted(centers)
    assert len(centers) == len(set(centers))


def test_pacific_box_no_continental_centers():
    centers = avalanche_centers_for_bbox(PACIFIC_BOX)
    for c in ("SNFAC", "CAIC", "UAC"):
        assert c not in centers


def test_avalanche_center_bboxes_includes_snfac_key():
    assert "SNFAC" in AVALANCHE_CENTER_BBOXES


# ===========================================================================
# resolve_adapter_coverage
# ===========================================================================


# ---- None-returning cases ------------------------------------------------


def test_central_feed_returns_none():
    assert resolve_adapter_coverage("nws", IDAHO_BOX, feed_source="central") is None


def test_empty_bbox_returns_none():
    assert resolve_adapter_coverage("nws", [], feed_source="native") is None


def test_invalid_bbox_returns_none():
    assert resolve_adapter_coverage("nws", [-116.5, 44.0, -112.0, 42.0]) is None


def test_swpc_returns_none():
    """SWPC is global — no geographic scope regardless of bbox."""
    assert resolve_adapter_coverage("swpc", IDAHO_BOX) is None


def test_unknown_adapter_returns_none():
    assert resolve_adapter_coverage("no_such_adapter", IDAHO_BOX) is None


# ---- fires adapter -------------------------------------------------------


def test_fires_returns_envelope_and_bbox():
    result = resolve_adapter_coverage("fires", IDAHO_BOX)
    assert result is not None
    assert "envelope" in result
    assert "bbox" in result
    assert result["bbox"] == IDAHO_BOX
    env = result["envelope"]
    assert "geometry" in env
    assert env["geometryType"] == "esriGeometryEnvelope"


# ---- nws adapter ---------------------------------------------------------


def test_nws_returns_areas_and_bbox():
    result = resolve_adapter_coverage("nws", IDAHO_BOX)
    assert result is not None
    assert "areas" in result
    assert "bbox" in result
    assert "ID" in result["areas"]
    assert result["bbox"] == IDAHO_BOX


def test_nws_areas_is_list():
    result = resolve_adapter_coverage("nws", IDAHO_BOX)
    assert isinstance(result["areas"], list)


# ---- wzdx adapter --------------------------------------------------------


def test_wzdx_returns_states_and_bbox():
    result = resolve_adapter_coverage("wzdx", IDAHO_BOX)
    assert result is not None
    assert "states" in result
    assert "bbox" in result
    assert "ID" in result["states"]


# ---- bbox-only adapters --------------------------------------------------


@pytest.mark.parametrize("adapter", ["usgs_quake", "firms", "roads511", "usgs"])
def test_bbox_only_adapters(adapter):
    result = resolve_adapter_coverage(adapter, IDAHO_BOX)
    assert result is not None
    assert list(result.keys()) == ["bbox"]
    assert result["bbox"] == IDAHO_BOX


# ---- avalanche adapter ---------------------------------------------------


def test_avalanche_returns_center_ids():
    result = resolve_adapter_coverage("avalanche", IDAHO_BOX)
    assert result is not None
    assert "center_ids" in result
    assert "SNFAC" in result["center_ids"]
    # Should NOT have a bbox key (avalanche uses center IDs, not a raw bbox)
    assert "bbox" not in result


# ---- traffic adapter -----------------------------------------------------


def test_traffic_returns_points():
    result = resolve_adapter_coverage("traffic", IDAHO_BOX)
    assert result is not None
    assert "points" in result
    pts = result["points"]
    assert isinstance(pts, list)
    assert len(pts) == 9  # default 3x3 grid


def test_traffic_points_are_tuples():
    result = resolve_adapter_coverage("traffic", IDAHO_BOX)
    assert all(isinstance(p, tuple) and len(p) == 2 for p in result["points"])


# ---- satpass / ducting adapters ------------------------------------------


@pytest.mark.parametrize("adapter", ["satpass", "ducting"])
def test_satpass_ducting_returns_centroid(adapter):
    result = resolve_adapter_coverage(adapter, IDAHO_BOX)
    assert result is not None
    assert "centroid" in result
    lat, lon = result["centroid"]
    assert isinstance(lat, float)
    assert isinstance(lon, float)


def test_satpass_centroid_is_correct():
    result = resolve_adapter_coverage("satpass", [-116.0, 42.0, -112.0, 44.0])
    lat, lon = result["centroid"]
    assert lat == pytest.approx(43.0)
    assert lon == pytest.approx(-114.0)


# ---- defensive copy of bbox ----------------------------------------------


def test_resolve_does_not_mutate_input_bbox():
    """The returned bbox should be a copy; mutating it must not affect the original."""
    original = [-116.5, 42.0, -112.0, 44.0]
    result = resolve_adapter_coverage("usgs_quake", original)
    result["bbox"].append(999)  # mutate return value
    assert len(original) == 4  # original untouched


# ===========================================================================
# Coordinate precision — all outputs must be ≤6 decimal places (Fix 1)
# ===========================================================================

# Raw Leaflet map-click bbox: ~14 decimal places, triggers USGS HTTP 400.
HIGH_PREC_BOX = [
    -116.99340820312501,
    41.95949009892467,
    -110.98388671875001,
    44.09547572946637,
]


def _decimal_places(v) -> int:
    """Return significant decimal places in *v* (trailing zeros not counted)."""
    s = f"{float(v):.10f}"
    decimal_part = s.split(".")[1]
    stripped = decimal_part.rstrip("0")
    return len(stripped) if stripped else 0


def _assert_max_6dp(v, label: str = "") -> None:
    """Assert float *v* has at most 6 decimal places."""
    n = _decimal_places(v)
    assert n <= 6, f"{label}: {v!r} has {n} decimal places (max 6)"


def test_high_precision_bbox_coords_rounded():
    """bbox key in every adapter that returns one must have ≤6dp per coordinate."""
    bbox_adapters = ("usgs_quake", "firms", "roads511", "usgs", "nws", "wzdx",
                     "fires", "wfigs", "nicf")
    for adapter in bbox_adapters:
        result = resolve_adapter_coverage(adapter, HIGH_PREC_BOX)
        assert result is not None, f"{adapter} unexpectedly returned None"
        if "bbox" in result:
            for i, coord in enumerate(result["bbox"]):
                _assert_max_6dp(coord, f"{adapter}.bbox[{i}]")


def test_high_precision_envelope_geometry_rounded():
    """ArcGIS envelope geometry string coords must have ≤6dp (fires adapter)."""
    result = resolve_adapter_coverage("fires", HIGH_PREC_BOX)
    assert result is not None
    geom = result["envelope"]["geometry"]
    for part in geom.split(","):
        _assert_max_6dp(part.strip(), f"fires.envelope.geometry part {part!r}")


def test_high_precision_centroid_rounded():
    """centroid() computed from a high-precision bbox must have ≤6dp per coord."""
    result = resolve_adapter_coverage("satpass", HIGH_PREC_BOX)
    assert result is not None
    lat, lon = result["centroid"]
    _assert_max_6dp(lat, "satpass.centroid.lat")
    _assert_max_6dp(lon, "satpass.centroid.lon")


def test_high_precision_grid_points_rounded():
    """All grid_points() coords from a high-precision bbox must have ≤6dp."""
    result = resolve_adapter_coverage("traffic", HIGH_PREC_BOX)
    assert result is not None
    for i, (plat, plon) in enumerate(result["points"]):
        _assert_max_6dp(plat, f"traffic.points[{i}].lat")
        _assert_max_6dp(plon, f"traffic.points[{i}].lon")


# ===========================================================================
# _significant_overlap
# ===========================================================================

# Southern-Idaho coverage box from the LA-leak bug report (high-precision form
# as produced by a Leaflet map click — used in several tests below).
IDAHO_BUG_BOX = [
    -116.99340820312501,
    41.95949009892467,
    -110.98388671875001,
    44.09547572946637,
]


def test_significant_overlap_solid_overlap_true():
    """Two boxes that genuinely overlap return True."""
    a = [-116.5, 42.0, -112.0, 44.0]
    b = [-117.3, 41.9, -111.0, 49.1]  # ID bbox — fully contains a
    assert _significant_overlap(a, b) is True


def test_significant_overlap_corner_clip_false():
    """A corner clip smaller than min_deg in one dimension returns False."""
    # CA bbox north=42.0, coverage box south=41.959 → lat overlap ~0.04 deg
    ca_bbox = [-124.5, 32.5, -114.1, 42.0]
    assert _significant_overlap(IDAHO_BUG_BOX, ca_bbox) is False


def test_significant_overlap_lon_edge_clip_false():
    """A lon-only edge clip smaller than min_deg returns False."""
    # WY bbox west=-111.1, coverage box east=-110.984 → lon overlap ~0.12 deg
    wy_bbox = [-111.1, 40.9, -104.0, 45.1]
    assert _significant_overlap(IDAHO_BUG_BOX, wy_bbox) is False


def test_significant_overlap_both_small_false():
    """Both dimensions below min_deg → False."""
    a = [-116.0, 42.0, -115.9, 42.09]  # tiny box
    b = [-115.95, 42.05, -115.85, 42.12]
    # overlap_lon = -115.9 - (-115.95) = 0.05 < 0.25 → False
    assert _significant_overlap(a, b) is False


def test_significant_overlap_custom_min_deg():
    """Caller can supply a tighter or looser min_deg."""
    a = [-116.5, 42.0, -112.0, 44.0]
    # NV bbox lat overlap ~0.14 deg — excluded at 0.25 but included at 0.10
    nv_bbox = [-120.0, 35.0, -114.0, 42.1]
    assert _significant_overlap(a, nv_bbox, min_deg=0.10) is True
    assert _significant_overlap(a, nv_bbox, min_deg=0.25) is False


def test_significant_overlap_symmetric():
    """Order of arguments should not matter."""
    a = [-116.5, 42.0, -112.0, 44.0]
    b = [-117.3, 41.9, -111.0, 49.1]
    assert _significant_overlap(a, b) == _significant_overlap(b, a)


# ===========================================================================
# states_for_bbox — LA leak regression + significant-overlap behavior
# ===========================================================================


def test_la_bug_box_yields_id_and_or_only():
    """Regression: the southern-Idaho box that caused the LA-alert leak must
    return only ID and OR — not CA, NV, UT, or WY."""
    states = states_for_bbox(IDAHO_BUG_BOX)
    assert states == ["ID", "OR"], (
        f"Expected ['ID', 'OR'], got {states} — "
        "CA/NV/UT/WY must be excluded (corner-clip, not meaningful overlap)"
    )


def test_la_bug_box_ca_not_present():
    """CA must NOT appear — its inclusion caused real LA alerts to broadcast."""
    assert "CA" not in states_for_bbox(IDAHO_BUG_BOX)


def test_la_bug_box_nv_ut_wy_not_present():
    """NV, UT, WY must NOT appear — they clip only a thin sliver of the box."""
    states = states_for_bbox(IDAHO_BUG_BOX)
    for dropped in ("NV", "UT", "WY"):
        assert dropped not in states, f"{dropped} unexpectedly in {states}"


def test_la_bug_box_id_and_or_present():
    """ID and OR must appear — they have genuine significant overlap."""
    states = states_for_bbox(IDAHO_BUG_BOX)
    assert "ID" in states
    assert "OR" in states


def test_genuine_two_state_box_returns_both():
    """A box that genuinely straddles ID and OR with >0.25 deg overlap returns both."""
    # ID_OR_BOX is [-118.0, 43.5, -115.0, 46.0] — overlaps both by >1 degree
    states = states_for_bbox(ID_OR_BOX)
    assert "ID" in states
    assert "OR" in states
