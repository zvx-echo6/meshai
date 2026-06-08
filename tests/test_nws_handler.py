"""Tests for v0.5.10 NWS handler."""
import pytest

from meshai.central.nws_handler import handle_nws, _emoji_for_event
from meshai.persistence import close_thread_connection, init_db
from meshai.persistence import db as persistence_db


@pytest.fixture
def mem_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "nws-test.sqlite")
    monkeypatch.setenv("MESHAI_DB_PATH", db_path)
    persistence_db._initialised.clear()
    close_thread_connection()
    conn = init_db()
    yield conn
    close_thread_connection()
    persistence_db._initialised.discard(db_path)


def _nws_env(*, cap_id="urn:oid:test.001",
              event="Severe Thunderstorm Warning",
              severity_str="Severe",
              area_desc="Twin Falls County",
              county="Twin Falls", state="ID",
              expires="2026-06-05T03:00:00Z",
              msg_type=None,
              lat=42.500, lon=-114.460,
              geocoder_city=None,
              category="wx.alert.severe_thunderstorm_warning"):
    return {
        "id": cap_id, "subject": "central.wx.alert.us.id",
        "data": {
            "id": cap_id, "adapter": "nws", "category": category,
            "severity": 2,
            "geo": {"centroid": [lon, lat], "primary_region": "US-ID"},
            "data": {
                "id": cap_id, "@type": "wx:Alert",
                "event": event, "severity": severity_str,
                "areaDesc": area_desc, "msgType": msg_type or "Alert",
                "headline": f"{event} for {area_desc}",
                "description": "Storm details.",
                "expires": expires,
                "_enriched": {"geocoder": {"city": geocoder_city,
                                              "county": county, "state": state}},
            },
        },
    }


def _commit(data, t):
    data["_on_broadcast_committed"](float(t))


# ---- severity gate ----


def test_severe_thunderstorm_warning_broadcasts(mem_db):
    env = _nws_env(severity_str="Severe", event="Severe Thunderstorm Warning")
    data = {}
    wire = handle_nws(env, env["subject"], data=data, now=1_000_000)
    assert wire is not None
    assert wire.startswith("🌩️")
    assert "Severe Thunderstorm Warning" in wire


def test_extreme_emergency_broadcasts(mem_db):
    env = _nws_env(severity_str="Extreme", event="Tornado Warning",
                    category="wx.alert.tornado_warning")
    data = {}
    wire = handle_nws(env, env["subject"], data=data, now=1_000_000)
    assert wire is not None
    assert wire.startswith("🌪️")


def test_special_weather_statement_skipped(mem_db):
    env = _nws_env(severity_str="Minor", event="Special Weather Statement",
                    category="wx.alert.special_weather_statement")
    data = {}
    wire = handle_nws(env, env["subject"], data=data, now=1_000_000)
    assert wire is None
    n_rows = mem_db.execute("SELECT COUNT(*) AS n FROM nws_alerts").fetchone()["n"]
    assert n_rows == 0
    n_log = mem_db.execute(
        "SELECT COUNT(*) AS n FROM event_log WHERE source='nws' AND handled=0"
    ).fetchone()["n"]
    assert n_log == 1


def test_watch_severity_moderate_skipped(mem_db):
    env = _nws_env(severity_str="Moderate", event="Severe Thunderstorm Watch",
                    category="wx.alert.severe_thunderstorm_watch")
    data = {}
    wire = handle_nws(env, env["subject"], data=data, now=1_000_000)
    assert wire is None


# ---- emoji map ----


@pytest.mark.parametrize("event_type, expected_emoji", [
    ("Severe Thunderstorm Warning", "🌩️"),
    ("Tornado Warning",             "🌪️"),
    ("Flash Flood Warning",         "🌊"),
    ("Flood Warning",               "🌊"),
    ("Winter Storm Warning",        "❄️"),
    ("Blizzard Warning",            "❄️"),
    ("Excessive Heat Warning",      "🌡️"),
    ("High Wind Warning",           "🌬️"),
    ("Red Flag Warning",            "🔥"),
    ("Fire Weather Watch",          "🔥"),
    ("Air Quality Alert",           "😷"),
    ("Freeze Warning",              "🥶"),
    ("Coastal Flood Warning",       "🌊"),
    ("(some other warning)",        "⚠️"),
])
def test_emoji_map(event_type, expected_emoji):
    assert _emoji_for_event(event_type) == expected_emoji


# ---- tombstone ----


def test_cancel_msgType_tombstone_skipped(mem_db):
    env = _nws_env(severity_str="Severe", event="Severe Thunderstorm Warning",
                    msg_type="Cancel")
    data = {}
    wire = handle_nws(env, env["subject"], data=data, now=1_000_000)
    assert wire is None
    n_log = mem_db.execute(
        "SELECT COUNT(*) AS n FROM event_log WHERE source='nws' AND handled=0"
    ).fetchone()["n"]
    assert n_log == 1


def test_expire_msgType_tombstone_skipped(mem_db):
    env = _nws_env(severity_str="Severe", event="Tornado Warning",
                    msg_type="Expire")
    wire = handle_nws(env, env["subject"], data={}, now=1_000_000)
    assert wire is None


# ---- per-CAP-id dedup ----


def test_per_cap_id_dedup_no_reissue(mem_db):
    env = _nws_env(severity_str="Severe")
    data1 = {}
    wire1 = handle_nws(env, env["subject"], data=data1, now=1_000_000)
    assert wire1 is not None
    _commit(data1, 1_000_001)

    # Same CAP id republishes (e.g. headline update). Should NOT re-broadcast.
    data2 = {}
    wire2 = handle_nws(env, env["subject"], data=data2, now=1_000_300)
    assert wire2 is None


# ---- area_desc fallback ----


def test_area_desc_used_when_geocoder_city_missing(mem_db):
    env = _nws_env(severity_str="Severe", area_desc="Twin Falls County",
                    geocoder_city=None)
    wire = handle_nws(env, env["subject"], data={}, now=1_000_000)
    assert "Twin Falls" in wire


def test_geocoder_city_preferred_over_area_desc(mem_db):
    env = _nws_env(severity_str="Severe", area_desc="Twin Falls County",
                    geocoder_city="Twin Falls")
    wire = handle_nws(env, env["subject"], data={}, now=1_000_000)
    assert "Twin Falls" in wire   # either source serves the same anchor


# ---- commit callback ----


def test_commit_callback_updates_last_broadcast(mem_db):
    env = _nws_env(severity_str="Severe")
    data = {}
    handle_nws(env, env["subject"], data=data, now=1_000_000)
    fr_pre = mem_db.execute(
        "SELECT last_broadcast_at FROM nws_alerts").fetchone()
    assert fr_pre["last_broadcast_at"] is None
    _commit(data, 1_000_001)
    fr_post = mem_db.execute(
        "SELECT last_broadcast_at FROM nws_alerts").fetchone()
    assert fr_post["last_broadcast_at"] == 1_000_001
    # event_log row flipped to handled=1.
    el = mem_db.execute(
        "SELECT handled FROM event_log WHERE source='nws' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert el["handled"] == 1


def test_wire_includes_event_area_and_expires(mem_db):
    env = _nws_env(severity_str="Severe", lat=42.500, lon=-114.460)
    wire = handle_nws(env, env["subject"], data={}, now=1_000_000)
    assert "Severe Thunderstorm Warning" in wire
    assert "Twin Falls County" in wire
    assert "until" in wire.lower()
