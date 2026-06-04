"""Tests for meshai/central_normalizer.py — adapter-specific envelope
normalization. First adapter wired: state_511_atis."""

import json
from datetime import datetime
from pathlib import Path

import pytest

from meshai.central_normalizer import normalize


FIXTURES = Path(__file__).parent / "fixtures" / "central_envelopes"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _norm_fixture(name: str) -> dict:
    n = normalize(_load(name))
    assert n is not None, f"normalize({name}) returned None"
    return n


# ---------- adapter dispatch -----------------------------------------------


def test_normalize_returns_none_for_unknown_adapter():
    env = {"data": {"adapter": "totally_made_up", "data": {}}}
    assert normalize(env) is None


def test_normalize_returns_none_for_non_envelope():
    assert normalize(None) is None
    assert normalize("not-a-dict") is None
    assert normalize([]) is None


# ---------- state_511_atis: MM-range fixture (I-15 SB 93→89) --------------


def test_mm_range_extracted_high_to_low():
    n = _norm_fixture("state_511_atis_01_I-15.json")
    assert n["source"] == "state_511_atis"
    assert n["road"] == "I-15"
    assert n["direction"] == "southbound"
    assert n["mile_start"] == 93
    assert n["mile_end"] == 89    # decreasing range is valid for SB I-15
    assert n["impact"] == "partial"
    assert n["sub_type"] == "road construction"
    assert isinstance(n["description"], str) and "MM (93)" in n["description"]


def test_mm_range_extracted_low_to_high():
    n = _norm_fixture("state_511_atis_03_I-15.json")
    assert n["road"] == "I-15"
    assert n["direction"] == "northbound"
    assert n["mile_start"] == 89
    assert n["mile_end"] == 93
    assert n["sub_type"] == "bridge construction"


# ---------- state_511_atis: MM-near (single mile post) --------------------


def test_mm_near_single_mile_post():
    n = _norm_fixture("state_511_atis_04_US-95.json")
    assert n["road"] == "US-95"
    assert n["direction"] == "southbound"
    assert n["mile_start"] == 495
    assert n["mile_end"] is None
    assert n["sub_type"] == "utility work"


# ---------- state_511_atis: no MM (cross-street / landmark) ---------------


def test_no_mm_in_description_yields_none_mile_posts():
    n = _norm_fixture("state_511_atis_05_W_Prairie_Ave.json")
    assert n["mile_start"] is None
    assert n["mile_end"] is None
    assert n["road"] == "W Prairie Ave"
    assert n["direction"] == "both"


def test_no_mm_emergency_repairs_landmark():
    n = _norm_fixture("state_511_atis_06_SH-55.json")
    assert n["mile_start"] is None
    assert n["road"] == "SH-55"
    assert n["direction"] == "both"
    assert n["sub_type"] == "emergency repairs"


# ---------- impact (full_closure vs partial) ------------------------------


def test_partial_impact_for_lane_restriction():
    n = _norm_fixture("state_511_atis_01_I-15.json")
    assert n["impact"] == "partial"


def test_full_closure_impact():
    # Synthetic — we didn't capture a full closure in the 60-sample probe,
    # so build one inline to exercise the branch.
    env = {
        "data": {
            "adapter": "state_511_atis",
            "category": "closure.state_511_atis",
            "data": {
                "roadway_name": "I-15",
                "direction": "South",
                "description": "Road construction on I-15 Southbound near Northgate Pkwy. "
                               "All lanes closed. 6/1/2026 7:00 AM to 6/10/2026 5:00 PM.",
                "event_sub_type": "roadConstruction",
                "is_full_closure": True,
                "county": "Bannock",
                "latitude": 42.8713,
                "longitude": -112.4455,
            },
        },
    }
    n = normalize(env)
    assert n["impact"] == "full_closure"


# ---------- direction normalization ---------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("North", "northbound"),
    ("south", "southbound"),
    ("Both", "both"),
    ("East", "eastbound"),
    ("West", "westbound"),
    ("Unknown", "unknown"),
    ("", "unknown"),
    ("NB", "northbound"),
    (None, None),
])
def test_direction_normalization(raw, expected):
    env = {"data": {"adapter": "state_511_atis", "category": "work_zone.state_511_atis",
                    "data": {"roadway_name": "X", "direction": raw, "description": ""}}}
    n = normalize(env)
    assert n["direction"] == expected


# ---------- ends_at parsing -----------------------------------------------


def test_ends_at_parsed_from_description():
    n = _norm_fixture("state_511_atis_04_US-95.json")
    assert isinstance(n["ends_at"], datetime)
    assert n["ends_at"].month == 6 and n["ends_at"].day == 2
    assert n["ends_at"].hour == 17  # 5 PM


def test_ends_at_missing_when_no_date_range():
    env = {"data": {"adapter": "state_511_atis", "category": "work_zone.state_511_atis",
                    "data": {"roadway_name": "X", "direction": "Both",
                             "description": "Just some text with no date."}}}
    n = normalize(env)
    assert n["ends_at"] is None


