"""Tests for meshai/coverage_area.py — Shapely geometry gate.

Includes a reproduction of the LA/OR leak that originally triggered this work:
an event whose geometry is firmly inside California must be DROPPED when only
an Idaho coverage box is configured.

Bbox/coordinate convention (GeoJSON):
  bbox  = [west, south, east, north] = [min_lon, min_lat, max_lon, max_lat]
  Point = [lon, lat]

Idaho reference area (magic-valley to eastern border, generous):
  west=-117, south=42, east=-111, north=44
"""

from __future__ import annotations

import json

import pytest

from meshai.coverage_area import (
    MonitoringArea,
    areas_from_config,
    build_geom_json,
    classify_geom_areas,
    event_in_areas,
)
from meshai.notifications.events import make_event
from meshai.notifications.pipeline.coverage_filter import CoverageFilter
from meshai.config import Coverage


# ---------------------------------------------------------------------------
# Reference areas
# ---------------------------------------------------------------------------

IDAHO = MonitoringArea(north=44.0, south=42.0, east=-111.0, west=-117.0)
# Second non-contiguous box covering Yellowstone / eastern corner of WY
YELLOWSTONE = MonitoringArea(north=45.2, south=44.0, east=-109.5, west=-111.5)

IDAHO_AREAS = [IDAHO]
TWO_AREAS = [IDAHO, YELLOWSTONE]

# A point firmly inside Idaho (Twin Falls area)
TF_LON, TF_LAT = -114.47, 42.56

# A point at downtown Los Angeles — should be OUT of Idaho
LA_LON, LA_LAT = -118.24, 34.05

# A CA polygon enclosing the LA area (4-corner box + close)
CA_POLYGON_COORDS = [
    [-119.0, 33.5],
    [-117.5, 33.5],
    [-117.5, 34.6],
    [-119.0, 34.6],
    [-119.0, 33.5],
]
CA_POLYGON_GEOJSON = json.dumps({
    "type": "Polygon",
    "coordinates": [CA_POLYGON_COORDS],
})

# A polygon straddling the Idaho western border (partly in Oregon, partly Idaho)
STRADDLER_COORDS = [
    [-117.5, 42.5],  # in Oregon
    [-116.0, 42.5],  # in Idaho
    [-116.0, 43.5],  # in Idaho
    [-117.5, 43.5],  # in Oregon
    [-117.5, 42.5],
]
STRADDLER_GEOJSON = json.dumps({
    "type": "Polygon",
    "coordinates": [STRADDLER_COORDS],
})


# ===========================================================================
# MonitoringArea.as_box
# ===========================================================================

class TestMonitoringArea:
    def test_as_box_is_shapely_polygon(self):
        from shapely.geometry import box
        b = IDAHO.as_box()
        expected = box(IDAHO.west, IDAHO.south, IDAHO.east, IDAHO.north)
        assert b.equals(expected)

    def test_frozen_immutability(self):
        with pytest.raises(Exception):
            IDAHO.north = 99.0  # type: ignore[misc]


# ===========================================================================
# build_geom_json — geometry extraction priority
# ===========================================================================

class TestBuildGeomJson:
    def test_none_input_returns_none(self):
        assert build_geom_json(None) is None

    def test_empty_dict_returns_none(self):
        assert build_geom_json({}) is None

    def test_geometry_wins_over_bbox(self):
        geo = {
            "geometry": {"type": "Point", "coordinates": [TF_LON, TF_LAT]},
            "bbox": [-115.0, 42.0, -113.0, 43.0],
        }
        result = build_geom_json(geo)
        assert json.loads(result) == {"type": "Point", "coordinates": [TF_LON, TF_LAT]}

    def test_bbox_renders_as_polygon(self):
        geo = {"bbox": [-115.0, 42.0, -113.0, 43.0]}
        result = build_geom_json(geo)
        parsed = json.loads(result)
        assert parsed["type"] == "Polygon"
        coords = parsed["coordinates"][0]
        # should be a closed ring of 5 points
        assert len(coords) == 5
        assert coords[0] == coords[-1]

    def test_centroid_renders_as_point(self):
        geo = {"centroid": [TF_LON, TF_LAT]}
        result = build_geom_json(geo)
        parsed = json.loads(result)
        assert parsed == {"type": "Point", "coordinates": [TF_LON, TF_LAT]}

    def test_bbox_wins_over_centroid(self):
        geo = {
            "bbox": [-115.0, 42.0, -113.0, 43.0],
            "centroid": [LA_LON, LA_LAT],
        }
        result = build_geom_json(geo)
        assert "Polygon" in result

    def test_returns_none_if_no_usable_shape(self):
        geo = {"name": "irrelevant"}
        assert build_geom_json(geo) is None


