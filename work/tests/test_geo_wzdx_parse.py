"""Tests for the live geo/WZDx-parsing code that used to live in the
Central-envelope adapter-normalizer module (deleted, name and all, in this
PR).

That module (its `normalize()` Central-envelope dispatcher,
`_parse_state_511_atis`, `_parse_itd_511_work_zone`,
`should_skip_state_511_atis_id`, `normalize_road_name`, and their fixtures/
tests) was deleted in chore/ripout-2e-geo-normalizer: it had zero live
production callers (Central's NATS consumer that dispatched envelopes to it
was deleted in an earlier ripout pass), and unlike env.fire_render
.handle_wfigs it wasn't kept as a parity-tested legacy contract for anything
else, so it -- and the tests that existed solely to exercise IT -- went with
it. See that PR's report for the full per-symbol accounting.

What remains here: the WZDx-federal-parser tests (now exercising
`meshai.env.wzdx_parse._parse_wzdx_federal` directly instead of routing
through the deleted `normalize()` dispatcher -- same assertions, same
fixtures, just called one layer closer to the code under test) and the
nearest_town()/Photon/H3-cache tests (now exercising `meshai.geo`, where
that code lives now).
"""

from datetime import datetime

import pytest

from meshai.env.wzdx_parse import _parse_wzdx_federal
from meshai.geo import nearest_town


def _parse_wzdx_envelope(env: dict) -> dict:
    """Unwrap a Central-envelope-shaped wzdx fixture and call the live
    parser directly (`normalize()`'s old wzdx dispatch was just this)."""
    inner = env["data"]
    return _parse_wzdx_federal(inner["data"], inner.get("geo") or {})


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
    from meshai.geo import _h3_cache
    _h3_cache.clear()


def test_nearest_town_returns_dict_for_known_coord(monkeypatch):
    _clear_h3_cache()
    from meshai import geo
    monkeypatch.setattr(geo, "_photon_reverse_places",
                        lambda lat, lon: _PHOTON_STANLEY["features"])
    n = nearest_town(44.2160, -114.9311)
    assert n is not None
    assert n["name"] == "Stanley"
    assert n["distance_mi"] >= 0 and n["distance_mi"] <= 1
    assert n["bearing"] in {"N", "NE", "E", "SE", "S", "SW", "W", "NW"}


def test_nearest_town_filters_non_place_osm_values(monkeypatch):
    _clear_h3_cache()
    from meshai import geo
    # Only the restaurant; no place tag at all.
    monkeypatch.setattr(geo, "_photon_reverse_places",
                        lambda lat, lon: [
                            {"geometry": {"coordinates": [-114.93, 44.2155]},
                             "properties": {"name": "Restaurant",
                                            "osm_key": "amenity", "osm_value": "restaurant"}},
                        ])
    assert nearest_town(44.2160, -114.9311) is None


def test_nearest_town_picks_closest_place(monkeypatch):
    _clear_h3_cache()
    from meshai import geo
    monkeypatch.setattr(geo, "_photon_reverse_places",
                        lambda lat, lon: _PHOTON_MULTI["features"])
    n = nearest_town(44.2160, -114.9311)
    assert n is not None
    assert n["name"] == "Stanley"     # closer than Lake Town


def test_nearest_town_returns_none_beyond_max_distance(monkeypatch):
    _clear_h3_cache()
    from meshai import geo
    monkeypatch.setattr(geo, "_photon_reverse_places",
                        lambda lat, lon: _PHOTON_STANLEY["features"])
    # Event 200 mi from Stanley; max_distance_mi=50 by default.
    far_lat = 44.2160 + 200 / 69.0
    n = nearest_town(far_lat, -114.9311)
    assert n is None


def test_nearest_town_returns_none_on_photon_failure(monkeypatch):
    _clear_h3_cache()
    from meshai import geo
    monkeypatch.setattr(geo, "_photon_reverse_places", lambda lat, lon: [])
    assert nearest_town(44.2160, -114.9311) is None


