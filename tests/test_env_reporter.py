"""v0.6-5 env_reporter tests.

Uses the autouse conftest fixture which sets MESHAI_DB_PATH to a fresh tmp
file and runs init_db (so v1..v7 migrations + adapter_meta seeding all
happen automatically).
"""
from __future__ import annotations

import json
import time

import pytest

from meshai.notifications.env_reporter import EnvReporter
from meshai.persistence import get_db


@pytest.fixture
def reporter():
    return EnvReporter()


def _seed_fire(conn, *, irwin_id, name, acres, contained, lat=43.6, lon=-116.2,
                county="Ada", state="ID", declared_at=None, last_event_at=None):
    now = int(time.time())
    conn.execute(
        "INSERT OR REPLACE INTO fires(irwin_id, incident_name, incident_type, "
        "current_acres, current_contained_pct, lat, lon, county, state, "
        "declared_at, last_event_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (irwin_id, name, "WF", acres, contained, lat, lon, county, state,
         declared_at or now, last_event_at or now),
    )


def _seed_nws_alert(conn, *, cap_id, alert_type, severity, state="ID",
                     headline="", expires_at=None):
    now = int(time.time())
    conn.execute(
        "INSERT OR REPLACE INTO nws_alerts(event_id, alert_type, severity, "
        "county, state, headline, expires_at, first_seen_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (cap_id, alert_type, severity, "Ada", state, headline,
         expires_at or (now + 3600), now),
    )


def _seed_quake(conn, *, event_id, magnitude, place, lat=44.5, lon=-114.5):
    now = int(time.time())
    conn.execute(
        "INSERT OR REPLACE INTO quake_events(event_id, magnitude, depth_km, "
        "place, lat, lon, occurred_at, first_seen_at) VALUES (?,?,?,?,?,?,?,?)",
        (event_id, magnitude, 10.0, place, lat, lon, now, now),
    )


def _seed_traffic(conn, *, source, external_id, road, county="Ada", state="ID"):
    now = int(time.time())
    conn.execute(
        "INSERT OR REPLACE INTO traffic_events(source, external_id, road, "
        "direction, county, state, sub_type, impact, first_seen_at, "
        "last_seen_at, delay_seconds) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (source, external_id, road, "N", county, state, "accident",
         None, now, now, 600),
    )


def _seed_gauge(conn, *, site_id, gauge_name, value, threshold_state="action"):
    now = int(time.time())
    conn.execute(
        "INSERT INTO gauge_readings(site_id, gauge_name, reading_value, "
        "reading_unit, threshold_state, reading_time) VALUES (?,?,?,?,?,?)",
        (site_id, gauge_name, value, "ft", threshold_state, now),
    )


def _seed_swpc(conn, *, event_id, event_type="swpc_kindex", severity=2):
    now = int(time.time())
    conn.execute(
        "INSERT OR REPLACE INTO swpc_events(event_id, event_type, severity_int, "
        "payload_json, occurred_at, first_seen_at) VALUES (?,?,?,?,?,?)",
        (event_id, event_type, severity, "{}", now, now),
    )


def _seed_band_broadcast(conn):
    now = int(time.time())
    conn.execute(
        "INSERT OR REPLACE INTO band_conditions_broadcasts("
        "sent_at, scheduled_for, ratings_json, source) VALUES (?,?,?,?)",
        (now, now - 60, '{"40m": "Good"}', "swpc_local"),
    )


# ============================================================================
# meta gate
# ============================================================================


def test_adapter_included_defaults_true(reporter):
    """Adapter not in adapter_meta defaults to True (defensive)."""
    assert reporter._adapter_included("brand_new_adapter") is True


def test_adapter_included_reads_meta(reporter):
    conn = get_db()
    conn.execute(
        "UPDATE adapter_meta SET include_in_llm_context=0 WHERE adapter='wfigs'"
    )
    assert reporter._adapter_included("wfigs") is False


# ============================================================================
# build_env_summary
# ============================================================================


def test_env_summary_empty_when_no_data(reporter):
    """Empty tables -> empty summary."""
    assert reporter.build_env_summary() == ""


def test_env_summary_includes_fires(reporter):
    conn = get_db()
    _seed_fire(conn, irwin_id="F1", name="Cache Peak", acres=135, contained=10)
    _seed_fire(conn, irwin_id="F2", name="Bald Mtn", acres=42, contained=0)

    s = reporter.build_env_summary()
    assert "Active fires" in s
    assert "2" in s


def test_env_summary_excludes_when_meta_off(reporter):
    conn = get_db()
    _seed_fire(conn, irwin_id="F1", name="X", acres=1, contained=0)
    conn.execute(
        "UPDATE adapter_meta SET include_in_llm_context=0 WHERE adapter='wfigs'"
    )
    s = reporter.build_env_summary()
    assert "Active fires" not in s