# ===========================================================================
# classify_geom_areas
# ===========================================================================

class TestClassifyGeomAreas:
    def test_null_geom_returns_null_geom(self):
        assert classify_geom_areas(None, IDAHO_AREAS) == "null-geom"

    def test_no_area_returns_no_area(self):
        geom_json = json.dumps({"type": "Point", "coordinates": [TF_LON, TF_LAT]})
        assert classify_geom_areas(geom_json, []) == "no-area"

    def test_idaho_point_in_bounds(self):
        geom_json = json.dumps({"type": "Point", "coordinates": [TF_LON, TF_LAT]})
        assert classify_geom_areas(geom_json, IDAHO_AREAS) == "in-bounds"

    def test_la_point_out_of_bounds(self):
        """LA repro: a point at (34.05N, 118.24W) must be OUT of an Idaho box."""
        geom_json = json.dumps({"type": "Point", "coordinates": [LA_LON, LA_LAT]})
        assert classify_geom_areas(geom_json, IDAHO_AREAS) == "out-of-bounds"

    def test_ca_polygon_out_of_bounds(self):
        """CA polygon straddling 34N should be fully outside Idaho."""
        assert classify_geom_areas(CA_POLYGON_GEOJSON, IDAHO_AREAS) == "out-of-bounds"

    def test_straddler_polygon_in_bounds(self):
        """Polygon crossing the ID/OR border still intersects Idaho → in-bounds."""
        assert classify_geom_areas(STRADDLER_GEOJSON, IDAHO_AREAS) == "in-bounds"

    def test_invalid_geom_returns_invalid_geom(self):
        assert classify_geom_areas("this is not json", IDAHO_AREAS) == "invalid-geom"

    def test_empty_geom_string_returns_invalid_geom(self):
        assert classify_geom_areas("", IDAHO_AREAS) == "invalid-geom"

    def test_multi_area_union_point_in_second_box(self):
        """A point inside the Yellowstone box (not Idaho) → in-bounds via union."""
        # Yellowstone area: north=45.2, south=44.0, east=-109.5, west=-111.5
        ys_point = json.dumps({"type": "Point", "coordinates": [-110.5, 44.5]})
        assert classify_geom_areas(ys_point, TWO_AREAS) == "in-bounds"

    def test_multi_area_union_point_in_neither_box(self):
        """A point in Wyoming (but not in either box) → out-of-bounds."""
        # Far eastern Wyoming, outside both boxes
        wy_point = json.dumps({"type": "Point", "coordinates": [-104.0, 42.5]})
        assert classify_geom_areas(wy_point, TWO_AREAS) == "out-of-bounds"

    def test_border_edge_point_is_in_bounds(self):
        """A point exactly on the Idaho box edge → in-bounds (intersects semantics)."""
        # Exactly on the western edge of Idaho box (lon=-117, lat=43)
        edge = json.dumps({"type": "Point", "coordinates": [-117.0, 43.0]})
        assert classify_geom_areas(edge, IDAHO_AREAS) == "in-bounds"


# ===========================================================================
# areas_from_config
# ===========================================================================

