"""Tests for 511 roads adapter Phase 2.8 — to_event() method."""

import time
from unittest.mock import MagicMock

import pytest

from meshai.env.roads511 import Roads511Adapter
from meshai.notifications.events import Event


# ============================================================
# FIXTURES
# ============================================================

@pytest.fixture
def mock_config():
    """Create a mock Roads511Config with real scalar fields."""
    config = MagicMock()
    config.api_key = ""
    config.base_url = "https://511.example.gov/api/v2"
    config.endpoints = ["/get/event"]
    config.bbox = []
    config.tick_seconds = 300
    return config


@pytest.fixture
def adapter(mock_config):
    """Create a Roads511Adapter with mocked config."""
    return Roads511Adapter(mock_config)


def make_511_event(
    event_id="511_evt123",
    event_type="Closure",
    roadway="US-93",
    description="Rockslide, road closed both directions",
    severity="priority",
    lat=42.6,
    lon=-114.46,
    is_closure=True,
    headline=None,
):
    """Helper to create a stored 511 event dict (mirrors _parse_event)."""
    now = time.time()
    if headline is None:
        headline = f"{roadway}: {description[:100]}"
    return {
        "source": "511",
        "event_id": event_id,
        "event_type": event_type,
        "headline": headline,
        "description": description,
        "severity": severity,
        "lat": lat,
        "lon": lon,
        "expires": now + 21600,
        "fetched_at": now,
        "properties": {
            "roadway": roadway,
            "is_closure": is_closure,
            "last_updated": "2026-05-27T12:00:00Z",
        },
    }


# ============================================================
# CATEGORY TESTS
# ============================================================

def test_to_event_category_is_road_closure(adapter):
    """511 events map to the road_closure category."""
    event = adapter.to_event(make_511_event())
    assert event is not None
    assert event.category == "road_closure"


def test_to_event_nonclosure_still_road_closure(adapter):
    """A construction event is still road_closure category (severity differs)."""
    event = adapter.to_event(
        make_511_event(event_type="Construction", severity="routine", is_closure=False)
    )
    assert event is not None
    assert event.category == "road_closure"


# ============================================================
# SEVERITY PASS-THROUGH TESTS
# ============================================================

def test_to_event_severity_passes_through(adapter):
    """Severity from the stored event passes through unchanged."""
    for sev in ["routine", "priority", "immediate"]:
        event = adapter.to_event(make_511_event(severity=sev))
        assert event is not None
        assert event.severity == sev


# ============================================================
# GROUP KEY / INHIBIT KEY TESTS
# ============================================================

def test_to_event_group_key_is_stable_event_id(adapter):
    """Group key is the stable 511_{event_id} key."""
    event = adapter.to_event(make_511_event(event_id="511_abc"))
    assert event is not None
    assert event.group_key == "511_abc"


def test_to_event_inhibit_keys_match_group_key(adapter):
    """The sole inhibit key equals the group key (Inhibitor does severity tiering)."""
    event = adapter.to_event(make_511_event())
    assert event is not None
    assert event.inhibit_keys == [event.group_key]


def test_two_polls_same_incident_share_group_key(adapter):
    """Two re-polls of the same incident (any severity) share the group key."""
    e1 = adapter.to_event(make_511_event(event_id="511_x", severity="routine"))
    e2 = adapter.to_event(make_511_event(event_id="511_x", severity="priority"))
    assert e1 is not None and e2 is not None
    assert e1.group_key == e2.group_key


def test_distinct_incidents_distinct_group_keys(adapter):
    """Distinct incidents get distinct group keys."""
    e1 = adapter.to_event(make_511_event(event_id="511_a"))
    e2 = adapter.to_event(make_511_event(event_id="511_b"))
    assert e1.group_key != e2.group_key


# ============================================================
# CONTENT / FIELD POPULATION TESTS
# ============================================================

def test_to_event_populates_core_fields(adapter):
    """Core Event fields are populated from the stored dict."""
    evt = make_511_event(lat=42.61, lon=-114.21)
    event = adapter.to_event(evt)
    assert event is not None
    assert event.source == "511"
    assert event.lat == 42.61
    assert event.lon == -114.21
    assert event.expires == evt["expires"]
    assert event.timestamp == evt["fetched_at"]
    assert event.id  # auto-computed


