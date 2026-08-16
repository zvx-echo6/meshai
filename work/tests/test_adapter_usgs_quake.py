"""Tests for USGS earthquake adapter Phase 2.14 — fetch/filter + to_event()."""

import time
from unittest.mock import MagicMock

import pytest

from meshai.env.usgs_quake import USGSQuakeAdapter
from meshai.notifications.events import Event


# ============================================================
# FIXTURES / HELPERS
# ============================================================

def fresh_adapter(min_magnitude=2.5, bbox=None, region="magic_valley"):
    cfg = MagicMock()
    cfg.feed_url = "https://example.test/feed.geojson"
    cfg.min_magnitude = min_magnitude
    cfg.bbox = bbox if bbox is not None else [-115.5, 42.0, -110.0, 45.2]
    cfg.region = region
    cfg.tick_seconds = 300
    return USGSQuakeAdapter(cfg)


@pytest.fixture
def adapter():
    return fresh_adapter()


def make_feature(quake_id="us6000abcd", mag=3.0, lon=-114.5, lat=42.6, depth=8.0,
                 place="10 km N of Twin Falls, ID", time_ms=None):
    """A USGS GeoJSON feature (mirrors the real feed)."""
    if time_ms is None:
        time_ms = int(time.time() * 1000)
    return {
        "type": "Feature",
        "id": quake_id,
        "properties": {
            "mag": mag,
            "place": place,
            "time": time_ms,
            "sig": int((mag or 0) * 30),
            "url": f"https://earthquake.usgs.gov/earthquakes/eventpage/{quake_id}",
            "title": f"M {mag} - {place}",
        },
        "geometry": {"type": "Point", "coordinates": [lon, lat, depth]},
    }


def feed(features):
    return {"type": "FeatureCollection", "features": features}


def make_quake_event(quake_id="us6000abcd", mag=3.0, severity="routine",
                     lat=42.6, lon=-114.5, depth=8.0, region="magic_valley"):
    """A stored quake dict (mirrors _fetch output) for to_event tests."""
    now = time.time()
    return {
        "source": "usgs_quake",
        "event_id": quake_id,
        "event_type": "Earthquake",
        "severity": severity,
        "headline": f"M {mag} - near Twin Falls",
        "magnitude": mag,
        "place": "near Twin Falls",
        "depth_km": depth,
        "sig": int(mag * 30),
        "url": "https://earthquake.usgs.gov/x",
        "region": region,
        "lat": lat,
        "lon": lon,
        "quake_time": now,
        "expires": now + 86400,
        "fetched_at": now,
    }


def run_fetch(adapter, features):
    """Patch urlopen to return a synthetic feed and run _fetch."""
    import meshai.env.usgs_quake as mod

    class FakeResp:
        def __init__(self, payload):
            self._p = payload.encode()
        def read(self):
            return self._p
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    import json as _json
    payload = _json.dumps(feed(features))
    orig = mod.urlopen
    mod.urlopen = lambda req, timeout=30: FakeResp(payload)
    try:
        adapter._fetch()
    finally:
        mod.urlopen = orig
    return adapter.get_events()


# ============================================================
# SEVERITY BIN TESTS
# ============================================================

def test_severity_bins(adapter):
    """M<3.5 routine, 3.5-5 priority, >=5 immediate."""
    assert adapter._severity_for_mag(2.9) == "routine"
    assert adapter._severity_for_mag(3.4) == "routine"
    assert adapter._severity_for_mag(3.5) == "priority"
    assert adapter._severity_for_mag(4.9) == "priority"
    assert adapter._severity_for_mag(5.0) == "immediate"
    assert adapter._severity_for_mag(6.5) == "immediate"


def test_fetch_assigns_severity(adapter):
    evs = run_fetch(adapter, [
        make_feature("q1", mag=3.0, lat=42.6, lon=-114.5),
        make_feature("q2", mag=4.2, lat=43.0, lon=-113.0),
        make_feature("q3", mag=5.5, lat=44.4, lon=-110.6),
    ])
    by_id = {e["event_id"]: e["severity"] for e in evs}
    assert by_id == {"q1": "routine", "q2": "priority", "q3": "immediate"}


# ============================================================
# MAGNITUDE + GEOGRAPHIC FILTER TESTS
# ============================================================

def test_magnitude_filter(adapter):
    """Quakes below min_magnitude are dropped."""
    evs = run_fetch(adapter, [
        make_feature("small", mag=2.0, lat=42.6, lon=-114.5),  # below 2.5
        make_feature("ok", mag=2.6, lat=42.6, lon=-114.5),
    ])
    ids = {e["event_id"] for e in evs}
    assert ids == {"ok"}


