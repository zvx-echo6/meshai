"""Tests for meshai.env.nws_zones — network-free, cache-verified.

All tests monkeypatch `meshai.env.nws_zones._fetch_zone` so no HTTP calls
are ever made.  The persistent SQLite cache uses the per-test temp DB that
`conftest._isolate_meshai_db` (autouse) sets up automatically.

The process-level in-memory cache (_mem_cache) is cleared by the
`_clear_zone_mem_cache` fixture (autouse within this module) so tests are
independent of execution order.
"""

import io
import json
from unittest.mock import MagicMock, patch

import pytest

import meshai.env.nws_zones as nws_zones
from meshai.env.nws_zones import resolve_zones_geometry
from meshai.persistence import get_db


# ---------------------------------------------------------------------------
# Module-local autouse: clear the in-memory hot cache around every test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_zone_mem_cache():
    """Ensure the module-level _mem_cache is empty before and after each test."""
    nws_zones._mem_cache.clear()
    yield
    nws_zones._mem_cache.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _polygon(ring):
    """Build a minimal GeoJSON Polygon geometry from a single outer ring."""
    return {"type": "Polygon", "coordinates": [ring]}


def _multipolygon(rings):
    """Build a GeoJSON MultiPolygon from a list of outer rings."""
    return {"type": "MultiPolygon", "coordinates": [[r] for r in rings]}


# Sample rings (lon, lat order as per GeoJSON)
RING_A = [[-116.5, 42.5], [-116.0, 42.5], [-116.0, 43.0], [-116.5, 43.0], [-116.5, 42.5]]
RING_B = [[-115.5, 43.0], [-115.0, 43.0], [-115.0, 43.5], [-115.5, 43.5], [-115.5, 43.0]]
RING_C = [[-114.5, 42.0], [-114.0, 42.0], [-114.0, 42.5], [-114.5, 42.5], [-114.5, 42.0]]
RING_D = [[-113.5, 41.5], [-113.0, 41.5], [-113.0, 42.0], [-113.5, 42.0], [-113.5, 41.5]]

URL_A = "https://api.weather.gov/zones/fire/IDZ001"
URL_B = "https://api.weather.gov/zones/fire/IDZ002"
URL_C = "https://api.weather.gov/zones/fire/IDZ003"


# ---------------------------------------------------------------------------
# 1. Two Polygon zones → combined MultiPolygon with both polygons' coordinates
# ---------------------------------------------------------------------------

def test_two_polygon_zones_combine(monkeypatch):
    """Two Polygon zones produce a MultiPolygon with both polygons."""
    geoms = {
        URL_A: _polygon(RING_A),
        URL_B: _polygon(RING_B),
    }
    monkeypatch.setattr(nws_zones, "_fetch_zone", lambda url: geoms.get(url))

    result = resolve_zones_geometry([URL_A, URL_B])

    assert result is not None
    assert result["type"] == "MultiPolygon"
    coords = result["coordinates"]
    assert len(coords) == 2
    # Each entry in a MultiPolygon is [outer_ring, *holes] — a list of rings.
    assert coords[0] == [RING_A]
    assert coords[1] == [RING_B]


# ---------------------------------------------------------------------------
# 2. Polygon + MultiPolygon → all rings folded into one MultiPolygon
# ---------------------------------------------------------------------------

def test_polygon_and_multipolygon_combine(monkeypatch):
    """A Polygon zone and a MultiPolygon zone are concatenated correctly."""
    geoms = {
        URL_A: _polygon(RING_A),
        URL_B: _multipolygon([RING_B, RING_C]),  # 2-polygon MultiPolygon
    }
    monkeypatch.setattr(nws_zones, "_fetch_zone", lambda url: geoms.get(url))

    result = resolve_zones_geometry([URL_A, URL_B])

    assert result is not None
    assert result["type"] == "MultiPolygon"
    coords = result["coordinates"]
    # RING_A from Polygon → 1 entry; RING_B and RING_C from MultiPolygon → 2
    assert len(coords) == 3
    assert coords[0] == [RING_A]
    assert coords[1] == [RING_B]
    assert coords[2] == [RING_C]


# ---------------------------------------------------------------------------
# 3. Cache hit: fetcher is NOT called again for a URL already resolved
# ---------------------------------------------------------------------------

