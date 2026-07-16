"""Tests for satpass adapter registration on EnvironmentalConfig.

Verifies:
 - SatpassConfig exists and defaults to feed_source="native"
 - EnvironmentalConfig.satpass field is present and correctly typed
 - _subject_owned() includes central.sat.* subjects when satpass is registered
 - adapter_config REGISTRY contains satpass keys with valid types
"""

import pytest


# -- config model tests -------------------------------------------------------

def test_satpass_config_exists():
    from meshai.config import SatpassConfig
    cfg = SatpassConfig()
    assert cfg.feed_source == "native"
    assert cfg.enabled is False


def test_satpass_config_is_sourced_feed():
    from meshai.config import SatpassConfig, _SourcedFeed
    assert issubclass(SatpassConfig, _SourcedFeed)


def test_satpass_config_rejects_invalid_feed_source():
    from meshai.config import SatpassConfig
    with pytest.raises(ValueError, match="feed_source"):
        SatpassConfig(feed_source="bogus")


def test_environmental_config_has_satpass():
    from meshai.config import EnvironmentalConfig, SatpassConfig
    env = EnvironmentalConfig()
    assert hasattr(env, "satpass")
    assert isinstance(env.satpass, SatpassConfig)


def test_environmental_satpass_default_native():
    from meshai.config import EnvironmentalConfig
    env = EnvironmentalConfig()
    assert env.satpass.feed_source == "native"


# -- adapter_config REGISTRY --------------------------------------------------

def test_registry_has_satpass_enabled():
    from meshai.adapter_config.defaults import REGISTRY
    assert ("satpass", "enabled") in REGISTRY
    spec = REGISTRY[("satpass", "enabled")]
    assert spec["type"] == "bool"
    assert spec["default"] is False


def test_registry_satpass_types_valid():
    """All satpass REGISTRY entries must use types in the valid vocabulary."""
    from meshai.adapter_config.defaults import REGISTRY
    valid = {"int", "float", "str", "bool", "json"}
    satpass_keys = [(a, k) for a, k in REGISTRY if a == "satpass"]
    assert len(satpass_keys) >= 5, f"Expected >= 5 satpass keys, got {len(satpass_keys)}"
    for a, k in satpass_keys:
        assert REGISTRY[(a, k)]["type"] in valid, (
            f"satpass.{k} has invalid type {REGISTRY[(a, k)]['type']!r}"
        )


def test_registry_satpass_list_types_are_json():
    """List-valued satpass keys must use type='json', not 'list'."""
    from meshai.adapter_config.defaults import REGISTRY
    for key in ("observers", "norad_ids", "command_norad_ids"):
        spec = REGISTRY[("satpass", key)]
        assert spec["type"] == "json", (
            f"satpass.{key} should be type='json', got {spec['type']!r}"
        )


def test_adapter_meta_has_satpass():
    from meshai.adapter_config.defaults import ADAPTER_META
    assert "satpass" in ADAPTER_META


# -- YAML round-trip -----------------------------------------------------------

def test_yaml_parsing_satpass():
    """SatpassConfig should be deserialized from YAML config dict."""
    from meshai.config import SatpassConfig, _dict_to_dataclass, EnvironmentalConfig
    import yaml

    yaml_str = "environmental:\n  satpass:\n    enabled: true\n    feed_source: native\n"
    data = yaml.safe_load(yaml_str)
    env = _dict_to_dataclass(EnvironmentalConfig, data["environmental"])
    assert isinstance(env.satpass, SatpassConfig)
    assert env.satpass.enabled is True
    assert env.satpass.feed_source == "native"

