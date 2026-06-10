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
