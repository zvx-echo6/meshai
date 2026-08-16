"""Tests for satpass observer-based region tagging (coverage_area.py).

Covers:
- observer_region_names: single observer in a region, two observers in different
  regions (union, config order, deduped), slug not found / outside all areas.
- event_region_names: geometry path takes priority; falls back to observer path
  only when geometry yields nothing.
- categories: satpass is now a first-class VALID_TOGGLE; categories_for_toggle
  returns ["sat_pass"].
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from meshai.coverage_area import (
    MonitoringArea,
    event_region_names,
    observer_region_names,
)
from meshai.notifications.categories import (
    VALID_TOGGLES,
    all_toggles,
    categories_for_toggle,
)
from meshai.notifications.events import make_event


# ---------------------------------------------------------------------------
# Reference areas — named bboxes
# ---------------------------------------------------------------------------

SC_IDAHO = MonitoringArea(north=43.0, south=42.0, east=-113.5, west=-115.5, name="SC Idaho")
MAGIC_VALLEY = MonitoringArea(north=43.2, south=42.3, east=-114.0, west=-115.0, name="Magic Valley")
# Unnamed area — must never appear in results
UNNAMED = MonitoringArea(north=44.0, south=43.0, east=-113.0, west=-116.0, name=None)

AREAS = [SC_IDAHO, MAGIC_VALLEY, UNNAMED]

# Coordinates inside SC_IDAHO (Twin Falls ~42.56N, -114.47W)
TF_LAT, TF_LON = 42.56, -114.47

# Coordinates inside MAGIC_VALLEY but NOT SC_IDAHO
MV_LAT, MV_LON = 42.5, -114.5   # falls in both SC_IDAHO and MAGIC_VALLEY per overlap
# Use a point that is inside MAGIC_VALLEY bbox only, outside SC_IDAHO:
# SC_IDAHO west=-115.5 east=-113.5  magic-valley west=-115.0 east=-114.0
# A point at lon=-114.8 (between -115.0 and -114.0) and lat=42.7 (inside both bboxes)
# Let's use distinct non-overlapping coords:
# SC_IDAHO  42.0–43.0 N, -115.5–-113.5 W
# MAGIC_VALLEY 42.3–43.2 N, -115.0–-114.0 W
# These boxes overlap. Use a point in SC_IDAHO only (lat=42.1, lon=-115.2):
SC_ONLY_LAT, SC_ONLY_LON = 42.1, -115.2   # south of MAGIC_VALLEY (42.3 min)
# And a point in MAGIC_VALLEY only (lat=43.1, lon=-114.3 — north of SC_IDAHO 43.0 max):
MV_ONLY_LAT, MV_ONLY_LON = 43.1, -114.3

# Outside everything
OUT_LAT, OUT_LON = 34.0, -118.0  # Los Angeles


def _fake_observers(entries):
    """Return a patcher for get_observers that yields `entries`."""
    return patch(
        "meshai.coverage_area.observer_region_names.__code__",  # won't work — use module path
    )


def _patch_observers(entries):
    return patch(
        "meshai.persistence.observer_locations.get_observers",
        return_value=entries,
    )


def _make_satpass_event(observer_list_str, **kwargs):
    """Build a minimal satpass Event-like object with no geometry."""
    return make_event(
        source="satpass",
        category="sat_pass",
        severity="routine",
        title="ISS Pass",
        data={"observer_list": observer_list_str},
        **kwargs,
    )


# ---------------------------------------------------------------------------
# observer_region_names — core unit tests
# ---------------------------------------------------------------------------

class TestObserverRegionNames:
    def test_single_observer_in_one_region(self):
        event = _make_satpass_event("twin-falls")
        with _patch_observers([{"slug": "twin-falls", "lat": SC_ONLY_LAT, "lon": SC_ONLY_LON}]):
            result = observer_region_names(event, AREAS)
        assert result == ["SC Idaho"]

    def test_two_observers_in_different_regions_union_config_order(self):
        """Two observers in SC Idaho and Magic Valley (non-overlapping points)
        → both region names returned in config order (SC Idaho first, then MV)."""
        event = _make_satpass_event("obs-a,obs-b")
        fake_obs = [
            {"slug": "obs-a", "lat": SC_ONLY_LAT, "lon": SC_ONLY_LON},  # SC Idaho only
            {"slug": "obs-b", "lat": MV_ONLY_LAT, "lon": MV_ONLY_LON},  # Magic Valley only
        ]
        with _patch_observers(fake_obs):
            result = observer_region_names(event, AREAS)
        assert result == ["SC Idaho", "Magic Valley"]

    def test_deduplication_same_region_multiple_observers(self):
        """Two observers both inside SC Idaho → region appears only once."""
        event = _make_satpass_event("obs-1,obs-2")
        fake_obs = [
            {"slug": "obs-1", "lat": SC_ONLY_LAT, "lon": SC_ONLY_LON},
            {"slug": "obs-2", "lat": 42.2, "lon": -115.0},  # also inside SC Idaho
        ]
        with _patch_observers(fake_obs):
            result = observer_region_names(event, AREAS)
        assert result.count("SC Idaho") == 1

    def test_unknown_slug_returns_empty(self):
        """Observer slug not in get_observers → []."""
        event = _make_satpass_event("nonexistent-slug")
        with _patch_observers([{"slug": "other", "lat": TF_LAT, "lon": TF_LON}]):
            result = observer_region_names(event, AREAS)
        assert result == []

    def test_observer_outside_all_areas_returns_empty(self):
        """Observer coords outside all configured areas → []."""
        event = _make_satpass_event("la-observer")
        with _patch_observers([{"slug": "la-observer", "lat": OUT_LAT, "lon": OUT_LON}]):
            result = observer_region_names(event, AREAS)
        assert result == []

    def test_unnamed_area_never_in_result(self):
        """Unnamed MonitoringArea (name=None) never contributes to output."""
        event = _make_satpass_event("obs")
        # Point inside UNNAMED bbox (lat=43.5, lon=-114.5) — outside SC Idaho / MV:
        fake_obs = [{"slug": "obs", "lat": 43.5, "lon": -114.5}]
        with _patch_observers(fake_obs):
            result = observer_region_names(event, AREAS)
        assert result == []

    def test_no_observer_list_returns_empty(self):
        """Event without observer_list key → []."""
        event = make_event(
            source="satpass", category="sat_pass", severity="routine",
            title="Pass", data={},
        )
        with _patch_observers([]):
            result = observer_region_names(event, AREAS)
        assert result == []

    def test_empty_areas_returns_empty(self):
        """No configured areas → []."""
        event = _make_satpass_event("obs")
        with _patch_observers([{"slug": "obs", "lat": TF_LAT, "lon": TF_LON}]):
            result = observer_region_names(event, [])
        assert result == []

    def test_get_observers_exception_returns_empty_failopen(self):
        """If get_observers raises, observer_region_names returns [] (fail-open)."""
        event = _make_satpass_event("obs")
        with patch(
            "meshai.persistence.observer_locations.get_observers",
            side_effect=RuntimeError("db gone"),
        ):
            result = observer_region_names(event, AREAS)
        assert result == []


# ---------------------------------------------------------------------------
# event_region_names — geometry takes priority; observer fallback for satpass
# ---------------------------------------------------------------------------

class TestEventRegionNames:
    def test_event_with_geometry_uses_geometry_not_observers(self):
        """An event with a polygon in SC Idaho must tag via geometry; the
        observer fallback must NOT be consulted (and if it were, it would
        return a different/no region because the observer slug is absent)."""
        geom = {
            "type": "Point",
            "coordinates": [SC_ONLY_LON, SC_ONLY_LAT],  # [lon, lat]
        }
        event = make_event(
            source="satpass", category="sat_pass", severity="routine",
            title="Pass",
            lat=SC_ONLY_LAT, lon=SC_ONLY_LON,
            data={"observer_list": "no-such-slug"},
        )
        # With a lat/lon centroid, _event_geom_json will produce a Point.
        # Even though the observer slug won't resolve, the geometry path fires first.
        # SC_ONLY_LAT=42.1, SC_ONLY_LON=-115.2 is inside SC_IDAHO bbox.
        result = event_region_names(event, AREAS)
        assert "SC Idaho" in result

    def test_satpass_event_no_geometry_falls_back_to_observers(self):
        """Satpass event with NO lat/lon and NO geometry data → geometry path
        yields [], observer fallback kicks in and returns correct region."""
        event = make_event(
            source="satpass", category="sat_pass", severity="routine",
            title="ISS Pass",
            data={"observer_list": "boise-obs"},
            # deliberately no lat/lon
        )
        assert event.lat is None
        assert event.lon is None
        fake_obs = [{"slug": "boise-obs", "lat": SC_ONLY_LAT, "lon": SC_ONLY_LON}]
        with _patch_observers(fake_obs):
            result = event_region_names(event, AREAS)
        assert result == ["SC Idaho"]

    def test_event_with_geometry_in_region_uses_geometry_observers_not_consulted(self):
        """When geometry tags a region, the observer path must not run — even if
        observers would tag a different region."""
        # Event has a geometry that puts it in SC Idaho
        event = make_event(
            source="satpass", category="sat_pass", severity="routine",
            title="Pass",
            lat=SC_ONLY_LAT, lon=SC_ONLY_LON,
            data={"observer_list": "mv-obs"},
        )
        # Observer is in Magic Valley (MV_ONLY) — if observer path ran it would
        # return Magic Valley. But geometry path should win: SC Idaho.
        fake_obs = [{"slug": "mv-obs", "lat": MV_ONLY_LAT, "lon": MV_ONLY_LON}]
        with _patch_observers(fake_obs):
            result = event_region_names(event, AREAS)
        # Geometry (lat/lon centroid at SC_ONLY) fires first → SC Idaho
        assert "SC Idaho" in result
        # Magic Valley should NOT be in result (observer path not reached)
        assert "Magic Valley" not in result


# ---------------------------------------------------------------------------
# categories.py — satpass is a first-class VALID_TOGGLE
# ---------------------------------------------------------------------------

class TestSatpassToggle:
    def test_satpass_in_valid_toggles(self):
        assert "satpass" in VALID_TOGGLES

    def test_satpass_in_all_toggles(self):
        assert "satpass" in all_toggles()

    def test_categories_for_toggle_satpass_returns_sat_pass(self):
        result = categories_for_toggle("satpass")
        assert result == ["sat_pass"]


# ---------------------------------------------------------------------------
# End-to-end: a satpass Event built through the REAL native-adapter path
# (predict -> consolidate -> gate_consolidated_pass -> to_event) must
# region-tag via event_region_names(), using the two production observers.
# This is the regression test for the observer_list wiring bug: before the
# fix, gate_consolidated_pass()'s data dict never carried observer_list, so
# this resolved to [] no matter how the areas were configured.
# ---------------------------------------------------------------------------

# Production ground stations (see adapter_config observers): Treasure Valley
# (Boise area) and Magic Valley (Twin Falls area).
_TREASURE_VALLEY = {"slug": "treasure_valley", "name": "Treasure Valley",
                     "lat": 43.6, "lon": -116.2, "alt_m": 0.0}
_MAGIC_VALLEY = {"slug": "magic_valley", "name": "Magic Valley",
                  "lat": 42.5558, "lon": -114.4701, "alt_m": 0.0}

# Coarse named boxes that cover each observer, standing in for the real
# region_routes cells ("SW Idaho" covers the Treasure Valley/Boise area,
# "SC Idaho" covers the Magic Valley/Twin Falls area).
_SW_IDAHO = MonitoringArea(north=44.5, south=42.8, east=-115.0, west=-117.5,
                           name="SW Idaho")
_SC_IDAHO_PROD = MonitoringArea(north=43.2, south=42.0, east=-113.0, west=-115.2,
                                name="SC Idaho")
_PROD_AREAS = [_SW_IDAHO, _SC_IDAHO_PROD]


class TestSatpassEndToEndRegionTagging:
    def test_real_gate_path_region_tags_nonempty(self, monkeypatch):
        """Drive the actual SatpassAdapter tick -> gate -> to_event path
        (no shortcuts through gate_consolidated_pass or make_event) with the
        two production observer coordinates, then confirm event_region_names
        returns a non-empty list built from event.data['observer_list']."""
        import json as _json
        from datetime import datetime, timezone

        from meshai.adapter_config import invalidate_cache
        from meshai.config import SatpassConfig
        from meshai.env.satellite.pass_predictor import PassInfo
        from meshai.env.satellite.tle_store import upsert_tle
        from meshai.env.satpass import SatpassAdapter
        from meshai.persistence import get_db

        # -- seed a fresh ISS TLE --------------------------------------------------
        conn = get_db()
        fresh = datetime.now(timezone.utc).isoformat()
        iss_l1 = "1 25544U 98067A   26182.50000000  .00016717  00000-0  10270-3 0  9008"
        iss_l2 = "2 25544  51.6400 208.9163 0007417  17.6777  85.6621 15.54225995 12345"
        upsert_tle(conn, 25544, "ISS (ZARYA)", iss_l1, iss_l2, fresh)

        # -- enable satpass, dry_run off so the gate actually returns data --------
        conn.execute("UPDATE adapter_config SET value_json='true' "
                     "WHERE adapter='satpass' AND key='enabled'")
        conn.execute("UPDATE adapter_config SET value_json='false' "
                     "WHERE adapter='satpass' AND key='dry_run'")
        conn.execute("UPDATE adapter_config SET value_json=? "
                     "WHERE adapter='satpass' AND key='max_broadcasts_per_hour'",
                     (_json.dumps(100),))
        invalidate_cache()

        # -- patch observers + predictor -------------------------------------------
        monkeypatch.setattr(
            "meshai.persistence.observer_locations.get_observers",
            lambda *a, **k: [_TREASURE_VALLEY, _MAGIC_VALLEY])

        t0 = (1783000000 // 3600) * 3600 + 100

        def _fake_compute_passes(l1, l2, lat, lon, alt, window_h, min_el, now):
            def _pi(aos, los, max_el, az_aos, az_los, az_peak):
                peak = (aos + los) // 2
                return PassInfo(
                    aos_time=datetime.fromtimestamp(aos, tz=timezone.utc),
                    los_time=datetime.fromtimestamp(los, tz=timezone.utc),
                    peak_time=datetime.fromtimestamp(peak, tz=timezone.utc),
                    max_elevation=max_el,
                    azimuth_at_aos=az_aos, azimuth_at_los=az_los,
                    azimuth_at_peak=az_peak)
            if abs(lat - _TREASURE_VALLEY["lat"]) < 0.1:
                return [_pi(t0, t0 + 300, 40.0, 225, 300, 90)]
            if abs(lat - _MAGIC_VALLEY["lat"]) < 0.1:
                return [_pi(t0 + 60, t0 + 400, 70.0, 270, 45, 180)]
            return []

        monkeypatch.setattr(
            "meshai.env.satellite.pass_predictor.compute_passes",
            _fake_compute_passes)

        cfg = SatpassConfig(enabled=True, feed_source="native",
                            norad_ids=[25544], min_elevation_deg=10.0,
                            window_hours=24)
        adapter = SatpassAdapter(cfg)
        assert adapter.tick(now=t0 - 1800) is True  # AOS 30 min out -> imminent

        staged = adapter.get_events()
        assert len(staged) == 1
        event = adapter.to_event(staged[0])
        assert event is not None

        # The bug: before the fix, data["observer_list"] was never set, so
        # this was always [] regardless of area config.
        assert event.data.get("observer_list"), (
            "gate_consolidated_pass() did not populate observer_list on the "
            "event data dict")

        names = event_region_names(event, _PROD_AREAS)
        assert names, "satpass event failed to region-tag via observer_list"
        assert set(names) == {"SW Idaho", "SC Idaho"}