def test_cache_hit_no_refetch(monkeypatch):
    """Once a zone is cached, _fetch_zone is not called again for that URL."""
    call_count = {"n": 0}

    def fake_fetch(url):
        call_count["n"] += 1
        return _polygon(RING_A)

    monkeypatch.setattr(nws_zones, "_fetch_zone", fake_fetch)

    # First call → cache miss → fetcher invoked
    r1 = resolve_zones_geometry([URL_A])
    assert r1 is not None
    assert call_count["n"] == 1

    # Clear only the in-memory cache to force SQLite lookup path
    nws_zones._mem_cache.clear()

    # Second call → SQLite cache hit → fetcher NOT called again
    r2 = resolve_zones_geometry([URL_A])
    assert r2 is not None
    assert call_count["n"] == 1, (
        f"_fetch_zone called {call_count['n']} times; expected exactly 1"
    )
    assert r2["type"] == "MultiPolygon"


# ---------------------------------------------------------------------------
# 4. All zones fail / return None → resolve_zones_geometry returns None
# ---------------------------------------------------------------------------

def test_all_zones_fail_returns_none(monkeypatch):
    """When every zone fetch fails, the resolver returns None."""
    monkeypatch.setattr(nws_zones, "_fetch_zone", lambda url: None)

    result = resolve_zones_geometry([URL_A, URL_B])
    assert result is None


def test_all_zones_none_geometry_returns_none(monkeypatch):
    """Zones that exist but have no geometry also yield None."""
    # Simulate NWS returning {geometry: null} for every zone
    monkeypatch.setattr(nws_zones, "_fetch_zone", lambda url: None)

    result = resolve_zones_geometry([URL_A])
    assert result is None


# ---------------------------------------------------------------------------
# 5. Empty list → None immediately (no fetch, no DB)
# ---------------------------------------------------------------------------

def test_empty_zone_list_returns_none(monkeypatch):
    """An empty zone list returns None without touching the fetcher."""
    called = {"n": 0}

    def fake_fetch(url):
        called["n"] += 1
        return _polygon(RING_A)

    monkeypatch.setattr(nws_zones, "_fetch_zone", fake_fetch)

    result = resolve_zones_geometry([])
    assert result is None
    assert called["n"] == 0


# ---------------------------------------------------------------------------
# 6. Null-marker caching: a zone with no geometry is cached as NULL so we
#    don't refetch it on the next call.
# ---------------------------------------------------------------------------

def test_null_geometry_zone_cached(monkeypatch):
    """A zone that resolves to None is stored as NULL; not re-fetched."""
    call_count = {"n": 0}

    def fake_fetch(url):
        call_count["n"] += 1
        return None  # zone exists but has no geometry

    monkeypatch.setattr(nws_zones, "_fetch_zone", fake_fetch)

    # First call: fetch + cache null
    resolve_zones_geometry([URL_A])
    assert call_count["n"] == 1

    # Evict in-memory cache to force SQLite path
    nws_zones._mem_cache.clear()

    # Second call: hit SQLite null marker — should NOT re-fetch
    resolve_zones_geometry([URL_A])
    assert call_count["n"] == 1, "null-marker zone was re-fetched (should be cached)"


# ---------------------------------------------------------------------------
# 7. SQLite table is created on first use (idempotent CREATE TABLE IF NOT EXISTS)
# ---------------------------------------------------------------------------

def test_db_table_created(monkeypatch):
    """The nws_zone_cache table is auto-created on first call."""
    monkeypatch.setattr(nws_zones, "_fetch_zone", lambda url: _polygon(RING_A))

    resolve_zones_geometry([URL_A])

    conn = get_db()
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='nws_zone_cache'"
    ).fetchone()
    assert row is not None, "nws_zone_cache table was not created"


# ---------------------------------------------------------------------------
# 8. Partial failure: one zone fails, others succeed → partial MultiPolygon
# ---------------------------------------------------------------------------

def test_partial_zone_failure_skipped(monkeypatch):
    """A failing zone is skipped; the resolver still combines the rest."""
    geoms = {
        URL_A: _polygon(RING_A),
        URL_B: None,  # this one fails
        URL_C: _polygon(RING_C),
    }
    monkeypatch.setattr(nws_zones, "_fetch_zone", lambda url: geoms.get(url))

    result = resolve_zones_geometry([URL_A, URL_B, URL_C])
    assert result is not None
    assert result["type"] == "MultiPolygon"
    assert len(result["coordinates"]) == 2


