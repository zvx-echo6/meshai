"""Tests for TomTom traffic adapter Phase 2.7 — to_event() method."""

import time
from unittest.mock import MagicMock

import pytest

from meshai.env.traffic import TomTomTrafficAdapter
from meshai.notifications.events import Event


# ============================================================
# FIXTURES
# ============================================================

@pytest.fixture
def mock_config():
    """Create a mock TomTomConfig with real scalar fields."""
    config = MagicMock()
    config.api_key = "test-key"
    config.corridors = []
    config.tick_seconds = 300
    return config


@pytest.fixture
def adapter(mock_config):
    """Create a TomTomTrafficAdapter with mocked config."""
    return TomTomTrafficAdapter(mock_config)


def make_traffic_event(
    name="Cole Rd",
    lat=43.6,
    lon=-116.3,
    severity="routine",
    headline="Cole Rd: 30mph (50% of free flow)",
    current_speed=30,
    free_flow_speed=60,
    ratio=0.5,
    road_closure=False,
    confidence=0.95,
):
    """Helper to create a stored traffic event dict (mirrors _fetch_point)."""
    now = time.time()
    return {
        "source": "traffic",
        "event_id": f"traffic_{name.replace(' ', '_').lower()}",
        "event_type": "Traffic Flow",
        "headline": headline,
        "severity": severity,
        "lat": lat,
        "lon": lon,
        "expires": now + 600,
        "fetched_at": now,
        "properties": {
            "corridor": name,
            "currentSpeed": current_speed,
            "freeFlowSpeed": free_flow_speed,
            "speedRatio": ratio,
            "currentTravelTime": 120,
            "freeFlowTravelTime": 60,
            "confidence": confidence,
            "roadClosure": road_closure,
        },
    }


# ============================================================
# CATEGORY TESTS
# ============================================================

def test_to_event_category_is_traffic_congestion(adapter):
    """Traffic events always map to the traffic_congestion category."""
    event = adapter.to_event(make_traffic_event())
    assert event is not None
    assert event.category == "traffic_congestion"


def test_to_event_closure_still_traffic_congestion(adapter):
    """A road closure is still traffic_congestion (severity differs, not category)."""
    event = adapter.to_event(make_traffic_event(road_closure=True, severity="priority"))
    assert event is not None
    assert event.category == "traffic_congestion"


# ============================================================
# SEVERITY PASS-THROUGH TESTS
# ============================================================

def test_to_event_severity_passes_through(adapter):
    """Severity from the stored event passes through unchanged."""
    for sev in ["routine", "priority", "immediate"]:
        event = adapter.to_event(make_traffic_event(severity=sev))
        assert event is not None
        assert event.severity == sev


# ============================================================
# GROUP KEY / INHIBIT KEY TESTS
# ============================================================

def test_to_event_group_key_is_stable_corridor_key(adapter):
    """Group key is the stable per-corridor key."""
    event = adapter.to_event(make_traffic_event(name="Cole Rd"))
    assert event is not None
    assert event.group_key == "traffic_cole_rd"


def test_to_event_inhibit_keys_match_group_key(adapter):
    """The sole inhibit key equals the group key (Inhibitor does severity tiering)."""
    event = adapter.to_event(make_traffic_event(name="Cole Rd"))
    assert event is not None
    assert event.inhibit_keys == [event.group_key]


def test_two_polls_same_corridor_share_group_key(adapter):
    """Two re-polls of the same corridor (any case/spacing) share the group key."""
    e1 = adapter.to_event(make_traffic_event(name="Cole Rd", severity="routine"))
    e2 = adapter.to_event(make_traffic_event(name="cole rd", severity="priority"))
    assert e1 is not None and e2 is not None
    assert e1.group_key == e2.group_key


def test_group_key_matches_adapter_event_id(adapter):
    """The group key matches the adapter's own stable event_id derivation."""
    evt = make_traffic_event(name="Eagle Rd")
    event = adapter.to_event(evt)
    assert event is not None
    assert event.group_key == evt["event_id"]


# ============================================================
# CONTENT / FIELD POPULATION TESTS
# ============================================================

def test_to_event_populates_core_fields(adapter):
    """Core Event fields are populated from the stored dict."""
    evt = make_traffic_event(lat=43.61, lon=-116.21)
    event = adapter.to_event(evt)
    assert event is not None
    assert event.source == "traffic"
    assert event.lat == 43.61
    assert event.lon == -116.21
    assert event.expires == evt["expires"]
    assert event.timestamp == evt["fetched_at"]
    assert event.id  # auto-computed


def test_to_event_summary_includes_speed(adapter):
    """Summary includes current/free-flow speed."""
    event = adapter.to_event(make_traffic_event(current_speed=25, free_flow_speed=65))
    assert event is not None
    assert "25/65 mph" in event.summary


def test_to_event_summary_includes_closure(adapter):
    """Summary notes a road closure."""
    event = adapter.to_event(make_traffic_event(road_closure=True))
    assert event is not None
    assert "road closed" in event.summary


def test_to_event_title_falls_back_when_headline_empty(adapter):
    """Empty headline falls back to a corridor-based title."""
    event = adapter.to_event(make_traffic_event(headline=""))
    assert event is not None
    assert event.title == "Traffic: Cole Rd"


# ============================================================
# DEFENSIVE TESTS
# ============================================================

def test_to_event_missing_coords_returns_none(adapter):
    """Missing coordinates returns None."""
    evt = make_traffic_event()
    evt["lat"] = None
    assert adapter.to_event(evt) is None


def test_to_event_missing_corridor_returns_none(adapter):
    """Missing corridor identity returns None (no stable group key)."""
    evt = make_traffic_event()
    evt["properties"]["corridor"] = None
    assert adapter.to_event(evt) is None


def test_to_event_missing_properties_returns_none(adapter):
    """No properties dict means no corridor, returns None."""
    evt = {
        "source": "traffic",
        "event_id": "traffic_x",
        "severity": "routine",
        "headline": "x",
        "lat": 43.6,
        "lon": -116.3,
        "fetched_at": time.time(),
    }
    assert adapter.to_event(evt) is None


def test_to_event_does_not_raise_on_corrupted_dict(adapter):
    """Corrupted dict returns None without raising."""
    assert adapter.to_event({"garbage": True}) is None
