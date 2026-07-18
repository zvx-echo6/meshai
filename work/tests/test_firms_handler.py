"""Tests for the shared FIRMS pixel-ingest core (`_ingest_pixel_core`) +
`_parse_acq_epoch` -- the parts of the retired v0.6-1 FIRMS handler that are
still LIVE, shared by the native `env/firms.py` adapter via
`ingest_hotspot_pixel`.

chore/ripout-2dii: `handle_firms` (the dead Central NATS-envelope entrypoint
this file used to test -- confidence/FRP/bbox filtering, envelope field
extraction, missing-coords/missing-acq-time drops, non-firms-adapter guard,
event_log accounting) has been REMOVED from `meshai.env.fire_fusion` -- zero
live production callers, and no live equivalent (the native adapter's own
confidence/bbox filtering happens upstream in `env/firms.py`'s CSV fetch, a
different code path entirely; see `tests/test_adapter_firms.py`). Those tests
were deleted with it.

What remains: the dedup behavior of `_ingest_pixel_core` (INSERT OR IGNORE on
a meters-quantized dedup key -- genuinely shared/live, exercised by BOTH the
native adapter and formerly by handle_firms) and `_parse_acq_epoch`'s
int/short/zero-padded acq_time parsing (also shared/live -- env/firms.py
imports and calls it directly). Both rewritten to drive
`ingest_hotspot_pixel` directly instead of the dead handler.
"""
import pytest

from meshai.env.fire_fusion import ingest_hotspot_pixel, _parse_acq_epoch
from meshai.persistence import close_thread_connection, init_db
from meshai.persistence import db as persistence_db


# ---------- fixtures --------------------------------------------------------


@pytest.fixture
def mem_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "firms-test.sqlite")
    monkeypatch.setenv("MESHAI_DB_PATH", db_path)
    persistence_db._initialised.clear()
    close_thread_connection()
    conn = init_db()
    yield conn
    close_thread_connection()
    persistence_db._initialised.discard(db_path)


def _pixel(*, lat=42.19664, lon=-113.70981, frp=135.93, confidence="high",
           satellite="N", acq_date="2026-05-28", acq_time="1949",
           brightness=367.0):
    acq_epoch = _parse_acq_epoch(acq_date, acq_time)
    return {"lat": lat, "lon": lon, "frp": frp, "confidence": confidence,
            "brightness": brightness, "satellite": satellite,
            "acq_epoch": acq_epoch}


def _row_count(mem_db, table):
    return mem_db.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


# ============================================================================
# _parse_acq_epoch -- acq_time format quirks (shared with env/firms.py)
# ============================================================================


def test_acq_time_int_accepted():
    """FIRMS sometimes publishes acq_time as int 2013 rather than '2013'."""
    assert _parse_acq_epoch("2026-05-28", 1949) is not None
    assert _parse_acq_epoch("2026-05-28", 1949) == _parse_acq_epoch("2026-05-28", "1949")


def test_short_acq_time_zero_padded():
    """acq_time '49' (early-morning pass) must zero-pad to '0049'."""
    epoch = _parse_acq_epoch("2026-05-28", "49")
    assert epoch is not None
    import datetime as _dt
    dt = _dt.datetime.fromtimestamp(epoch, tz=_dt.timezone.utc)
    assert (dt.hour, dt.minute) == (0, 49)


def test_missing_acq_time_returns_none():
    assert _parse_acq_epoch(None, None) is None
    assert _parse_acq_epoch("2026-05-28", None) is None


# ============================================================================
# Dedup: same satellite pixel observation arriving twice = no-op.
# `_ingest_pixel_core`'s meters-quantized dedup_key + INSERT OR IGNORE is the
# LIVE, source-agnostic core shared by ingest_hotspot_pixel.
# ============================================================================


def test_dedup_same_pixel_idempotent(mem_db):
    p = _pixel()
    ingest_hotspot_pixel(p, now=1_780_660_000)
    ingest_hotspot_pixel(p, now=1_780_660_001)
    assert _row_count(mem_db, "firms_pixels") == 1, "OR IGNORE collapses dup"


def test_dedup_collapses_lat_lon_float_noise(mem_db):
    """Same coord with sub-1m float noise must hit the same dedup key.
    Meters-based quantization absorbs differences well under 1 pixel."""
    p1 = _pixel(lat=42.196641234567, lon=-113.709810000001)
    p2 = _pixel(lat=42.196641111111, lon=-113.709810999999)
    ingest_hotspot_pixel(p1, now=1_780_660_000)
    ingest_hotspot_pixel(p2, now=1_780_660_001)
    assert _row_count(mem_db, "firms_pixels") == 1


def test_dedup_different_satellite_stored_separately(mem_db):
    """Same coord + acq_time but different satellite is 2 distinct observations."""
    ingest_hotspot_pixel(_pixel(satellite="N"), now=1_780_660_000)
    ingest_hotspot_pixel(_pixel(satellite="N20"), now=1_780_660_001)
    assert _row_count(mem_db, "firms_pixels") == 2


def test_dedup_different_acq_time_stored_separately(mem_db):
    """Same pixel observed on two passes 12h apart -> 2 rows."""
    ingest_hotspot_pixel(_pixel(acq_time="0700"), now=1_780_660_000)
    ingest_hotspot_pixel(_pixel(acq_time="1900"), now=1_780_660_001)
    assert _row_count(mem_db, "firms_pixels") == 2


def test_missing_frp_stored(mem_db):
    """A pixel with no FRP still stores (null in column) -- ingest_hotspot_pixel
    never filters on FRP (that was handle_firms-specific, now gone)."""
    p = _pixel()
    p["frp"] = None
    ingest_hotspot_pixel(p, now=1_780_660_000)
    assert _row_count(mem_db, "firms_pixels") == 1
    row = mem_db.execute("SELECT * FROM firms_pixels").fetchone()
    assert row["frp"] is None
