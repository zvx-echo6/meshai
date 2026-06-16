"""Tests for v0.5.11 band-conditions scheduled broadcaster."""
import asyncio
import json
import time
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from meshai.notifications.scheduled.band_conditions import (
    BandConditionsScheduler,
    _heuristic_ratings,
    compute_band_ratings,
    format_band_conditions_wire,
    is_day_slot,
    record_slot_attempt,
    slot_epoch,
)
from meshai.persistence import close_thread_connection, init_db
from meshai.persistence import db as persistence_db


@pytest.fixture
def mem_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "band-test.sqlite")
    monkeypatch.setenv("MESHAI_DB_PATH", db_path)
    persistence_db._initialised.clear()
    close_thread_connection()
    conn = init_db()
    yield conn
    close_thread_connection()
    persistence_db._initialised.discard(db_path)


def _insert_swpc_kindex(conn, *, kp, when=None):
    when = when or int(time.time())
    conn.execute(
        "INSERT INTO swpc_events(event_id, event_type, payload_json, "
        "occurred_at, first_seen_at, last_broadcast_at) VALUES (?,?,?,?,?,?)",
        (f"kp_{when}", "swpc_kindex",
         json.dumps({"kp_index": kp, "time": "2026-06-05T15:00:00Z"}),
         when, when, None),
    )


def _insert_swpc_alert(conn, *, sfi, when=None):
    when = when or int(time.time())
    conn.execute(
        "INSERT INTO swpc_events(event_id, event_type, payload_json, "
        "occurred_at, first_seen_at, last_broadcast_at) VALUES (?,?,?,?,?,?)",
        (f"sfi_{when}", "swpc_alerts",
         json.dumps({"F10.7": sfi, "product_id": "F10.7-Daily"}),
         when, when, None),
    )


# ---- (a) compute with fresh SWPC quiet conditions ----------------------


def test_compute_quiet_conditions_all_good_to_fair(mem_db):
    """Kp=2 SFI=140 -> all bands Good/Fair on day; 80-40m Good at night."""
    now = int(time.time())
    _insert_swpc_kindex(mem_db, kp=2.0, when=now - 600)
    _insert_swpc_alert(mem_db, sfi=140.0, when=now - 1200)

    result = compute_band_ratings(now=now, hh_mm="14:00", allow_hamqsl=False)
    assert result is not None
    ratings, source = result
    assert source == "swpc_local"
    # High SFI + low Kp on day -> 30-20m and 17-15m and 12-10m all Good.
    assert ratings["30-20m"] == "Good"
    assert ratings["17-15m"] == "Good"
    assert ratings["12-10m"] == "Fair"  # SFI 140 right at the edge of Fair/Good
    # 80-40m on day is usually Fair at best.
    assert ratings["80-40m"] in ("Fair", "Good")


# ---- (b) compute with storm conditions ---------------------------------


def test_compute_storm_conditions_mostly_poor(mem_db):
    """Kp=7 (G3) crushes ratings to Poor on upper bands."""
    now = int(time.time())
    _insert_swpc_kindex(mem_db, kp=7.0, when=now - 600)
    _insert_swpc_alert(mem_db, sfi=90.0, when=now - 1200)

    result = compute_band_ratings(now=now, hh_mm="14:00", allow_hamqsl=False)
    assert result is not None
    ratings, _ = result
    assert ratings["17-15m"] == "Poor"
    assert ratings["12-10m"] == "Poor"
    assert ratings["30-20m"] == "Poor"
    assert ratings["80-40m"] == "Poor"


# ---- (c) HamQSL fallback when SWPC missing -----------------------------


