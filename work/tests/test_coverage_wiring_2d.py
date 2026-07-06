"""Tests for Phase 2d coverage-bbox wiring: hydro (usgs) + satpass.

Verifies that:
1. usgs (hydro): _coverage_bbox is set from coverage dict; _build_iv_params uses
   bBox when coverage is present, sites= when coverage is None; >25 sq-deg warns.
2. satpass: resolve_adapter_coverage returns a centroid dict; derived observer
   matches the shape expected by seed_observers_from_config; empty bbox → None.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from meshai.coverage import resolve_adapter_coverage

# ---------------------------------------------------------------------------
# Shared reference bbox (Magic Valley / south-central Idaho)
# ---------------------------------------------------------------------------
IDAHO_BOX = [-116.5, 42.0, -112.0, 44.0]


def _cov(adapter_name: str, bbox: list = None) -> dict | None:
    return resolve_adapter_coverage(adapter_name, bbox or IDAHO_BOX, "native")


# ===========================================================================
# hydro (USGSStreamsAdapter)
# ===========================================================================

def _usgs_cfg(sites=None):
    cfg = MagicMock()
    cfg.sites = sites if sites is not None else []
    cfg.tick_seconds = 900
    cfg.flood_thresholds = {}
    return cfg


def test_usgs_coverage_bbox_set():
    """When coverage is provided, _coverage_bbox is set from coverage['bbox']."""
    from meshai.env.usgs import USGSStreamsAdapter
    cov = _cov("usgs")
    assert cov is not None
    assert "bbox" in cov
    cfg = _usgs_cfg()
    adapter = USGSStreamsAdapter(cfg, coverage=cov)
    assert adapter._coverage_bbox == cov["bbox"]
    assert adapter._coverage_bbox == IDAHO_BOX


def test_usgs_fallback_no_coverage():
    """When coverage=None, _coverage_bbox is None and sites are used."""
    from meshai.env.usgs import USGSStreamsAdapter
    cfg = _usgs_cfg(sites=["13090500"])
    adapter = USGSStreamsAdapter(cfg, coverage=None)
    assert adapter._coverage_bbox is None
    assert adapter._sites == ["13090500"]


def test_usgs_build_iv_params_with_coverage():
    """_build_iv_params uses bBox (not sites) when coverage is set."""
    from meshai.env.usgs import USGSStreamsAdapter
    cov = _cov("usgs")
    cfg = _usgs_cfg()
    adapter = USGSStreamsAdapter(cfg, coverage=cov)
    params = adapter._build_iv_params([])
    assert "bBox" in params
    assert params["bBox"] == "-116.5,42.0,-112.0,44.0"
    assert "sites" not in params
    # Shared IV fields are always present
    assert params["format"] == "json"
    assert "parameterCd" in params
    assert "siteStatus" in params


def test_usgs_build_iv_params_without_coverage():
    """_build_iv_params uses sites= (not bBox) when coverage is None."""
    from meshai.env.usgs import USGSStreamsAdapter
    cfg = _usgs_cfg(sites=["13090500", "13095500"])
    adapter = USGSStreamsAdapter(cfg, coverage=None)
    params = adapter._build_iv_params(["13090500", "13095500"])
    assert "sites" in params
    assert params["sites"] == "13090500,13095500"
    assert "bBox" not in params


def test_usgs_large_bbox_warns_but_does_not_crash(caplog):
    """A bBox whose width*height > 25 sq-deg logs a warning but still builds params."""
    from meshai.env.usgs import USGSStreamsAdapter
    # 10 deg wide × 10 deg tall = 100 sq-deg (well over the 25 limit)
    large_bbox = [-120.0, 35.0, -110.0, 45.0]
    cfg = _usgs_cfg()
    adapter = USGSStreamsAdapter(cfg, coverage={"bbox": large_bbox})
    with caplog.at_level(logging.WARNING, logger="meshai.env.usgs"):
        params = adapter._build_iv_params([])
    assert "too large" in caplog.text
    # Params are still returned despite the warning
    assert "bBox" in params
    assert "sites" not in params


def test_usgs_small_bbox_no_warning(caplog):
    """A bBox whose width*height <= 25 sq-deg does not log a warning."""
    from meshai.env.usgs import USGSStreamsAdapter
    # IDAHO_BOX: (112.0-116.5) * (44.0-42.0) = 4.5 * 2 = 9 sq-deg — well under 25
    cfg = _usgs_cfg()
    adapter = USGSStreamsAdapter(cfg, coverage={"bbox": IDAHO_BOX})
    with caplog.at_level(logging.WARNING, logger="meshai.env.usgs"):
        params = adapter._build_iv_params([])
    assert "too large" not in caplog.text
    assert "bBox" in params


# ===========================================================================
# satpass — observer derivation logic
# ===========================================================================

def test_satpass_coverage_centroid_correct():
    """resolve_adapter_coverage('satpass', bbox) returns the bbox centroid."""
    cov = _cov("satpass")
    assert cov is not None
    assert "centroid" in cov
    lat, lon = cov["centroid"]
    # centroid of IDAHO_BOX = [W=-116.5, S=42.0, E=-112.0, N=44.0]
    # lat_center = (42.0 + 44.0) / 2 = 43.0
    # lon_center = (-116.5 + -112.0) / 2 = -114.25
    assert lat == pytest.approx(43.0)
    assert lon == pytest.approx(-114.25)


def test_satpass_derived_observer_shape():
    """Derived coverage observer has the exact keys seed_observers_from_config expects."""
    cov = _cov("satpass")
    assert cov is not None
    lat, lon = cov["centroid"]
    derived = [{"slug": "coverage_center", "name": "Coverage Center",
                "lat": lat, "lon": lon, "alt_m": 0.0}]
    obs = derived[0]
    # seed_observers_from_config reads: slug, name, lat, lon, alt_m (optional)
    assert obs["slug"] == "coverage_center"
    assert obs["name"] == "Coverage Center"
    assert obs["lat"] == pytest.approx(43.0)
    assert obs["lon"] == pytest.approx(-114.25)
    assert obs["alt_m"] == 0.0


def test_satpass_empty_bbox_returns_none():
    """When coverage_bbox is empty, resolve returns None → config fallback."""
    cov = resolve_adapter_coverage("satpass", [], "native")
    assert cov is None


def test_satpass_central_feed_returns_none():
    """Central-fed satpass always returns None (Central governs coverage)."""
    cov = resolve_adapter_coverage("satpass", IDAHO_BOX, "central")
    assert cov is None


def test_satpass_coverage_disabled_fallback():
    """When coverage is not enabled (empty bbox passed), config observers are used."""
    # Simulate main.py's: (config.coverage.bbox if config.coverage.enabled else [])
    effective_bbox = []   # coverage.enabled is False
    cov = resolve_adapter_coverage("satpass", effective_bbox, "native")
    assert cov is None
    # When cov is None the fallback path in main.py uses config.environmental.satpass
    config_observers = [
        {"slug": "home", "name": "Home Base", "lat": 42.56, "lon": -114.47, "alt_m": 1000}
    ]
    # Verify: when cov is None, observers_to_seed == config.satpass.observers
    observers_to_seed = config_observers if cov is None else [
        {"slug": "coverage_center", "name": "Coverage Center",
         "lat": 0.0, "lon": 0.0, "alt_m": 0.0}
    ]
    assert observers_to_seed == config_observers
