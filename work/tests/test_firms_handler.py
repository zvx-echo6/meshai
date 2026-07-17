"""Tests for v0.6-1 FIRMS handler (storage-only).

The handler never returns a wire string (storage-only contract). All
assertions check side effects on `firms_pixels` + `event_log`. Per the
audit doc finding #2 the handler must close the v0.5.13 silent-drop on
`central.fire.hotspot.>` envelopes.

Envelope shape sourced from firms-investigation.md sampling (250 envelopes
2026-05-28..06-04, all VIIRS).
"""
import pytest

from meshai.central import firms_handler
from meshai.central.firms_handler import handle_firms
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


def _firms_env(*,
               lat=42.19664, lon=-113.70981, frp=135.93,
               confidence="high", satellite="N",
               acq_date="2026-05-28", acq_time="1949",
               bright_ti4=367.0, daynight="D",
               envelope_id="firms_001",
               category="fire.hotspot.viirs",
               severity=3,
               region="unknown",
               extras=None):
    """Build a FIRMS CloudEvents envelope matching the live wire shape.

    Default fixture is SAMPLE B from firms-investigation.md (Cache Peak
    high-confidence 135 MW fire). Override individual fields for filter
    coverage.
    """
    payload = {
        "id":          envelope_id,
        "latitude":    lat,
        "longitude":   lon,
        "frp":         frp,
        "confidence":  confidence,
        "satellite":   satellite,
        "instrument":  "VIIRS",
        "acq_date":    acq_date,
        "acq_time":    acq_time,
        "bright_ti4":  bright_ti4,
        "daynight":    daynight,
        "version":     "2.0NRT",
        "_enriched":   {"geocoder": {"landclass": "Cache Peak Roadless Area",
                                       "elevation_m": 2151.9}},
    }
    if extras: payload.update(extras)
    return {
        "subject": f"central.fire.hotspot.viirs_snpp.{confidence}.{region}",
        "id":      envelope_id,
        "data": {
            "id":       envelope_id,
            "adapter":  "firms",
            "category": category,
            "severity": severity,
            "time":     "2026-05-28T19:49:00Z",
            "geo":      {"centroid": [lon, lat]},
            "data":     payload,
        },
    }


def _row_count(mem_db, table):
    return mem_db.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


