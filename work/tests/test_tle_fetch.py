"""Tests for the native Celestrak TLE fetcher (env.tle_fetch).

Covers epoch parsing, 3-line block parsing, upsert into the shared
sat_tles table, latest-epoch-wins on re-fetch, and malformed-block
tolerance. HTTP is monkeypatched — no network.
"""
from __future__ import annotations

import pytest

from meshai.env.tle_fetch import (
    TLEFetchAdapter,
    parse_tle_block,
    parse_tle_epoch,
)
from meshai.central.tle_handler import get_tle_by_norad
from meshai.config import SatpassConfig
from meshai.persistence import get_db


# A valid ISS 3-line set. Line-1 epoch field (cols 19-32) = 26182.50000000
# -> 2026 day-of-year 182.5 -> 2026-07-01T12:00:00+00:00.
ISS_TLE = (
    "ISS (ZARYA)\n"
    "1 25544U 98067A   26182.50000000  .00016717  00000-0  10270-3 0  9008\n"
    "2 25544  51.6400 208.9163 0007417  17.6777  85.6621 15.54225995 12345\n"
)

# Same satellite, a NEWER epoch (26183.50 -> 2026-07-02T12:00Z).
ISS_TLE_NEWER = (
    "ISS (ZARYA)\n"
    "1 25544U 98067A   26183.50000000  .00016717  00000-0  10270-3 0  9010\n"
    "2 25544  51.6400 208.9163 0007417  17.6777  85.6621 15.54225995 12347\n"
)

# Same satellite, an OLDER epoch (26181.50 -> 2026-06-30T12:00Z).
ISS_TLE_OLDER = (
    "ISS (ZARYA)\n"
    "1 25544U 98067A   26181.50000000  .00016717  00000-0  10270-3 0  9006\n"
    "2 25544  51.6400 208.9163 0007417  17.6777  85.6621 15.54225995 12343\n"
)


def _adapter(**overrides) -> TLEFetchAdapter:
    cfg = SatpassConfig(enabled=True, feed_source="native",
                        tle_groups=[], norad_ids=[25544], **overrides)
    return TLEFetchAdapter(cfg)


# -- epoch parsing ------------------------------------------------------------

def test_parse_tle_epoch_iso():
    iso = parse_tle_epoch(
        "1 25544U 98067A   26182.50000000  .00016717  00000-0  10270-3 0  9008")
    assert iso.startswith("2026-07-01T12:00:00")
    assert "+00:00" in iso


def test_parse_tle_epoch_two_digit_year_window():
    # YY=98 -> 1998 (>= 57 maps to 1900s).
    iso = parse_tle_epoch("1 25544U 98067A   98001.00000000  .0  0  0 0  1")
    assert iso.startswith("1998-01-01")


# -- block parsing ------------------------------------------------------------

def test_parse_tle_block_basic():
    recs = parse_tle_block(ISS_TLE)
    assert len(recs) == 1
    r = recs[0]
    assert r["norad_id"] == 25544
    assert r["name"] == "ISS (ZARYA)"
    assert r["line1"].startswith("1 25544")
    assert r["line2"].startswith("2 25544")
    assert r["epoch"].startswith("2026-07-01T12:00:00")


def test_parse_tle_block_skips_malformed():
    # First triple is garbage (line1 doesn't start with "1 "); second is valid.
    block = (
        "GARBAGE SAT\n"
        "not a real line1\n"
        "also not line2\n"
        + ISS_TLE
    )
    recs = parse_tle_block(block)
    norads = [r["norad_id"] for r in recs]
    assert 25544 in norads
    # The garbage entry must not have produced a record.
    assert all(isinstance(n, int) for n in norads)


# -- fetch + upsert -----------------------------------------------------------

def test_tick_upserts_into_sat_tles(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_fetch", lambda url: ISS_TLE)

    changed = adapter.tick(now=1_000_000)
    assert changed is True

    row = get_tle_by_norad(25544)
    assert row is not None
    assert row["name"] == "ISS (ZARYA)"
    assert row["line1"].startswith("1 25544")
    assert row["line2"].startswith("2 25544")
    assert row["epoch"].startswith("2026-07-01T12:00:00")


def test_latest_epoch_wins_on_refetch(monkeypatch):
    adapter = _adapter()

    monkeypatch.setattr(adapter, "_fetch", lambda url: ISS_TLE)
    assert adapter.tick(now=1_000_000) is True
    first = get_tle_by_norad(25544)["epoch"]

    # A newer epoch replaces it.
    adapter._last_tick = 0  # bypass interval gate for the test
    monkeypatch.setattr(adapter, "_fetch", lambda url: ISS_TLE_NEWER)
    assert adapter.tick(now=2_000_000) is True
    newer = get_tle_by_norad(25544)["epoch"]
    assert newer > first

    # An older epoch is ignored (no write).
    adapter._last_tick = 0
    monkeypatch.setattr(adapter, "_fetch", lambda url: ISS_TLE_OLDER)
    changed = adapter.tick(now=3_000_000)
    assert changed is False
    assert get_tle_by_norad(25544)["epoch"] == newer


def test_malformed_block_does_not_crash_tick(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(
        adapter, "_fetch",
        lambda url: "COMPLETE GARBAGE\nno lines here\n")
    # Should complete without raising and write nothing.
    changed = adapter.tick(now=1_000_000)
    assert changed is False
    assert get_tle_by_norad(25544) is None


def test_fetch_error_is_isolated(monkeypatch):
    adapter = _adapter()

    def boom(url):
        raise RuntimeError("HTTP 503")

    monkeypatch.setattr(adapter, "_fetch", boom)
    changed = adapter.tick(now=1_000_000)
    assert changed is False
    assert adapter.health_status["last_error"] is not None
    assert adapter.health_status["consecutive_errors"] == 1


def test_storage_only_no_events(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_fetch", lambda url: ISS_TLE)
    adapter.tick(now=1_000_000)
    assert adapter.get_events() == []
    assert adapter.to_event({}) is None


def test_interval_gate_skips_early_ticks(monkeypatch):
    adapter = _adapter(tle_refresh_seconds=21600)
    calls = []
    monkeypatch.setattr(adapter, "_fetch",
                        lambda url: calls.append(url) or ISS_TLE)
    adapter.tick(now=1_000_000.0)    # first tick fetches
    adapter.tick(now=1_000_100.0)    # 100s later, within interval -> skipped
    assert len(calls) == 1


def test_no_targets_configured_is_noop():
    cfg = SatpassConfig(enabled=True, feed_source="native",
                        tle_groups=[], norad_ids=[])
    adapter = TLEFetchAdapter(cfg)
    assert adapter.tick(now=1_000_000) is False


def test_group_and_catnr_urls():
    cfg = SatpassConfig(tle_groups=["weather"], norad_ids=[25544])
    adapter = TLEFetchAdapter(cfg)
    urls = [u for _, u in adapter._targets()]
    assert any("GROUP=weather&FORMAT=tle" in u for u in urls)
    assert any("CATNR=25544&FORMAT=tle" in u for u in urls)
    assert all(u.startswith("https://celestrak.org/NORAD/elements/gp.php") for u in urls)
