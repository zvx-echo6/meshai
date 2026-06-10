"""Tests for NWS adapter Phase 2.6 — to_event() and _derive_category()."""

import time
from unittest.mock import MagicMock, patch

import pytest

from meshai.env.nws import NWSAlertsAdapter
from meshai.notifications.events import Event


# ============================================================
# FIXTURES
# ============================================================

@pytest.fixture
def mock_config():
    """Create a mock NWSConfig."""
    config = MagicMock()
    config.areas = ["ID"]
    config.user_agent = "(test, test@example.com)"
    config.severity_min = "moderate"
    config.tick_seconds = 60
    return config


@pytest.fixture
def adapter(mock_config):
    """Create an NWSAlertsAdapter with mocked config."""
    return NWSAlertsAdapter(mock_config)


# ============================================================
# _derive_category TESTS
# ============================================================

def test_derive_category_warning(adapter):
    """Warning suffix maps to weather_warning."""
    assert adapter._derive_category("Tornado Warning") == "weather_warning"
    assert adapter._derive_category("Red Flag Warning") == "weather_warning"
    assert adapter._derive_category("Winter Storm Warning") == "weather_warning"


def test_derive_category_watch(adapter):
    """Watch suffix maps to weather_watch."""
    assert adapter._derive_category("Tornado Watch") == "weather_watch"
    assert adapter._derive_category("Winter Storm Watch") == "weather_watch"
    assert adapter._derive_category("Fire Weather Watch") == "weather_watch"


def test_derive_category_advisory(adapter):
    """Advisory suffix maps to weather_advisory."""
    assert adapter._derive_category("Wind Advisory") == "weather_advisory"
    assert adapter._derive_category("Heat Advisory") == "weather_advisory"
    assert adapter._derive_category("Frost Advisory") == "weather_advisory"


def test_derive_category_statement(adapter):
    """Non-standard suffixes map to weather_statement."""
    assert adapter._derive_category("Special Weather Statement") == "weather_statement"
    assert adapter._derive_category("Short Term Forecast") == "weather_statement"
    assert adapter._derive_category("Hazardous Weather Outlook") == "weather_statement"


def test_derive_category_case_insensitive(adapter):
    """Category derivation is case-insensitive."""
    assert adapter._derive_category("TORNADO WARNING") == "weather_warning"
    assert adapter._derive_category("winter storm watch") == "weather_watch"
    assert adapter._derive_category("Wind ADVISORY") == "weather_advisory"


# ============================================================
# to_event TESTS
# ============================================================

def test_to_event_returns_event_instance(adapter):
    """to_event returns an Event instance."""
    raw = {
        "source": "nws",
        "event_id": "urn:oid:2.49.0.1.840.0.abc123",
        "event_type": "Tornado Warning",
        "severity": "extreme",
        "headline": "Tornado Warning issued for Ada County",
        "description": "A tornado warning has been issued...",
        "onset": time.time(),
        "expires": time.time() + 3600,
        "areas": ["IDZ016"],
        "lat": 43.615,
        "lon": -116.2023,
    }
    event = adapter.to_event(raw)
    assert isinstance(event, Event)


def test_to_event_sets_source_nws(adapter):
    """to_event sets source to 'nws'."""
    raw = {
        "source": "nws",
        "event_id": "test-id",
        "event_type": "Wind Advisory",
        "severity": "moderate",
        "headline": "Wind Advisory",
    }
    event = adapter.to_event(raw)
    assert event.source == "nws"


def test_to_event_derives_category_from_event_type(adapter):
    """to_event uses _derive_category for category field."""
    raw = {
        "event_id": "test",
        "event_type": "Winter Storm Watch",
        "severity": "severe",
        "headline": "Winter Storm Watch",
    }
    event = adapter.to_event(raw)
    assert event.category == "weather_watch"


def test_to_event_maps_severity(adapter):
    """to_event maps NWS severity to 3-level system."""
    # Extreme -> immediate
    raw = {"event_id": "1", "event_type": "Tornado Warning", "severity": "extreme", "headline": "test"}
    assert adapter.to_event(raw).severity == "immediate"

    # Severe -> priority
    raw = {"event_id": "2", "event_type": "Tornado Warning", "severity": "severe", "headline": "test"}
    assert adapter.to_event(raw).severity == "priority"

    # Moderate -> routine
    raw = {"event_id": "3", "event_type": "Wind Advisory", "severity": "moderate", "headline": "test"}
    assert adapter.to_event(raw).severity == "routine"


