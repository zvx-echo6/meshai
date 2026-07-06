"""Tests for Phase 3a coverage override (per-adapter excluded_adapters escape hatch).

Verifies:
1. Coverage.excluded_adapters defaults to [].
2. A store built with coverage_bbox + coverage_excluded=["traffic"]:
   - _coverage_for("traffic") returns None (excluded).
   - _coverage_for("usgs_quake") returns a dict (non-excluded still derives).
3. With coverage_excluded=[]: _coverage_for("traffic") returns a dict.
"""

from __future__ import annotations

from dataclasses import field
from unittest.mock import MagicMock

import pytest

# Shared reference bbox (Magic Valley / south-central Idaho)
IDAHO_BOX = [-116.5, 42.0, -112.0, 44.0]


# ---------------------------------------------------------------------------
# Helpers — minimal EnvironmentalConfig mock (mirrors test_coverage_wiring.py)
# ---------------------------------------------------------------------------

def _make_env_cfg():
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


# ---------------------------------------------------------------------------
# Coverage dataclass
# ---------------------------------------------------------------------------

def test_coverage_excluded_adapters_default():
    """Coverage.excluded_adapters defaults to an empty list."""
    from meshai.config import Coverage
    cov = Coverage()
    assert cov.excluded_adapters == []


# ---------------------------------------------------------------------------
# EnvironmentalStore exclusion gate
# ---------------------------------------------------------------------------

def test_coverage_for_excluded_adapter_returns_none():
    """_coverage_for returns None for an adapter in coverage_excluded."""
    from meshai.env.store import EnvironmentalStore
    env_cfg = _make_env_cfg()
    store = EnvironmentalStore(
        config=env_cfg,
        coverage_bbox=IDAHO_BOX,
        coverage_excluded=["traffic"],
    )
    result = store._coverage_for("traffic")
    assert result is None


def test_coverage_for_non_excluded_adapter_returns_dict():
    """_coverage_for returns a dict for adapters NOT in coverage_excluded."""
    from meshai.env.store import EnvironmentalStore
    env_cfg = _make_env_cfg()
    store = EnvironmentalStore(
        config=env_cfg,
        coverage_bbox=IDAHO_BOX,
        coverage_excluded=["traffic"],
    )
    result = store._coverage_for("usgs_quake")
    assert isinstance(result, dict)
    assert "bbox" in result
    assert result["bbox"] == IDAHO_BOX


def test_coverage_for_empty_excluded_list_returns_dict():
    """With coverage_excluded=[], _coverage_for returns a dict for any adapter that supports coverage."""
    from meshai.env.store import EnvironmentalStore
    env_cfg = _make_env_cfg()
    store = EnvironmentalStore(
        config=env_cfg,
        coverage_bbox=IDAHO_BOX,
        coverage_excluded=[],
    )
    result = store._coverage_for("traffic")
    # traffic adapter IS wired to coverage (returns a dict when bbox is set)
    assert isinstance(result, dict)


def test_coverage_excluded_set_stored_as_set():
    """_coverage_excluded is stored as a set for O(1) lookup."""
    from meshai.env.store import EnvironmentalStore
    env_cfg = _make_env_cfg()
    store = EnvironmentalStore(
        config=env_cfg,
        coverage_bbox=IDAHO_BOX,
        coverage_excluded=["traffic", "satpass"],
    )
    assert isinstance(store._coverage_excluded, set)
    assert "traffic" in store._coverage_excluded
    assert "satpass" in store._coverage_excluded


def test_coverage_excluded_none_defaults_to_empty_set():
    """When coverage_excluded is not passed, _coverage_excluded is an empty set."""
    from meshai.env.store import EnvironmentalStore
    env_cfg = _make_env_cfg()
    store = EnvironmentalStore(config=env_cfg, coverage_bbox=IDAHO_BOX)
    assert store._coverage_excluded == set()
