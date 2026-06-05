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


# ============================================================================
# v0.5.8-wzdx federal parser tests
# ============================================================================

# --- representative envelopes (flat shape, as Central actually publishes) ---

_WZDX_ID_FULL = {
    "data": {
        "adapter":  "wzdx",
        "category": "work_zone.wzdx",
        "time":     "2026-06-01T13:00:00Z",
        "severity": 3,
        "geo":      {"centroid": [-112.408309608311, 43.0208066348276],
                     "primary_region": "US-ID", "regions": ["US-ID"]},
        "data": {
            "road_names":      ["Exit 80 On Ramp"],
            "direction":       "southbound",
            "description":     " Road construction on Exit 80 On Ramp Southbound near MM (80)."
                               " All lanes closed. 6/1/2026 7:00 AM to 6/10/2026 6:00 PM Mon, Tue ...",
            "vehicle_impact":  "all-lanes-closed",
            "event_status":    None,
            "start_date":      "2026-06-01T13:00:00Z",
            "end_date":        "2026-06-11T00:00:00Z",
            "data_source_id":  "ERS",
            "feed_name":       "iddot",
            "feed_state_code": "ID",
            "latitude":        43.0208066348276,
            "longitude":       -112.408309608311,
            "_enriched": {"geocoder": {"city": None, "name": "Ross Fork Creek",
                                       "county": "Bannock", "state": "Idaho"}},
        },
    },
}

_WZDX_WA = {
    "data": {
        "adapter":  "wzdx",
        "category": "work_zone.wzdx",
        "time":     "2026-06-01T00:00:00+00:00",
        "severity": 1,
        "geo":      {"centroid": [-117.33633, 46.433365], "primary_region": "US-WA"},
        "data": {
            "road_names":      ["012"],
            "direction":       "westbound",
            "description":     "Contract - XE3608 SR 12",
            "vehicle_impact":  "unknown",
            "event_status":    "pending",
            "start_date":      "2026-06-01T00:00:00+00:00",
            "end_date":        "2026-06-05T00:00:00+00:00",
            "data_source_id":  "WSDOT-WZDB",
            "feed_name":       "wsdot",
            "feed_state_code": "WA",
            "latitude":        46.433365,
            "longitude":       -117.33633,
            "_enriched": {"geocoder": {"city": None, "name": "US Highway 12",
                                       "county": "Garfield", "state": "Washington"}},
        },
    },
}

_WZDX_MCCALL = {
    "data": {
        "adapter": "wzdx", "category": "work_zone.wzdx",
        "time": "2026-05-28T23:00:00Z", "severity": 1,
        "geo": {"centroid": [-116.09759, 44.9065083834611], "primary_region": "US-ID"},
        "data": {
            "road_names":      ["SH-55"],
            "direction":       "unknown",
            "description":     " Emergency repairs on SH-55 Both Directions near Washington St."
                               " 5/28/2026 5:00 PM to 5/29/2026 8:00 AM Thu, Fri: ...",
            "vehicle_impact":  "all-lanes-open",
            "start_date":      "2026-05-28T23:00:00Z",
            "end_date":        "2026-05-29T14:00:00Z",
            "feed_state_code": "ID",
            "latitude":        44.9065083834611,
            "longitude":       -116.09759,
            "_enriched": {"geocoder": {"city": "McCall", "county": "Valley", "state": "ID"}},
        },
    },
}


def _normalize_wzdx(env):
    n = normalize(env)
    assert n is not None
    assert n["source"] == "wzdx"
    return n


# --- (a) Idaho wzdx full-field parse ---------------------------------------

def test_wzdx_idaho_full_fields_normalized(monkeypatch):
    _clear_h3_cache()
    from meshai import central_normalizer as cn
    # Mock Photon for the SECONDARY town path (city is null in this envelope).
    monkeypatch.setattr(cn, "_photon_reverse_places",
                        lambda lat, lon: [
                            {"geometry": {"coordinates": [-112.4373, 43.0299]},
                             "properties": {"name": "Fort Hall",
                                            "osm_key": "place", "osm_value": "village"}},
                        ])
    n = _normalize_wzdx(_WZDX_ID_FULL)
    assert n["road"] is None            # Exit-ramp pattern → uninformative-road drop
    assert n["direction"] == "southbound"
    # sub_type combines impact-phrase (suppressed under full-closure) + work_type
    # (None here — types_of_work absent). With full-closure, sub_type stays None
    # and the renderer prepends "all lanes closed".
    assert n["sub_type"] is None
    assert n["impact"] == "full_closure"
    assert n["mile_start"] == 80 and n["mile_end"] is None
    assert n["town"] == "Fort Hall"     # via Photon nearest_town
    assert isinstance(n["ends_at"], datetime)
    assert n["ends_at"].year == 2026 and n["ends_at"].month == 6 and n["ends_at"].day == 11


