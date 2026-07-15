"""Tests proving every native environmental adapter's upstream feed URL(s)
are config-driven (mirrors the roads511 pattern).

For each of the 9 previously-hardcoded adapters, verifies:
  (a) with NO config override, the built/fetched URL equals the historical
      hardcoded literal (backward compat), and
  (b) setting the new config field changes the built URL accordingly.

HTTP is monkeypatched at the module level (urlopen) -- no network calls.
Where possible, the request URL is captured via a fake urlopen so the test
exercises the real fetch path rather than just constructor plumbing.
"""
from __future__ import annotations

from urllib.error import URLError

import pytest

from meshai.config import (
    AvalancheConfig,
    DuctingConfig,
    FIRMSConfig,
    NICFFiresConfig,
    NWSConfig,
    SWPCConfig,
    SatpassConfig,
    TomTomConfig,
    USGSConfig,
)
from meshai.env.avalanche import AvalancheAdapter
from meshai.env.ducting import DuctingAdapter
from meshai.env.fires import NICFFiresAdapter
from meshai.env.firms import FIRMSAdapter
from meshai.env.nws import NWSAlertsAdapter
from meshai.env.swpc import SWPCAdapter
from meshai.env.tle_fetch import TLEFetchAdapter
from meshai.env.traffic import TomTomTrafficAdapter
from meshai.env.usgs import USGSStreamsAdapter


class _FakeResp:
    """Minimal urlopen() context-manager mock returning fixed bytes."""

    def __init__(self, text: str = "{}"):
        self._text = text

    def read(self) -> bytes:
        return self._text.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _capturing_urlopen(captured: list):
    """Build a fake urlopen() that records the request's full_url and
    returns an empty-but-valid response (parse errors are swallowed by
    every adapter's broad except-Exception around parsing)."""

    def _fake(req, timeout=None):
        captured.append(req.full_url)
        return _FakeResp("{}")

    return _fake


# ============================================================
# 1. nws.py — NWSConfig.base_url
# ============================================================

def test_nws_default_url_matches_hardcoded(monkeypatch):
    captured: list = []
    monkeypatch.setattr("meshai.env.nws.urlopen", _capturing_urlopen(captured))
    adapter = NWSAlertsAdapter(NWSConfig())
    adapter._fetch()
    assert captured == ["https://api.weather.gov/alerts/active?area=ID"]


def test_nws_override_url_changes_fetch(monkeypatch):
    captured: list = []
    monkeypatch.setattr("meshai.env.nws.urlopen", _capturing_urlopen(captured))
    cfg = NWSConfig(base_url="https://example.test/alerts")
    adapter = NWSAlertsAdapter(cfg)
    adapter._fetch()
    assert captured == ["https://example.test/alerts?area=ID"]


# ============================================================
# 2. swpc.py — SWPCConfig.endpoints (4 endpoints)
# ============================================================

def test_swpc_default_endpoints_match_hardcoded():
    adapter = SWPCAdapter(SWPCConfig())
    assert adapter._endpoint_urls == {
        "scales": "https://services.swpc.noaa.gov/products/noaa-scales.json",
        "kp": "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json",
        "alerts": "https://services.swpc.noaa.gov/products/alerts.json",
        "f107": "https://services.swpc.noaa.gov/json/f107_cm_flux.json",
    }


def test_swpc_default_tick_fetches_hardcoded_urls(monkeypatch):
    captured: list = []
    monkeypatch.setattr("meshai.env.swpc.urlopen", _capturing_urlopen(captured))
    adapter = SWPCAdapter(SWPCConfig())
    adapter.tick()
    assert set(captured) == {
        "https://services.swpc.noaa.gov/products/noaa-scales.json",
        "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json",
        "https://services.swpc.noaa.gov/products/alerts.json",
        "https://services.swpc.noaa.gov/json/f107_cm_flux.json",
    }


def test_swpc_override_endpoint_changes_fetch(monkeypatch):
    captured: list = []
    monkeypatch.setattr("meshai.env.swpc.urlopen", _capturing_urlopen(captured))
    cfg = SWPCConfig(endpoints={"scales": "https://example.test/scales.json"})
    adapter = SWPCAdapter(cfg)
    assert adapter._endpoint_urls == {"scales": "https://example.test/scales.json"}
    adapter.tick()
    assert captured == ["https://example.test/scales.json"]


# ============================================================
# 3. ducting.py — DuctingConfig.base_url
# ============================================================

def test_ducting_default_url_matches_hardcoded(monkeypatch):
    captured: list = []
    monkeypatch.setattr("meshai.env.ducting.urlopen", _capturing_urlopen(captured))
    adapter = DuctingAdapter(DuctingConfig())
    adapter._fetch()
    assert len(captured) == 1
    assert captured[0].startswith("https://api.open-meteo.com/v1/gfs?")