# ---------------------------------------------------------------------------
# 9. (Optional) NWS _fetch parse path: zone-only feature → event has geometry
#    Tests the wiring in nws.py without hitting any network.
# ---------------------------------------------------------------------------

def test_nws_parse_zone_only_alert_gets_geometry(monkeypatch):
    """A feature with geometry=null + affectedZones produces an event with
    a non-None geometry after zone resolution (exercises the nws.py wire-up)."""
    import json as _json
    from io import BytesIO
    from unittest.mock import patch

    # Fake zone returns a simple polygon
    monkeypatch.setattr(nws_zones, "_fetch_zone", lambda url: _polygon(RING_A))

    # Build a minimal NWS API response with geometry=null
    fake_response_body = _json.dumps({
        "features": [
            {
                "geometry": None,
                "properties": {
                    "id": "urn:oid:test.zone.only.001",
                    "event": "Red Flag Warning",
                    "severity": "Severe",
                    "headline": "Red Flag Warning for Owyhee Mountains",
                    "description": "Dry and windy conditions expected.",
                    "onset": "2026-07-08T14:00:00Z",
                    "expires": "2026-07-08T22:00:00Z",
                    "areaDesc": "Owyhee Mountains",
                    "geocode": {"UGC": ["IDZ423"]},
                    "affectedZones": [URL_A],
                },
            }
        ]
    }).encode()

    class _FakeResp:
        def read(self): return fake_response_body
        def __enter__(self): return self
        def __exit__(self, *a): pass

    from meshai.env.nws import NWSAlertsAdapter

    config = MagicMock()
    config.areas = ["ID"]
    config.user_agent = "(meshai-test, test@example.com)"
    config.severity_min = "moderate"
    config.tick_seconds = 60

    adapter = NWSAlertsAdapter(config)

    with patch("meshai.env.nws.urlopen", return_value=_FakeResp()):
        adapter._fetch()

    events = adapter.get_events()
    assert len(events) == 1, f"Expected 1 event, got {len(events)}"

    ev = events[0]
    assert ev["geometry"] is not None, "zone-only alert has no geometry after resolution"
    assert ev["geometry"]["type"] == "MultiPolygon"
    assert ev["affected_zones"] == [URL_A]
    # lat/lon should be computed from the resolved MultiPolygon
    assert "lat" in ev, "lat not computed from resolved zone geometry"
    assert "lon" in ev, "lon not computed from resolved zone geometry"


def test_nws_parse_polygon_alert_unaffected(monkeypatch):
    """An alert that already has a polygon geometry is NOT touched by zone resolution."""
    # The fetcher should never be called for alerts that already have geometry
    called = {"n": 0}

    def should_not_be_called(url):
        called["n"] += 1
        return _polygon(RING_A)

    monkeypatch.setattr(nws_zones, "_fetch_zone", should_not_be_called)

    import json as _json
    from unittest.mock import patch

    polygon_geom = {
        "type": "Polygon",
        "coordinates": [RING_B],
    }
    fake_response_body = _json.dumps({
        "features": [
            {
                "geometry": polygon_geom,
                "properties": {
                    "id": "urn:oid:test.polygon.001",
                    "event": "Tornado Warning",
                    "severity": "Extreme",
                    "headline": "Tornado Warning",
                    "description": "A tornado warning.",
                    "onset": "2026-07-08T14:00:00Z",
                    "expires": "2026-07-08T15:00:00Z",
                    "areaDesc": "Ada County",
                    "geocode": {"UGC": ["IDZ016"]},
                    "affectedZones": [URL_B],
                },
            }
        ]
    }).encode()

    class _FakeResp:
        def read(self): return fake_response_body
        def __enter__(self): return self
        def __exit__(self, *a): pass

    from meshai.env.nws import NWSAlertsAdapter

    config = MagicMock()
    config.areas = ["ID"]
    config.user_agent = "(meshai-test, test@example.com)"
    config.severity_min = "moderate"
    config.tick_seconds = 60

    adapter = NWSAlertsAdapter(config)

    with patch("meshai.env.nws.urlopen", return_value=_FakeResp()):
        adapter._fetch()

    events = adapter.get_events()
    assert len(events) == 1
    ev = events[0]

    # The original polygon geometry must be preserved as-is
    assert ev["geometry"] == polygon_geom
    # Zone fetcher must NOT have been called
    assert called["n"] == 0, (
        "_fetch_zone was called even though alert already had polygon geometry"
    )