def test_to_event_sets_title_from_headline(adapter):
    """to_event uses headline as title."""
    raw = {
        "event_id": "test",
        "event_type": "Heat Advisory",
        "severity": "moderate",
        "headline": "Heat Advisory issued for Magic Valley",
    }
    event = adapter.to_event(raw)
    assert event.title == "Heat Advisory issued for Magic Valley"


def test_to_event_sets_body_from_description(adapter):
    """to_event uses description as body."""
    raw = {
        "event_id": "test",
        "event_type": "Heat Advisory",
        "severity": "moderate",
        "headline": "Heat Advisory",
        "description": "Dangerously hot conditions expected...",
    }
    event = adapter.to_event(raw)
    assert event.body == "Dangerously hot conditions expected..."


def test_to_event_sets_effective_and_expires(adapter):
    """to_event sets effective and expires from onset/expires."""
    onset = time.time()
    expires = time.time() + 7200
    raw = {
        "event_id": "test",
        "event_type": "Wind Advisory",
        "severity": "moderate",
        "headline": "Wind Advisory",
        "onset": onset,
        "expires": expires,
    }
    event = adapter.to_event(raw)
    assert event.effective == onset
    assert event.expires == expires


def test_to_event_sets_lat_lon(adapter):
    """to_event sets lat/lon from raw event."""
    raw = {
        "event_id": "test",
        "event_type": "Tornado Warning",
        "severity": "extreme",
        "headline": "Tornado Warning",
        "lat": 43.615,
        "lon": -116.2023,
    }
    event = adapter.to_event(raw)
    assert event.lat == 43.615
    assert event.lon == -116.2023


def test_to_event_sets_nws_zones(adapter):
    """to_event sets nws_zones from areas."""
    raw = {
        "event_id": "test",
        "event_type": "Red Flag Warning",
        "severity": "severe",
        "headline": "Red Flag Warning",
        "areas": ["IDZ016", "IDZ030", "IDZ031"],
    }
    event = adapter.to_event(raw)
    assert event.nws_zones == ["IDZ016", "IDZ030", "IDZ031"]


def test_to_event_sets_group_key_from_event_id(adapter):
    """to_event sets group_key to event_id for dedup."""
    raw = {
        "event_id": "urn:oid:2.49.0.1.840.0.abc123",
        "event_type": "Tornado Warning",
        "severity": "extreme",
        "headline": "Tornado Warning",
    }
    event = adapter.to_event(raw)
    assert event.group_key == "urn:oid:2.49.0.1.840.0.abc123"


def test_to_event_warning_sets_inhibit_keys(adapter):
    """Warnings set inhibit_keys for corresponding Watch/Advisory."""
    raw = {
        "event_id": "test",
        "event_type": "Winter Storm Warning",
        "severity": "severe",
        "headline": "Winter Storm Warning",
    }
    event = adapter.to_event(raw)
    assert "nws:Winter Storm Watch" in event.inhibit_keys
    assert "nws:Winter Storm Advisory" in event.inhibit_keys


def test_to_event_watch_no_inhibit_keys(adapter):
    """Watches do not set inhibit_keys."""
    raw = {
        "event_id": "test",
        "event_type": "Tornado Watch",
        "severity": "moderate",
        "headline": "Tornado Watch",
    }
    event = adapter.to_event(raw)
    assert event.inhibit_keys == []


def test_to_event_preserves_raw_in_data(adapter):
    """to_event preserves raw event dict in data field."""
    raw = {
        "event_id": "test-123",
        "event_type": "Wind Advisory",
        "severity": "moderate",
        "headline": "Wind Advisory",
        "custom_field": "custom_value",
    }
    event = adapter.to_event(raw)
    assert event.data == raw
    assert event.data["custom_field"] == "custom_value"


# ============================================================
# INTEGRATION: _map_nws_severity
# ============================================================

def test_map_nws_severity_extreme_to_immediate(adapter):
    """Extreme NWS severity maps to immediate."""
    assert adapter._map_nws_severity("extreme") == "immediate"


def test_map_nws_severity_severe_to_priority(adapter):
    """Severe NWS severity maps to priority."""
    assert adapter._map_nws_severity("severe") == "priority"


def test_map_nws_severity_moderate_to_routine(adapter):
    """Moderate NWS severity maps to routine."""
    assert adapter._map_nws_severity("moderate") == "routine"


def test_map_nws_severity_minor_to_routine(adapter):
    """Minor NWS severity maps to routine."""
    assert adapter._map_nws_severity("minor") == "routine"
