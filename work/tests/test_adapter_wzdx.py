"""Tests for the FHWA WZDx native adapter — to_event() + discovery/fetch.

Mirrors the style of test_adapter_firms.py: build realistic WZDx v4 GeoJSON
fixtures, drive the adapter's parse + to_event, and assert the canonical
``work_zone`` data dict is populated correctly and renders through the real
Phase-2 incident formatter.  nearest_town() is patched off (no Photon
network) for deterministic town-independent assertions.
"""

import calendar
import time
from types import SimpleNamespace

import pytest

import meshai.central_normalizer as cn
from meshai.env.wzdx import WZDxAdapter
from meshai.notifications.events import Event
from meshai.notifications.formatters.incident import format as format_incident


# ============================================================
# FIXTURES
# ============================================================

@pytest.fixture(autouse=True)
def _no_photon(monkeypatch):
    """Disable the Photon nearest_town lookup so parsing is network-free."""
    monkeypatch.setattr(cn, "nearest_town", lambda *a, **k: None)


@pytest.fixture
def mock_config():
    """A WZDx config with registry discovery enabled, keyless."""
    return SimpleNamespace(
        enabled=True,
        feed_source="native",
        api_key="",
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


def make_wzdx_feature(
    feat_id="WZ-0001",
    data_source_id="idot-1",
    road="US-95",
    direction="southbound",
    event_type="work-zone",
    work_type="surface-work",
    vehicle_impact="some-lanes-closed",
    description="Paving Operations on US-95 from MM (93) to MM (89).",
    start_date="2026-07-18T13:00:00Z",
    end_date="2026-07-19T00:59:59Z",
    coordinates=None,
):
    """Build a realistic WZDx v4 GeoJSON road_event feature."""
    if coordinates is None:
        coordinates = [[-116.91, 43.66], [-116.92, 43.60]]
    core = {"event_type": event_type, "data_source_id": data_source_id}
    if road is not None:
        core["road_names"] = [road]
    if direction is not None:
        core["direction"] = direction
    if description is not None:
        core["description"] = description
    props = {"core_details": core}
    if work_type is not None:
        props["types_of_work"] = [{"type_name": work_type}]
    if vehicle_impact is not None:
        props["vehicle_impact"] = vehicle_impact
    if start_date is not None:
        props["start_date"] = start_date
    if end_date is not None:
        props["end_date"] = end_date
    return {
        "id": feat_id,
        "type": "Feature",
        "properties": props,
        "geometry": {"type": "LineString", "coordinates": coordinates},
    }


def make_feature_collection(*features):
    return {"type": "FeatureCollection", "features": list(features)}


# ============================================================
# REGISTRY DISCOVERY
# ============================================================

def test_select_feeds_filters_state_and_geojson(adapter):
    """Registry selection keeps configured-state GeoJSON feeds only."""
    rows = [
        {"state": "Idaho", "format": "geojson",
         "url": "https://itd.idaho.gov/wzdx.geojson", "version": "4.0"},
        {"state": "ID", "format": "GeoJSON", "apiurl": "https://alt.idaho/wzdx"},
        {"state": "Utah", "format": "geojson", "url": "https://udot/wzdx"},
        {"state": "Idaho", "format": "xml", "url": "https://itd/xml"},
    ]
    feeds = adapter._select_feeds(rows)
    assert feeds == ["https://itd.idaho.gov/wzdx.geojson", "https://alt.idaho/wzdx"]


def test_wanted_states_expands_abbrev_and_name(adapter):
    """'ID' matches both the abbreviation and 'idaho'."""
    assert {"id", "idaho"} <= adapter._wanted_states()


def test_select_feeds_handles_garbage(adapter):
    """Non-list / non-dict rows never crash selection."""
    assert adapter._select_feeds("not a list") == []
    assert adapter._select_feeds([None, 3, {"state": "ID"}]) == []  # no url


# ============================================================
# CATEGORY + CANONICAL DATA
# ============================================================

def test_to_event_category_is_work_zone(adapter):
    evt = adapter._parse_feature(make_wzdx_feature(), time.time())
    event = adapter.to_event(evt)
    assert event is not None
    assert event.category == "work_zone"


def test_canonical_data_core_fields(adapter):
    """road / direction / mile posts / lat/lon map correctly."""
    evt = adapter._parse_feature(make_wzdx_feature(), time.time())
    d = adapter.to_event(evt).data
    assert d["road"] == "US-95"
    assert d["direction"] == "southbound"      # full form the renderer prints
    assert d["mile_start"] == 93
    assert d["mile_end"] == 89
    assert d["lat"] == pytest.approx(43.63, abs=0.05)
    assert d["lon"] == pytest.approx(-116.915, abs=0.05)


def test_canonical_external_id_is_stable(adapter):
    """external_id combines data_source_id + feature id for gating dedup."""
    evt = adapter._parse_feature(make_wzdx_feature(), time.time())
    d = adapter.to_event(evt).data
    assert d["external_id"] == "idot-1:WZ-0001"
    assert d["source"] == "wzdx"


def test_canonical_start_end_and_ends_epoch(adapter):
    """start_at / end_at epochs and ends_at_epoch are populated."""
    evt = adapter._parse_feature(make_wzdx_feature(), time.time())
    d = adapter.to_event(evt).data
    # end_date 2026-07-19T00:59:59Z
    expected_end = calendar.timegm((2026, 7, 19, 0, 59, 59, 0, 0, 0))
    assert d["end_at"] == expected_end
    assert d["start_at"] == calendar.timegm((2026, 7, 18, 13, 0, 0, 0, 0, 0))
    assert d["ends_at_epoch"] == float(expected_end)


def test_canonical_lanes_and_subtype_partial(adapter):
    """some-lanes-closed → impact partial + folded sub_type + lanes phrase."""
    evt = adapter._parse_feature(make_wzdx_feature(), time.time())
    d = adapter.to_event(evt).data
    assert d["impact"] == "partial"
    assert d["sub_type"] == "lanes reduced, surface work"
    assert d["lanes_affected"] == "lanes affected"
    assert d["icon_category"] == "road_works"


def test_full_closure_maps_to_priority(adapter):
    """all-lanes-closed → priority severity + full_closure impact."""
    feat = make_wzdx_feature(
        vehicle_impact="all-lanes-closed", work_type="bridge-construction",
        road="I-84", direction="eastbound",
    )
    evt = adapter._parse_feature(feat, time.time())
    assert evt["severity"] == "priority"
    event = adapter.to_event(evt)
    assert event.severity == "priority"
    assert event.data["impact"] == "full_closure"
    assert event.data["lanes_affected"] == "all lanes closed"


# ============================================================
# FORMATTER INTEGRATION (real Phase-2 renderer)
# ============================================================

def test_renders_through_real_formatter(adapter):
    """Emitted data flows through formatters/incident.format() to a wire."""
    now = time.time()
    evt = adapter._parse_feature(make_wzdx_feature(), now)
    event = adapter.to_event(evt)
    wire = format_incident(event, now=now, budget=200)
    assert wire.startswith("🚧")
    assert "US-95" in wire
    assert "mile 93" in wire
    assert len(wire.encode("utf-8")) <= 80   # work-zone byte cap


def test_full_closure_wire_shows_all_lanes_closed(adapter):
    feat = make_wzdx_feature(vehicle_impact="all-lanes-closed",
                             work_type="bridge-construction", road="I-84")
    evt = adapter._parse_feature(feat, time.time())
    wire = format_incident(adapter.to_event(evt), now=time.time(), budget=200)
    assert "all lanes closed" in wire


# ============================================================
# FEED FETCH FLOW
# ============================================================

def test_tick_fetches_and_populates_events(adapter, monkeypatch):
    """A full tick discovers a feed, fetches it, and stores parsed events."""
    registry = [{"state": "Idaho", "format": "geojson",
                 "url": "https://itd.idaho.gov/wzdx.geojson"}]
    fc = make_feature_collection(
        make_wzdx_feature(feat_id="A"),
        make_wzdx_feature(feat_id="B", road="I-15", description="Road work"),
    )

    def fake_get(url, timeout=30):
        return registry if "datahub" in url else fc

    monkeypatch.setattr(adapter, "_http_get_json", fake_get)
    changed = adapter.tick()
    assert changed is True
    events = adapter.get_events()
    assert len(events) == 2
    assert {e["external_id"] for e in events} == {"idot-1:A", "idot-1:B"}
    assert adapter.health_status["feed_count"] == 1


def test_feed_failure_does_not_crash(adapter, monkeypatch):
    """A down feed is skipped; the adapter survives and stays healthy-ish."""
    registry = [{"state": "Idaho", "format": "geojson", "url": "https://itd/wzdx"}]

    def fake_get(url, timeout=30):
        if "datahub" in url:
            return registry
        raise TimeoutError("feed down")

    monkeypatch.setattr(adapter, "_http_get_json", fake_get)
    # Should not raise; no events, last_error recorded.
    assert adapter.tick() is False
    assert adapter.get_events() == []
    assert adapter.health_status["last_error"] is not None


# ============================================================
# DEFENSIVE
# ============================================================

def test_empty_feature_returns_none(adapter):
    assert adapter._parse_feature({}, time.time()) is None


def test_non_workzone_feature_skipped(adapter):
    feat = make_wzdx_feature(event_type="special-event")
    assert adapter._parse_feature(feat, time.time()) is None


def test_missing_coords_returns_none(adapter):
    feat = make_wzdx_feature(coordinates=[])
    feat["properties"].pop("core_details", None)  # also no lat/lon
    feat["geometry"] = {"type": "LineString", "coordinates": []}
    assert adapter._parse_feature(feat, time.time()) is None


def test_no_stable_id_returns_none(adapter):
    feat = make_wzdx_feature(data_source_id=None)
    feat.pop("id", None)
    feat["properties"]["core_details"].pop("data_source_id", None)
    assert adapter._parse_feature(feat, time.time()) is None


def test_to_event_corrupted_dict_returns_none(adapter):
    assert adapter.to_event({"garbage": True}) is None


def test_to_event_missing_coords_returns_none(adapter):
    evt = adapter._parse_feature(make_wzdx_feature(), time.time())
    evt["lat"] = None
    assert adapter.to_event(evt) is None
