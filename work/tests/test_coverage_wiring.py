"""Tests for Phase 2a coverage-bbox wiring.

Verifies that:
1. Each of the four simple adapters respects the `coverage` kwarg — derived
   scope wins when coverage is provided, config field is the fallback when None.
2. EnvironmentalStore._coverage_for returns a dict for a valid bbox and None
   for an empty bbox.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from meshai.coverage import resolve_adapter_coverage

# ---------------------------------------------------------------------------
# Shared reference bbox (Magic Valley / south-central Idaho)
# ---------------------------------------------------------------------------
IDAHO_BOX = [-116.5, 42.0, -112.0, 44.0]


# ---------------------------------------------------------------------------
# Helpers — derive the expected coverage dicts from the module itself
# ---------------------------------------------------------------------------

def _cov(adapter_name: str, bbox: list = None) -> dict | None:
    return resolve_adapter_coverage(adapter_name, bbox or IDAHO_BOX, "native")


# ===========================================================================
# usgs_quake
# ===========================================================================

def _quake_cfg(bbox=None):
    cfg = MagicMock()
    cfg.feed_url = "https://example.test/feed.geojson"
    cfg.min_magnitude = 2.5
    cfg.bbox = bbox if bbox is not None else [-115.5, 42.0, -110.0, 45.2]
    cfg.region = "magic_valley"
    cfg.tick_seconds = 300
    return cfg


def test_usgs_quake_coverage_wins():
    """When coverage is provided, adapter._bbox comes from coverage['bbox']."""
    from meshai.env.usgs_quake import USGSQuakeAdapter
    cov = _cov("usgs_quake")
    assert cov is not None
    adapter = USGSQuakeAdapter(_quake_cfg(), coverage=cov)
    assert adapter._bbox == cov["bbox"]


def test_usgs_quake_fallback_to_config():
    """When coverage=None, adapter._bbox falls back to config.bbox."""
    from meshai.env.usgs_quake import USGSQuakeAdapter
    cfg = _quake_cfg(bbox=[-115.5, 42.0, -110.0, 45.2])
    adapter = USGSQuakeAdapter(cfg, coverage=None)
    assert adapter._bbox == cfg.bbox


# ===========================================================================
# roads511
# ===========================================================================

def _roads_cfg(bbox=None):
    cfg = MagicMock()
    cfg.api_key = ""
    cfg.base_url = ""
    cfg.endpoints = ["/get/event"]
    cfg.bbox = bbox if bbox is not None else [-115.0, 42.5, -111.0, 44.0]
    cfg.tick_seconds = 300
    return cfg


def test_roads511_coverage_wins():
    """When coverage is provided, adapter._bbox comes from coverage['bbox']."""
    from meshai.env.roads511 import Roads511Adapter
    cov = _cov("roads511")
    assert cov is not None
    adapter = Roads511Adapter(_roads_cfg(), coverage=cov)
    assert adapter._bbox == cov["bbox"]


def test_roads511_fallback_to_config():
    """When coverage=None, adapter._bbox falls back to config.bbox."""
    from meshai.env.roads511 import Roads511Adapter
    cfg = _roads_cfg(bbox=[-115.0, 42.5, -111.0, 44.0])
    adapter = Roads511Adapter(cfg, coverage=None)
    assert adapter._bbox == cfg.bbox


# ===========================================================================
# wzdx
# ===========================================================================

def _wzdx_cfg(states=None, bbox=None):
    cfg = MagicMock()
    cfg.api_key = ""
    cfg.base_url = ""
    cfg.registry_url = "https://example.test/registry.json"
    cfg.registry_ttl = 21600
    cfg.tick_seconds = 300
    cfg.states = states if states is not None else ["ID"]
    cfg.bbox = bbox if bbox is not None else []
    return cfg


def test_wzdx_coverage_wins():
    """When coverage is provided, both _states and _bbox come from coverage."""
    from meshai.env.wzdx import WZDxAdapter
    cov = _cov("wzdx")
    assert cov is not None
    assert "states" in cov
    assert "bbox" in cov
    adapter = WZDxAdapter(_wzdx_cfg(), coverage=cov)
    assert adapter._states == cov["states"]
    assert adapter._bbox == cov["bbox"]


def test_wzdx_fallback_to_config():
    """When coverage=None, _states and _bbox fall back to config fields."""
    from meshai.env.wzdx import WZDxAdapter
    cfg = _wzdx_cfg(states=["ID", "MT"], bbox=[-116.0, 42.0, -111.0, 45.0])
    adapter = WZDxAdapter(cfg, coverage=None)
    assert "ID" in adapter._states
    assert "MT" in adapter._states
    assert adapter._bbox == cfg.bbox


def test_wzdx_empty_coverage_states_wins():
    """Empty derived states list still wins over config (coverage governs)."""
    from meshai.env.wzdx import WZDxAdapter
    # Provide a bbox that maps to no states (unlikely but logically possible)
    empty_states_cov = {"states": [], "bbox": [-1.0, 0.0, 1.0, 1.0]}
    cfg = _wzdx_cfg(states=["ID"])
    adapter = WZDxAdapter(cfg, coverage=empty_states_cov)
    assert adapter._states == []  # coverage governs even when empty


# ===========================================================================
# avalanche
# ===========================================================================

def _avy_cfg(center_ids=None):
    cfg = MagicMock()
    cfg.center_ids = center_ids if center_ids is not None else ["SNFAC"]
    cfg.tick_seconds = 1800
    cfg.season_months = [12, 1, 2, 3, 4]
    return cfg


def test_avalanche_coverage_wins():
    """When coverage is provided, adapter._center_ids comes from coverage['center_ids']."""
    from meshai.env.avalanche import AvalancheAdapter
    cov = _cov("avalanche")
    assert cov is not None
    assert "center_ids" in cov
    adapter = AvalancheAdapter(_avy_cfg(), coverage=cov)
    assert adapter._center_ids == cov["center_ids"]


def test_avalanche_fallback_to_config():
    """When coverage=None, adapter._center_ids falls back to config.center_ids."""
    from meshai.env.avalanche import AvalancheAdapter
    cfg = _avy_cfg(center_ids=["SNFAC", "PAC"])
    adapter = AvalancheAdapter(cfg, coverage=None)
    assert adapter._center_ids == ["SNFAC", "PAC"]


def test_avalanche_empty_coverage_center_ids_wins():
    """Empty derived center_ids list wins over config (coverage governs)."""
    from meshai.env.avalanche import AvalancheAdapter
    # A bbox in the Pacific would yield no centers
    no_center_cov = {"center_ids": []}
    cfg = _avy_cfg(center_ids=["SNFAC"])
    adapter = AvalancheAdapter(cfg, coverage=no_center_cov)
    assert adapter._center_ids == []  # coverage governs even when empty


# ===========================================================================
# EnvironmentalStore._coverage_for
# ===========================================================================

def _make_env_cfg(enabled=False):
    """Minimal EnvironmentalConfig mock with all adapters disabled."""
    env_cfg = MagicMock()
    for attr in ("nws", "swpc", "ducting", "fires", "avalanche", "usgs",
                 "usgs_quake", "traffic", "roads511", "wzdx", "satpass", "firms"):
        sub = MagicMock()
        sub.enabled = False
        sub.feed_source = "central"
        setattr(env_cfg, attr, sub)
    env_cfg.nws_zones = []
    return env_cfg


def test_coverage_for_returns_dict_for_valid_bbox():
    """_coverage_for returns a dict when coverage_bbox is a valid box."""
    from meshai.env.store import EnvironmentalStore
    env_cfg = _make_env_cfg()
    store = EnvironmentalStore(config=env_cfg, coverage_bbox=IDAHO_BOX)
    result = store._coverage_for("usgs_quake")
    assert isinstance(result, dict)
    assert "bbox" in result
    assert result["bbox"] == IDAHO_BOX


def test_coverage_for_returns_none_for_empty_bbox():
    """_coverage_for returns None when coverage_bbox is empty."""
    from meshai.env.store import EnvironmentalStore
    env_cfg = _make_env_cfg()
    store = EnvironmentalStore(config=env_cfg, coverage_bbox=[])
    result = store._coverage_for("usgs_quake")
    assert result is None


def test_coverage_for_avalanche_returns_center_ids():
    """_coverage_for('avalanche') returns center_ids for a valid Idaho box."""
    from meshai.env.store import EnvironmentalStore
    env_cfg = _make_env_cfg()
    store = EnvironmentalStore(config=env_cfg, coverage_bbox=IDAHO_BOX)
    result = store._coverage_for("avalanche")
    assert result is not None
    assert "center_ids" in result
    assert isinstance(result["center_ids"], list)
    # SNFAC (Sawtooth) covers south-central Idaho — should be in the result
    assert "SNFAC" in result["center_ids"]


def test_coverage_for_wzdx_returns_states_and_bbox():
    """_coverage_for('wzdx') returns both states and bbox."""
    from meshai.env.store import EnvironmentalStore
    env_cfg = _make_env_cfg()
    store = EnvironmentalStore(config=env_cfg, coverage_bbox=IDAHO_BOX)
    result = store._coverage_for("wzdx")
    assert result is not None
    assert "states" in result and "bbox" in result
    assert "ID" in result["states"]
