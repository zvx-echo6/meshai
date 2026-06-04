"""v0.5.1: sub-adapter (owned-sources) routing for shared Central subjects."""

import json

from meshai.config import EnvironmentalConfig
from meshai.central.consumer import CentralConsumer
from meshai.notifications.pipeline.bus import EventBus


def _envelope(adapter, category="x.y", eid="e1"):
    return {"id": eid, "data": {
        "id": eid, "adapter": adapter, "category": category,
        "time": "2026-05-28T00:00:00Z", "severity": 1,
        "geo": {"centroid": [-114.0, 42.0], "primary_region": "US-ID", "regions": ["US-ID"]},
        "data": {}}}


def _route(central, adapter, subject, category="x.y"):
    """Simulate a message arriving on the subscription that matches `subject`,
    with that subscription's owned-sources, and return the emitted Event (or None).

    v0.5.4: this helper deliberately clears central.region so sub-adapter
    routing is exercised against bare wildcards (its concern is the
    owned-sources filter, not the region-aware subject shape — those are
    tested in test_central_region_routing.py).
    """
    env = EnvironmentalConfig()
    env.central.region = ""
    for a in central:
        getattr(env, a).feed_source = "central"
    rec = []
    bus = EventBus(); bus.subscribe(rec.append)
    c = CentralConsumer(env, bus)
    so = c._subject_owned()
    owned = None
    for filt, o in so.items():
        prefix = filt[:-1] if filt.endswith(">") else filt
        if subject == filt or subject.startswith(prefix):
            owned = o
            break
    ev = c._handle(subject, json.dumps(_envelope(adapter, category)).encode(), owned)
    return ev, rec


def test_roads511_only_drops_wzdx():
    ev, rec = _route(["roads511"], "wzdx", "central.traffic.work_zone.ok")
    assert ev is None and rec == []


def test_roads511_only_emits_state_511_atis():
    ev, rec = _route(["roads511"], "state_511_atis", "central.traffic.event.id.1")
    assert ev is not None and ev.source == "roads511" and len(rec) == 1


def test_both_central_wzdx_routes_to_traffic():
    ev, rec = _route(["traffic", "roads511"], "wzdx", "central.traffic.work_zone.ok")
    assert ev is not None and ev.source == "traffic"


def test_both_central_state511_routes_to_roads511():
    ev, rec = _route(["traffic", "roads511"], "state_511_atis", "central.traffic.event.id.1")
    assert ev is not None and ev.source == "roads511"


def test_firms_only_drops_wfigs():
    ev, rec = _route(["firms"], "wfigs_incidents", "central.fire.incident.mt.x")
    assert ev is None and rec == []


def test_firms_only_emits_firms():
    ev, rec = _route(["firms"], "firms", "central.fire.hotspot.viirs_noaa20.high")
    assert ev is not None and ev.source == "firms" and len(rec) == 1


def test_tomtom_incidents_remaps_to_traffic():
    ev, rec = _route(["traffic"], "tomtom_incidents", "central.traffic.incident.x")
    assert ev is not None and ev.source == "traffic"


def test_subject_owned_shares_traffic_subject():
    # v0.5.4: assert the legacy bare-wildcard shape by clearing region.
    # Region-aware shared-subject behaviour ('central.traffic.>.id' for both
    # traffic and roads511) is covered in test_central_region_routing.py.
    env = EnvironmentalConfig()
    env.central.region = ""
    env.traffic.feed_source = "central"
    env.roads511.feed_source = "central"
    so = CentralConsumer(env, None)._subject_owned()
    assert so.get("central.traffic.>") == {"traffic", "roads511"}
