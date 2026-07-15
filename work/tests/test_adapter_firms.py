"""Tests for FIRMS adapter Phase 2.6 — to_event() method."""

import time
from unittest.mock import MagicMock, patch

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
# ATTRIBUTION-ONLY CONTRACT
# ============================================================
#
# Firm rule (Matt): "we do NOT broadcast hotspots." The native FIRMS adapter
# must NEVER produce a broadcastable Event from a raw satellite pixel --
# neither a near-known-fire `wildfire_hotspot` nor a standalone `new_ignition`.
# `to_event()` is now attribution-only and ALWAYS returns None, mirroring the
# storage-only Central path (central/firms_handler.py). The only FIRMS signals
# that ever reach the mesh are the fusion outputs (wildfire_growth /
# wildfire_spotting / wildfire_halted) produced by the Central handler.
#
# These tests lock that contract: NOTHING the adapter emits reaches the bus.

def test_to_event_hotspot_returns_none(adapter):
    """A representative hotspot (near a known fire) never broadcasts."""
    evt = make_firms_event(new_ignition=False, near_fire="Snake River Fire",
                           frp=85.5, confidence="h")
    assert adapter.to_event(evt) is None


def test_to_event_new_ignition_returns_none(adapter):
    """A representative new-ignition hotspot never broadcasts."""
    evt = make_firms_event(new_ignition=True, frp=120.0, confidence="h",
                           distance_km=12, nearest_anchor="TFL")
    assert adapter.to_event(evt) is None


def test_to_event_returns_none_across_severities(adapter):
    """No severity tier re-opens the hotspot broadcast path."""
    for sev in ["routine", "priority", "immediate"]:
        evt = make_firms_event(severity=sev)
        assert adapter.to_event(evt) is None


def test_to_event_never_broadcasts_even_with_full_payload(adapter):
    """A fully-populated hotspot dict still produces no Event."""
    evt = make_firms_event(
        lat=42.5678, lon=-114.3456, new_ignition=True, severity="immediate",
        headline="NEW HOTSPOT detected", frp=250.0, confidence="h",
        distance_km=3, nearest_anchor="MHR",
    )
    assert adapter.to_event(evt) is None


# ============================================================
# DEFENSIVE TESTS
# ============================================================

def test_to_event_missing_coords_returns_none(adapter):
    """Missing coordinates returns None."""
    evt = make_firms_event()
    evt["lat"] = None
    event = adapter.to_event(evt)
    assert event is None


def test_to_event_missing_properties_returns_none(adapter):
    """Missing properties dict still produces no broadcast."""
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
    assert event is None


def test_to_event_does_not_raise_on_corrupted_dict(adapter):
    """Corrupted dict returns None without raising."""
    evt = {"garbage": True}
    # Should not raise
    event = adapter.to_event(evt)
    assert event is None


# ============================================================
# OPTIONAL MAP_KEY — Conduit keyless-request support
#
# The FIRMS MAP_KEY moves to Conduit's keystore; meshai sends a keyless
# request and Conduit injects the key into the URL path downstream. The
# key gate must not idle the adapter, and a blank key must build a URL
# with NO map_key path segment. A configured key must produce a
# byte-identical URL to before (backward compat).
# ============================================================

class _FakeCM:
    """Minimal context manager mimicking urlopen()'s return value."""

    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


# Empty-body CSV (header only, no data rows) short-circuits _parse_csv
# before any fusion/persistence path is touched.
_EMPTY_CSV = b"latitude,longitude,confidence,frp,acq_date,acq_time\n"


def test_fetch_url_with_map_key_is_byte_identical(mock_config):
    """With MAP_KEY configured, the built URL is unchanged (backward compat)."""
    adapter = FIRMSAdapter(mock_config, region_anchors=[], fires_adapter=None)
    captured = {}

    def fake_urlopen(req, timeout=30):
        captured["url"] = req.full_url
        return _FakeCM(_EMPTY_CSV)

    with patch("meshai.env.firms.urlopen", side_effect=fake_urlopen):
        adapter._fetch()

    assert captured["url"] == (
        "https://firms.modaps.eosdis.nasa.gov/api/area/csv/"
        "test-key/VIIRS_SNPP_NRT/-117,42,-114,44/1"
    )


def test_fetch_url_blank_map_key_omits_path_segment(mock_config):
    """With a blank MAP_KEY, the URL is built keyless (no map_key path
    segment) for a key-injecting proxy (Conduit) to complete downstream."""
    mock_config.map_key = ""
    adapter = FIRMSAdapter(mock_config, region_anchors=[], fires_adapter=None)
    captured = {}

    def fake_urlopen(req, timeout=30):
        captured["url"] = req.full_url
        return _FakeCM(_EMPTY_CSV)

    with patch("meshai.env.firms.urlopen", side_effect=fake_urlopen):
        adapter._fetch()

    assert captured["url"] == (
        "https://firms.modaps.eosdis.nasa.gov/api/area/csv/"
        "VIIRS_SNPP_NRT/-117,42,-114,44/1"
    )
    assert "test-key" not in captured["url"]


def test_tick_does_not_idle_when_map_key_blank(mock_config):
    """A blank MAP_KEY must not gate tick(); bbox is the real prerequisite."""
    mock_config.map_key = ""
    adapter = FIRMSAdapter(mock_config, region_anchors=[], fires_adapter=None)

    with patch.object(adapter, "_fetch", return_value=False) as fetch:
        result = adapter.tick()

    fetch.assert_called_once()
    assert result is False


def test_tick_still_gates_on_no_bbox(mock_config):
    """bbox remains a real prerequisite — tick() still idles without it,
    even with MAP_KEY configured."""
    mock_config.bbox = []
    adapter = FIRMSAdapter(mock_config, region_anchors=[], fires_adapter=None)

    with patch.object(adapter, "_fetch") as fetch:
        result = adapter.tick()

    fetch.assert_not_called()
    assert result is False
