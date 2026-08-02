"""Tests for the universal config-driven GenericHttpAdapter (env/generic_http.py).

Ported-behavior coverage:
  * _dig dotted-path walker (nested dict, list index, miss cases)
  * _item_to_event field mapping (Idaho Power outage item) + _render template
  * summary_template with a missing key does not crash
  * cold-start-silent: first poll seeds + persists but broadcasts nothing
  * geometry-path Point -> centroid extraction
"""
from __future__ import annotations
import asyncio

import json

from urllib.error import HTTPError

from meshai.env.generic_http import GenericHttpAdapter, _BROWSER_UA, _dig
from meshai.env.store import EnvironmentalStore
from meshai.config import EnvironmentalConfig
from meshai.notifications.pipeline.bus import EventBus
from meshai.persistence import get_db


# --- Idaho Power source config (CONFIGURED, not hardcoded) -------------------

IDAHO_POWER_SOURCE = {
    "name": "idaho_power",
    "enabled": True,
    "url": "https://apiedge.idahopower.com/api/Outage/GetCurrentOutageInformation",
    "items_path": "object.outages",
    "id_path": "omsOutageId",
    "lat_path": "latitude",
    "lon_path": "longitude",
    "title_path": "probableCause",
    "category": "power_outage",
    "poll_seconds": 300,
    "severity": "routine",
    "field_mappings": [
        {"source_path": "omsCustomerCount", "dest_key": "customers"},
        {"source_path": "omsEstimatedTimeToRestorationMessage", "dest_key": "eta"},
        {"source_path": "omsStatusDescription", "dest_key": "status"},
    ],
    "summary_template": "⚡ Power out — {customers} affected, ETA {eta} ({status})",
    "emoji": "⚡",
}

IDAHO_POWER_ITEM = {
    "omsOutageId": "123",
    "latitude": 43.6,
    "longitude": -116.2,
    "omsCustomerCount": 250,
    "probableCause": "Equipment",
    "omsEstimatedTimeToRestorationMessage": "10:30 PM",
    "omsStatusDescription": "Crew assigned",
}


# ===========================================================================
# _dig
# ===========================================================================

def test_dig_nested_dict():
    assert _dig({"a": {"b": 1}}, "a.b") == 1
    assert _dig({"object": {"outages": [1, 2]}}, "object.outages") == [1, 2]


def test_dig_list_index():
    assert _dig({"a": [10, 20]}, "a.1") == 20
    assert _dig({"geometry": {"coordinates": [-116.2, 43.6]}},
                "geometry.coordinates.0") == -116.2


def test_dig_miss_cases():
    assert _dig({"a": 1}, "a.b") is None          # scalar then key
    assert _dig(None, "anything") is None          # None root
    assert _dig({"a": [1]}, "a.5") is None         # out-of-range index
    assert _dig({"a": {}}, "a.missing") is None    # missing key
    assert _dig({"a": [1]}, "a.x") is None         # non-int index on list


# ===========================================================================
# _item_to_event + _render
# ===========================================================================

def test_item_to_event_idaho_power_mapping():
    adapter = GenericHttpAdapter([IDAHO_POWER_SOURCE])
    evt = adapter._item_to_event(IDAHO_POWER_ITEM, IDAHO_POWER_SOURCE, now=1000.0)

    assert evt is not None
    assert evt["event_id"] == "123"
    assert evt["category"] == "power_outage"
    assert evt["latitude"] == 43.6
    assert evt["longitude"] == -116.2
    assert evt["title"] == "Equipment"
    assert evt["data"]["customers"] == 250
    assert evt["data"]["eta"] == "10:30 PM"
    assert evt["data"]["status"] == "Crew assigned"
    assert evt["data"]["latitude"] == 43.6
    assert evt["data"]["longitude"] == -116.2
    # source namespaced per configured name for independent dedup
    assert evt["source"] == "generic:idaho_power"
    assert evt["_source"] == "idaho_power"


def test_render_summary_template():
    adapter = GenericHttpAdapter([IDAHO_POWER_SOURCE])
    evt = adapter._item_to_event(IDAHO_POWER_ITEM, IDAHO_POWER_SOURCE)
    assert adapter._render(evt) == (
        "⚡ Power out — 250 affected, ETA 10:30 PM (Crew assigned)"
    )