def test_geographic_filter(adapter):
    """Quakes outside the bbox are dropped."""
    evs = run_fetch(adapter, [
        make_feature("in", mag=3.0, lat=42.6, lon=-114.5),       # Magic Valley -> in
        make_feature("ca", mag=3.0, lat=40.6, lon=-121.5),       # California -> out
        make_feature("yellowstone", mag=3.0, lat=44.4, lon=-110.6),  # in
    ])
    ids = {e["event_id"] for e in evs}
    assert ids == {"in", "yellowstone"}


def test_no_bbox_accepts_all():
    """An empty bbox disables the geographic filter."""
    a = fresh_adapter(bbox=[])
    evs = run_fetch(a, [
        make_feature("ca", mag=3.0, lat=40.6, lon=-121.5),
        make_feature("id", mag=3.0, lat=42.6, lon=-114.5),
    ])
    assert {e["event_id"] for e in evs} == {"ca", "id"}


# ============================================================
# DEDUP REGRESSION TEST
# ============================================================

def test_dedup_id_stable_across_ticks(adapter):
    """A quake keeps its USGS id as event_id across ticks (store dedups it)."""
    e1 = run_fetch(adapter, [make_feature("us6000abcd", mag=3.0, lat=42.6, lon=-114.5)])[0]["event_id"]
    time.sleep(0.01)
    e2 = run_fetch(adapter, [make_feature("us6000abcd", mag=3.1, lat=42.6, lon=-114.5)])[0]["event_id"]
    assert e1 == e2 == "us6000abcd"


# ============================================================
# to_event() — CATEGORY / KEYS / FIELDS
# ============================================================

def test_category_is_earthquake_event(adapter):
    event = adapter.to_event(make_quake_event())
    assert event is not None
    assert event.category == "earthquake_event"


def test_severity_passes_through(adapter):
    for sev in ["routine", "priority", "immediate"]:
        event = adapter.to_event(make_quake_event(severity=sev))
        assert event.severity == sev


def test_group_key_is_usgs_id(adapter):
    event = adapter.to_event(make_quake_event(quake_id="us6000xyz"))
    assert event.group_key == "us6000xyz"
    assert event.inhibit_keys == ["us6000xyz"]


def test_populates_core_fields(adapter):
    evt = make_quake_event(lat=42.61, lon=-114.48, region="magic_valley")
    event = adapter.to_event(evt)
    assert event.source == "usgs_quake"
    assert event.lat == 42.61
    assert event.lon == -114.48
    # to_event() deliberately does NOT propagate the raw dict's "region"
    # (a fixed config default that never matches region_routes cell keys)
    # onto the Event -- Event.region is left unset so CoverageFilter's
    # geometry-based tagger (lat/lon vs named coverage areas) can set the
    # real region name downstream. See test_region_left_unset_for_tagger
    # and tests/test_coverage_area.py for the tagging behavior itself.
    assert event.region is None
    assert event.expires == evt["expires"]
    assert event.timestamp == evt["quake_time"]
    assert event.id


def test_region_left_unset_for_tagger(adapter):
    """Regression: to_event() must never propagate the adapter's fixed
    config region (e.g. "magic_valley") onto the Event. That value never
    matches a region_routes cell key ("East Idaho"/"SC Idaho"/"SW Idaho"),
    so pre-setting it would silently break region-routed delivery. Leaving
    event.region/regions unset lets CoverageFilter's geometry tagger (which
    only fires when `not event.regions`) stamp the real area name from the
    quake's actual lat/lon."""
    evt = make_quake_event(lat=44.2, lon=-114.0, region="magic_valley")
    event = adapter.to_event(evt)
    assert event.region is None
    assert event.regions == []


# ============================================================
# DEFENSIVE TESTS
# ============================================================

def test_missing_id_returns_none(adapter):
    evt = make_quake_event()
    evt["event_id"] = None
    assert adapter.to_event(evt) is None


def test_missing_coords_returns_none(adapter):
    evt = make_quake_event()
    evt["lat"] = None
    assert adapter.to_event(evt) is None


def test_missing_magnitude_returns_none(adapter):
    evt = make_quake_event()
    evt["magnitude"] = None
    assert adapter.to_event(evt) is None


def test_does_not_raise_on_corrupted_dict(adapter):
    assert adapter.to_event({"garbage": True}) is None


def test_fetch_skips_malformed_features(adapter):
    """Features missing id/mag/coords are skipped without raising."""
    evs = run_fetch(adapter, [
        {"id": "noprops", "geometry": {"coordinates": [-114.5, 42.6, 5]}},  # no mag
        {"id": "nogeom", "properties": {"mag": 3.0}},  # no coords
        make_feature("good", mag=3.0, lat=42.6, lon=-114.5),
    ])
    assert {e["event_id"] for e in evs} == {"good"}