class TestAreasFromConfig:
    def test_from_areas_list(self):
        cov = Coverage(areas=[
            {"name": "Idaho", "west": -117.0, "south": 42.0, "east": -111.0, "north": 44.0}
        ])
        areas = areas_from_config(cov)
        assert len(areas) == 1
        a = areas[0]
        assert a.west == -117.0 and a.south == 42.0
        assert a.east == -111.0 and a.north == 44.0

    def test_falls_back_to_bbox(self):
        cov = Coverage(bbox=[-117.0, 42.0, -111.0, 44.0])
        areas = areas_from_config(cov)
        assert len(areas) == 1
        assert areas[0].west == -117.0

    def test_areas_takes_precedence_over_bbox(self):
        cov = Coverage(
            bbox=[-110.0, 30.0, -100.0, 35.0],  # TX — totally different
            areas=[{"name": "ID", "west": -117.0, "south": 42.0, "east": -111.0, "north": 44.0}],
        )
        areas = areas_from_config(cov)
        assert len(areas) == 1
        assert areas[0].west == -117.0  # Idaho, not TX

    def test_empty_config_returns_empty(self):
        cov = Coverage()
        assert areas_from_config(cov) == []

    def test_multi_area_list(self):
        cov = Coverage(areas=[
            {"name": "Idaho", "west": -117.0, "south": 42.0, "east": -111.0, "north": 44.0},
            {"name": "Yellowstone", "west": -111.5, "south": 44.0, "east": -109.5, "north": 45.2},
        ])
        areas = areas_from_config(cov)
        assert len(areas) == 2

    def test_malformed_area_dict_skipped(self):
        cov = Coverage(areas=[
            {"name": "bad"},  # missing west/south/east/north
            {"name": "Idaho", "west": -117.0, "south": 42.0, "east": -111.0, "north": 44.0},
        ])
        areas = areas_from_config(cov)
        assert len(areas) == 1  # only the valid one


# ===========================================================================
# event_in_areas
# ===========================================================================

class TestEventInAreas:
    def _event_with_geom(self, geometry_dict, lat=None, lon=None):
        return make_event(
            source="nws", category="weather_warning", severity="priority",
            title="Test", lat=lat, lon=lon,
            data={"geometry": geometry_dict},
        )

    def _event_with_centroid(self, lat, lon):
        return make_event(
            source="nws", category="weather_warning", severity="priority",
            title="Test", lat=lat, lon=lon,
        )

    def test_idaho_centroid_kept(self):
        event = self._event_with_centroid(TF_LAT, TF_LON)
        assert event_in_areas(event, IDAHO_AREAS) is True

    def test_la_centroid_dropped(self):
        """LA-repro: centroid at (34.05, -118.24) must be dropped by Idaho gate."""
        event = self._event_with_centroid(LA_LAT, LA_LON)
        assert event_in_areas(event, IDAHO_AREAS) is False

    def test_ca_polygon_dropped(self):
        """LA-repro: event carrying a CA polygon must be dropped by Idaho gate."""
        ca_geom = {"type": "Polygon", "coordinates": [CA_POLYGON_COORDS]}
        event = self._event_with_geom(ca_geom)
        assert event_in_areas(event, IDAHO_AREAS) is False

    def test_idaho_polygon_kept(self):
        idaho_geom = {
            "type": "Polygon",
            "coordinates": [[
                [-115.0, 42.5], [-113.0, 42.5], [-113.0, 43.5],
                [-115.0, 43.5], [-115.0, 42.5],
            ]],
        }
        event = self._event_with_geom(idaho_geom)
        assert event_in_areas(event, IDAHO_AREAS) is True

    def test_no_geometry_kept_fail_open(self):
        """Event with no lat/lon and no data geometry → null-geom → kept."""
        event = make_event(
            source="swpc", category="kp_index", severity="priority",
            title="No geometry event",
        )
        assert event_in_areas(event, IDAHO_AREAS) is True

    def test_empty_areas_kept(self):
        event = self._event_with_centroid(LA_LAT, LA_LON)
        assert event_in_areas(event, []) is True

    def test_event_with_bbox_in_data(self):
        """Event carrying data[bbox] (not geometry) is classified via bbox polygon."""
        event = make_event(
            source="firms", category="wildfire_hotspot", severity="priority",
            title="Fire",
            data={"bbox": [-114.5, 42.5, -113.5, 43.5]},  # Idaho bbox
        )
        assert event_in_areas(event, IDAHO_AREAS) is True

    def test_event_with_out_of_area_bbox_in_data(self):
        event = make_event(
            source="firms", category="wildfire_hotspot", severity="priority",
            title="Fire",
            data={"bbox": [-119.0, 33.5, -117.5, 34.6]},  # CA bbox
        )
        assert event_in_areas(event, IDAHO_AREAS) is False


