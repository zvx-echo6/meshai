"""Tests for R3 enclosing_bbox helper and cross-state fetch scope.

Verifies:
- enclosing_bbox: two non-contiguous ID boxes → union covers both
- enclosing_bbox: single ID/OR-crossing box → enclosing == that box
- enclosing_bbox: empty input → []
- enclosing_bbox: malformed entries are skipped
- resolve_adapter_coverage("nws", enclosing_bbox([ID/OR box]), "native")["areas"]
  includes both ID and OR, proving cross-state fetch scope
"""

from __future__ import annotations

import pytest

from meshai.coverage import enclosing_bbox, resolve_adapter_coverage

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

# Two non-contiguous Idaho boxes
ID_NORTH = {"name": "North Idaho", "west": -117.3, "south": 46.5, "east": -115.0, "north": 49.1}
ID_SOUTH = {"name": "Magic Valley", "west": -116.5, "south": 42.0, "east": -112.0, "north": 44.0}

# Single box straddling ID/OR border (west edge is inside Oregon; OR bbox west=-124.6)
ID_OR_BOX = {"name": "ID/OR border", "west": -118.0, "south": 43.5, "east": -115.0, "north": 46.0}


# ---------------------------------------------------------------------------
# enclosing_bbox — two non-contiguous Idaho boxes
# ---------------------------------------------------------------------------

def test_enclosing_two_idaho_boxes():
    result = enclosing_bbox([ID_NORTH, ID_SOUTH])
    assert result == [
        round(min(-117.3, -116.5), 6),  # westernmost
        round(min(46.5, 42.0), 6),      # southernmost
        round(max(-115.0, -112.0), 6),  # easternmost
        round(max(49.1, 44.0), 6),      # northernmost
    ]
    west, south, east, north = result
    # Enclosing box must contain both sub-boxes
    assert west <= ID_NORTH["west"] and west <= ID_SOUTH["west"]
    assert south <= ID_NORTH["south"] and south <= ID_SOUTH["south"]
    assert east >= ID_NORTH["east"] and east >= ID_SOUTH["east"]
    assert north >= ID_NORTH["north"] and north >= ID_SOUTH["north"]


# ---------------------------------------------------------------------------
# enclosing_bbox — single box that crosses the ID/OR line
# ---------------------------------------------------------------------------

def test_enclosing_single_id_or_crossing_box():
    result = enclosing_bbox([ID_OR_BOX])
    assert result == [
        round(ID_OR_BOX["west"], 6),
        round(ID_OR_BOX["south"], 6),
        round(ID_OR_BOX["east"], 6),
        round(ID_OR_BOX["north"], 6),
    ]
    # The box's west longitude is well into Oregon (OR west = -124.6)
    west, _, _, _ = result
    assert west < -117.0, "enclosing box should extend into Oregon longitude range"


# ---------------------------------------------------------------------------
# enclosing_bbox — empty / malformed inputs
# ---------------------------------------------------------------------------

def test_enclosing_empty_list():
    assert enclosing_bbox([]) == []


def test_enclosing_none_input():
    assert enclosing_bbox(None) == []


def test_enclosing_malformed_entries_skipped():
    areas = [
        {"name": "bad", "west": "X", "south": 42.0, "east": -112.0, "north": 44.0},
        {"name": "missing key"},
        ID_SOUTH,
    ]
    result = enclosing_bbox(areas)
    # Only ID_SOUTH should contribute
    assert result == [
        round(ID_SOUTH["west"], 6),
        round(ID_SOUTH["south"], 6),
        round(ID_SOUTH["east"], 6),
        round(ID_SOUTH["north"], 6),
    ]


def test_enclosing_all_malformed_returns_empty():
    areas = [{"name": "bad"}, None, {"west": "X"}]
    assert enclosing_bbox(areas) == []


# ---------------------------------------------------------------------------
# Cross-state fetch scope: ID/OR crossing box → nws areas includes ID and OR
# ---------------------------------------------------------------------------

def test_nws_cross_state_fetch_scope_id_or():
    """enclosing_bbox of a box crossing the ID/OR line → nws adapter returns
    areas that include both ID and OR, so cross-state NWS data gets fetched."""
    bbox = enclosing_bbox([ID_OR_BOX])
    assert bbox, "enclosing_bbox should return a non-empty list"
    scope = resolve_adapter_coverage("nws", bbox, "native")
    assert scope is not None, "resolve_adapter_coverage should return scope for nws with valid bbox"
    areas = scope["areas"]
    assert "ID" in areas, f"ID should be in nws areas for ID/OR bbox, got: {areas}"
    assert "OR" in areas, f"OR should be in nws areas for ID/OR bbox, got: {areas}"


# ---------------------------------------------------------------------------
# Cross-state fetch scope: two non-contiguous boxes → enclosing spans both
# ---------------------------------------------------------------------------

def test_nws_fetch_scope_two_id_boxes():
    """enclosing_bbox of two separate ID boxes → nws areas includes ID."""
    bbox = enclosing_bbox([ID_NORTH, ID_SOUTH])
    scope = resolve_adapter_coverage("nws", bbox, "native")
    assert scope is not None
    areas = scope["areas"]
    assert "ID" in areas


# ---------------------------------------------------------------------------
# Precision: enclosing_bbox rounds to 6 decimal places
# ---------------------------------------------------------------------------

def test_enclosing_bbox_rounds_to_6dp():
    areas = [{"name": "precise", "west": -116.12345678, "south": 42.12345678,
              "east": -112.12345678, "north": 44.12345678}]
    result = enclosing_bbox(areas)
    for val in result:
        # 6 decimal places max (repr may be shorter if trailing zeros)
        assert len(str(val).split(".")[-1]) <= 6
