"""Tests for TomTom traffic adapter Phase 2.7 — to_event() method."""

import time
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

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


# ============================================================
# _fetch_point 400 — roadless cell is no-data, not an error
# ============================================================

def test_fetch_point_400_does_not_set_last_error(mock_config):
    """A 400 HTTPError (roadless cell) must not set _last_error or increment
    consecutive_errors — it is expected no-data, not a failure."""
    adapter = TomTomTrafficAdapter(mock_config)
    assert adapter._last_error is None
    assert adapter._consecutive_errors == 0

    err = HTTPError(
        url="https://api.tomtom.com/...",
        code=400,
        msg="Bad Request",
        hdrs=None,
        fp=None,
    )
    with patch("meshai.env.traffic.urlopen", side_effect=err):
        result = adapter._fetch_point("wilderness_cell", 43.5, -115.0, 0.0)

    assert result is None, "400 must return None (no data)"
    assert adapter._last_error is None, "_last_error must not be set on 400"
    assert adapter._consecutive_errors == 0, "consecutive_errors must not increment on 400"


def test_fetch_point_non400_http_error_sets_last_error(mock_config):
    """Non-400/401/403 HTTP errors (e.g. 503) must still set _last_error."""
    adapter = TomTomTrafficAdapter(mock_config)

    err = HTTPError(
        url="https://api.tomtom.com/...",
        code=503,
        msg="Service Unavailable",
        hdrs=None,
        fp=None,
    )
    with patch("meshai.env.traffic.urlopen", side_effect=err):
        result = adapter._fetch_point("some_corridor", 43.5, -116.0, 0.0)

    assert result is None
    assert adapter._last_error == "HTTP 503"
    assert adapter._consecutive_errors == 1


# ============================================================
# OPTIONAL API KEY — Conduit keyless-request support
#
# TomTom key moves to Conduit's keystore; meshai sends a keyless request
# and Conduit injects the key downstream. Key gate must not idle the
# adapter, and a blank key must build a keyless URL. A configured key
# must produce a byte-identical URL to before (backward compat).
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


def test_fetch_point_url_with_key_is_byte_identical(mock_config):
    """With a key configured, the built URL is unchanged (backward compat)."""
    adapter = TomTomTrafficAdapter(mock_config)
    captured = {}

    def fake_urlopen(req, timeout=15):
        captured["url"] = req.full_url
        return _FakeCM(b'{"flowSegmentData": {}}')

    with patch("meshai.env.traffic.urlopen", side_effect=fake_urlopen):
        adapter._fetch_point("Cole Rd", 43.6, -116.3, 0.0)

    assert captured["url"] == (
        "https://api.tomtom.com/traffic/services/4/flowSegmentData/relative0/10/json"
        "?point=43.6%2C-116.3&key=test-key&unit=MPH"
    )


def test_fetch_point_url_blank_key_omits_key_param(mock_config):
    """With a blank key, the URL is built keyless (no key= param) for a
    key-injecting proxy (Conduit) to complete downstream."""
    mock_config.api_key = ""
    adapter = TomTomTrafficAdapter(mock_config)
    captured = {}

    def fake_urlopen(req, timeout=15):
        captured["url"] = req.full_url
        return _FakeCM(b'{"flowSegmentData": {}}')

    with patch("meshai.env.traffic.urlopen", side_effect=fake_urlopen):
        adapter._fetch_point("Cole Rd", 43.6, -116.3, 0.0)

    assert captured["url"] == (
        "https://api.tomtom.com/traffic/services/4/flowSegmentData/relative0/10/json"
        "?point=43.6%2C-116.3&unit=MPH"
    )
    assert "key=" not in captured["url"]


def test_tick_does_not_idle_when_key_blank(mock_config):
    """A blank key must not gate tick(); corridors is the real prerequisite."""
    mock_config.api_key = ""
    mock_config.corridors = [{"name": "Cole Rd", "lat": 43.6, "lon": -116.3}]
    adapter = TomTomTrafficAdapter(mock_config)

    with patch.object(adapter, "_fetch_all", return_value=True) as fetch_all:
        result = adapter.tick()

    fetch_all.assert_called_once()
    assert result is True


def test_tick_still_gates_on_no_corridors(mock_config):
    """Corridors remain a real prerequisite — tick() still idles without them,
    even with a key configured."""
    mock_config.corridors = []
    adapter = TomTomTrafficAdapter(mock_config)

    with patch.object(adapter, "_fetch_all") as fetch_all:
        result = adapter.tick()

    fetch_all.assert_not_called()
    assert result is False