def test_to_event_summary_notes_closure(adapter):
    """Summary notes a road closure."""
    event = adapter.to_event(make_511_event(is_closure=True))
    assert event is not None
    assert "road closed" in event.summary


def test_to_event_title_falls_back_when_headline_empty(adapter):
    """Empty headline falls back to event_type."""
    event = adapter.to_event(make_511_event(headline="", event_type="Incident"))
    assert event is not None
    assert event.title == "Incident"


# ============================================================
# DEFENSIVE TESTS
# ============================================================

def test_to_event_missing_coords_returns_none(adapter):
    """Missing coordinates returns None."""
    evt = make_511_event()
    evt["lat"] = None
    assert adapter.to_event(evt) is None


def test_to_event_missing_event_id_returns_none(adapter):
    """Missing event_id returns None (no stable group key)."""
    evt = make_511_event()
    evt["event_id"] = None
    assert adapter.to_event(evt) is None


def test_to_event_missing_properties_returns_event(adapter):
    """No properties dict still yields an event (props only enrich the summary)."""
    evt = {
        "source": "511",
        "event_id": "511_z",
        "event_type": "Closure",
        "headline": "US-30 closed",
        "severity": "priority",
        "lat": 42.6,
        "lon": -114.4,
        "fetched_at": time.time(),
    }
    event = adapter.to_event(evt)
    assert event is not None
    assert event.category == "road_closure"
    assert event.group_key == "511_z"


def test_to_event_does_not_raise_on_corrupted_dict(adapter):
    """Corrupted dict returns None without raising."""
    assert adapter.to_event({"garbage": True}) is None


# ============================================================
# EventType / EventSubType / Cause -> sub_type MAPPING TESTS
#
# Drives real upstream-shaped ITD 511 v2 payloads through the actual
# _parse_event() -> to_event() pipeline (not the make_511_event() stored-
# dict helper) so the EventSubType/Cause capture at parse time and the
# sub_type precedence in to_event() are both exercised end to end.
# ============================================================

def _itd_raw(
    event_id="RW-1001",
    event_type="roadwork",
    event_sub_type="roadConstruction",
    cause="Construction",
    description="Paving operations, expect delays",
    roadway="I-84",
    is_full_closure=False,
    lat=43.5,
    lon=-116.2,
):
    """Realistic raw ITD 511 v2 API item shape (PascalCase fields)."""
    return {
        "EventId": event_id,
        "EventType": event_type,
        "EventSubType": event_sub_type,
        "Cause": cause,
        "RoadwayName": roadway,
        "Description": description,
        "Latitude": lat,
        "Longitude": lon,
        "IsFullClosure": is_full_closure,
        "LastUpdated": "2026-08-01T00:00:00Z",
    }


def _through_pipeline(adapter, raw_item):
    """Parse a raw ITD item then translate to a pipeline Event, mirroring
    the real adapter flow (_fetch -> _parse_event -> get_events -> to_event)."""
    stored = adapter._parse_event(raw_item, time.time())
    assert stored is not None
    return adapter.to_event(stored)


def test_mapping_roadwork_not_full_closure_is_road_works(adapter):
    """Routine roadwork, not a full closure -> sub_type 'road_works' (suppressed)."""
    raw = _itd_raw(event_type="roadwork", is_full_closure=False)
    event = _through_pipeline(adapter, raw)
    assert event is not None
    assert event.data["sub_type"] == "road_works"


def test_mapping_roadwork_with_full_closure_is_road_closed(adapter):
    """Construction-caused FULL closure is still genuine news -> 'road_closed',
    and must NOT be suppressed like routine roadwork."""
    raw = _itd_raw(
        event_type="roadwork",
        event_sub_type="bridgeConstruction",
        description="Bridge replacement in progress",
        is_full_closure=True,
    )
    event = _through_pipeline(adapter, raw)
    assert event is not None
    assert event.data["sub_type"] == "road_closed"


def test_mapping_accidents_and_incidents_is_incident(adapter):
    """EventType accidentsAndIncidents -> sub_type 'incident'."""
    raw = _itd_raw(
        event_id="INC-2001",
        event_type="accidentsAndIncidents",
        event_sub_type="crash",
        cause="Collision",
        description="Two vehicle collision blocking right lane",
        is_full_closure=False,
    )
    event = _through_pipeline(adapter, raw)
    assert event is not None
    assert event.data["sub_type"] == "incident"