def test_nearest_town_caches_via_h3(monkeypatch):
    _clear_h3_cache()
    from meshai import geo
    calls = []
    def stub(lat, lon):
        calls.append((lat, lon))
        return _PHOTON_STANLEY["features"]
    monkeypatch.setattr(geo, "_photon_reverse_places", stub)
    # Two calls at the same coord → only one Photon hit.
    nearest_town(44.2160, -114.9311)
    nearest_town(44.2160, -114.9311)
    assert len(calls) == 1


def test_nearest_town_handles_none_inputs():
    _clear_h3_cache()
    assert nearest_town(None, -114.9311) is None
    assert nearest_town(44.2160, None) is None


# ============================================================================
# v0.5.8-wzdx federal parser tests
# ============================================================================

# --- representative envelopes (flat shape, as Central actually published) ---

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
    n = _parse_wzdx_envelope(env)
    assert n is not None
    assert n["source"] == "wzdx"
    return n


# --- (a) Idaho wzdx full-field parse ---------------------------------------

def test_wzdx_idaho_full_fields_normalized(monkeypatch):
    _clear_h3_cache()
    from meshai import geo
    # Mock Photon for the SECONDARY town path (city is null in this envelope).
    monkeypatch.setattr(geo, "_photon_reverse_places",
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
    from meshai import geo
    monkeypatch.setattr(geo, "_photon_reverse_places", lambda lat, lon: [])
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
    from meshai import geo
    monkeypatch.setattr(geo, "_photon_reverse_places", lambda lat, lon: [])
    env = {"data": {"adapter": "wzdx", "category": "work_zone.wzdx", "time": "2026-06-01T00:00:00Z",
                    "geo": {"centroid": [-116.0, 44.0]},
                    "data": {"road_names": ["SH-1"], "direction": "northbound",
                             "description": "X", "vehicle_impact": vi_raw,
                             "end_date": "2026-06-05T17:00:00Z",
                             "latitude": 44.0, "longitude": -116.0,
                             "_enriched": {"geocoder": {"city": "Boise"}}}}}
    n = _parse_wzdx_envelope(env)
    assert n["sub_type"] == expected_sub_type
    assert n["impact"] == expected_impact


# --- (d) structured end_date parses to friendly format --------------------

def test_wzdx_end_date_iso_parsed_to_datetime(monkeypatch):
    _clear_h3_cache()
    from meshai import geo
    monkeypatch.setattr(geo, "_photon_reverse_places", lambda lat, lon: [])
    env = {"data": {"adapter": "wzdx", "category": "work_zone.wzdx",
                    "geo": {"centroid": [-116.0, 44.0]},
                    "data": {"road_names": ["SH-1"], "direction": "northbound",
                             "description": "x", "vehicle_impact": "unknown",
                             "end_date": "2026-06-15T18:30:00+00:00",
                             "latitude": 44.0, "longitude": -116.0,
                             "_enriched": {"geocoder": {"city": "Boise"}}}}}
    n = _parse_wzdx_envelope(env)
    assert isinstance(n["ends_at"], datetime)
    assert n["ends_at"].month == 6 and n["ends_at"].day == 15
    assert n["ends_at"].hour in (18, 11, 12)   # depending on local-tz coercion


# --- (e) MM regex extraction on ID description ----------------------------

def test_wzdx_mile_post_regex_from_description(monkeypatch):
    _clear_h3_cache()
    from meshai import geo
    monkeypatch.setattr(geo, "_photon_reverse_places", lambda lat, lon: [])
    env = {"data": {"adapter": "wzdx", "category": "work_zone.wzdx",
                    "geo": {"centroid": [-116.0, 44.0]},
                    "data": {"road_names": ["I-15"], "direction": "southbound",
                             "description": "Bridge work on I-15 SB from MM (89) to MM (93). 6/1/2026 7:00 AM to 6/3/2026 5:00 PM",
                             "vehicle_impact": "some-lanes-closed",
                             "end_date": "2026-06-03T22:00:00Z",
                             "latitude": 44.0, "longitude": -116.0,
                             "_enriched": {"geocoder": {"city": "Blackfoot"}}}}}
    n = _parse_wzdx_envelope(env)
    assert n["mile_start"] == 89
    assert n["mile_end"] == 93


# --- (f) WA event without MM yields mile_start=None -----------------------

def test_wzdx_wa_no_mm_in_description(monkeypatch):
    _clear_h3_cache()
    from meshai import geo
    monkeypatch.setattr(geo, "_photon_reverse_places", lambda lat, lon: [])
    n = _normalize_wzdx(_WZDX_WA)
    assert n["mile_start"] is None
    assert n["mile_end"] is None


# --- (g) town fallback chain ----------------------------------------------

def test_wzdx_town_uses_geocoder_city_when_present(monkeypatch):
    _clear_h3_cache()
    from meshai import geo
    calls = []
    monkeypatch.setattr(geo, "_photon_reverse_places",
                        lambda lat, lon: calls.append("called") or [])
    n = _normalize_wzdx(_WZDX_MCCALL)
    assert n["town"] == "McCall"
    assert calls == []   # city present → Photon NOT called


def test_wzdx_town_falls_back_to_nearest_town_when_city_null(monkeypatch):
    _clear_h3_cache()
    from meshai import geo
    monkeypatch.setattr(geo, "_photon_reverse_places",
                        lambda lat, lon: [
                            {"geometry": {"coordinates": [-117.293, 46.475]},
                             "properties": {"name": "Pomeroy",
                                            "osm_key": "place", "osm_value": "city"}},
                        ])
    n = _normalize_wzdx(_WZDX_WA)
    assert n["town"] == "Pomeroy"


# --- work_type from types_of_work or event_type ---------------------------

def test_wzdx_sub_type_from_types_of_work(monkeypatch):
    _clear_h3_cache()
    from meshai import geo
    monkeypatch.setattr(geo, "_photon_reverse_places", lambda lat, lon: [])
    env = {"data": {"adapter": "wzdx", "category": "work_zone.wzdx",
                    "geo": {"centroid": [-116.0, 44.0]},
                    "data": {"road_names": ["SH-1"], "direction": "both",
                             "description": "x",
                             "types_of_work": [{"type_name": "paving"}],
                             "vehicle_impact": "some-lanes-closed",
                             "end_date": "2026-06-05T17:00:00Z",
                             "latitude": 44.0, "longitude": -116.0,
                             "_enriched": {"geocoder": {"city": "Boise"}}}}}
    n = _parse_wzdx_envelope(env)
    # Folded form: impact_phrase + work_type (paving)
    assert n["sub_type"] == "lanes reduced, paving"


def test_wzdx_sub_type_unknown_vocab_is_lowercased_with_spaces(monkeypatch):
    _clear_h3_cache()
    from meshai import geo
    monkeypatch.setattr(geo, "_photon_reverse_places", lambda lat, lon: [])
    env = {"data": {"adapter": "wzdx", "category": "work_zone.wzdx",
                    "geo": {"centroid": [-116.0, 44.0]},
                    "data": {"road_names": ["SH-1"], "direction": "northbound",
                             "description": "x",
                             "types_of_work": [{"type_name": "Some-Custom-Work"}],
                             "vehicle_impact": "all-lanes-open",
                             "end_date": "2026-06-05T17:00:00Z",
                             "latitude": 44.0, "longitude": -116.0,
                             "_enriched": {"geocoder": {"city": "Boise"}}}}}
    n = _parse_wzdx_envelope(env)
    assert n["sub_type"] == "some custom work"   # lowercased + hyphens→spaces
