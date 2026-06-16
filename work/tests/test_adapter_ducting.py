"""Tests for ducting adapter Phase 2.13 — tier-based threshold-crossing emission."""

import time
from unittest.mock import MagicMock

import pytest

from meshai.env.ducting import DuctingAdapter
from meshai.notifications.events import Event


# ============================================================
# FIXTURES / HELPERS
# ============================================================

def fresh_adapter():
    """A DuctingAdapter with the Twin Falls default location."""
    cfg = MagicMock()
    cfg.latitude = 42.56
    cfg.longitude = -114.47
    cfg.tick_seconds = 10800
    return DuctingAdapter(cfg)


@pytest.fixture
def adapter():
    return fresh_adapter()


def set_status(adapter, min_gradient, condition="normal", assessment="", base=None, thick=None):
    """Drive _update_events from a synthetic status blob (mirrors _parse_response)."""
    adapter._status = {
        "min_gradient": min_gradient,
        "condition": condition,
        "assessment": assessment,
        "duct_base_m": base,
        "duct_thickness_m": thick,
    }
    adapter._update_events()
    return adapter.get_events()


def make_ducting_event(
    tier="duct",
    min_gradient=-30,
    severity="priority",
    lat=42.56,
    lon=-114.47,
    surface=False,
    base=1500,
    thick=300,
):
    """A stored ducting tier event dict (mirrors _update_events output)."""
    code = {"super_refraction": "superrefraction", "duct": "duct", "surface_duct": "surfaceduct"}[tier]
    label = {
        "super_refraction": "Super-refraction (enhanced range)",
        "duct": "Tropospheric ducting (elevated)",
        "surface_duct": "Tropospheric ducting (surface)",
    }[tier]
    now = time.time()
    return {
        "source": "ducting",
        "event_id": f"ducting_{code}_{round(lat, 2)}_{round(lon, 2)}",
        "event_type": label,
        "tier": tier,
        "severity": severity,
        "headline": label,
        "min_gradient": min_gradient,
        "duct_base_m": base,
        "duct_thickness_m": thick,
        "surface_duct": surface,
        "lat": lat,
        "lon": lon,
        "expires": now + 6 * 3600,
        "fetched_at": now,
    }


# ============================================================
# TIER CLASSIFICATION TESTS
# ============================================================

def test_normal_emits_nothing(adapter):
    """A normal atmosphere (gradient >= 79) stages no event."""
    evs = set_status(adapter, 118, "normal")
    assert evs == []
    assert adapter._last_tier == "normal"


def test_super_refraction_tier(adapter):
    """0 <= gradient < 79 is super-refraction (routine)."""
    evs = set_status(adapter, 40, "super_refraction")
    assert len(evs) == 1
    assert evs[0]["tier"] == "super_refraction"
    assert evs[0]["severity"] == "routine"


def test_duct_tier(adapter):
    """gradient < 0 (elevated) is ducting (priority)."""
    evs = set_status(adapter, -30, "elevated_duct")
    assert evs[0]["tier"] == "duct"
    assert evs[0]["severity"] == "priority"


def test_surface_duct_tier(adapter):
    """A surface duct condition is the strong/surface tier (immediate)."""
    evs = set_status(adapter, -30, "surface_duct")
    assert evs[0]["tier"] == "surface_duct"
    assert evs[0]["severity"] == "immediate"
    assert evs[0]["surface_duct"] is True


def test_steep_gradient_is_surface_duct(adapter):
    """A very steep gradient (< -100) escalates to the surface/strong tier."""
    evs = set_status(adapter, -150, "elevated_duct")
    assert evs[0]["tier"] == "surface_duct"


def test_update_events_severity_tiering():
    """Fresh-adapter severity per tier: routine / priority / immediate."""
    cases = [(40, "super_refraction", "routine"), (-30, "elevated_duct", "priority"), (-30, "surface_duct", "immediate")]
    for g, cond, sev in cases:
        a = fresh_adapter()
        assert set_status(a, g, cond)[0]["severity"] == sev


# ============================================================
# DEDUP / ESCALATION / DEADBAND (regression guards)
# ============================================================

