"""Tests for USGS water adapter Phase 2.9 — to_event() method."""

import time
from unittest.mock import MagicMock

import pytest

from meshai.env.usgs import USGSStreamsAdapter
from meshai.notifications.events import Event


# ============================================================
# FIXTURES
# ============================================================

@pytest.fixture
def mock_config():
    """Create a mock USGSConfig with real scalar fields."""
    config = MagicMock()
    config.sites = []
    config.tick_seconds = 900
    config.flood_thresholds = {}
    return config


@pytest.fixture
def adapter(mock_config):
    """Create a USGSStreamsAdapter with mocked config."""
    return USGSStreamsAdapter(mock_config)


def make_usgs_event(
    site_id="13090500",
    param_type="height",
    site_name="SNAKE RIVER NR TWIN FALLS ID",
    severity="priority",
    flood_status="Minor Flood",
    value=10.8,
    unit="ft",
    lat=42.6,
    lon=-114.47,
    headline=None,
):
    """Helper to create a stored USGS event dict (mirrors _fetch)."""
    now = time.time()
    if headline is None:
        headline = f"{site_name}: {value} {unit}"
        if flood_status:
            headline += f" — {flood_status}"
    return {
        "source": "usgs",
        "event_id": f"{site_id}_{param_type}",
        "event_type": "Stream Gauge",
        "headline": headline,
        "severity": severity,
        "lat": lat,
        "lon": lon,
        "expires": now + 1800,
        "fetched_at": now,
        "properties": {
            "site_id": site_id,
            "site_name": site_name,
            "parameter": "Gage height" if param_type == "height" else "Streamflow",
            "value": value,
            "unit": unit,
            "timestamp": "2026-05-27T12:00:00",
            "flood_status": flood_status,
            "flood_stages": {"action_stage": 9.0, "flood_stage": 10.5},
        },
    }


# ============================================================
# CATEGORY TESTS
# ============================================================

def test_minor_flood_is_flood_warning(adapter):
    """Minor/Moderate/Major flood maps to stream_flood_warning."""
    for status in ["Minor Flood", "Moderate Flood", "Major Flood"]:
        event = adapter.to_event(make_usgs_event(flood_status=status))
        assert event is not None
        assert event.category == "stream_flood_warning"


def test_action_stage_is_high_water(adapter):
    """Action Stage maps to stream_high_water."""
    event = adapter.to_event(make_usgs_event(flood_status="Action Stage", severity="routine"))
    assert event is not None
    assert event.category == "stream_high_water"


# ============================================================
# SEVERITY PASS-THROUGH TESTS
# ============================================================

def test_severity_passes_through(adapter):
    """Severity from the stored event passes through unchanged."""
    for sev in ["routine", "priority", "immediate"]:
        event = adapter.to_event(make_usgs_event(severity=sev, flood_status="Minor Flood"))
        assert event is not None
        assert event.severity == sev


# ============================================================
# GROUP KEY / INHIBIT KEY TESTS
# ============================================================

def test_group_key_is_site_param(adapter):
    """Group key is the stable {site_id}_{param} key."""
    event = adapter.to_event(make_usgs_event(site_id="13090500", param_type="height"))
    assert event is not None
    assert event.group_key == "13090500_height"


def test_inhibit_keys_match_group_key(adapter):
    """The sole inhibit key equals the group key (Inhibitor does severity tiering)."""
    event = adapter.to_event(make_usgs_event())
    assert event is not None
    assert event.inhibit_keys == [event.group_key]


def test_flow_and_height_distinct_keys(adapter):
    """Flow and height for the same site get distinct group keys."""
    e_h = adapter.to_event(make_usgs_event(param_type="height"))
    e_f = adapter.to_event(make_usgs_event(param_type="flow"))
    assert e_h.group_key != e_f.group_key


# ============================================================
# CONTENT / FIELD POPULATION TESTS
# ============================================================

def test_populates_core_fields(adapter):
    """Core Event fields are populated from the stored dict."""
    evt = make_usgs_event(lat=42.61, lon=-114.48)
    event = adapter.to_event(evt)
    assert event is not None
    assert event.source == "usgs"
    assert event.lat == 42.61
    assert event.lon == -114.48
    assert event.expires == evt["expires"]
    assert event.timestamp == evt["fetched_at"]
    assert event.id  # auto-computed


def test_summary_includes_value_and_status(adapter):
    """Summary includes the reading value and flood status."""
    event = adapter.to_event(make_usgs_event(value=11.2, unit="ft", flood_status="Moderate Flood"))
    assert event is not None
    assert "11.2 ft" in event.summary
    assert "Moderate Flood" in event.summary


# ============================================================
# DEFENSIVE / NON-EMIT TESTS
# ============================================================

def test_routine_reading_returns_none(adapter):
    """A routine reading (no flood_status) is not emitted."""
    assert adapter.to_event(make_usgs_event(flood_status=None, severity="routine")) is None


def test_missing_coords_returns_none(adapter):
    """Missing coordinates returns None."""
    evt = make_usgs_event()
    evt["lat"] = None
    assert adapter.to_event(evt) is None


def test_missing_event_id_returns_none(adapter):
    """Missing event_id returns None (no stable group key)."""
    evt = make_usgs_event()
    evt["event_id"] = None
    assert adapter.to_event(evt) is None


def test_missing_properties_returns_none(adapter):
    """No properties dict means no flood_status, so no emit."""
    evt = {
        "source": "usgs",
        "event_id": "13090500_height",
        "severity": "routine",
        "headline": "x",
        "lat": 42.6,
        "lon": -114.47,
        "fetched_at": time.time(),
    }
    assert adapter.to_event(evt) is None


def test_does_not_raise_on_corrupted_dict(adapter):
    """Corrupted dict returns None without raising."""
    assert adapter.to_event({"garbage": True}) is None