def test_render_template_missing_key_does_not_crash():
    source = dict(IDAHO_POWER_SOURCE)
    # Reference a key that no field mapping produces -> must render blank.
    source["summary_template"] = "⚡ {customers} out, cause {nonexistent_key}!"
    adapter = GenericHttpAdapter([source])
    evt = adapter._item_to_event(IDAHO_POWER_ITEM, source)
    rendered = adapter._render(evt)  # must not raise
    assert "250" in rendered
    assert "{nonexistent_key}" not in rendered   # token substituted (blank)
    assert rendered == "⚡ 250 out, cause !"


def test_render_default_no_template():
    source = dict(IDAHO_POWER_SOURCE)
    source.pop("summary_template")
    adapter = GenericHttpAdapter([source])
    evt = adapter._item_to_event(IDAHO_POWER_ITEM, source)
    rendered = adapter._render(evt)
    assert "Equipment" in rendered          # title
    assert "customers: 250" in rendered      # mapped field


def test_item_missing_id_is_skipped():
    adapter = GenericHttpAdapter([IDAHO_POWER_SOURCE])
    evt = adapter._item_to_event({"latitude": 43.6, "longitude": -116.2},
                                 IDAHO_POWER_SOURCE)
    assert evt is None


# ===========================================================================
# geometry-path Point -> centroid
# ===========================================================================

def test_geometry_point_centroid():
    source = {
        "name": "geo_feed",
        "enabled": True,
        "url": "https://example.com/feed.geojson",
        "items_path": "features",
        "id_path": "id",
        "geometry_path": "geometry",
        "title_path": "properties.name",
        "category": "geo",
    }
    item = {
        "id": "g1",
        "geometry": {"type": "Point", "coordinates": [-116.2, 43.6]},
        "properties": {"name": "Test Point"},
    }
    adapter = GenericHttpAdapter([source])
    evt = adapter._item_to_event(item, source)
    assert evt["latitude"] == 43.6
    assert evt["longitude"] == -116.2
    # the resolved geometry dict rides on data so the coverage gate can use it
    assert evt["data"]["geometry"]["type"] == "Point"


def test_to_event_carries_latlon_for_coverage_gate():
    adapter = GenericHttpAdapter([IDAHO_POWER_SOURCE])
    evt = adapter._item_to_event(IDAHO_POWER_ITEM, IDAHO_POWER_SOURCE)
    event = adapter.to_event(evt)
    assert event is not None
    assert event.lat == 43.6
    assert event.lon == -116.2
    assert event.category == "power_outage"
    assert event.summary == "⚡ Power out — 250 affected, ETA 10:30 PM (Crew assigned)"


# ===========================================================================
# cold-start-silent (full store path) + persistence
# ===========================================================================

def _payload(items):
    return {"object": {"outages": items, "totalCustomersAffected": sum(
        i.get("omsCustomerCount", 0) for i in items)}}


def _make_store_with_generic():
    bus = EventBus()
    captured = []
    bus.subscribe(lambda e: captured.append(e))
    store = EnvironmentalStore(
        EnvironmentalConfig(), event_bus=bus,
        generic_sources=[dict(IDAHO_POWER_SOURCE)],
    )
    adapter = store._adapters["generic_http"]
    return store, adapter, captured


def test_cold_start_silent_first_poll_seeds_persists_no_emit():
    store, adapter, captured = _make_store_with_generic()
    # Stub the network fetch with one active outage.
    adapter._fetch = lambda url, extra_headers=None: _payload([IDAHO_POWER_ITEM])

    asyncio.run(store.refresh())  # poll 1 == pre-existing backlog

    # Nothing broadcast on the cold-start poll...
    assert captured == [], "first poll must broadcast NOTHING (cold-start seed)"
    # ...but the item IS persisted so the LLM sees it immediately.
    row = get_db().execute(
        "SELECT source, event_id, category, title, lat, lon FROM generic_events "
        "WHERE source=? AND event_id=?", ("idaho_power", "123")).fetchone()
    assert row is not None
    assert row["category"] == "power_outage"
    assert row["title"] == "Equipment"
    assert abs(row["lat"] - 43.6) < 1e-6


def test_later_poll_broadcasts_newly_received_item():
    store, adapter, captured = _make_store_with_generic()
    adapter._fetch = lambda url, extra_headers=None: _payload([IDAHO_POWER_ITEM])
    asyncio.run(store.refresh())  # poll 1 — seed silently
    assert captured == []

    # A genuinely NEW outage appears on a later poll -> it must broadcast.
    new_item = dict(IDAHO_POWER_ITEM, omsOutageId="456", omsCustomerCount=99)
    adapter._fetch = lambda url, extra_headers=None: _payload([IDAHO_POWER_ITEM, new_item])
    adapter._last_poll.clear()  # force cadence to elapse
    asyncio.run(store.refresh())  # poll 2

    assert len(captured) == 1, "only the newly-received outage broadcasts"
    assert captured[0].category == "power_outage"
    # both items persisted (dedup by source+event_id)
    n = get_db().execute(
        "SELECT COUNT(*) c FROM generic_events WHERE source=?",
        ("idaho_power",)).fetchone()["c"]
    assert n == 2