def test_dedup_id_stable_across_ticks(adapter):
    """REGRESSION: a sustained tier keeps the SAME event_id across ticks."""
    e1 = set_status(adapter, -30, "elevated_duct")[0]["event_id"]
    time.sleep(0.01)
    e2 = set_status(adapter, -28, "elevated_duct")[0]["event_id"]
    assert e1 == e2
    assert e1 == "ducting_duct_42.56_-114.47"


def test_tier_escalation_new_event_id(adapter):
    """An escalation to a stronger tier yields a new (stable) event_id."""
    e_sr = set_status(adapter, 40, "super_refraction")[0]["event_id"]
    e_duct = set_status(adapter, -30, "elevated_duct")[0]["event_id"]
    assert e_sr == "ducting_superrefraction_42.56_-114.47"
    assert e_duct == "ducting_duct_42.56_-114.47"
    assert e_sr != e_duct


def test_deadband_holds_tier_on_zero_boundary_wiggle(adapter):
    """A tiny dip just below 0 (within the 5 M-unit deadband) holds the prior tier."""
    set_status(adapter, 40, "super_refraction")
    assert adapter._last_tier == "super_refraction"
    evs = set_status(adapter, -2, "elevated_duct")  # within deadband of 0
    assert adapter._last_tier == "super_refraction"
    assert evs[0]["tier"] == "super_refraction"
    evs2 = set_status(adapter, -8, "elevated_duct")  # clearly past deadband
    assert adapter._last_tier == "duct"
    assert evs2[0]["tier"] == "duct"


def test_deadband_holds_normal_on_79_boundary_wiggle(adapter):
    """A dip just below 79 (within the deadband) holds normal (no event)."""
    set_status(adapter, 100, "normal")
    assert adapter._last_tier == "normal"
    evs = set_status(adapter, 76, "super_refraction")  # within deadband of 79
    assert adapter._last_tier == "normal"
    assert evs == []
    evs2 = set_status(adapter, 70, "super_refraction")  # past deadband
    assert evs2[0]["tier"] == "super_refraction"


def test_surface_duct_not_held_by_deadband(adapter):
    """The categorical surface duct tier fires promptly regardless of deadband."""
    set_status(adapter, 40, "super_refraction")
    evs = set_status(adapter, -30, "surface_duct")
    assert evs[0]["tier"] == "surface_duct"


# ============================================================
# to_event() — CATEGORY / SEVERITY / KEYS / FIELDS
# ============================================================

def test_tier_categories(adapter):
    """super-refraction -> anomalous prop; (surface_)duct -> ducting enhancement."""
    assert adapter.to_event(make_ducting_event(tier="super_refraction", severity="routine")).category == "rf_anomalous_propagation"
    assert adapter.to_event(make_ducting_event(tier="duct")).category == "rf_ducting_enhancement"
    assert adapter.to_event(make_ducting_event(tier="surface_duct", severity="immediate", surface=True)).category == "rf_ducting_enhancement"


def test_severity_passes_through(adapter):
    for sev in ["routine", "priority", "immediate"]:
        assert adapter.to_event(make_ducting_event(severity=sev)).severity == sev


def test_group_key_and_inhibit_keys(adapter):
    event = adapter.to_event(make_ducting_event(tier="duct"))
    assert event.group_key == "ducting_duct_42.56_-114.47"
    assert event.inhibit_keys == [event.group_key]


def test_populates_core_fields(adapter):
    evt = make_ducting_event(tier="duct", lat=42.56, lon=-114.47)
    event = adapter.to_event(evt)
    assert event.source == "ducting"
    assert event.lat == 42.56
    assert event.lon == -114.47
    assert event.expires == evt["expires"]
    assert event.timestamp == evt["fetched_at"]
    assert event.id  # auto-computed


# ============================================================
# DEFENSIVE / NON-EMIT TESTS
# ============================================================

def test_normal_tier_returns_none(adapter):
    evt = make_ducting_event()
    evt["tier"] = "normal"
    assert adapter.to_event(evt) is None


def test_missing_location_returns_none(adapter):
    evt = make_ducting_event()
    evt["lat"] = None
    assert adapter.to_event(evt) is None


def test_missing_gradient_returns_none(adapter):
    evt = make_ducting_event()
    evt["min_gradient"] = None
    assert adapter.to_event(evt) is None


def test_does_not_raise_on_corrupted_dict(adapter):
    assert adapter.to_event({"garbage": True}) is None
