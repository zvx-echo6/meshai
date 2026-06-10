"""Tests for SWPC space weather adapter Phase 2.12 — to_event() + dedup fix."""

import time
from unittest.mock import MagicMock

import pytest

from meshai.env.swpc import SWPCAdapter
from meshai.notifications.events import Event


# ============================================================
# FIXTURES
# ============================================================

@pytest.fixture
def mock_config():
    """Create a mock SWPCConfig."""
    return MagicMock()


@pytest.fixture
def adapter(mock_config):
    """Create a SWPCAdapter with mocked config."""
    return SWPCAdapter(mock_config)


def make_swpc_event(
    scale="G",
    level=3,
    severity="priority",
    headline=None,
):
    """Helper to create a stored SWPC event dict (mirrors _update_events)."""
    now = time.time()
    label = {"R": "Radio Blackout", "S": "Solar Radiation Storm", "G": "Geomagnetic Storm"}[scale]
    if headline is None:
        headline = f"{scale}{level} {label} in progress"
    return {
        "source": "swpc",
        "event_id": f"swpc_{scale.lower()}{level}",
        "event_type": f"{scale}{level} {label}",
        "scale": scale,
        "level": level,
        "severity": severity,
        "headline": headline,
        "expires": now + 3600,
        "areas": [],
        "fetched_at": now,
    }


# ============================================================
# CATEGORY TESTS
# ============================================================

def test_scale_categories(adapter):
    """Each NOAA scale maps to its category."""
    cases = {
        "R": "rf_propagation_alert",
        "S": "solar_radiation_storm",
        "G": "geomagnetic_storm",
    }
    for scale, category in cases.items():
        event = adapter.to_event(make_swpc_event(scale=scale, level=3))
        assert event is not None
        assert event.category == category


# ============================================================
# SEVERITY TESTS
# ============================================================

def test_severity_passes_through(adapter):
    """Severity from the stored event passes through unchanged."""
    for sev in ["routine", "priority", "immediate"]:
        event = adapter.to_event(make_swpc_event(severity=sev))
        assert event is not None
        assert event.severity == sev


def test_update_events_severity_tiering(adapter):
    """_update_events tiers severity: 1-2 routine, 3-4 priority, 5 immediate."""
    expected = {1: "routine", 2: "routine", 3: "priority", 4: "priority", 5: "immediate"}
    for level, sev in expected.items():
        adapter._status = {"g_scale": level}
        adapter._update_events()
        evs = adapter.get_events()
        assert len(evs) == 1
        assert evs[0]["severity"] == sev


# ============================================================
# DEDUP REGRESSION TEST (the bug we are fixing)
# ============================================================

def test_dedup_id_stable_across_ticks(adapter):
    """REGRESSION: a sustained condition keeps the SAME event_id across ticks.

    The old code embedded int(time.time()) in event_id, making every tick
    unique and defeating the store's (source, event_id) dedup.
    """
    adapter._status = {"r_scale": 3, "s_scale": 0, "g_scale": 0}
    adapter._update_events()
    id1 = adapter.get_events()[0]["event_id"]
    time.sleep(0.01)  # wall clock advances
    adapter._update_events()
    id2 = adapter.get_events()[0]["event_id"]
    assert id1 == id2
    assert id1 == "swpc_r3"


def test_event_id_changes_with_level(adapter):
    """An escalation to a new level produces a new (stable) event_id."""
    adapter._status = {"g_scale": 3}
    adapter._update_events()
    id_g3 = adapter.get_events()[0]["event_id"]
    adapter._status = {"g_scale": 5}
    adapter._update_events()
    id_g5 = adapter.get_events()[0]["event_id"]
    assert id_g3 == "swpc_g3"
    assert id_g5 == "swpc_g5"
    assert id_g3 != id_g5


# ============================================================
# EMISSION SCOPE TESTS
# ============================================================

def test_all_three_scales_emit_when_active(adapter):
    """R, S, and G each produce an event when active (level >= 1)."""
    adapter._status = {"r_scale": 2, "s_scale": 1, "g_scale": 4}
    adapter._update_events()
    scales = {e["scale"] for e in adapter.get_events()}
    assert scales == {"R", "S", "G"}


def test_quiet_conditions_emit_nothing(adapter):
    """All scales at 0 (quiet) produce no events."""
    adapter._status = {"r_scale": 0, "s_scale": 0, "g_scale": 0}
    adapter._update_events()
    assert adapter.get_events() == []


# ============================================================
# GROUP KEY / INHIBIT KEY TESTS
# ============================================================

def test_group_key_is_event_id(adapter):
    """Group key is the stable swpc_{scale}{level} key."""
    event = adapter.to_event(make_swpc_event(scale="G", level=3))
    assert event is not None
    assert event.group_key == "swpc_g3"


def test_inhibit_keys_match_group_key(adapter):
    """The sole inhibit key equals the group key (Inhibitor does severity tiering)."""
    event = adapter.to_event(make_swpc_event())
    assert event is not None
    assert event.inhibit_keys == [event.group_key]


# ============================================================
# CONTENT / FIELD POPULATION TESTS
# ============================================================

def test_populates_core_fields_global(adapter):
    """Core fields populate; SWPC is global so lat/lon are None, region set."""
    evt = make_swpc_event(scale="G", level=4)
    event = adapter.to_event(evt)
    assert event is not None
    assert event.source == "swpc"
    assert event.lat is None
    assert event.lon is None
    assert event.region == "global"
    assert event.expires == evt["expires"]
    assert event.timestamp == evt["fetched_at"]
    assert event.id  # auto-computed


# ============================================================
# DEFENSIVE / NON-EMIT TESTS
# ============================================================

def test_missing_scale_returns_none(adapter):
    """Missing scale discriminator returns None."""
    evt = make_swpc_event()
    evt["scale"] = None
    assert adapter.to_event(evt) is None


def test_level_zero_returns_none(adapter):
    """A level-0 (quiet) condition returns None."""
    assert adapter.to_event(make_swpc_event(level=0, severity="routine")) is None


def test_missing_event_id_returns_none(adapter):
    """Missing event_id returns None (no stable group key)."""
    evt = make_swpc_event()
    evt["event_id"] = None
    assert adapter.to_event(evt) is None


def test_does_not_raise_on_corrupted_dict(adapter):
    """Corrupted dict returns None without raising."""
    assert adapter.to_event({"garbage": True}) is None