def test_env_summary_combines_multiple_adapters(reporter):
    conn = get_db()
    _seed_fire(conn, irwin_id="F1", name="X", acres=1, contained=0)
    _seed_nws_alert(conn, cap_id="A1", alert_type="Tornado Warning",
                     severity="Extreme", headline="Tornado approaching")
    _seed_quake(conn, event_id="Q1", magnitude=3.2, place="3km E of Boise")
    _seed_traffic(conn, source="tomtom_incidents", external_id="T1",
                    road="I-84")
    s = reporter.build_env_summary()
    assert "Active fires" in s
    assert "NWS active alerts" in s
    assert "USGS earthquakes" in s
    assert "Active traffic incidents" in s


# ============================================================================
# build_fires_detail
# ============================================================================


def test_fires_detail_empty(reporter):
    assert reporter.build_fires_detail() == ""


def test_fires_detail_renders_rows(reporter):
    conn = get_db()
    _seed_fire(conn, irwin_id="F1", name="Cache Peak", acres=2_345,
                contained=23, county="Cassia", state="ID")
    _seed_fire(conn, irwin_id="F2", name="Bald Mountain", acres=420,
                contained=0, county="Boise", state="ID")

    text = reporter.build_fires_detail()
    assert "ACTIVE WILDFIRES" in text
    assert "Cache Peak" in text
    assert "2,345 ac" in text
    assert "23% contained" in text
    assert "Bald Mountain" in text


def test_fires_detail_includes_firms_summary(reporter):
    conn = get_db()
    now = int(time.time())
    for i in range(5):
        conn.execute(
            "INSERT INTO firms_pixels(lat, lon, acq_time, frp, confidence, satellite) "
            "VALUES (?,?,?,?,?,?)",
            (42.0 + i*0.01, -113.0, now, 50.0, "high", "N"),
        )
    text = reporter.build_fires_detail()
    assert "FIRMS HOTSPOTS" in text
    assert "5 pixels" in text


def test_fires_detail_meta_off_drops_block(reporter):
    conn = get_db()
    _seed_fire(conn, irwin_id="F1", name="X", acres=1, contained=0)
    conn.execute(
        "UPDATE adapter_meta SET include_in_llm_context=0 WHERE adapter='wfigs'"
    )
    conn.execute(
        "UPDATE adapter_meta SET include_in_llm_context=0 WHERE adapter='firms'"
    )
    assert reporter.build_fires_detail() == ""


# ============================================================================
# build_alerts_detail / build_quakes_detail / build_traffic_detail /
# build_gauges_detail / build_swpc_detail
# ============================================================================


def test_alerts_detail(reporter):
    conn = get_db()
    _seed_nws_alert(conn, cap_id="A1", alert_type="Tornado Warning",
                     severity="Extreme", headline="Tornado approaching Boise")
    text = reporter.build_alerts_detail()
    assert "Tornado Warning" in text
    assert "Extreme" in text


def test_quakes_detail(reporter):
    conn = get_db()
    _seed_quake(conn, event_id="Q1", magnitude=4.2, place="20km W of Salmon")
    text = reporter.build_quakes_detail()
    assert "M4.2" in text
    assert "20km W of Salmon" in text


def test_traffic_detail(reporter):
    conn = get_db()
    _seed_traffic(conn, source="tomtom_incidents", external_id="T1",
                    road="I-84")
    text = reporter.build_traffic_detail()
    assert "I-84" in text
    assert "accident" in text


def test_gauges_detail(reporter):
    conn = get_db()
    _seed_gauge(conn, site_id="USGS-13139510", gauge_name="Big Lost",
                  value=7.1, threshold_state="flood_minor")
    text = reporter.build_gauges_detail()
    assert "Big Lost" in text
    assert "flood_minor" in text


def test_swpc_detail(reporter):
    conn = get_db()
    _seed_swpc(conn, event_id="S1")
    _seed_band_broadcast(conn)
    text = reporter.build_swpc_detail()
    assert "RECENT SPACE WEATHER" in text
    assert "LATEST BAND CONDITIONS" in text


# ============================================================================
# build_drop_audit + build_all
# ============================================================================


def test_drop_audit_includes_dispatcher_counters(reporter):
    conn = get_db()
    conn.execute(
        "UPDATE dispatcher_state SET stale_dropped=4, cooldown_dropped=10 WHERE id=1"
    )
    text = reporter.build_drop_audit()
    assert "DISPATCHER COUNTERS" in text
    assert "stale=4" in text
    assert "cooldown=10" in text


def test_build_all_combines_all_non_empty_blocks(reporter):
    conn = get_db()
    _seed_fire(conn, irwin_id="F1", name="X", acres=1, contained=0)
    _seed_nws_alert(conn, cap_id="A1", alert_type="Tornado Warning",
                     severity="Extreme", headline="approaching")
    text = reporter.build_all()
    assert "ENVIRONMENTAL CONTEXT" in text
    assert "ACTIVE WILDFIRES" in text
    assert "ACTIVE NWS ALERTS" in text


def test_build_all_empty_when_all_off(reporter):
    """With every include_in_llm_context off, build_all returns empty."""
    conn = get_db()
    conn.execute("UPDATE adapter_meta SET include_in_llm_context=0")
    assert reporter.build_all() == ""