# ===========================================================================
# _fetch: browser UA default, per-source header override, 403 retry
# ===========================================================================

def test_fetch_uses_browser_ua_by_default(monkeypatch):
    """Default fetch sends the browser UA (not the WAF-tripping MeshAI/1.0)."""
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true}'

    def _fake_urlopen(req, timeout=None):
        captured["headers"] = dict(req.headers)
        return _Resp()

    monkeypatch.setattr("meshai.env.generic_http.urlopen", _fake_urlopen)
    adapter = GenericHttpAdapter([IDAHO_POWER_SOURCE])
    result = adapter._fetch("https://example.com/feed")

    assert result == {"ok": True}
    # urllib title-cases header names in Request.headers.
    assert captured["headers"].get("User-agent") == _BROWSER_UA


def test_fetch_per_source_headers_override_ua_and_add_auth(monkeypatch):
    """A source's headers dict overrides the UA and adds arbitrary auth."""
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true}'

    def _fake_urlopen(req, timeout=None):
        captured["headers"] = dict(req.headers)
        return _Resp()

    monkeypatch.setattr("meshai.env.generic_http.urlopen", _fake_urlopen)
    adapter = GenericHttpAdapter([IDAHO_POWER_SOURCE])
    result = adapter._fetch(
        "https://example.com/feed",
        extra_headers={"User-Agent": "X", "Authorization": "Bearer y"},
    )

    assert result == {"ok": True}
    assert captured["headers"].get("User-agent") == "X"        # overridden
    assert captured["headers"].get("Authorization") == "Bearer y"  # added


def test_fetch_retries_once_on_403_then_succeeds(monkeypatch):
    """First 403 (WAF block) is retried once and the retry's JSON is returned."""
    calls = {"n": 0}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true}'

    def _fake_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise HTTPError("https://example.com/feed", 403, "Forbidden", {}, None)
        return _Resp()

    monkeypatch.setattr("meshai.env.generic_http.urlopen", _fake_urlopen)
    monkeypatch.setattr("meshai.env.generic_http.time.sleep", lambda *_: None)
    adapter = GenericHttpAdapter([IDAHO_POWER_SOURCE])
    result = adapter._fetch("https://example.com/feed")

    assert result == {"ok": True}
    assert calls["n"] == 2, "must retry exactly once on 403"


def test_fetch_persistent_403_returns_none_after_one_retry(monkeypatch):
    """A feed that 403s on both attempts gives up (None) after the single retry."""
    calls = {"n": 0}

    def _fake_urlopen(req, timeout=None):
        calls["n"] += 1
        raise HTTPError("https://example.com/feed", 403, "Forbidden", {}, None)

    monkeypatch.setattr("meshai.env.generic_http.urlopen", _fake_urlopen)
    monkeypatch.setattr("meshai.env.generic_http.time.sleep", lambda *_: None)
    adapter = GenericHttpAdapter([IDAHO_POWER_SOURCE])
    result = adapter._fetch("https://example.com/feed")

    assert result is None
    assert calls["n"] == 2, "one initial attempt + one retry, then give up"
    assert adapter._last_error == "HTTP 403"


def test_poll_source_threads_source_headers_into_fetch(monkeypatch):
    """_poll_source passes the source's headers through to _fetch."""
    seen = {}

    def _fake_fetch(url, extra_headers=None):
        seen["url"] = url
        seen["extra_headers"] = extra_headers
        return _payload([IDAHO_POWER_ITEM])

    source = dict(IDAHO_POWER_SOURCE, headers={"Authorization": "Bearer z"})
    adapter = GenericHttpAdapter([source])
    adapter._fetch = _fake_fetch
    adapter._poll_source(source, now=1000.0)

    assert seen["extra_headers"] == {"Authorization": "Bearer z"}


def test_build_generic_detail_reader():
    from meshai.notifications.env_reporter import EnvReporter
    store, adapter, captured = _make_store_with_generic()
    adapter._fetch = lambda url, extra_headers=None: _payload([IDAHO_POWER_ITEM])
    asyncio.run(store.refresh())

    text = EnvReporter().build_generic_detail()
    assert "idaho_power" in text
    assert "power_outage" in text
    assert "Equipment" in text