def test_wzdx_wa_road_passes_through_verbatim(monkeypatch):
    _clear_h3_cache()
    from meshai import central_normalizer as cn
    monkeypatch.setattr(cn, "_photon_reverse_places", lambda lat, lon: [])
    n = _normalize_wzdx(_WZDX_WA)
    # Per spec: "honor upstream verbatim, no expansion" -- raw '012' passes through.
    assert n["road"] == "012"
    assert n["direction"] == "westbound"
    # vehicle_impact='unknown' → impact_phrase=None; sub_type stays None.
    assert n["sub_type"] is None
    assert n["impact"] == "partial"
    # No MM in WA descriptions; mile_start stays None.
    assert n["mile_start"] is None
    assert isinstance(n["ends_at"], datetime)
    assert n["town"] is None             # city null + Photon returned no places


# --- (c) vehicle_impact mapping for each main value ------------------------

@pytest.mark.parametrize("vi_raw,expected_sub_type,expected_impact", [
    ("all-lanes-closed",    None,                  "full_closure"),
    ("some-lanes-closed",   "lanes reduced",       "partial"),
    ("alternating-one-way", "one-way alternating", "partial"),
    ("unknown",             None,                  "partial"),
    ("all-lanes-open",      None,                  "partial"),
    ("totally-made-up",     None,                  "partial"),
])
def test_wzdx_vehicle_impact_mapping(vi_raw, expected_sub_type, expected_impact, monkeypatch):
    _clear_h3_cache()
    from meshai import central_normalizer as cn
    monkeypatch.setattr(cn, "_photon_reverse_places", lambda lat, lon: [])
    env = {"data": {"adapter": "wzdx", "category": "work_zone.wzdx", "time": "2026-06-01T00:00:00Z",
                    "geo": {"centroid": [-116.0, 44.0]},
                    "data": {"road_names": ["SH-1"], "direction": "northbound",
                             "description": "X", "vehicle_impact": vi_raw,
                             "end_date": "2026-06-05T17:00:00Z",
                             "latitude": 44.0, "longitude": -116.0,
                             "_enriched": {"geocoder": {"city": "Boise"}}}}}
    n = normalize(env)
    assert n["sub_type"] == expected_sub_type
    assert n["impact"] == expected_impact


# --- (d) structured end_date parses to friendly format --------------------

def test_wzdx_end_date_iso_parsed_to_datetime(monkeypatch):
    _clear_h3_cache()
    from meshai import central_normalizer as cn
    monkeypatch.setattr(cn, "_photon_reverse_places", lambda lat, lon: [])
    env = {"data": {"adapter": "wzdx", "category": "work_zone.wzdx",
                    "geo": {"centroid": [-116.0, 44.0]},
                    "data": {"road_names": ["SH-1"], "direction": "northbound",
                             "description": "x", "vehicle_impact": "unknown",
                             "end_date": "2026-06-15T18:30:00+00:00",
                             "latitude": 44.0, "longitude": -116.0,
                             "_enriched": {"geocoder": {"city": "Boise"}}}}}
    n = normalize(env)
    assert isinstance(n["ends_at"], datetime)
    assert n["ends_at"].month == 6 and n["ends_at"].day == 15
    assert n["ends_at"].hour in (18, 11, 12)   # depending on local-tz coercion


# --- (e) MM regex extraction on ID description ----------------------------