# ---------- _enriched geocoder + town -------------------------------------


def test_town_from_geocoder_city():
    # Use a fixture and check town came from geocoder city/name.
    n = _norm_fixture("state_511_atis_01_I-15.json")
    assert isinstance(n["town"], str) and n["town"]


def test_town_missing_when_no_enriched():
    env = {"data": {"adapter": "state_511_atis", "category": "work_zone.state_511_atis",
                    "data": {"roadway_name": "X", "direction": "Both", "description": ""}}}
    n = normalize(env)
    assert n["town"] is None
    assert n["distance_mi"] is None
    assert n["bearing"] is None


def test_distance_bearing_when_town_in_lookup():
    # A known town (Idaho Falls) at known coords; event placed 8 mi north.
    env = {"data": {"adapter": "state_511_atis", "category": "work_zone.state_511_atis",
                    "data": {"roadway_name": "US-20", "direction": "Both",
                             "description": "Test event",
                             "_enriched": {"geocoder": {"city": "Idaho Falls"}},
                             "latitude": 43.4666 + 8.0 / 69.0,  # ~8 mi north
                             "longitude": -112.0340}}}
    n = normalize(env)
    assert n["town"] == "Idaho Falls"
    assert n["distance_mi"] is not None
    assert 7 <= n["distance_mi"] <= 9     # ~8 mi
    assert n["bearing"] == "N"


def test_distance_none_when_town_not_in_lookup():
    env = {"data": {"adapter": "state_511_atis", "category": "work_zone.state_511_atis",
                    "data": {"roadway_name": "X", "direction": "Both",
                             "description": "Test event",
                             "_enriched": {"geocoder": {"city": "Unknownsville"}},
                             "latitude": 43.0, "longitude": -116.0}}}
    n = normalize(env)
    assert n["town"] == "Unknownsville"
    assert n["distance_mi"] is None
    assert n["bearing"] is None


# ---------- v0.5.8 normalize_road_name (SB/NB/EB/WB → S/N/E/W) ------------

from meshai.central_normalizer import normalize_road_name, nearest_town

@pytest.mark.parametrize("raw,expected", [
    ("I-15 SB Off Ramp",        "I-15 S Off Ramp"),
    ("I-15 NB Off Ramp",        "I-15 N Off Ramp"),
    ("US-95 NB",                "US-95 N"),
    ("SH-55 EB",                "SH-55 E"),
    ("Exit 80 WB On Ramp",      "Exit 80 W On Ramp"),
    ("I-86-BL",                 "I-86-BL"),       # no SB/NB token; untouched
    ("I-15",                    "I-15"),
    ("",                        None),
    (None,                      None),
])
def test_normalize_road_name(raw, expected):
    assert normalize_road_name(raw) == expected


# ---------- v0.5.8 nearest_town: Photon + H3 cache ------------------------

# Photon /reverse?osm_tag=place returns features like:
_PHOTON_STANLEY = {
    "features": [
        {"geometry": {"coordinates": [-114.9378523, 44.2161414]},
         "properties": {"name": "Stanley", "osm_key": "place", "osm_value": "city"}},
    ],
}
_PHOTON_MULTI = {
    "features": [
        # Closer but a "natural" feature -- must NOT be picked (not a place).
        {"geometry": {"coordinates": [-114.93, 44.2155]},
         "properties": {"name": "Mountain Village Restaurant", "osm_key": "amenity", "osm_value": "restaurant"}},
        # Town (~1km away).
        {"geometry": {"coordinates": [-114.9378523, 44.2161414]},
         "properties": {"name": "Stanley", "osm_key": "place", "osm_value": "city"}},
        # Town further out.
        {"geometry": {"coordinates": [-115.0588585, 44.2436215]},
         "properties": {"name": "Lake Town", "osm_key": "place", "osm_value": "village"}},
    ],
}


def _clear_h3_cache():
    from meshai.central_normalizer import _h3_cache
    _h3_cache.clear()


def test_nearest_town_returns_dict_for_known_coord(monkeypatch):
    _clear_h3_cache()
    from meshai import central_normalizer as cn
    monkeypatch.setattr(cn, "_photon_reverse_places",
                        lambda lat, lon: _PHOTON_STANLEY["features"])
    n = nearest_town(44.2160, -114.9311)
    assert n is not None
    assert n["name"] == "Stanley"
    assert n["distance_mi"] >= 0 and n["distance_mi"] <= 1
    assert n["bearing"] in {"N", "NE", "E", "SE", "S", "SW", "W", "NW"}


def test_nearest_town_filters_non_place_osm_values(monkeypatch):
    _clear_h3_cache()
    from meshai import central_normalizer as cn
    # Only the restaurant; no place tag at all.
    monkeypatch.setattr(cn, "_photon_reverse_places",
                        lambda lat, lon: [
                            {"geometry": {"coordinates": [-114.93, 44.2155]},
                             "properties": {"name": "Restaurant",
                                            "osm_key": "amenity", "osm_value": "restaurant"}},
                        ])
    assert nearest_town(44.2160, -114.9311) is None