# ===========================================================================
# CoverageFilter — unit tests
# ===========================================================================

class TestCoverageFilter:
    def _make_filter(self, areas=None, enabled=True, excluded=None, received=None):
        if received is None:
            received = []
        flt = CoverageFilter(
            next_handler=received.append,
            areas=areas if areas is not None else IDAHO_AREAS,
            enabled=enabled,
            excluded_adapters=set(excluded or []),
        )
        return flt, received

    # -- LA leak repro -------------------------------------------------------

    def test_la_event_with_polygon_dropped(self):
        """LA-repro: event with CA polygon GeoJSON is dropped by Idaho gate."""
        flt, received = self._make_filter()
        ca_geom = {"type": "Polygon", "coordinates": [CA_POLYGON_COORDS]}
        event = make_event(
            source="nws", category="weather_warning", severity="priority",
            title="Severe Thunderstorm Warning — Los Angeles County",
            lat=LA_LAT, lon=LA_LON,
            data={"geometry": ca_geom},
        )
        flt.handle(event)
        assert len(received) == 0, "LA event with CA polygon must be dropped"

    def test_la_event_with_centroid_dropped(self):
        """LA-repro: event with centroid at LA is dropped by Idaho gate."""
        flt, received = self._make_filter()
        event = make_event(
            source="nws", category="weather_warning", severity="priority",
            title="LA weather alert", lat=LA_LAT, lon=LA_LON,
        )
        flt.handle(event)
        assert len(received) == 0, "LA centroid event must be dropped"

    # -- Idaho events pass ---------------------------------------------------

    def test_idaho_event_kept(self):
        flt, received = self._make_filter()
        event = make_event(
            source="nws", category="weather_warning", severity="priority",
            title="Idaho alert", lat=TF_LAT, lon=TF_LON,
        )
        flt.handle(event)
        assert len(received) == 1

    # -- Disabled / no-areas pass-through ------------------------------------

    def test_coverage_disabled_keeps_everything(self):
        flt, received = self._make_filter(enabled=False)
        event = make_event(
            source="nws", category="weather_warning", severity="priority",
            title="LA alert", lat=LA_LAT, lon=LA_LON,
        )
        flt.handle(event)
        assert len(received) == 1

    def test_empty_areas_keeps_everything(self):
        flt, received = self._make_filter(areas=[])
        event = make_event(
            source="nws", category="weather_warning", severity="priority",
            title="LA alert", lat=LA_LAT, lon=LA_LON,
        )
        flt.handle(event)
        assert len(received) == 1

    # -- Excluded adapter bypass ---------------------------------------------

    def test_excluded_adapter_bypasses_gate(self):
        """Events from excluded adapters pass even if out of area."""
        flt, received = self._make_filter(excluded=["swpc"])
        event = make_event(
            source="swpc", category="kp_index", severity="priority",
            title="Solar event", lat=LA_LAT, lon=LA_LON,
        )
        flt.handle(event)
        assert len(received) == 1

    def test_non_excluded_adapter_is_gated(self):
        """Events from non-excluded adapters still go through the gate."""
        flt, received = self._make_filter(excluded=["swpc"])
        event = make_event(
            source="nws", category="weather_warning", severity="priority",
            title="LA alert", lat=LA_LAT, lon=LA_LON,
        )
        flt.handle(event)
        assert len(received) == 0

    # -- fail-open -----------------------------------------------------------

    def test_no_geometry_event_kept_fail_open(self):
        """Events with no geometry (null-geom) are kept (fail-open)."""
        flt, received = self._make_filter()
        event = make_event(
            source="swpc", category="kp_index", severity="priority",
            title="Solar storm — no location",
        )
        flt.handle(event)
        assert len(received) == 1

    # -- multi-area union ----------------------------------------------------

    def test_multi_area_point_in_second_box_kept(self):
        flt, received = self._make_filter(areas=TWO_AREAS)
        # A point in the Yellowstone box (not Idaho proper)
        event = make_event(
            source="nws", category="weather_warning", severity="priority",
            title="Yellowstone area alert", lat=44.5, lon=-110.5,
        )
        flt.handle(event)
        assert len(received) == 1

    def test_multi_area_point_in_neither_dropped(self):
        flt, received = self._make_filter(areas=TWO_AREAS)
        # Far eastern Wyoming — outside both boxes
        event = make_event(
            source="nws", category="weather_warning", severity="priority",
            title="Wyoming alert", lat=42.5, lon=-104.0,
        )
        flt.handle(event)
        assert len(received) == 0