def test_ducting_override_url_changes_fetch(monkeypatch):
    captured: list = []
    monkeypatch.setattr("meshai.env.ducting.urlopen", _capturing_urlopen(captured))
    cfg = DuctingConfig(base_url="https://example.test/gfs")
    adapter = DuctingAdapter(cfg)
    adapter._fetch()
    assert len(captured) == 1
    assert captured[0].startswith("https://example.test/gfs?")


# ============================================================
# 4. fires.py — NICFFiresConfig.feed_url / .points_url
# ============================================================

def test_fires_default_urls_match_hardcoded(monkeypatch):
    captured: list = []
    monkeypatch.setattr("meshai.env.fires.urlopen", _capturing_urlopen(captured))
    adapter = NICFFiresAdapter(NICFFiresConfig())
    adapter._fetch()
    assert len(captured) == 2
    assert captured[0].startswith(
        "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
        "WFIGS_Interagency_Perimeters_Current/FeatureServer/0/query?"
    )
    assert captured[1].startswith(
        "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
        "WFIGS_Incident_Locations_Current/FeatureServer/0/query?"
    )


def test_fires_override_urls_change_fetch(monkeypatch):
    captured: list = []
    monkeypatch.setattr("meshai.env.fires.urlopen", _capturing_urlopen(captured))
    cfg = NICFFiresConfig(
        feed_url="https://example.test/perimeters",
        points_url="https://example.test/points",
    )
    adapter = NICFFiresAdapter(cfg)
    adapter._fetch()
    assert len(captured) == 2
    assert captured[0].startswith("https://example.test/perimeters?")
    assert captured[1].startswith("https://example.test/points?")


# ============================================================
# 5. firms.py — FIRMSConfig.base_url
# ============================================================

def test_firms_default_url_matches_hardcoded(monkeypatch):
    captured: list = []
    monkeypatch.setattr("meshai.env.firms.urlopen", _capturing_urlopen(captured))
    cfg = FIRMSConfig(map_key="test-key", bbox=[-117, 42, -114, 44])
    adapter = FIRMSAdapter(cfg)
    adapter._fetch()
    assert captured == [
        "https://firms.modaps.eosdis.nasa.gov/api/area/csv/test-key/VIIRS_SNPP_NRT/-117,42,-114,44/1"
    ]


def test_firms_override_url_changes_fetch(monkeypatch):
    captured: list = []
    monkeypatch.setattr("meshai.env.firms.urlopen", _capturing_urlopen(captured))
    cfg = FIRMSConfig(
        map_key="test-key",
        bbox=[-117, 42, -114, 44],
        base_url="https://example.test/firms",
    )
    adapter = FIRMSAdapter(cfg)
    adapter._fetch()
    assert captured == [
        "https://example.test/firms/test-key/VIIRS_SNPP_NRT/-117,42,-114,44/1"
    ]


# ============================================================
# 6. avalanche.py — AvalancheConfig.base_url
# ============================================================

def test_avalanche_default_url_matches_hardcoded(monkeypatch):
    captured: list = []
    monkeypatch.setattr("meshai.env.avalanche.urlopen", _capturing_urlopen(captured))
    cfg = AvalancheConfig(season_months=list(range(1, 13)))  # force in-season
    adapter = AvalancheAdapter(cfg)
    adapter._fetch()
    assert captured == ["https://api.avalanche.org/v2/public/products/map-layer/SNFAC"]


def test_avalanche_override_url_changes_fetch(monkeypatch):
    captured: list = []
    monkeypatch.setattr("meshai.env.avalanche.urlopen", _capturing_urlopen(captured))
    cfg = AvalancheConfig(
        season_months=list(range(1, 13)),
        base_url="https://example.test/map-layer",
    )
    adapter = AvalancheAdapter(cfg)
    adapter._fetch()
    assert captured == ["https://example.test/map-layer/SNFAC"]


# ============================================================
# 7. usgs.py — USGSConfig.base_url / .nwps_base_url / .site_info_url
# ============================================================

def test_usgs_default_iv_url_matches_hardcoded(monkeypatch):
    captured: list = []
    monkeypatch.setattr("meshai.env.usgs.urlopen", _capturing_urlopen(captured))
    cfg = USGSConfig(sites=["13090500"])
    adapter = USGSStreamsAdapter(cfg)
    adapter._fetch()
    assert len(captured) == 1
    assert captured[0].startswith("https://waterservices.usgs.gov/nwis/iv/?")


