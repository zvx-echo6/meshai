"""Tests for NIFC fires adapter Phase 2.11 — to_event() method."""

import time
from unittest.mock import MagicMock

import pytest

from meshai.env.fires import NICFFiresAdapter
from meshai.notifications.events import Event


# ============================================================
# FIXTURES
# ============================================================

@pytest.fixture
def mock_config():
    """Create a mock NICFFiresConfig with real scalar fields."""
    config = MagicMock()
    config.state = "US-ID"
    config.tick_seconds = 600
    return config


@pytest.fixture
def adapter(mock_config):
    """Create a NICFFiresAdapter with mocked config."""
    return NICFFiresAdapter(mock_config)


def make_fire_event(
    name="Ross Fork Fire",
    state="US-ID",
    acres=12500,
    pct_contained=40,
    severity="priority",
    lat=43.6,
    lon=-114.9,
    distance_km=18.0,
    nearest_anchor="Twin Falls",
    headline=None,
):
    """Helper to create a stored NIFC event dict (mirrors _fetch)."""
    now = time.time()
    if headline is None:
        headline = f"{name} -- {int(acres):,} ac, {int(pct_contained)}% contained"
    evt = {
        "source": "nifc",
        "event_id": f"nifc_{name.replace(' ', '_').lower()}_{state}",
        "event_type": "Wildfire",
        "severity": severity,
        "headline": headline,
        "name": name,
        "acres": acres,
        "pct_contained": pct_contained,
        "lat": lat,
        "lon": lon,
        "distance_km": distance_km,
        "nearest_anchor": nearest_anchor,
        "state": state,
        "expires": now + 21600,
        "fetched_at": now,
    }
    return evt


# ============================================================
# CATEGORY TESTS
# ============================================================

def test_active_perimeter_is_wildfire_incident(adapter):
    """Any active perimeter with a size maps to wildfire_incident."""
    event = adapter.to_event(make_fire_event())
    assert event is not None
    assert event.category == "wildfire_incident"


def test_category_independent_of_severity(adapter):
    """Category stays wildfire_incident regardless of severity."""
    for sev in ["routine", "priority"]:
        event = adapter.to_event(make_fire_event(severity=sev))
        assert event is not None
        assert event.category == "wildfire_incident"


# ============================================================
# SEVERITY PASS-THROUGH TESTS
# ============================================================

def test_severity_passes_through(adapter):
    """The adapter's proximity-based severity passes through unchanged."""
    for sev in ["routine", "priority", "immediate"]:
        event = adapter.to_event(make_fire_event(severity=sev))
        assert event is not None
        assert event.severity == sev


# ============================================================
# GROUP KEY / INHIBIT KEY TESTS
# ============================================================

def test_group_key_is_event_id(adapter):
    """Group key is the stable nifc_{name}_{state} key."""
    event = adapter.to_event(make_fire_event(name="Ross Fork Fire", state="US-ID"))
    assert event is not None
    assert event.group_key == "nifc_ross_fork_fire_US-ID"


def test_inhibit_keys_match_group_key(adapter):
    """The sole inhibit key equals the group key (Inhibitor does severity tiering)."""
    event = adapter.to_event(make_fire_event())
    assert event is not None
    assert event.inhibit_keys == [event.group_key]


def test_distinct_fires_get_distinct_keys(adapter):
    """Two different incidents get distinct group keys."""
    e1 = adapter.to_event(make_fire_event(name="Ross Fork Fire"))
    e2 = adapter.to_event(make_fire_event(name="Wapiti Fire"))
    assert e1.group_key != e2.group_key


# ============================================================
# CONTENT / FIELD POPULATION TESTS
# ============================================================

def test_populates_core_fields(adapter):
    """Core Event fields are populated from the stored dict."""
    evt = make_fire_event(lat=43.61, lon=-114.92)
    event = adapter.to_event(evt)
    assert event is not None
    assert event.source == "nifc"
    assert event.lat == 43.61
    assert event.lon == -114.92
    assert event.expires == evt["expires"]
    assert event.timestamp == evt["fetched_at"]
    assert event.id  # auto-computed


def test_summary_includes_size_containment_proximity(adapter):
    """Summary includes acreage, containment, and nearest-anchor proximity."""
    event = adapter.to_event(
        make_fire_event(acres=12500, pct_contained=40, distance_km=18.0, nearest_anchor="Twin Falls")
    )
    assert event is not None
    assert "12,500 ac" in event.summary
    assert "40% contained" in event.summary
    assert "18 km from Twin Falls" in event.summary


# ============================================================
# DEFENSIVE / NON-EMIT TESTS
# ============================================================

def test_missing_acres_returns_none(adapter):
    """A perimeter with no reported size (0 acres) is not emitted."""
    assert adapter.to_event(make_fire_event(acres=0)) is None


def test_missing_centroid_returns_none(adapter):
    """Missing centroid (lat/lon) returns None."""
    evt = make_fire_event()
    evt["lat"] = None
    assert adapter.to_event(evt) is None


def test_missing_event_id_returns_none(adapter):
    """Missing event_id returns None (no stable group key)."""
    evt = make_fire_event()
    evt["event_id"] = None
    assert adapter.to_event(evt) is None


def test_does_not_raise_on_corrupted_dict(adapter):
    """Corrupted dict returns None without raising."""
    assert adapter.to_event({"garbage": True}) is None