def _last_event_log(mem_db):
    r = mem_db.execute(
        "SELECT * FROM event_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(r) if r else None


# ============================================================================
# Happy path: high-confidence pixel stored
# ============================================================================


def test_high_confidence_pixel_persisted(mem_db):
    env = _firms_env()
    out = handle_firms(env, env["subject"], data={}, now=1_780_660_000)

    assert out is None, "storage-only handler must never return a wire string"
    assert _row_count(mem_db, "firms_pixels") == 1

    row = mem_db.execute("SELECT * FROM firms_pixels").fetchone()
    assert row["lat"] == pytest.approx(42.19664)
    assert row["lon"] == pytest.approx(-113.70981)
    assert row["frp"] == pytest.approx(135.93)
    assert row["confidence"] == "high"
    assert row["satellite"] == "N"
    assert row["brightness"] == pytest.approx(367.0)
    assert row["irwin_id"] is None  # unattached; v0.6 fire-tracker fills later

    # acq_time should be the parsed UTC epoch of 2026-05-28 19:49Z.
    import datetime as _dt
    expected = int(_dt.datetime(2026, 5, 28, 19, 49,
                                  tzinfo=_dt.timezone.utc).timestamp())
    assert row["acq_time"] == expected

    # event_log: handled=1, table_name set, table_pk = inserted rowid.
    log = _last_event_log(mem_db)
    assert log["source"] == "firms"
    assert log["handled"] == 1
    assert log["table_name"] == "firms_pixels"
    assert log["table_pk"] == str(row["id"])


def test_nominal_confidence_pixel_persisted_under_default_floor(mem_db):
    """Default FIRMS_CONFIDENCE_FLOOR='low' must accept nominal/high/low."""
    env = _firms_env(confidence="nominal", envelope_id="firms_002")
    out = handle_firms(env, env["subject"], data={}, now=1_780_660_000)
    assert out is None
    assert _row_count(mem_db, "firms_pixels") == 1


def test_low_confidence_pixel_persisted_under_default_floor(mem_db):
    env = _firms_env(confidence="low", envelope_id="firms_003")
    out = handle_firms(env, env["subject"], data={}, now=1_780_660_000)
    assert out is None
    assert _row_count(mem_db, "firms_pixels") == 1


# ============================================================================
# Confidence floor when bumped up
# ============================================================================


def test_low_confidence_dropped_when_floor_is_nominal(mem_db, monkeypatch):
    monkeypatch.setattr(firms_handler, "FIRMS_CONFIDENCE_FLOOR", "nominal")
    env = _firms_env(confidence="low", envelope_id="firms_lo")
    out = handle_firms(env, env["subject"], data={}, now=1_780_660_000)
    assert out is None
    assert _row_count(mem_db, "firms_pixels") == 0

    log = _last_event_log(mem_db)
    assert log["handled"] == 0
    assert log["category"].endswith("|below_confidence_floor")


def test_unknown_confidence_value_dropped(mem_db):
    env = _firms_env(confidence="bogus", envelope_id="firms_bog")
    out = handle_firms(env, env["subject"], data={}, now=1_780_660_000)
    assert out is None
    assert _row_count(mem_db, "firms_pixels") == 0
    log = _last_event_log(mem_db)
    assert log["category"].endswith("|below_confidence_floor")


# ============================================================================
# FRP floor
# ============================================================================


def test_low_frp_dropped_when_floor_set(mem_db, monkeypatch):
    monkeypatch.setattr(firms_handler, "FIRMS_FRP_FLOOR", 5.0)
    env = _firms_env(frp=1.4, confidence="high", envelope_id="firms_frp_low")
    out = handle_firms(env, env["subject"], data={}, now=1_780_660_000)
    assert out is None
    assert _row_count(mem_db, "firms_pixels") == 0
    log = _last_event_log(mem_db)
    assert log["category"].endswith("|below_frp_floor")


def test_frp_at_floor_stored(mem_db, monkeypatch):
    monkeypatch.setattr(firms_handler, "FIRMS_FRP_FLOOR", 5.0)
    env = _firms_env(frp=5.0, confidence="high", envelope_id="firms_frp_at")
    out = handle_firms(env, env["subject"], data={}, now=1_780_660_000)
    assert out is None
    assert _row_count(mem_db, "firms_pixels") == 1


def test_missing_frp_dropped_when_floor_set(mem_db, monkeypatch):
    monkeypatch.setattr(firms_handler, "FIRMS_FRP_FLOOR", 5.0)
    env = _firms_env(confidence="high", envelope_id="firms_no_frp",
                      extras={"frp": None})
    out = handle_firms(env, env["subject"], data={}, now=1_780_660_000)
    assert out is None
    assert _row_count(mem_db, "firms_pixels") == 0


def test_missing_frp_stored_when_floor_zero(mem_db):
    """Default FIRMS_FRP_FLOOR=0: missing FRP still stores (null in column)."""
    env = _firms_env(confidence="high", envelope_id="firms_no_frp_2",
                      extras={"frp": None})
    out = handle_firms(env, env["subject"], data={}, now=1_780_660_000)
    assert out is None
    assert _row_count(mem_db, "firms_pixels") == 1
    row = mem_db.execute("SELECT * FROM firms_pixels").fetchone()
    assert row["frp"] is None


# ============================================================================
# Bbox filter (default None = pass-through; explicit bbox tested both sides)
# ============================================================================


def test_bbox_none_passes_through(mem_db):
    env = _firms_env(lat=51.0, lon=10.0,   # Germany
                      envelope_id="firms_eu")
    out = handle_firms(env, env["subject"], data={}, now=1_780_660_000)
    assert out is None
    assert _row_count(mem_db, "firms_pixels") == 1


def test_bbox_drops_outside(mem_db, monkeypatch):
    # Idaho-ish bbox.
    monkeypatch.setattr(firms_handler, "FIRMS_BBOX_OPTIONAL",
                        (42.0, -117.5, 49.0, -111.0))
    env = _firms_env(lat=51.0, lon=10.0, envelope_id="firms_out")
    out = handle_firms(env, env["subject"], data={}, now=1_780_660_000)
    assert out is None
    assert _row_count(mem_db, "firms_pixels") == 0
    log = _last_event_log(mem_db)
    assert log["category"].endswith("|outside_bbox")


def test_bbox_keeps_inside(mem_db, monkeypatch):
    monkeypatch.setattr(firms_handler, "FIRMS_BBOX_OPTIONAL",
                        (42.0, -117.5, 49.0, -111.0))
    env = _firms_env(envelope_id="firms_in")   # Cache Peak is inside
    out = handle_firms(env, env["subject"], data={}, now=1_780_660_000)
    assert out is None
    assert _row_count(mem_db, "firms_pixels") == 1


# ============================================================================
# Dedup: same satellite pixel observation arriving twice = no-op
# ============================================================================


def test_dedup_same_pixel_idempotent(mem_db):
    env = _firms_env(envelope_id="dup_001")
    handle_firms(env, env["subject"], data={}, now=1_780_660_000)
    handle_firms(env, env["subject"], data={}, now=1_780_660_001)

    assert _row_count(mem_db, "firms_pixels") == 1, "OR IGNORE collapses dup"
    # Two event_log rows; second is dedup_hit.
    rows = mem_db.execute(
        "SELECT category, table_name, handled FROM event_log "
        "WHERE source='firms' ORDER BY id"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["table_name"] == "firms_pixels"
    assert rows[1]["table_name"] is None
    assert rows[1]["category"].endswith("|dedup_hit")


def test_dedup_collapses_lat_lon_float_noise(mem_db):
    """Same coord with sub-1m float noise must hit the same dedup key.
    5-decimal rounding => differences in the 6th+ decimal are absorbed."""
    e1 = _firms_env(lat=42.196641234567, lon=-113.709810000001,
                     envelope_id="dup_a")
    e2 = _firms_env(lat=42.196641111111, lon=-113.709810999999,
                     envelope_id="dup_b")
    handle_firms(e1, e1["subject"], data={}, now=1_780_660_000)
    handle_firms(e2, e2["subject"], data={}, now=1_780_660_001)
    assert _row_count(mem_db, "firms_pixels") == 1


def test_dedup_different_satellite_stored_separately(mem_db):
    """Same coord + acq_time but different satellite is 2 distinct observations."""
    e1 = _firms_env(satellite="N",   envelope_id="sat_n")
    e2 = _firms_env(satellite="N20", envelope_id="sat_n20")
    handle_firms(e1, e1["subject"], data={}, now=1_780_660_000)
    handle_firms(e2, e2["subject"], data={}, now=1_780_660_001)
    assert _row_count(mem_db, "firms_pixels") == 2


def test_dedup_different_acq_time_stored_separately(mem_db):
    """Same pixel observed on two passes 12h apart -> 2 rows."""
    e1 = _firms_env(acq_time="0700", envelope_id="t1")
    e2 = _firms_env(acq_time="1900", envelope_id="t2")
    handle_firms(e1, e1["subject"], data={}, now=1_780_660_000)
    handle_firms(e2, e2["subject"], data={}, now=1_780_660_001)
    assert _row_count(mem_db, "firms_pixels") == 2


# ============================================================================
# Bad / missing inputs
# ============================================================================


def test_missing_coords_dropped(mem_db):
    env = _firms_env(envelope_id="no_coords",
                      extras={"latitude": None, "longitude": None})
    out = handle_firms(env, env["subject"], data={}, now=1_780_660_000)
    assert out is None
    assert _row_count(mem_db, "firms_pixels") == 0
    log = _last_event_log(mem_db)
    assert log["category"].endswith("|missing_coords")


def test_missing_acq_time_dropped(mem_db):
    env = _firms_env(envelope_id="no_acq",
                      extras={"acq_date": None, "acq_time": None})
    out = handle_firms(env, env["subject"], data={}, now=1_780_660_000)
    assert out is None
    assert _row_count(mem_db, "firms_pixels") == 0
    log = _last_event_log(mem_db)
    assert log["category"].endswith("|missing_acq_time")


def test_non_firms_adapter_passes_through(mem_db):
    """Defense in depth: handler must early-return on non-firms envelopes."""
    env = _firms_env(envelope_id="wrong_adapter")
    env["data"]["adapter"] = "nws"
    out = handle_firms(env, env["subject"], data={}, now=1_780_660_000)
    assert out is None
    assert _row_count(mem_db, "firms_pixels") == 0
    assert _row_count(mem_db, "event_log") == 0


def test_acq_time_int_accepted(mem_db):
    """FIRMS sometimes publishes acq_time as int 2013 rather than '2013'."""
    env = _firms_env(envelope_id="int_acq",
                      extras={"acq_time": 1949})
    out = handle_firms(env, env["subject"], data={}, now=1_780_660_000)
    assert out is None
    assert _row_count(mem_db, "firms_pixels") == 1


def test_short_acq_time_zero_padded(mem_db):
    """acq_time '49' (early-morning pass) must zero-pad to '0049'."""
    env = _firms_env(envelope_id="short_acq",
                      extras={"acq_time": "49"})
    out = handle_firms(env, env["subject"], data={}, now=1_780_660_000)
    assert out is None
    assert _row_count(mem_db, "firms_pixels") == 1