def test_usgs_override_iv_url_changes_fetch(monkeypatch):
    captured: list = []
    monkeypatch.setattr("meshai.env.usgs.urlopen", _capturing_urlopen(captured))
    cfg = USGSConfig(sites=["13090500"], base_url="https://example.test/nwis/iv/")
    adapter = USGSStreamsAdapter(cfg)
    adapter._fetch()
    assert len(captured) == 1
    assert captured[0].startswith("https://example.test/nwis/iv/?")


def test_usgs_default_nwps_and_site_info_urls_match_hardcoded(monkeypatch):
    captured: list = []
    monkeypatch.setattr("meshai.env.usgs.urlopen", _capturing_urlopen(captured))
    adapter = USGSStreamsAdapter(USGSConfig())
    adapter._lookup_nwps_stages("13090500")
    # crosswalk (site_info_url) fires first, then the NWPS gauge lookup.
    assert len(captured) == 2
    assert captured[0].startswith("https://waterservices.usgs.gov/nwis/site/?")
    assert captured[1].startswith("https://api.water.noaa.gov/nwps/v1/gauges/")


def test_usgs_override_nwps_and_site_info_urls_change_fetch(monkeypatch):
    captured: list = []
    monkeypatch.setattr("meshai.env.usgs.urlopen", _capturing_urlopen(captured))
    cfg = USGSConfig(
        nwps_base_url="https://example.test/nwps",
        site_info_url="https://example.test/site/",
    )
    adapter = USGSStreamsAdapter(cfg)
    # A distinct site id (not "13090500") avoids the module-level
    # _nwps_cache/_nwps_cache_time from the default-URL test above short-
    # circuiting this call with a cached (stale-URL) result.
    adapter._lookup_nwps_stages("09876543")
    assert len(captured) == 2
    assert captured[0].startswith("https://example.test/site/?")
    assert captured[1].startswith("https://example.test/nwps/")


def test_usgs_lookup_site_uses_site_info_url(monkeypatch):
    captured: list = []

    def _fake(req, timeout=None):
        captured.append(req.full_url)
        raise URLError("no network in test")

    monkeypatch.setattr("meshai.env.usgs.urlopen", _fake)
    adapter = USGSStreamsAdapter(USGSConfig())
    adapter.lookup_site("13090500")
    assert captured, "lookup_site must attempt a site-info request"
    assert captured[0].startswith("https://waterservices.usgs.gov/nwis/site/?")


# ============================================================
# 8. traffic.py — TomTomConfig.base_url
# ============================================================

def test_traffic_default_url_matches_hardcoded(monkeypatch):
    captured: list = []
    monkeypatch.setattr("meshai.env.traffic.urlopen", _capturing_urlopen(captured))
    cfg = TomTomConfig(api_key="test-key")
    adapter = TomTomTrafficAdapter(cfg)
    adapter._fetch_point("wilderness_cell", 43.5, -115.0, 0.0)
    assert len(captured) == 1
    assert captured[0].startswith(
        "https://api.tomtom.com/traffic/services/4/flowSegmentData/relative0/10/json?"
    )


def test_traffic_override_url_changes_fetch(monkeypatch):
    captured: list = []
    monkeypatch.setattr("meshai.env.traffic.urlopen", _capturing_urlopen(captured))
    cfg = TomTomConfig(api_key="test-key", base_url="https://example.test/flow")
    adapter = TomTomTrafficAdapter(cfg)
    adapter._fetch_point("wilderness_cell", 43.5, -115.0, 0.0)
    assert len(captured) == 1
    assert captured[0].startswith("https://example.test/flow?")


# ============================================================
# 9. tle_fetch.py — SatpassConfig.tle_base_url
# ============================================================

def test_tle_fetch_default_url_matches_hardcoded():
    cfg = SatpassConfig(tle_groups=["weather"], norad_ids=[25544])
    adapter = TLEFetchAdapter(cfg)
    urls = [u for _, u in adapter._targets()]
    assert any(
        u == "https://celestrak.org/NORAD/elements/gp.php?GROUP=weather&FORMAT=tle"
        for u in urls
    )
    assert any(
        u == "https://celestrak.org/NORAD/elements/gp.php?CATNR=25544&FORMAT=tle"
        for u in urls
    )


def test_tle_fetch_override_url_changes_targets():
    cfg = SatpassConfig(
        tle_groups=["weather"],
        norad_ids=[25544],
        tle_base_url="https://example.test/gp.php",
    )
    adapter = TLEFetchAdapter(cfg)
    urls = [u for _, u in adapter._targets()]
    assert any(
        u == "https://example.test/gp.php?GROUP=weather&FORMAT=tle" for u in urls
    )
    assert any(
        u == "https://example.test/gp.php?CATNR=25544&FORMAT=tle" for u in urls
    )
