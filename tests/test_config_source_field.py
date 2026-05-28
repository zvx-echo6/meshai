"""v0.4 C.1: per-adapter `source` field + CentralConsumerConfig."""

import pytest

from meshai.config import (
    NWSConfig, FIRMSConfig, USGSQuakeConfig,
    EnvironmentalConfig, CentralConsumerConfig,
)

_ADAPTERS = ("nws", "swpc", "ducting", "fires", "avalanche",
             "usgs", "usgs_quake", "traffic", "roads511", "firms")


def test_source_defaults_native():
    assert NWSConfig().feed_source == "native"
    assert FIRMSConfig().feed_source == "native"
    assert USGSQuakeConfig().feed_source == "native"


def test_all_adapters_default_native():
    env = EnvironmentalConfig()
    for attr in _ADAPTERS:
        assert getattr(env, attr).feed_source == "native", attr


def test_source_central_validates():
    assert NWSConfig(feed_source="central").feed_source == "central"
    assert USGSQuakeConfig(feed_source="central").feed_source == "central"


def test_source_garbage_rejects():
    with pytest.raises(ValueError):
        NWSConfig(feed_source="garbage")
    with pytest.raises(ValueError):
        FIRMSConfig(feed_source="")


def test_environmental_has_central_default():
    env = EnvironmentalConfig()
    assert isinstance(env.central, CentralConsumerConfig)
    assert env.central.enabled is False
    assert env.central.url.startswith("nats://")


def test_source_field_survives_dict_coercion():
    """A `source` in yaml/dict is coerced onto the adapter config."""
    from meshai.config import Config, _dict_to_dataclass
    cfg = _dict_to_dataclass(Config, {"environmental": {"usgs_quake": {"enabled": True, "feed_source": "central"}}})
    assert cfg.environmental.usgs_quake.feed_source == "central"
    assert cfg.environmental.nws.feed_source == "native"  # untouched default
