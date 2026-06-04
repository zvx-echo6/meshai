"""v0.5.4: Central v0.9.20 region-aware subject building.

Exercises `_subjects_for(adapter, region)` and the wiring through
`CentralConsumer._subject_owned()`. The spec is hard-coded in the test
strings on purpose so a future drift in the v0.9.20 subject scheme
fails noisily here instead of silently shipping wrong filters.
"""

from meshai.central.consumer import (
    CentralConsumer,
    _subjects_for,
    _SUBJECTS_BARE,
)
from meshai.config import EnvironmentalConfig


# --------------------------------------------------------------------- per-adapter

def test_subjects_for_nws_us_id():
    """NWS: region BEFORE wildcard (matches alert.<region>.<...>)."""
    assert _subjects_for("nws", "us.id") == ["central.wx.alert.us.id.>"]


def test_subjects_for_usgs_quake_us_id():
    """USGS quake: region AFTER wildcard."""
    assert _subjects_for("usgs_quake", "us.id") == ["central.quake.event.>.us.id"]


def test_subjects_for_firms_us_id():
    """FIRMS hotspots: region AFTER wildcard, hotspot domain explicit."""
    assert _subjects_for("firms", "us.id") == ["central.fire.hotspot.>.us.id"]


def test_subjects_for_fires_us_id_uses_state_token():
    """NIFC fires: state-only token at depth-4 for both incident + perimeter."""
    assert _subjects_for("fires", "us.id") == [
        "central.fire.incident.id.>",
        "central.fire.perimeter.id.>",
    ]


def test_subjects_for_traffic_and_roads511_share_state_token():
    """Traffic family: bare-state suffix (no us. prefix), shared by both adapters."""
    assert _subjects_for("traffic",  "us.id") == ["central.traffic.>.id"]
    assert _subjects_for("roads511", "us.id") == ["central.traffic.>.id"]


def test_subjects_for_usgs_includes_unknown_workaround():
    """USGS hydro: subscribes to BOTH the region-tagged filter and the
    ".unknown" filter to cover gauges whose state Central v0.9.20 can't
    infer yet (workaround until v0.9.20.1 backfills the tag)."""
    assert _subjects_for("usgs", "us.id") == [
        "central.hydro.>.us.id",
        "central.hydro.>.unknown",
    ]


def test_subjects_for_swpc_stays_global():
    """SWPC: space weather is planetary; region argument is ignored."""
    assert _subjects_for("swpc", "us.id") == ["central.space.>"]
    assert _subjects_for("swpc", "us.mt") == ["central.space.>"]  # same regardless
    assert _subjects_for("swpc", "") == ["central.space.>"]


# --------------------------------------------------------------------- backward compat

def test_subjects_for_empty_region_falls_back_to_bare_wildcards():
    """Empty/None region = pre-v0.9.20 behaviour for every adapter, byte-identical
    to the legacy _SUBJECTS_BARE map. Adapters absent from the map return []."""
    for adapter, expected in _SUBJECTS_BARE.items():
        assert _subjects_for(adapter, "") == expected, f"empty region mismatch for {adapter}"
        assert _subjects_for(adapter, None) == expected, f"None region mismatch for {adapter}"
    # Unknown adapters return empty regardless of region.
    assert _subjects_for("ducting", "us.id") == []
    assert _subjects_for("avalanche", "") == []


# --------------------------------------------------------------------- integration

def test_central_region_default_propagates_to_consumer_subjects():
    """Default region = 'us.id': flipping nws to central → consumer subscribes
    to the region-aware subject, not the bare wildcard."""
    env = EnvironmentalConfig()
    assert env.central.region == "us.id"        # spec default
    env.nws.feed_source = "central"
    so = CentralConsumer(env, None)._subject_owned()
    assert list(so.keys()) == ["central.wx.alert.us.id.>"]
    assert so["central.wx.alert.us.id.>"] == {"nws"}