def test_mapping_closures_is_road_closed(adapter):
    """EventType closures -> sub_type 'road_closed'."""
    raw = _itd_raw(
        event_id="CL-3001",
        event_type="closures",
        event_sub_type="bridgeClosure",
        cause="Maintenance",
        description="Bridge out of service",
        is_full_closure=False,
    )
    event = _through_pipeline(adapter, raw)
    assert event is not None
    assert event.data["sub_type"] == "road_closed"


def test_mapping_special_events_is_incident(adapter):
    """EventType specialEvents -> sub_type 'incident' (not suppressed)."""
    raw = _itd_raw(
        event_id="SE-4001",
        event_type="specialEvents",
        event_sub_type="parade",
        cause=None,
        description="Downtown parade route",
        is_full_closure=False,
    )
    event = _through_pipeline(adapter, raw)
    assert event is not None
    assert event.data["sub_type"] == "incident"


def test_mapping_unknown_or_missing_event_type_is_incident(adapter):
    """Missing/unrecognized EventType fails open to 'incident' (not suppressed)."""
    raw = {
        "EventId": "UNK-5001",
        "RoadwayName": "SH-21",
        "Description": "Unusual event",
        "Latitude": 43.5,
        "Longitude": -115.5,
    }
    event = _through_pipeline(adapter, raw)
    assert event is not None
    assert event.data["sub_type"] == "incident"


def test_mapping_event_subtype_construction_overrides_non_roadwork_eventtype(adapter):
    """EventSubType containing 'construction' forces 'road_works' even when
    EventType itself is not literally 'roadwork'."""
    raw = _itd_raw(
        event_id="SE-6001",
        event_type="specialEvents",
        event_sub_type="bridgeConstruction",
        cause="Construction",
        description="Bridge deck construction nearby",
        is_full_closure=False,
    )
    event = _through_pipeline(adapter, raw)
    assert event is not None
    assert event.data["sub_type"] == "road_works"


def test_mapping_cause_is_carried_through_not_none(adapter):
    """Cause from the upstream payload reaches canonical_data, not hardcoded None."""
    raw = _itd_raw(cause="Weather")
    event = _through_pipeline(adapter, raw)
    assert event is not None
    assert event.data["cause"] == "Weather"
    assert event.data["cause"] is not None


def test_mapping_missing_cause_is_none(adapter):
    """No Cause field upstream -> canonical cause is None, not a crash."""
    raw = _itd_raw(cause=None)
    del raw["Cause"]
    event = _through_pipeline(adapter, raw)
    assert event is not None
    assert event.data["cause"] is None


# ============================================================
# END-TO-END GATING TEST — proves a real roadwork event, run through the
# actual adapter pipeline, is suppressed by gating/incident.py's work-zone
# rule, while a real crash event from the same pipeline still broadcasts.
# ============================================================

def test_roadwork_event_suppressed_by_incident_gate_end_to_end(adapter):
    """A real ITD roadwork payload, parsed and translated by the adapter,
    must be suppressed at the gating layer (not just mapped to the right
    sub_type in isolation)."""
    from meshai.notifications.gating.incident import decide

    raw = _itd_raw(event_id="RW-7001", event_type="roadwork", is_full_closure=False)
    event = _through_pipeline(adapter, raw)
    assert event is not None
    assert event.data["sub_type"] == "road_works"

    result = decide(dict(event.data), source="511", now=time.time())
    assert result.broadcast is False
    assert result.lifecycle == "suppress"


def test_crash_event_still_broadcasts_through_incident_gate_end_to_end(adapter):
    """A real ITD crash payload, parsed and translated by the adapter,
    still broadcasts (gating layer is unaffected for non-work-zone sub_types)."""
    from meshai.notifications.gating.incident import decide

    raw = _itd_raw(
        event_id="INC-7002",
        event_type="accidentsAndIncidents",
        event_sub_type="crash",
        description="Vehicle collision, right lane blocked",
        is_full_closure=False,
    )
    event = _through_pipeline(adapter, raw)
    assert event is not None
    assert event.data["sub_type"] == "incident"


# ============================================================
# FULL-CLOSURE vs. PARTIAL-RESTRICTION LANGUAGE TESTS
#
# The loose `is_closure` property (also used for severity/summary) matches
# any "closed" substring, including partial-restriction wording like "All
# Shoulders Closed" that ITD explicitly does not want broadcast. The
# sub_type mapping's full-closure branch must use a stricter,
# mapping-only determination instead.
# ============================================================