# ===========================================================================
# Pipeline wiring — CoverageFilter is in the chain
# ===========================================================================

class TestCoverageFilterPipelineWiring:
    def test_coverage_filter_wired_in_pipeline(self):
        """build_pipeline stashes coverage_filter in _pipeline_components."""
        from unittest.mock import MagicMock, AsyncMock
        from meshai.notifications.pipeline import build_pipeline

        config = Coverage.__new__(Coverage)  # we need a full Config below
        from meshai.config import Config
        config = Config()
        config.coverage = Coverage(
            areas=[{"name": "Idaho", "west": -117.0, "south": 42.0,
                    "east": -111.0, "north": 44.0}],
            enabled=True,
        )
        mock_backend = MagicMock()
        mock_backend.generate = AsyncMock(return_value="stub")

        bus = build_pipeline(config, mock_backend)
        comps = bus._pipeline_components
        assert "coverage_filter" in comps
        cf = comps["coverage_filter"]
        assert hasattr(cf, "handle")
        assert isinstance(cf, CoverageFilter)

    def test_coverage_filter_gate_fires_in_pipeline(self):
        """Events emitted to the bus from out-of-area are dropped before dispatch."""
        from unittest.mock import MagicMock, AsyncMock
        from meshai.notifications.pipeline import build_pipeline
        from meshai.config import Config, NotificationToggle

        config = Config()
        config.coverage = Coverage(
            areas=[{"name": "Idaho", "west": -117.0, "south": 42.0,
                    "east": -111.0, "north": 44.0}],
            enabled=True,
        )
        # Enable weather toggle so ToggleFilter passes the event through to
        # CoverageFilter (otherwise toggle filter swallows it first)
        config.notifications.toggles["weather"].enabled = True

        mock_backend = MagicMock()
        mock_backend.generate = AsyncMock(return_value="stub")

        received_by_tee: list = []
        bus = build_pipeline(config, mock_backend)

        # Monkeypatch the coverage_filter's next_handler to observe what reaches _tee
        cf = bus._pipeline_components["coverage_filter"]
        cf._next = received_by_tee.append  # type: ignore[assignment]

        # Emit an out-of-area LA event
        la_event = make_event(
            source="nws", category="weather_warning", severity="priority",
            title="LA Thunderstorm Warning", lat=LA_LAT, lon=LA_LON,
        )
        bus.emit(la_event)
        assert len(received_by_tee) == 0, "LA event must not reach _tee"

        # Emit an in-area Idaho event
        id_event = make_event(
            source="nws", category="weather_warning", severity="priority",
            title="Idaho Thunderstorm Warning", lat=TF_LAT, lon=TF_LON,
        )
        bus.emit(id_event)
        assert len(received_by_tee) == 1, "Idaho event must reach _tee"
