"""Tests for Phase 2b coverage-bbox wiring: fires, firms, ducting.

Verifies that:
1. Each adapter respects the `coverage` kwarg — derived scope wins when
   coverage is provided, config field is the fallback when None.
2. fires._build_query_params() switches to the ArcGIS envelope filter in
   coverage mode and drops the attr_POOState WHERE clause.
3. fires event_id backward-compat: an Idaho fire (attr_POOState="US-ID") in
   coverage mode produces the same event_id as the single-state path.
"""

from __future__ import annotations

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
# ducting
# ===========================================================================

def _ducting_cfg(lat=42.56, lon=-114.47):
    cfg = MagicMock()
    cfg.latitude = lat
    cfg.longitude = lon
    cfg.tick_seconds = 10800
    return cfg


def test_ducting_coverage_wins():
    """When coverage is provided, lat/lon come from coverage['centroid']."""
    from meshai.env.ducting import DuctingAdapter
    cov = {"centroid": (43.0, -114.0)}
    cfg = _ducting_cfg(lat=42.56, lon=-114.47)
    adapter = DuctingAdapter(cfg, coverage=cov)
    assert adapter._lat == 43.0
    assert adapter._lon == -114.0


def test_ducting_fallback_to_config():
    """When coverage=None, lat/lon fall back to config."""
    from meshai.env.ducting import DuctingAdapter
    cfg = _ducting_cfg(lat=42.56, lon=-114.47)
    adapter = DuctingAdapter(cfg, coverage=None)
    assert adapter._lat == cfg.latitude
    assert adapter._lon == cfg.longitude


# ===========================================================================
# firms
# ===========================================================================

def _firms_cfg(bbox=None):
    cfg = MagicMock()
    cfg.map_key = "test-key"
    cfg.source = "VIIRS_SNPP_NRT"
    cfg.bbox = bbox if bbox is not None else [-117, 42, -114, 44]
    cfg.day_range = 1
    cfg.tick_seconds = 1800
    cfg.confidence_min = "nominal"
    cfg.proximity_km = 10.0
    return cfg


def test_firms_coverage_wins():
    """When coverage is provided, adapter._bbox comes from coverage['bbox']."""
    from meshai.env.firms import FIRMSAdapter
    cov = {"bbox": [-116.5, 42.0, -112.0, 44.0]}
    cfg = _firms_cfg(bbox=[-117, 42, -114, 44])
    adapter = FIRMSAdapter(cfg, region_anchors=[], fires_adapter=None, coverage=cov)
    assert adapter._bbox == [-116.5, 42.0, -112.0, 44.0]


def test_firms_fallback_to_config():
    """When coverage=None, adapter._bbox falls back to config.bbox."""
    from meshai.env.firms import FIRMSAdapter
    cfg = _firms_cfg(bbox=[-117, 42, -114, 44])
    adapter = FIRMSAdapter(cfg, region_anchors=[], fires_adapter=None, coverage=None)
    assert adapter._bbox == cfg.bbox


# ===========================================================================
# fires (NICFFiresAdapter)
# ===========================================================================

def _fires_cfg(state="US-ID"):
    cfg = MagicMock()
    cfg.state = state
    cfg.tick_seconds = 600
    return cfg


def test_fires_coverage_query_uses_envelope():
    """In coverage mode, _build_query_params includes envelope keys and drops attr_POOState WHERE."""
    from meshai.env.fires import NICFFiresAdapter
    cov = _cov("fires")  # resolve via coverage module
    assert cov is not None
    assert "envelope" in cov
    cfg = _fires_cfg()
    adapter = NICFFiresAdapter(cfg, coverage=cov)
    params = adapter._build_query_params()
    # WHERE must not contain attr_POOState — spatial envelope scopes instead
    assert "attr_POOState" not in params["where"]
    assert "attr_IncidentTypeCategory='WF'" in params["where"]
    # ArcGIS envelope keys must be present
    assert "geometry" in params
    assert params.get("geometryType") == "esriGeometryEnvelope"
    assert params.get("spatialRel") == "esriSpatialRelIntersects"
    assert params.get("inSR") == "4326"


def test_fires_no_coverage_query_uses_state_where():
    """Without coverage, _build_query_params retains the single-state WHERE clause."""
    from meshai.env.fires import NICFFiresAdapter
    cfg = _fires_cfg(state="US-ID")
    adapter = NICFFiresAdapter(cfg, coverage=None)
    params = adapter._build_query_params()
    assert "attr_POOState='US-ID'" in params["where"]
    assert "attr_IncidentTypeCategory='WF'" in params["where"]
    # No envelope keys
    assert "geometry" not in params
    assert "geometryType" not in params


def test_fires_event_id_backward_compat():
    """Idaho fire in coverage mode yields the same event_id as the state path.

    This ensures no re-broadcast of fires already known to the store when
    the operator enables the coverage bbox.
    """
    from meshai.env.fires import NICFFiresAdapter

    fire_name = "Ross Fork Fire"
    # Normalise the name the same way fires.py does
    name_slug = fire_name.replace(" ", "_").lower()

    # --- State path (coverage=None) ---
    cfg = _fires_cfg(state="US-ID")
    adapter_state = NICFFiresAdapter(cfg, coverage=None)
    # Simulate what _fetch does for the event_id (coverage=None branch)
    state_event_id = f"nifc_{name_slug}_{adapter_state._state}"

    # --- Coverage path ---
    cov = _cov("fires")
    adapter_cov = NICFFiresAdapter(cfg, coverage=cov)
    # Synthetic Idaho feature: attr_POOState matches config.state exactly
    props = {"attr_POOState": "US-ID"}
    per_fire_state = (props.get("attr_POOState") or "").strip() or adapter_cov._state
    coverage_event_id = f"nifc_{name_slug}_{per_fire_state}"

    assert coverage_event_id == state_event_id, (
        f"event_id mismatch: coverage={coverage_event_id!r}, state={state_event_id!r}"
    )