def test_compute_falls_back_to_hamqsl_when_no_swpc(mem_db):
    """No SWPC rows -> HamQSL fallback. Mock the HTTP call."""
    XML = '''<?xml version="1.0"?>
<solar><solardata>
<calculatedconditions>
  <band name="80m-40m" time="day">Fair</band>
  <band name="80m-40m" time="night">Good</band>
  <band name="30m-20m" time="day">Good</band>
  <band name="30m-20m" time="night">Fair</band>
  <band name="17m-15m" time="day">Good</band>
  <band name="17m-15m" time="night">Poor</band>
  <band name="12m-10m" time="day">Fair</band>
  <band name="12m-10m" time="night">Poor</band>
</calculatedconditions>
</solardata></solar>
'''
    class Resp:
        status_code = 200
        text = XML
    def mock_get(url, timeout):
        return Resp()

    result = compute_band_ratings(now=int(time.time()), hh_mm="14:00",
                                    _http_get=mock_get)
    assert result is not None
    ratings, source = result
    assert source == "hamqsl_fallback"
    assert ratings["80-40m"] == "Fair"
    assert ratings["12-10m"] == "Fair"


# ---- (d) both fail -> None silent skip ---------------------------------


def test_compute_returns_none_when_both_sources_fail(mem_db):
    def mock_get(url, timeout):
        raise httpx_TimeoutError("simulated")
    class httpx_TimeoutError(Exception): pass
    result = compute_band_ratings(now=int(time.time()), hh_mm="14:00",
                                    _http_get=lambda u,t: (_ for _ in ()).throw(Exception("timeout")))
    assert result is None


# ---- (e) time-of-day selection -----------------------------------------


@pytest.mark.parametrize("hh_mm, expected_day", [
    ("06:00", True),
    ("14:00", True),
    ("22:00", False),
    ("23:30", False),
])
def test_is_day_slot(hh_mm, expected_day):
    assert is_day_slot(hh_mm) is expected_day


# ---- (f) wire format ---------------------------------------------------


def test_wire_format_day_slot():
    ratings = {"80-40m": "Fair", "30-20m": "Good",
                "17-15m": "Fair", "12-10m": "Poor"}
    wire = format_band_conditions_wire(ratings, "14:00")
    assert wire.startswith("🌞 Day Propagation")
    assert "📡 Band Conditions:" in wire
    assert "80-40m: 🟡 Fair" in wire
    assert "30-20m: 🟢 Good" in wire
    assert "17-15m: 🟡 Fair" in wire
    assert "12-10m: 🔴 Poor" in wire


def test_wire_format_night_slot():
    ratings = {"80-40m": "Good", "30-20m": "Fair",
                "17-15m": "Poor", "12-10m": "Poor"}
    wire = format_band_conditions_wire(ratings, "22:00")
    assert wire.startswith("🌙 Night Propagation")
    assert "80-40m: 🟢 Good" in wire


def test_wire_format_morning_slot():
    ratings = {"80-40m": "Fair", "30-20m": "Good",
                "17-15m": "Fair", "12-10m": "Poor"}
    wire = format_band_conditions_wire(ratings, "06:00")
    assert wire.startswith("☀️ Day Propagation")


# ---- (g) byte size under target ----------------------------------------


def test_wire_byte_size_under_target():
    ratings = {"80-40m": "Good", "30-20m": "Good",
                "17-15m": "Good", "12-10m": "Good"}
    wire = format_band_conditions_wire(ratings, "14:00")
    nb = len(wire.encode("utf-8"))
    assert nb < 130, f"wire {nb}B exceeds soft target -- {wire!r}"


def test_wire_has_4_band_rows_and_2_header_lines():
    ratings = {"80-40m": "Good", "30-20m": "Fair",
                "17-15m": "Fair", "12-10m": "Poor"}
    wire = format_band_conditions_wire(ratings, "06:00")
    lines = wire.split("\n")
    assert len(lines) == 6  # emoji headline + 📡 header + 4 band rows


# ---- (h) cold-start grace + dedup (i) via scheduler.fire_slot ----------


class _MockDispatcher:
    def __init__(self):
        self.calls = []

    async def dispatch_scheduled_broadcast(self, *, text, source_event_table,
                                              source_event_pk):
        self.calls.append((text, source_event_table, source_event_pk))
        return True


def _build_cfg():
    from meshai.config import Config
    cfg = Config()
    cfg.notifications.rules = []
    cfg.notifications.cold_start_grace_seconds = 0
    cfg.notifications.band_conditions_enabled = True
    cfg.notifications.band_conditions_schedule = ["06:00", "14:00", "22:00"]
    rf = cfg.notifications.toggles.get("rf_propagation")
    if rf is not None:
        rf.enabled = True
        rf.broadcast_channel = 1
    return cfg


