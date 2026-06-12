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


def test_subjects_for_usgs_quake_us_id_uses_tail_only_wildcard():
    """v0.5.7-seismic: USGS quake publishes `central.quake.event.<tier>` with
    NO region in the subject (per Central v0.10.0 guide §usgs_quake; same
    situation as FIRMS). The pre-v0.5.7-seismic `central.quake.event.>.us.id`
    was syntactically invalid (`>` mid-subject) AND wouldn't have matched
    anything Central publishes (only 4 tokens, no us.<state>). Region
    filtering for quakes now happens client-side via data.latitude/longitude.
    Subscription uses tail-only `>` (NATS-legal)."""
    assert _subjects_for("usgs_quake", "us.id") == ["central.quake.event.>"]


def test_subjects_for_firms_us_id_uses_tail_only_wildcard():
    """v0.5.7-fire: FIRMS publishes `central.fire.hotspot.<satellite>.<confidence>`
    with NO region in the subject (per Central v0.10.0 guide §firms). The
    pre-v0.5.7-fire `central.fire.hotspot.>.us.id` was syntactically invalid
    (`>` mid-subject) AND wouldn't have matched anything Central actually
    publishes. Region filtering for FIRMS now happens client-side via
    data.latitude/longitude. Subscription uses tail-only `>` (NATS-legal)."""
    assert _subjects_for("firms", "us.id") == ["central.fire.hotspot.>"]


def test_subjects_for_fires_us_id_includes_tombstones():
    """v0.5.7-fire: WFIGS subjects -- active state-token at depth-3 + the
    removal-tombstone subjects (`central.fire.{incident,perimeter}.removed.<state>`)
    per Central v0.10.0 guide §wfigs_incidents §wfigs_perimeters. Pre-v0.5.7-fire
    we only subscribed to active subjects, silently dropping fall-off signals."""
    assert _subjects_for("fires", "us.id") == [
        "central.fire.incident.id.>",
        "central.fire.perimeter.id.>",
        "central.fire.incident.removed.id",
        "central.fire.perimeter.removed.id",
    ]


def test_subjects_for_traffic_uses_convention_b():
    """v0.5.7-traffic: traffic adapter -> bare-state Convention B with `*`
    in the event_type slot. Pre-v0.5.7-traffic this was `>.{state}` which
    is invalid NATS (`>` must be at the tail). The bare-state subject is
    shared with roads511 (sub-adapter routing picks the right meshai source)."""
    assert _subjects_for("traffic", "us.id") == ["central.traffic.*.id"]


def test_subjects_for_roads511_dual_subscribes_convention_a_and_b():
    """v0.5.7-traffic: roads511 owns BOTH the shared bare-state subject
    (Convention B, shared with traffic) AND the us.<state> subject
    (Convention A) where the new Idaho-only itd_511 adapter publishes."""
    assert _subjects_for("roads511", "us.id") == [
        "central.traffic.*.id",
        "central.traffic.*.us.id",
    ]


def test_subjects_for_usgs_includes_unknown_workaround():
    """v0.5.7-water: USGS NWIS hydro subscribes to BOTH the region-tagged
    filter and the .unknown filter. Per the v0.10.0-itd-511 nwis.py
    producer, the actual published subject is
    `central.hydro.<param>.<agency>.<site>.<region>` where <region> is
    either `us.<state>` (7 tokens) or `unknown` (6 tokens). The
    pre-v0.5.7-water shape `central.hydro.>.<state>` was invalid NATS
    (`>` mid-subject). Fixed by using three single-token `*` wildcards
    in the parameter/agency/site slots."""
    assert _subjects_for("usgs", "us.id") == [
        "central.hydro.*.*.*.us.id",
        "central.hydro.*.*.*.unknown",
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
    assert _subjects_for("avalanche", "") != []  # avalanche now in central pipeline


# --------------------------------------------------------------------- integration

def test_central_region_default_propagates_to_consumer_subjects():
    """Default region = 'us.id': flipping nws to central → consumer subscribes
    to the region-aware subject, not the bare wildcard."""
    env = EnvironmentalConfig()
    assert env.central.region == "us.id"        # spec default
    env.nws.feed_source = "central"
    so = CentralConsumer(env, None)._subject_owned()
    # satpass also defaults to feed_source='central', so it appears too
    assert "central.wx.alert.us.id.>" in so
    assert so["central.wx.alert.us.id.>"] == {"nws"}
    assert "central.sat.pass.us.id.>" in so
    assert so["central.sat.pass.us.id.>"] == {"satpass"}
    assert "central.sat.tle.>" in so
    assert so["central.sat.tle.>"] == {"satpass"}
