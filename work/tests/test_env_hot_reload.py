"""Config hot-reload tests for EnvironmentalStore.apply_config().

A config PUT to the "environmental" (or "generic_sources") section should
take effect LIVE -- only the adapter(s) whose own config actually changed are
rebuilt in place, on the SAME running store, with no container restart:

  * an unchanged adapter's object identity is preserved (untouched);
  * a changed adapter (still feed_source=="native") is rebuilt in place and
    the NEW instance carries the new config values;
  * store-level dedup/seen state (self._seen, self._seeded, ...) is keyed by
    event source, not by adapter object, and survives a swap unchanged;
  * flipping feed_source to/from "central" is the one case that still needs
    a restart -- it must NOT hot-swap;
  * rebuilding "nifc" cascades to also rebuild "firms" (it holds a hard
    reference to the nifc adapter instance).
"""
from __future__ import annotations

import dataclasses

from meshai.config import EnvironmentalConfig
from meshai.env.store import EnvironmentalStore


def _cfg(**overrides) -> EnvironmentalConfig:
    """A minimal EnvironmentalConfig with usgs_quake enabled+native (has a
    plain feed_url field, no network I/O at construction) so tests have a
    real adapter to diff/swap, plus whatever overrides the test needs.
    """
    cfg = EnvironmentalConfig()
    cfg.usgs_quake = dataclasses.replace(
        cfg.usgs_quake, enabled=True, feed_source="native",
        feed_url="https://example.invalid/quakes-a.geojson",
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def test_unchanged_adapter_config_is_a_noop():
    store = EnvironmentalStore(_cfg())
    nws_before = store._adapters["nws"]
    usgs_quake_before = store._adapters["usgs_quake"]

    same_cfg = _cfg()  # byte-identical field values, fresh dataclass instances
    result = store.apply_config(same_cfg)

    assert result["nws"] == "unchanged"
    assert result["usgs_quake"] == "unchanged"
    assert store._adapters["nws"] is nws_before
    assert store._adapters["usgs_quake"] is usgs_quake_before


def test_changed_feed_url_swaps_adapter_and_preserves_dedup_state():
    store = EnvironmentalStore(_cfg())
    nws_before = store._adapters["nws"]
    usgs_quake_before = store._adapters["usgs_quake"]
    assert usgs_quake_before._feed_url == "https://example.invalid/quakes-a.geojson"

    # Store-level dedup state, keyed by event SOURCE -- must survive a swap.
    store._seen["usgs_quake"] = {"usgs_quake\x1eeid:us1000aaaa"}
    store._seeded.add("usgs_quake")

    new_cfg = _cfg()
    new_cfg.usgs_quake = dataclasses.replace(
        new_cfg.usgs_quake, feed_url="https://example.invalid/quakes-b.geojson")

    result = store.apply_config(new_cfg)

    assert result["usgs_quake"] == "reloaded"
    assert result["nws"] == "unchanged"

    usgs_quake_after = store._adapters["usgs_quake"]
    assert usgs_quake_after is not usgs_quake_before, "changed adapter must be a NEW object"
    assert usgs_quake_after._feed_url == "https://example.invalid/quakes-b.geojson", \
        "the new instance must carry the new config value"

    # Sibling adapter untouched (identity preserved).
    assert store._adapters["nws"] is nws_before

    # Store-level dedup/seen state survives the swap unchanged.
    assert store._seen["usgs_quake"] == {"usgs_quake\x1eeid:us1000aaaa"}
    assert "usgs_quake" in store._seeded


def test_disabling_an_adapter_removes_it_live():
    store = EnvironmentalStore(_cfg())
    assert "usgs_quake" in store._adapters

    new_cfg = _cfg()
    new_cfg.usgs_quake = dataclasses.replace(new_cfg.usgs_quake, enabled=False)
    result = store.apply_config(new_cfg)

    assert result["usgs_quake"] == "reloaded"
    assert "usgs_quake" not in store._adapters


def test_feed_source_flip_to_central_requires_restart_and_does_not_swap():
    store = EnvironmentalStore(_cfg())
    nws_before = store._adapters["nws"]

    new_cfg = _cfg()
    new_cfg.nws = dataclasses.replace(new_cfg.nws, feed_source="central")
    result = store.apply_config(new_cfg)

    assert result["nws"] == "restart_required"
    assert store._adapters["nws"] is nws_before, "must NOT hot-swap across the Central boundary"

    # Repeated PUTs with the same (still-unapplied) value keep reporting
    # restart_required rather than silently settling to "unchanged".
    result2 = store.apply_config(new_cfg)
    assert result2["nws"] == "restart_required"
    assert store._adapters["nws"] is nws_before


def test_feed_source_flip_from_central_to_native_requires_restart():
    # satpass now defaults to feed_source="native", so pin the starting state
    # to "central" explicitly -- this exercises the flip, not the default.
    base_cfg = _cfg()
    base_cfg.satpass = dataclasses.replace(base_cfg.satpass, feed_source="central")
    store = EnvironmentalStore(base_cfg)
    assert "satpass" not in store._adapters  # central -> no native instance

    new_cfg = _cfg()
    new_cfg.satpass = dataclasses.replace(
        new_cfg.satpass, enabled=True, feed_source="native")
    result = store.apply_config(new_cfg)

    assert result["satpass"] == "restart_required"
    assert result["satpass_tle"] == "restart_required"
    assert "satpass" not in store._adapters
    assert "satpass_tle" not in store._adapters


def test_nifc_rebuild_cascades_to_firms():
    cfg = _cfg()
    cfg.fires = dataclasses.replace(cfg.fires, enabled=True, feed_source="native")
    cfg.firms = dataclasses.replace(
        cfg.firms, enabled=True, feed_source="native", map_key="test-key")

    store = EnvironmentalStore(cfg)
    nifc_before = store._adapters["nifc"]
    firms_before = store._adapters["firms"]
    assert firms_before._fires_adapter is nifc_before

    # Change something on the "fires" (nifc) config only; firms' OWN config
    # is untouched.
    new_cfg = _cfg()
    new_cfg.fires = dataclasses.replace(cfg.fires, state="US-NV")
    new_cfg.firms = cfg.firms  # byte-identical

    result = store.apply_config(new_cfg)

    assert result["nifc"] == "reloaded"
    assert result["firms"] == "reloaded", \
        "firms must cascade-rebuild even though its own config didn't change"

    nifc_after = store._adapters["nifc"]
    firms_after = store._adapters["firms"]
    assert nifc_after is not nifc_before
    assert firms_after is not firms_before
    assert firms_after._fires_adapter is nifc_after, \
        "firms must hold the FRESH nifc reference, not the stale one"


def test_generic_sources_change_rebuilds_generic_http_only():
    store = EnvironmentalStore(
        _cfg(), generic_sources=[{"name": "a", "url": "https://a.example"}])
    generic_before = store._adapters.get("generic_http")
    nws_before = store._adapters["nws"]

    same_cfg = _cfg()
    result = store.apply_config(
        same_cfg, generic_sources=[{"name": "a", "url": "https://a.example"}])
    assert result["generic_http"] == "unchanged"
    assert store._adapters.get("generic_http") is generic_before

    result2 = store.apply_config(
        same_cfg, generic_sources=[{"name": "a", "url": "https://a-changed.example"}])
    assert result2["generic_http"] == "reloaded"
    assert result2["nws"] == "unchanged"
    assert store._adapters["nws"] is nws_before
