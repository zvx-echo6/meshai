"""Tests for avalanche adapter Phase 2.10 — to_event() method."""

import time
from unittest.mock import MagicMock

import pytest

from meshai.env.avalanche import AvalancheAdapter
from meshai.notifications.events import Event


# ============================================================
# FIXTURES
# ============================================================

@pytest.fixture
def mock_config():
    """Create a mock AvalancheConfig with real scalar fields."""
    config = MagicMock()
    config.center_ids = ["SNFAC"]
    config.tick_seconds = 1800
    config.season_months = [12, 1, 2, 3, 4]
    return config


@pytest.fixture
def adapter(mock_config):
    """Create an AvalancheAdapter with mocked config."""
    return AvalancheAdapter(mock_config)


def make_avy_event(
    center_id="SNFAC",
    zone_name="Banner Summit",
    danger_level=4,
    danger_name="High",
    severity="priority",
    travel_advice="Dangerous avalanche conditions.",
    lat=44.3,
    lon=-115.2,
    headline=None,
):
    """Helper to create a stored avalanche event dict (mirrors _fetch)."""
    now = time.time()
    if headline is None:
        headline = f"{zone_name}: {danger_name} avalanche danger"
        if travel_advice:
            headline += f" -- {travel_advice[:100]}"
    return {
        "source": "avalanche",
        "event_id": f"avy_{center_id}_{zone_name.replace(' ', '_').lower()}",
        "event_type": "Avalanche Advisory",
        "severity": severity,
        "headline": headline,
        "zone_name": zone_name,
        "center": "Sawtooth Avalanche Center",
        "center_id": center_id,
        "center_link": "https://www.sawtoothavalanche.com",
        "forecast_link": "https://www.sawtoothavalanche.com/forecast",
        "danger": danger_name.lower(),
        "danger_level": danger_level,
        "danger_name": danger_name,
        "travel_advice": travel_advice,
        "state": "ID",
        "lat": lat,
        "lon": lon,
        "expires": now + 3600,
        "fetched_at": now,
    }


# ============================================================
# CATEGORY TESTS
# ============================================================

def test_high_and_extreme_are_warning(adapter):
    """Danger level 4 (High) and 5 (Extreme) map to avalanche_warning."""
    for level, name in [(4, "High"), (5, "Extreme")]:
        event = adapter.to_event(make_avy_event(danger_level=level, danger_name=name))
        assert event is not None
        assert event.category == "avalanche_warning"


def test_considerable_is_watch(adapter):
    """Danger level 3 (Considerable) maps to avalanche_watch."""
    event = adapter.to_event(
        make_avy_event(danger_level=3, danger_name="Considerable", severity="routine")
    )
    assert event is not None
    assert event.category == "avalanche_watch"


# ============================================================
# SEVERITY PASS-THROUGH TESTS
# ============================================================

def test_severity_passes_through(adapter):
    """Severity from the stored event passes through unchanged."""
    for sev in ["routine", "priority", "immediate"]:
        event = adapter.to_event(make_avy_event(severity=sev, danger_level=4))
        assert event is not None
        assert event.severity == sev


# ============================================================
# GROUP KEY / INHIBIT KEY TESTS
# ============================================================

def test_group_key_is_event_id(adapter):
    """Group key is the stable avy_{center}_{zone} key."""
    event = adapter.to_event(make_avy_event(center_id="SNFAC", zone_name="Banner Summit"))
    assert event is not None
    assert event.group_key == "avy_SNFAC_banner_summit"


def test_inhibit_keys_match_group_key(adapter):
    """The sole inhibit key equals the group key (Inhibitor does severity tiering)."""
    event = adapter.to_event(make_avy_event())
    assert event is not None
    assert event.inhibit_keys == [event.group_key]


def test_distinct_zones_get_distinct_keys(adapter):
    """Two zones in the same center get distinct group keys."""
    e1 = adapter.to_event(make_avy_event(zone_name="Banner Summit"))
    e2 = adapter.to_event(make_avy_event(zone_name="Galena Summit"))
    assert e1.group_key != e2.group_key