def test_nearest_town_picks_closest_place(monkeypatch):
    _clear_h3_cache()
    from meshai import central_normalizer as cn
    monkeypatch.setattr(cn, "_photon_reverse_places",
                        lambda lat, lon: _PHOTON_MULTI["features"])
    n = nearest_town(44.2160, -114.9311)
    assert n is not None
    assert n["name"] == "Stanley"     # closer than Lake Town


def test_nearest_town_returns_none_beyond_max_distance(monkeypatch):
    _clear_h3_cache()
    from meshai import central_normalizer as cn
    monkeypatch.setattr(cn, "_photon_reverse_places",
                        lambda lat, lon: _PHOTON_STANLEY["features"])
    # Event 200 mi from Stanley; max_distance_mi=50 by default.
    far_lat = 44.2160 + 200 / 69.0
    n = nearest_town(far_lat, -114.9311)
    assert n is None


def test_nearest_town_returns_none_on_photon_failure(monkeypatch):
    _clear_h3_cache()
    from meshai import central_normalizer as cn
    monkeypatch.setattr(cn, "_photon_reverse_places", lambda lat, lon: [])
    assert nearest_town(44.2160, -114.9311) is None


def test_nearest_town_caches_via_h3(monkeypatch):
    _clear_h3_cache()
    from meshai import central_normalizer as cn
    calls = []
    def stub(lat, lon):
        calls.append((lat, lon))
        return _PHOTON_STANLEY["features"]
    monkeypatch.setattr(cn, "_photon_reverse_places", stub)
    # Two calls at the same coord → only one Photon hit.
    nearest_town(44.2160, -114.9311)
    nearest_town(44.2160, -114.9311)
    assert len(calls) == 1


def test_nearest_town_handles_none_inputs():
    _clear_h3_cache()
    assert nearest_town(None, -114.9311) is None
    assert nearest_town(44.2160, None) is None


# ---------- v0.5.8 town fallback chain in _parse_state_511_atis ------------

def test_town_uses_geocoder_city_when_present(monkeypatch):
    _clear_h3_cache()
    from meshai import central_normalizer as cn
    photon_calls = []
    monkeypatch.setattr(cn, "_photon_reverse_places",
                        lambda lat, lon: photon_calls.append("called") or [])
    env = {"data": {"adapter": "state_511_atis", "category": "work_zone.state_511_atis",
                    "data": {"roadway_name": "I-15", "direction": "South",
                             "description": "construction",
                             "_enriched": {"geocoder": {"city": "Idaho Falls"}},
                             "latitude": 43.4666, "longitude": -112.0340}}}
    n = normalize(env)
    assert n["town"] == "Idaho Falls"
    # When city is present, nearest_town should NOT be called.
    assert photon_calls == []


def test_town_falls_back_to_nearest_town_when_city_null(monkeypatch):
    _clear_h3_cache()
    from meshai import central_normalizer as cn
    monkeypatch.setattr(cn, "_photon_reverse_places",
                        lambda lat, lon: _PHOTON_STANLEY["features"])
    env = {"data": {"adapter": "state_511_atis", "category": "work_zone.state_511_atis",
                    "data": {"roadway_name": "ID 21", "direction": "Both",
                             "description": "construction",
                             "_enriched": {"geocoder": {"city": None, "name": "Some Trail"}},
                             "latitude": 44.2160, "longitude": -114.9311}}}
    n = normalize(env)
    assert n["town"] == "Stanley"


def test_town_is_none_when_city_and_photon_both_fail(monkeypatch):
    _clear_h3_cache()
    from meshai import central_normalizer as cn
    monkeypatch.setattr(cn, "_photon_reverse_places", lambda lat, lon: [])
    env = {"data": {"adapter": "state_511_atis", "category": "work_zone.state_511_atis",
                    "data": {"roadway_name": "X", "direction": "Both",
                             "description": "x",
                             "_enriched": {"geocoder": {"city": None, "name": "Old Road"}},
                             "latitude": 44.2160, "longitude": -114.9311}}}
    n = normalize(env)
    assert n["town"] is None
    assert n["distance_mi"] is None
    assert n["bearing"] is None


def test_geocoder_name_is_never_used_as_town_fallback(monkeypatch):
    """Per Matt's locked plan: geocoder.name is forbidden as a town fallback.
    Only geocoder.city (PRIMARY) or nearest_town() (SECONDARY) populate it."""
    _clear_h3_cache()
    from meshai import central_normalizer as cn
    monkeypatch.setattr(cn, "_photon_reverse_places", lambda lat, lon: [])
    env = {"data": {"adapter": "state_511_atis", "category": "work_zone.state_511_atis",
                    "data": {"roadway_name": "SH-3", "direction": "Both",
                             "description": "x",
                             "_enriched": {"geocoder": {"city": None,
                                                        "name": "Cache Nf Road 444"}},
                             "latitude": 42.2, "longitude": -113.7}}}
    n = normalize(env)
    # Must NOT pick up "Cache Nf Road 444" from geocoder.name.
    assert n["town"] is None
