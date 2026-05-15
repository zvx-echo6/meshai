"""Tests for FIRMS adapter Phase 2.6 — to_event() method."""

import time
from unittest.mock import MagicMock

import pytest

from meshai.env.firms import FIRMSAdapter
from meshai.notifications.events import Event


# ============================================================
# FIXTURES
# ============================================================

@pytest.fixture
def mock_config():
    """Create a mock FIRMSConfig."""
    config = MagicMock()
    config.map_key = "test-key"
    config.source = "VIIRS_SNPP_NRT"
    config.bbox = [-117, 42, -114, 44]
    config.day_range = 1
    config.tick_seconds = 1800
    config.confidence_min = "nominal"
    config.proximity_km = 10.0
    return config


@pytest.fixture
def adapter(mock_config):
    """Create a FIRMSAdapter with mocked dependencies."""
    return FIRMSAdapter(mock_config, region_anchors=[], fires_adapter=None)


def make_firms_event(
    lat=42.5,
    lon=-114.5,
    new_ignition=False,
    severity="routine",
    headline="Test Hotspot",
    frp=None,
    confidence="n",
    distance_km=None,
    nearest_anchor=None,
    near_fire=None,
):
    """Helper to create a FIRMS event dict."""
    now = time.time()
    return {
        "source": "firms",
        "event_id": f"firms_{lat:.4f}_{lon:.4f}_2026-05-15_1200",
        "event_type": "Fire Hotspot",
        "severity": severity,
        "headline": headline,
        "lat": lat,
        "lon": lon,
        "expires": now + 21600,
        "fetched_at": now,
        "properties": {
            "new_ignition": new_ignition,
            "confidence": confidence,
            "frp": frp,
            "brightness": 350.0,
            "acq_date": "2026-05-15",
            "acq_time": "1200",
            "near_fire": near_fire,
            "distance_to_fire_km": 5.0 if near_fire else None,
            "distance_km": distance_km,
            "nearest_anchor": nearest_anchor,
        },
    }


# ============================================================
# CATEGORY DECISION TESTS
# ============================================================

def test_to_event_new_ignition(adapter):
    """New ignition maps to new_ignition category."""
    evt = make_firms_event(new_ignition=True)
    event = adapter.to_event(evt)
    assert event is not None
    assert event.category == "new_ignition"


def test_to_event_near_known_fire(adapter):
    """Hotspot near known fire maps to wildfire_proximity."""
    evt = make_firms_event(new_ignition=False, near_fire="Snake River Fire")
    event = adapter.to_event(evt)
    assert event is not None
    assert event.category == "wildfire_proximity"


# ============================================================
# SEVERITY PASS-THROUGH TESTS
# ============================================================

def test_to_event_severity_passes_through(adapter):
    """Severity from FIRMS event passes through unchanged."""
    for sev in ["routine", "priority", "immediate"]:
        evt = make_firms_event(severity=sev)
        event = adapter.to_event(evt)
        assert event is not None
        assert event.severity == sev


# ============================================================
# CONTENT TESTS
# ============================================================

def test_to_event_summary_includes_frp(adapter):
    """Summary includes FRP when present."""
    evt = make_firms_event(frp=85.5)
    event = adapter.to_event(evt)
    assert event is not None
    assert "FRP 85" in event.summary


def test_to_event_summary_handles_missing_frp(adapter):
    """Missing FRP doesn't break to_event."""
    evt = make_firms_event(frp=None)
    event = adapter.to_event(evt)
    assert event is not None
    assert "FRP" not in event.summary


def test_to_event_summary_includes_distance_when_present(adapter):
    """Summary includes distance and anchor when present."""
    evt = make_firms_event(distance_km=12, nearest_anchor="TFL")
    event = adapter.to_event(evt)
    assert event is not None
    assert "12 km" in event.summary
    assert "TFL" in event.summary


def test_to_event_region_uses_nearest_anchor(adapter):
    """Region is set from nearest_anchor."""
    evt = make_firms_event(nearest_anchor="MHR")
    event = adapter.to_event(evt)
    assert event is not None
    assert event.region == "MHR"


# ============================================================
# SPATIAL KEY TESTS
# ============================================================

def test_to_event_group_key_is_spatial_grid(adapter):
    """Group key is spatial grid based on rounded lat/lon."""
    evt = make_firms_event(lat=42.5678, lon=-114.3456)
    event = adapter.to_event(evt)
    assert event is not None
    assert event.group_key == "firms:42.57:-114.35"


def test_to_event_inhibit_keys_match_group_key(adapter):
    """Inhibit keys contain the same spatial key as group_key."""
    evt = make_firms_event(lat=42.5678, lon=-114.3456)
    event = adapter.to_event(evt)
    assert event is not None
    assert event.group_key in event.inhibit_keys


def test_two_nearby_detections_share_group_key(adapter):
    """Two detections in same grid cell share group_key."""
    # Both round to 42.57:-114.35
    evt1 = make_firms_event(lat=42.571, lon=-114.351)
    evt2 = make_firms_event(lat=42.572, lon=-114.352)
    event1 = adapter.to_event(evt1)
    event2 = adapter.to_event(evt2)
    assert event1 is not None
    assert event2 is not None
    assert event1.group_key == event2.group_key


# ============================================================
# DEFENSIVE TESTS
# ============================================================

def test_to_event_missing_coords_returns_none(adapter):
    """Missing coordinates returns None."""
    evt = make_firms_event()
    evt["lat"] = None
    event = adapter.to_event(evt)
    assert event is None


def test_to_event_missing_properties_returns_event(adapter):
    """Missing properties dict defaults to wildfire_proximity."""
    evt = {
        "source": "firms",
        "event_id": "test",
        "event_type": "Fire Hotspot",
        "severity": "routine",
        "headline": "Test",
        "lat": 42.5,
        "lon": -114.5,
        "fetched_at": time.time(),
    }
    # No "properties" key at all
    event = adapter.to_event(evt)
    assert event is not None
    assert event.category == "wildfire_proximity"


def test_to_event_does_not_raise_on_corrupted_dict(adapter):
    """Corrupted dict returns None without raising."""
    evt = {"garbage": True}
    # Should not raise
    event = adapter.to_event(evt)
    assert event is None