def test_fire_slot_records_broadcast_row(mem_db):
    now = int(time.time())
    _insert_swpc_kindex(mem_db, kp=2.0, when=now - 600)
    _insert_swpc_alert(mem_db, sfi=140.0, when=now - 1200)
    sched = BandConditionsScheduler(_build_cfg(), _MockDispatcher())
    slot = now + 60   # arbitrary epoch
    asyncio.run(sched.fire_slot(slot, "14:00"))
    row = mem_db.execute(
        "SELECT source, ratings_json FROM band_conditions_broadcasts "
        "WHERE scheduled_for=?", (slot,)).fetchone()
    assert row is not None
    assert row["source"] == "swpc_local"
    assert json.loads(row["ratings_json"])["30-20m"] == "Good"


def test_fire_slot_dedup_via_unique_constraint(mem_db):
    """Same scheduled_for fired twice -> only one row, only one broadcast."""
    now = int(time.time())
    _insert_swpc_kindex(mem_db, kp=2.0, when=now - 600)
    _insert_swpc_alert(mem_db, sfi=140.0, when=now - 1200)
    disp = _MockDispatcher()
    sched = BandConditionsScheduler(_build_cfg(), disp)
    slot = now + 60
    asyncio.run(sched.fire_slot(slot, "14:00"))
    asyncio.run(sched.fire_slot(slot, "14:00"))   # dup firing

    n_rows = mem_db.execute(
        "SELECT COUNT(*) AS n FROM band_conditions_broadcasts "
        "WHERE scheduled_for=?", (slot,)).fetchone()["n"]
    assert n_rows == 1
    assert len(disp.calls) == 1   # second firing aborted before dispatch


def test_fire_slot_silent_skip_when_no_data(mem_db):
    """No SWPC + (mocked) HamQSL failure -> source='skipped_no_data'."""
    # Patch the HamQSL fetcher inside the module to simulate the HTTP error.
    from meshai.notifications.scheduled import band_conditions as bc_mod
    original = bc_mod._fetch_hamqsl
    bc_mod._fetch_hamqsl = lambda day, _http_get=None: None
    try:
        sched = BandConditionsScheduler(_build_cfg(), _MockDispatcher())
        slot = int(time.time()) + 60
        asyncio.run(sched.fire_slot(slot, "14:00"))
        row = mem_db.execute(
            "SELECT source FROM band_conditions_broadcasts "
            "WHERE scheduled_for=?", (slot,)).fetchone()
        assert row is not None
        assert row["source"] == "skipped_no_data"
    finally:
        bc_mod._fetch_hamqsl = original


# ---- record_slot_attempt direct unit test ------------------------------


def test_record_slot_attempt_returns_id_first_then_none(mem_db):
    """First insert returns a row id; duplicate returns None."""
    bid1 = record_slot_attempt(1_000_000, source="swpc_local",
                                ratings={"80-40m": "Good"})
    assert isinstance(bid1, int)
    bid2 = record_slot_attempt(1_000_000, source="swpc_local",
                                ratings={"80-40m": "Good"})
    assert bid2 is None


# ---- slot_epoch alignment ----------------------------------------------


def test_slot_epoch_returns_local_time_alignment():
    """slot_epoch('06:00', America/Boise) should be the same wall-clock
    hour regardless of UTC offset (DST handled automatically)."""
    now_dt = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    ep = slot_epoch(now_dt, "06:00", "America/Boise")
    # Mountain Daylight Time in June -> UTC-6 -> 06:00 MDT == 12:00 UTC.
    dt = datetime.fromtimestamp(ep, tz=timezone.utc)
    assert dt.hour == 12 and dt.minute == 0   # 06:00 MDT in June

    # Winter date -> UTC-7 -> 06:00 MST == 13:00 UTC.
    winter_dt = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
    ep = slot_epoch(winter_dt, "06:00", "America/Boise")
    dt = datetime.fromtimestamp(ep, tz=timezone.utc)
    assert dt.hour == 13 and dt.minute == 0   # 06:00 MST in January
