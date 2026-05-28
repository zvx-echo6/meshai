"""v0.4 C.1: Central connector backend — normalization, lifecycle, source gate."""

import asyncio
import json

import pytest

from meshai.config import EnvironmentalConfig
from meshai.central.consumer import CentralConsumer, map_category, map_severity
from meshai.notifications.pipeline.bus import EventBus


def make_consumer():
    env = EnvironmentalConfig()
    bus = EventBus()
    rec = []
    bus.subscribe(rec.append)
    return CentralConsumer(env, bus), env, rec


def envelope(adapter="usgs_quake", category="quake.event", severity=2,
             eid="us6000abcd", centroid=(-114.5, 42.6), upstream=None,
             time="2026-05-27T12:00:00Z", expires=None):
    return {
        "id": eid, "source": "central.echo6.co",
        "type": f"central.{category}.v1", "time": time,
        "centralcategory": category, "centralseverity": severity,
        "specversion": "1.0", "datacontenttype": "application/json",
        "data": {
            "id": eid, "adapter": adapter, "category": category,
            "time": time, "expires": expires, "severity": severity,
            "geo": {"centroid": list(centroid), "bbox": None,
                    "regions": ["US-ID"], "primary_region": "US-ID"},
            "data": upstream if upstream is not None else {"magnitude": 4.2, "place": "near Twin Falls"},
        },
    }


class FakeMsg:
    def __init__(self, subject, env):
        self.subject = subject
        self.data = json.dumps(env).encode()
        self.acked = False

    async def ack(self):
        self.acked = True


# ---- subject derivation / source gate ----

def test_no_subjects_when_all_native():
    c, env, rec = make_consumer()
    assert c.subjects() == []


def test_subjects_when_central():
    c, env, rec = make_consumer()
    env.usgs_quake.feed_source = "central"
    assert "central.quake.>" in c.subjects()


def test_source_central_skips_native_instantiation():
    from meshai.env.store import EnvironmentalStore
    env = EnvironmentalConfig()
    env.enabled = True
    env.usgs_quake.enabled = True
    env.usgs_quake.feed_source = "central"   # should be skipped natively
    env.nws.enabled = True              # native -> present
    store = EnvironmentalStore(config=env, region_anchors=[], event_bus=None)
    assert "usgs_quake" not in store._adapters
    assert "nws" in store._adapters


# ---- normalization ----

def test_normalize_and_emit():
    c, env, rec = make_consumer()
    ev = c._handle("central.quake.event.moderate", json.dumps(envelope()).encode())
    assert ev is not None
    assert len(rec) == 1
    e = rec[0]
    assert e.source == "usgs_quake"
    assert e.category == "earthquake_event"
    assert e.severity == "priority"            # central severity 2
    assert e.lat == 42.6 and e.lon == -114.5   # [lon,lat] -> (lat,lon)
    assert e.group_key == "us6000abcd"
    assert e.region == "US-ID"
    assert e.data.get("magnitude") == 4.2      # upstream preserved verbatim


def test_enriched_preserved_verbatim():
    c, env, rec = make_consumer()
    up = {"magnitude": 5.1, "_enriched": {"geocoder": {"state": "Idaho"}, "usgs_stats": {"x": 1}}}
    ev = c._handle("central.quake.event.strong", json.dumps(envelope(severity=4, upstream=up)).encode())
    assert ev.severity == "immediate"
    assert ev.data["_enriched"]["geocoder"]["state"] == "Idaho"
    assert ev.data["_enriched"]["usgs_stats"] == {"x": 1}


def test_tombstone_translates_to_clear():
    c, env, rec = make_consumer()
    msg = envelope(adapter="gdacs", category="disaster.fl.removed", severity=0, eid="FL1103885:removed")
    ev = c._handle("central.disaster.fl.removed.austria", json.dumps(msg).encode())
    assert ev is not None
    assert ev.group_key == "FL1103885"          # ':removed' stripped -> matches original
    assert ev.data.get("_central_tombstone") is True


def test_severity_mapping():
    assert map_severity(0) == "routine"
    assert map_severity(1) == "routine"
    assert map_severity(2) == "priority"
    assert map_severity(3) == "immediate"
    assert map_severity(4) == "immediate"
    assert map_severity(None) == "routine"


def test_category_mapping():
    assert map_category("wx.alert.severe_thunderstorm_warning") == "weather_warning"
    assert map_category("quake.event") == "earthquake_event"
    assert map_category("fire.hotspot.viirs_noaa20.high") == "wildfire_hotspot"
    assert map_category("hydro.00060.usgs.06901250") == "stream_flow"


# ---- async callback path ----

def test_on_message_emits_and_acks():
    c, env, rec = make_consumer()
    msg = FakeMsg("central.quake.event.moderate", envelope())
    asyncio.run(c._on_message(msg))
    assert msg.acked is True
    assert len(rec) == 1


def test_start_no_op_when_all_native():
    """start() is a no-op (no NATS connect) when no adapter is central."""
    c, env, rec = make_consumer()
    asyncio.run(c.start())   # must not raise / must not require NATS
    assert c._nc is None