def test_wzdx_mile_post_regex_from_description(monkeypatch):
    _clear_h3_cache()
    from meshai import central_normalizer as cn
    monkeypatch.setattr(cn, "_photon_reverse_places", lambda lat, lon: [])
    env = {"data": {"adapter": "wzdx", "category": "work_zone.wzdx",
                    "geo": {"centroid": [-116.0, 44.0]},
                    "data": {"road_names": ["I-15"], "direction": "southbound",
                             "description": "Bridge work on I-15 SB from MM (89) to MM (93). 6/1/2026 7:00 AM to 6/3/2026 5:00 PM",
                             "vehicle_impact": "some-lanes-closed",
                             "end_date": "2026-06-03T22:00:00Z",
                             "latitude": 44.0, "longitude": -116.0,
                             "_enriched": {"geocoder": {"city": "Blackfoot"}}}}}
    n = normalize(env)
    assert n["mile_start"] == 89
    assert n["mile_end"] == 93


# --- (f) WA event without MM yields mile_start=None -----------------------

def test_wzdx_wa_no_mm_in_description(monkeypatch):
    _clear_h3_cache()
    from meshai import central_normalizer as cn
    monkeypatch.setattr(cn, "_photon_reverse_places", lambda lat, lon: [])
    n = _normalize_wzdx(_WZDX_WA)
    assert n["mile_start"] is None
    assert n["mile_end"] is None


# --- (g) town fallback chain ----------------------------------------------

def test_wzdx_town_uses_geocoder_city_when_present(monkeypatch):
    _clear_h3_cache()
    from meshai import central_normalizer as cn
    calls = []
    monkeypatch.setattr(cn, "_photon_reverse_places",
                        lambda lat, lon: calls.append("called") or [])
    n = _normalize_wzdx(_WZDX_MCCALL)
    assert n["town"] == "McCall"
    assert calls == []   # city present → Photon NOT called


def test_wzdx_town_falls_back_to_nearest_town_when_city_null(monkeypatch):
    _clear_h3_cache()
    from meshai import central_normalizer as cn
    monkeypatch.setattr(cn, "_photon_reverse_places",
                        lambda lat, lon: [
                            {"geometry": {"coordinates": [-117.293, 46.475]},
                             "properties": {"name": "Pomeroy",
                                            "osm_key": "place", "osm_value": "city"}},
                        ])
    n = _normalize_wzdx(_WZDX_WA)
    assert n["town"] == "Pomeroy"


# --- adapter dispatch routes wzdx → _parse_wzdx_federal -------------------

def test_wzdx_adapter_routes_to_wzdx_parser(monkeypatch):
    _clear_h3_cache()
    from meshai import central_normalizer as cn
    monkeypatch.setattr(cn, "_photon_reverse_places", lambda lat, lon: [])
    n = normalize(_WZDX_WA)
    assert n is not None
    assert n["source"] == "wzdx"


# --- work_type from types_of_work or event_type ---------------------------

def test_wzdx_sub_type_from_types_of_work(monkeypatch):
    _clear_h3_cache()
    from meshai import central_normalizer as cn
    monkeypatch.setattr(cn, "_photon_reverse_places", lambda lat, lon: [])
    env = {"data": {"adapter": "wzdx", "category": "work_zone.wzdx",
                    "geo": {"centroid": [-116.0, 44.0]},
                    "data": {"road_names": ["SH-1"], "direction": "both",
                             "description": "x",
                             "types_of_work": [{"type_name": "paving"}],
                             "vehicle_impact": "some-lanes-closed",
                             "end_date": "2026-06-05T17:00:00Z",
                             "latitude": 44.0, "longitude": -116.0,
                             "_enriched": {"geocoder": {"city": "Boise"}}}}}
    n = normalize(env)
    # Folded form: impact_phrase + work_type (paving)
    assert n["sub_type"] == "lanes reduced, paving"


def test_wzdx_sub_type_unknown_vocab_is_lowercased_with_spaces(monkeypatch):
    _clear_h3_cache()
    from meshai import central_normalizer as cn
    monkeypatch.setattr(cn, "_photon_reverse_places", lambda lat, lon: [])
    env = {"data": {"adapter": "wzdx", "category": "work_zone.wzdx",
                    "geo": {"centroid": [-116.0, 44.0]},
                    "data": {"road_names": ["SH-1"], "direction": "northbound",
                             "description": "x",
                             "types_of_work": [{"type_name": "Some-Custom-Work"}],
                             "vehicle_impact": "all-lanes-open",
                             "end_date": "2026-06-05T17:00:00Z",
                             "latitude": 44.0, "longitude": -116.0,
                             "_enriched": {"geocoder": {"city": "Boise"}}}}}
    n = normalize(env)
    assert n["sub_type"] == "some custom work"   # lowercased + hyphens→spaces