# ============================================================
# CONTENT / FIELD POPULATION TESTS
# ============================================================

def test_populates_core_fields(adapter):
    """Core Event fields are populated from the stored dict."""
    evt = make_avy_event(lat=44.31, lon=-115.22)
    event = adapter.to_event(evt)
    assert event is not None
    assert event.source == "avalanche"
    assert event.lat == 44.31
    assert event.lon == -115.22
    assert event.expires == evt["expires"]
    assert event.timestamp == evt["fetched_at"]
    assert event.id  # auto-computed


def test_summary_includes_danger_and_advice(adapter):
    """Summary includes the danger name and travel advice."""
    event = adapter.to_event(
        make_avy_event(danger_name="Extreme", danger_level=5, travel_advice="Avoid all terrain.")
    )
    assert event is not None
    assert "Extreme" in event.summary
    assert "Avoid all terrain." in event.summary


# ============================================================
# DEFENSIVE / NON-EMIT TESTS
# ============================================================

def test_low_danger_returns_none(adapter):
    """Low/Moderate (1-2) danger is not actionable, returns None."""
    for level, name in [(1, "Low"), (2, "Moderate")]:
        assert adapter.to_event(make_avy_event(danger_level=level, danger_name=name)) is None


def test_no_rating_returns_none(adapter):
    """A no-rating (-1/0) advisory returns None."""
    for level in [-1, 0]:
        assert adapter.to_event(make_avy_event(danger_level=level, danger_name="No Rating")) is None


def test_missing_danger_level_returns_none(adapter):
    """Missing danger_level returns None."""
    evt = make_avy_event()
    evt["danger_level"] = None
    assert adapter.to_event(evt) is None


def test_missing_centroid_returns_none(adapter):
    """Missing centroid (lat/lon) returns None."""
    evt = make_avy_event()
    evt["lat"] = None
    assert adapter.to_event(evt) is None


def test_missing_event_id_returns_none(adapter):
    """Missing event_id returns None (no stable group key)."""
    evt = make_avy_event()
    evt["event_id"] = None
    assert adapter.to_event(evt) is None


def test_does_not_raise_on_corrupted_dict(adapter):
    """Corrupted dict returns None without raising."""
    assert adapter.to_event({"garbage": True}) is None


# ============================================================================
# Central avy_handler._render (mesh broadcast wire) -- budget-fit format.
#   advice -> FIRST SENTENCE only; zone/level/source kept FULL; fits 140.
# ============================================================================

from meshai.central.avy_handler import _render as _avy_render


def test_avy_render_advice_first_sentence_only():
    wire = _avy_render(
        danger_level=4, danger_name="High",
        zone_name="Western Mountains",
        center_id="SNFAC",
        travel=("Avoid all avalanche terrain today. Natural and human-triggered "
                "avalanches are likely on steep slopes."),
    )
    # first sentence retained (with its period), the rest dropped
    assert "Avoid all avalanche terrain today." in wire
    assert "Natural and human-triggered" not in wire
    # zone / level / source kept FULL (never abbreviated)
    assert "Western Mountains" in wire
    assert "High (4)" in wire
    assert "SNFAC" in wire


def test_avy_render_worst_case_fits_140():
    # Long multi-sentence advice paragraph -- only the first sentence is kept,
    # which lets the full zone / level / source survive under the 140 budget.
    wire = _avy_render(
        danger_level=5, danger_name="Extreme",
        zone_name="Western Mountains",
        center_id="Sawtooth Avalanche Center",
        travel=("Avoid all avalanche terrain today! Very dangerous conditions "
                "exist across all elevations and aspects with widespread natural "
                "avalanche activity likely through the afternoon and overnight."),
    )
    assert len(wire) <= 140, f"{len(wire)} chars:\n{wire!r}"
    # zone, level, source, and the first-sentence advice all present
    assert "Western Mountains" in wire
    assert "Extreme (5)" in wire
    assert "Sawtooth Avalanche Center" in wire
    assert "Avoid all avalanche terrain today!" in wire
    # first sentence terminates at the '!' -> the rest is gone
    assert "Very dangerous conditions" not in wire