def test_all_shoulders_closed_roadwork_is_road_works(adapter):
    """'All Shoulders Closed' is a partial restriction, not a full closure
    -> stays 'road_works' (suppressed), despite containing 'closed'."""
    raw = _itd_raw(
        event_id="RW-8001",
        event_type="roadwork",
        description="All Shoulders Closed",
        is_full_closure=False,
    )
    event = _through_pipeline(adapter, raw)
    assert event is not None
    assert event.data["sub_type"] == "road_works"


@pytest.mark.parametrize(
    "description",
    ["One lane closed", "Right lane closed", "Left lane closed"],
)
def test_single_lane_closed_roadwork_is_road_works(adapter, description):
    """Single-lane restriction wording -> stays 'road_works' (suppressed)."""
    raw = _itd_raw(
        event_id="RW-8002",
        event_type="roadwork",
        description=description,
        is_full_closure=False,
    )
    event = _through_pipeline(adapter, raw)
    assert event is not None
    assert event.data["sub_type"] == "road_works"


def test_ramp_closed_roadwork_is_road_works(adapter):
    """'Ramp closed' is a partial restriction -> stays 'road_works' (suppressed)."""
    raw = _itd_raw(
        event_id="RW-8003",
        event_type="roadwork",
        description="Ramp closed for repaving",
        is_full_closure=False,
    )
    event = _through_pipeline(adapter, raw)
    assert event is not None
    assert event.data["sub_type"] == "road_works"


def test_all_lanes_closed_roadwork_is_road_closed(adapter):
    """'All lanes closed' is a genuine full closure -> 'road_closed', and
    must STILL BROADCAST (this is the safety case: a real closure phrased
    as roadwork must not be suppressed by the work-zone gate)."""
    from meshai.notifications.gating.incident import decide

    raw = _itd_raw(
        event_id="RW-8004",
        event_type="roadwork",
        description="All lanes closed for bridge demolition",
        is_full_closure=False,
    )
    event = _through_pipeline(adapter, raw)
    assert event is not None
    assert event.data["sub_type"] == "road_closed"

    result = decide(dict(event.data), source="511", now=time.time())
    assert result.broadcast is True
    assert result.lifecycle != "suppress"


def test_is_full_closure_flag_true_no_closure_words_is_road_closed(adapter):
    """IsFullClosure=true + roadwork, with a description containing no
    closure wording at all -> still 'road_closed' via the raw flag."""
    raw = _itd_raw(
        event_id="RW-8005",
        event_type="roadwork",
        description="Bridge deck replacement in progress",
        is_full_closure=True,
    )
    event = _through_pipeline(adapter, raw)
    assert event is not None
    assert event.data["sub_type"] == "road_closed"


def test_road_closed_due_to_rock_slide_incident_is_road_closed(adapter):
    """Explicit full-closure wording on an accidentsAndIncidents event
    still wins the mapping -> 'road_closed'."""
    raw = _itd_raw(
        event_id="INC-8006",
        event_type="accidentsAndIncidents",
        event_sub_type="hazard",
        cause="Rock slide",
        description="Road closed due to rock slide",
        is_full_closure=False,
    )
    event = _through_pipeline(adapter, raw)
    assert event is not None
    assert event.data["sub_type"] == "road_closed"


def test_lane_restrictions_roadwork_is_road_works(adapter):
    """'Lane restrictions' wording -> stays 'road_works' (suppressed)."""
    raw = _itd_raw(
        event_id="RW-8007",
        event_type="roadwork",
        description="Lane restrictions in effect through Friday",
        is_full_closure=False,
    )
    event = _through_pipeline(adapter, raw)
    assert event is not None
    assert event.data["sub_type"] == "road_works"


def test_alternating_traffic_roadwork_is_road_works(adapter):
    """'Alternating' one-lane traffic control -> stays 'road_works' (suppressed)."""
    raw = _itd_raw(
        event_id="RW-8008",
        event_type="roadwork",
        description="Alternating traffic controlled by flaggers",
        is_full_closure=False,
    )
    event = _through_pipeline(adapter, raw)
    assert event is not None
    assert event.data["sub_type"] == "road_works"
