"""Tests for Phase 2c coverage wiring: nws + traffic.

Verifies that:
1. traffic: coverage wins (9 grid corridors derived from bbox); None falls back
   to config.corridors.
2. nws: coverage wins for the ``area=`` fetch-scope (``_areas``); None falls back
   to config.areas.

NOTE: the old adapter-level ``_in_coverage`` / ``_coverage_bbox`` geometry
heuristic was removed — the pipeline coverage gate
(``notifications/pipeline/coverage_filter.py``) supersedes it and fails CLOSED
for zone-only weather alerts (see test_coverage_area.py). ``_areas`` remains as
a fetch-scope optimization only.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from meshai.coverage import resolve_adapter_coverage

# ---------------------------------------------------------------------------
# Shared reference bboxes
# ---------------------------------------------------------------------------
IDAHO_BOX = [-116.5, 42.0, -112.0, 44.0]   # Magic Valley / south-central Idaho


def _cov(adapter_name: str, bbox: list = None) -> dict | None:
    return resolve_adapter_coverage(adapter_name, bbox or IDAHO_BOX, "native")


# ===========================================================================
# traffic (TomTomTrafficAdapter)
# ===========================================================================

def _traffic_cfg(corridors=None):
    cfg = MagicMock()
    cfg.api_key = "test-key"
    cfg.corridors = corridors if corridors is not None else []
    cfg.tick_seconds = 300
    return cfg


def test_traffic_coverage_wins():
    """When coverage is provided, _corridors is a 3x3 grid from the bbox."""
    from meshai.env.traffic import TomTomTrafficAdapter
    cov = _cov("traffic")
    assert cov is not None
    assert "points" in cov
    cfg = _traffic_cfg(corridors=[])
    adapter = TomTomTrafficAdapter(cfg, coverage=cov)
    # Must have exactly 9 corridors (3x3 grid)
    assert len(adapter._corridors) == 9
    # Each corridor must be a dict with name, lat, lon
    for i, corridor in enumerate(adapter._corridors):
        assert isinstance(corridor, dict), f"corridor {i} is not a dict"
        assert "name" in corridor, f"corridor {i} missing 'name'"
        assert "lat" in corridor, f"corridor {i} missing 'lat'"
        assert "lon" in corridor, f"corridor {i} missing 'lon'"
    # All names must follow grid_N pattern
    names = [c["name"] for c in adapter._corridors]
    assert names == [f"grid_{i}" for i in range(9)]


def test_traffic_coverage_corridors_inside_bbox():
    """All grid corridor points must fall inside the coverage bbox."""
    from meshai.env.traffic import TomTomTrafficAdapter
    from meshai.coverage import point_in_bbox
    cov = _cov("traffic")
    assert cov is not None
    cfg = _traffic_cfg()
    adapter = TomTomTrafficAdapter(cfg, coverage=cov)
    west, south, east, north = IDAHO_BOX
    for corridor in adapter._corridors:
        lat, lon = corridor["lat"], corridor["lon"]
        assert point_in_bbox(lat, lon, IDAHO_BOX), (
            f"corridor {corridor['name']} ({lat},{lon}) outside bbox"
        )


def test_traffic_fallback_to_config():
    """When coverage=None, _corridors falls back to config.corridors."""
    from meshai.env.traffic import TomTomTrafficAdapter
    config_corridors = [
        {"name": "US-93 Twin Falls", "lat": 42.56, "lon": -114.47},
        {"name": "I-84 Bliss", "lat": 42.93, "lon": -114.98},
    ]
    cfg = _traffic_cfg(corridors=config_corridors)
    adapter = TomTomTrafficAdapter(cfg, coverage=None)
    assert adapter._corridors == config_corridors


# ===========================================================================
# nws (NWSAlertsAdapter)
# ===========================================================================

def _nws_cfg(areas=None):
    cfg = MagicMock()
    cfg.areas = areas if areas is not None else ["ID"]
    cfg.user_agent = "(meshai-test, test@example.com)"
    cfg.severity_min = "moderate"
    cfg.tick_seconds = 60
    return cfg


def test_nws_coverage_areas():
    """When coverage is provided, _areas comes from coverage['areas']."""
    from meshai.env.nws import NWSAlertsAdapter
    cov = _cov("nws")
    assert cov is not None
    assert "areas" in cov
    assert "ID" in cov["areas"], "Idaho box should overlap ID state"
    cfg = _nws_cfg(areas=["WY"])  # deliberately different config default
    adapter = NWSAlertsAdapter(cfg, coverage=cov)
    assert adapter._areas == cov["areas"]


def test_nws_fallback_to_config():
    """When coverage=None, _areas falls back to config.areas."""
    from meshai.env.nws import NWSAlertsAdapter
    cfg = _nws_cfg(areas=["ID", "OR"])
    adapter = NWSAlertsAdapter(cfg, coverage=None)
    assert adapter._areas == ["ID", "OR"]


def test_nws_fallback_config_areas_default():
    """When coverage=None and config.areas is empty/None, falls back to ['ID']."""
    from meshai.env.nws import NWSAlertsAdapter
    cfg = _nws_cfg(areas=None)
    cfg.areas = None  # force the falsy case
    adapter = NWSAlertsAdapter(cfg, coverage=None)
    assert adapter._areas == ["ID"]


def test_nws_empty_areas_fallback_for_api():
    """When derived areas is empty, config areas are used for the API call.

    (The old _coverage_bbox geometry filter was removed; the pipeline coverage
    gate now does geographic filtering. _areas is a fetch-scope hint only.)
    """
    from meshai.env.nws import NWSAlertsAdapter
    # Synthesise a coverage dict whose areas list is empty (bbox outside all states)
    empty_areas_cov = {"areas": [], "bbox": [-1.0, 0.0, 1.0, 1.0]}
    cfg = _nws_cfg(areas=["ID"])
    adapter = NWSAlertsAdapter(cfg, coverage=empty_areas_cov)
    # API area= query falls back to config areas
    assert adapter._areas == ["ID"]
    # The removed geometry filter no longer stashes a bbox on the adapter.
    assert not hasattr(adapter, "_coverage_bbox")
