"""Part 1 coalescing tests -- env/wzdx.py external_id merge.

Multiple WZDx features that resolve to the SAME coalescing key (same road +
rounded lat/lon + folded sub_type) must collapse to ONE merged stored event
(earliest start_at, latest end_at). Two features on DIFFERENT roads must
stay as 2 distinct keys (no over-collapsing).
"""

import time
from types import SimpleNamespace

import pytest

from meshai import geo
from meshai.env.wzdx import WZDxAdapter


@pytest.fixture(autouse=True)
def _no_photon(monkeypatch):
    monkeypatch.setattr(geo, "nearest_town", lambda *a, **k: None)


@pytest.fixture
def mock_config():
    return SimpleNamespace(
        enabled=True,
        feed_source="native",
        base_url="",
        registry_url="https://datahub.transportation.gov/resource/69qe-yiui.json?$limit=200",
        registry_ttl=21600,
        tick_seconds=300,
        states=["ID"],
        bbox=[],
    )


@pytest.fixture
def adapter(mock_config):
    return WZDxAdapter(mock_config)


def _feature(feat_id, data_source_id, road, direction, start_date, end_date,
             coordinates, description="Paving operations."):
    """Build a WZDx v4 GeoJSON road_event feature (US-26/Gooding-style fans:
    same road/lat/lon/sub_type, different direction / feat_id / schedule
    window)."""
    core = {
        "event_type": "work-zone",
        "data_source_id": data_source_id,
        "road_names": [road],
        "direction": direction,
        "description": description,
    }
    props = {
        "core_details": core,
        "types_of_work": [{"type_name": "surface-work"}],
        "vehicle_impact": "some-lanes-closed",
        "start_date": start_date,
        "end_date": end_date,
    }
    return {
        "id": feat_id,
        "type": "Feature",
        "properties": props,
        "geometry": {"type": "LineString", "coordinates": coordinates},
    }


def make_feature_collection(*features):
    return {"type": "FeatureCollection", "features": list(features)}


# Same road/coords (US-26 near Gooding) fanned across direction + feat_id +
# schedule-day window -- all 3 features describe the SAME physical zone.
_GOODING_COORDS = [[-114.71, 42.94], [-114.72, 42.93]]


def test_same_zone_fan_merges_to_one_event_earliest_start_latest_end(adapter):
    """Three US-26/Gooding-style features (same road+lat+lon+sub_type, fanned
    across direction/feat_id/schedule-day) collapse to ONE merged event_id
    with the earliest start_at and latest end_at across all three."""
    feats = [
        _feature("WZ-100", "idot-1", "US-26", "eastbound",
                  "2026-07-18T06:00:00Z", "2026-07-18T18:00:00Z", _GOODING_COORDS),
        _feature("WZ-101", "idot-1", "US-26", "eastbound",
                  "2026-07-17T06:00:00Z", "2026-07-19T18:00:00Z", _GOODING_COORDS),
        _feature("WZ-102", "idot-1", "US-26", "eastbound",
                  "2026-07-19T06:00:00Z", "2026-07-17T18:00:00Z", _GOODING_COORDS),
    ]
    fc = make_feature_collection(*feats)

    def fake_get(url, timeout=30):
        return fc

    now = time.time()
    events = [adapter._parse_feature(f, now) for f in feats]
    assert all(e is not None for e in events)

    # All three parse to the SAME coalescing key (event_id == external_id).
    keys = {e["event_id"] for e in events}
    assert len(keys) == 1, f"expected 1 coalescing key, got {keys}"

    merged = adapter._coalesce_events(events)
    assert len(merged) == 1

    m = merged[0]
    # Earliest start_at across the three (2026-07-17T06:00:00Z is earliest).
    import calendar
    expected_start = calendar.timegm((2026, 7, 17, 6, 0, 0, 0, 0, 0))
    expected_end = calendar.timegm((2026, 7, 19, 18, 0, 0, 0, 0, 0))
    assert m["start_at"] == expected_start
    assert m["end_at"] == expected_end


def test_different_roads_stay_distinct_keys(adapter):
    """Two features on DIFFERENT roads (same lat/lon/sub_type otherwise)
    must NOT collapse -- 2 distinct coalescing keys survive coalescing."""
    feat_a = _feature("WZ-200", "idot-1", "US-26", "eastbound",
                        "2026-07-18T06:00:00Z", "2026-07-18T18:00:00Z", _GOODING_COORDS)
    feat_b = _feature("WZ-201", "idot-1", "I-84", "eastbound",
                        "2026-07-18T06:00:00Z", "2026-07-18T18:00:00Z", _GOODING_COORDS)

    now = time.time()
    ev_a = adapter._parse_feature(feat_a, now)
    ev_b = adapter._parse_feature(feat_b, now)
    assert ev_a is not None and ev_b is not None
    assert ev_a["event_id"] != ev_b["event_id"]

    merged = adapter._coalesce_events([ev_a, ev_b])
    assert len(merged) == 2
    assert {m["event_id"] for m in merged} == {ev_a["event_id"], ev_b["event_id"]}


def test_merge_keeps_none_end_at_when_either_side_open_ended(adapter):
    """If EITHER merged feature has no end_date, the merged end_at stays
    None (an open-ended zone must never get a fabricated end)."""
    feat_a = _feature("WZ-300", "idot-1", "US-26", "eastbound",
                        "2026-07-18T06:00:00Z", "2026-07-18T18:00:00Z", _GOODING_COORDS)
    feat_b = dict(feat_a)
    feat_b["id"] = "WZ-301"
    feat_b["properties"] = dict(feat_a["properties"])
    feat_b["properties"]["end_date"] = None

    now = time.time()
    ev_a = adapter._parse_feature(feat_a, now)
    ev_b = adapter._parse_feature(feat_b, now)
    assert ev_a["event_id"] == ev_b["event_id"]
    assert ev_a["end_at"] is not None
    assert ev_b["end_at"] is None

    merged = adapter._coalesce_events([ev_a, ev_b])
    assert len(merged) == 1
    assert merged[0]["end_at"] is None


def test_fetch_all_end_to_end_coalesces_via_tick(adapter, monkeypatch):
    """End-to-end: adapter.tick() -> _fetch_all() coalesces a same-zone fan
    down to one stored event via the real feed-fetch path."""
    registry = [{"state": "Idaho", "format": "geojson",
                 "url": "https://itd.idaho.gov/wzdx.geojson"}]
    feats = [
        _feature("WZ-400", "idot-1", "US-26", "eastbound",
                  "2026-07-18T06:00:00Z", "2026-07-18T18:00:00Z", _GOODING_COORDS),
        _feature("WZ-401", "idot-1", "US-26", "westbound",
                  "2026-07-17T06:00:00Z", "2026-07-19T18:00:00Z", _GOODING_COORDS),
    ]
    fc = make_feature_collection(*feats)

    def fake_get(url, timeout=30):
        return registry if "datahub" in url else fc

    monkeypatch.setattr(adapter, "_http_get_json", fake_get)
    changed = adapter.tick()
    assert changed is True
    events = adapter.get_events()
    assert len(events) == 1
    import calendar
    assert events[0]["start_at"] == calendar.timegm((2026, 7, 17, 6, 0, 0, 0, 0, 0))
    assert events[0]["end_at"] == calendar.timegm((2026, 7, 19, 18, 0, 0, 0, 0, 0))
