"""Tests for the native Celestrak TLE fetcher (env.tle_fetch).

Covers epoch parsing, 3-line block parsing, upsert into the shared
sat_tles table, latest-epoch-wins on re-fetch, and malformed-block
tolerance. HTTP is monkeypatched — no network.
"""
from __future__ import annotations

import datetime as _dt

import pytest

from meshai.env.tle_fetch import (
    TLEFetchAdapter,
    parse_tle_block,
    parse_tle_epoch,
)
from meshai.env.satellite.tle_store import get_tle_by_norad
from meshai.config import SatpassConfig
from meshai.persistence import get_db


# -- time-relative TLE fixture generation --------------------------------
#
# These used to be hardcoded 3-line sets with a fixed 2026-06-30..07-02
# epoch. get_tle_by_norad() filters on `epoch >= now() - STALE_DAYS`
# (STALE_DAYS=14, real wall-clock `now`), so a fixed-date fixture ages out
# 14 days after it's written and silently starts failing
# test_tick_upserts_into_sat_tles / test_latest_epoch_wins_on_refetch.
# Deriving the epoch from the real current time at test-collection time
# means these can never expire again.


def _tle_epoch_field(dt: _dt.datetime) -> str:
    """Build a TLE line-1 epoch field (cols 19-32): YYDDD.DDDDDDDD."""
    yy = dt.year % 100
    start_of_year = _dt.datetime(dt.year, 1, 1, tzinfo=_dt.timezone.utc)
    doy_frac = (dt - start_of_year).total_seconds() / 86400.0 + 1.0
    doy_int = int(doy_frac)
    frac = doy_frac - doy_int
    return f"{yy:02d}{doy_int:03d}.{round(frac * 1e8):08d}"


def _tle_checksum(line_without_checksum_digit: str) -> int:
    """Standard TLE mod-10 checksum: sum of digits, '-' counts as 1,
    everything else 0."""
    total = 0
    for ch in line_without_checksum_digit:
        if ch.isdigit():
            total += int(ch)
        elif ch == "-":
            total += 1
    return total % 10


def _iss_tle_block(dt: _dt.datetime, element_set_num: str, revnum: str) -> str:
    """Build a valid 3-line ISS (ZARYA) TLE with the given epoch. All
    other orbital elements are fixed (arbitrary but internally
    consistent) — only epoch/checksum/element-set/revnum vary, mirroring
    the original hand-written fixtures."""
    line1_body = (f"1 25544U 98067A   {_tle_epoch_field(dt)}  .00016717  "
                  f"00000-0  10270-3 0  {element_set_num}")
    line1 = f"{line1_body}{_tle_checksum(line1_body)}"
    line2 = (f"2 25544  51.6400 208.9163 0007417  17.6777  85.6621 "
             f"15.54225995 {revnum}")
    return f"ISS (ZARYA)\n{line1}\n{line2}\n"


# Base epoch: 2 days ago. Comfortably inside the 14-day STALE_DAYS window
# no matter when the suite runs, and never in the future.
_NOW = _dt.datetime.now(_dt.timezone.utc)
_BASE_EPOCH = _NOW - _dt.timedelta(days=2)

# A valid ISS 3-line set at the base epoch.
ISS_TLE = _iss_tle_block(_BASE_EPOCH, "900", "12345")
# Same satellite, a NEWER epoch (1 day after base).
ISS_TLE_NEWER = _iss_tle_block(_BASE_EPOCH + _dt.timedelta(days=1), "901", "12347")
# Same satellite, an OLDER epoch (1 day before base).
ISS_TLE_OLDER = _iss_tle_block(_BASE_EPOCH - _dt.timedelta(days=1), "900", "12343")

# ISO epoch string parse_tle_epoch() will produce for ISS_TLE's line 1 --
# computed via the real parser (not re-derived independently) so
# assertions can't drift from the actual parsing behavior under test.
ISS_TLE_EPOCH_ISO = parse_tle_epoch(ISS_TLE.splitlines()[1])


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
    assert r["epoch"] == ISS_TLE_EPOCH_ISO


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
    assert row["epoch"] == ISS_TLE_EPOCH_ISO


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
