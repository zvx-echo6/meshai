"""Tests for satpass adapter registration on EnvironmentalConfig.

Verifies:
 - SatpassConfig exists and defaults to feed_source="central"
 - EnvironmentalConfig.satpass field is present and correctly typed
 - _subject_owned() includes central.sat.* subjects when satpass is registered
 - adapter_config REGISTRY contains satpass keys with valid types
"""

import pytest


# -- config model tests -------------------------------------------------------

def test_satpass_config_exists():
    from meshai.config import SatpassConfig
    cfg = SatpassConfig()
    assert cfg.feed_source == "central"
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


def test_environmental_satpass_default_central():
    from meshai.config import EnvironmentalConfig
    env = EnvironmentalConfig()
    assert env.satpass.feed_source == "central"


# -- _subject_owned() integration ---------------------------------------------

def test_subject_owned_includes_satpass_subjects():
    """When EnvironmentalConfig has satpass with feed_source='central',
    _subject_owned() must return subjects containing 'central.sat.'."""
    from meshai.config import EnvironmentalConfig
    from meshai.central.consumer import _SUBJECTS_BARE

    env = EnvironmentalConfig()

    # Simulate what _subject_owned does for the satpass attr
    cfg = getattr(env, "satpass", None)
    assert cfg is not None, "satpass attr missing from EnvironmentalConfig"
    assert getattr(cfg, "feed_source", "native") == "central"

    # Verify _SUBJECTS_BARE has satpass entry
    assert "satpass" in _SUBJECTS_BARE, "satpass missing from _SUBJECTS_BARE"
    subjects = _SUBJECTS_BARE["satpass"]
    assert any("central.sat.pass" in s for s in subjects)
    assert any("central.sat.tle" in s for s in subjects)


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

    yaml_str = "environmental:\n  satpass:\n    enabled: true\n    feed_source: central\n"
    data = yaml.safe_load(yaml_str)
    env = _dict_to_dataclass(EnvironmentalConfig, data["environmental"])
    assert isinstance(env.satpass, SatpassConfig)
    assert env.satpass.enabled is True
    assert env.satpass.feed_source == "central"



# -- _subjects_for() region rewrite table ------------------------------------

def test_subjects_for_satpass_with_region():
    """_subjects_for('satpass', 'us.id') must return region-scoped pass
    subjects and global TLE subject."""
    from meshai.central.consumer import _subjects_for
    result = _subjects_for('satpass', 'us.id')
    assert len(result) == 2, f'Expected 2 subjects, got {len(result)}: {result}'
    assert result[0] == 'central.sat.pass.us.id.>'
    assert result[1] == 'central.sat.tle.>'


def test_subjects_for_satpass_no_region_falls_back():
    """_subjects_for('satpass', None) must return bare-wildcard forms
    from _SUBJECTS_BARE."""
    from meshai.central.consumer import _subjects_for
    result = _subjects_for('satpass', None)
    assert len(result) == 2, f'Expected 2 subjects, got {len(result)}: {result}'
    assert result[0] == 'central.sat.pass.>'
    assert result[1] == 'central.sat.tle.>'


def test_subjects_for_satpass_empty_region_falls_back():
    """_subjects_for('satpass', '') must behave like None — bare wildcards."""
    from meshai.central.consumer import _subjects_for
    result = _subjects_for('satpass', '')
    assert result == _subjects_for('satpass', None)
