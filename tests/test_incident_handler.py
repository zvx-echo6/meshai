"""Tests for meshai.central.incident_handler (v0.5.9).

Coverage:
  Tomtom parsing + rendering (a/b/c/d):
    (a) jam, accident, road_closed, lane_closed, road_works each render with
        correct emoji + phrase + delay segment
    (b) magnitude_of_delay == 0 events filtered at entrance
    (c) delay == null events render WITHOUT the delay segment
    (d) time_validity past/future events filtered

  Per-incident change-detection (e-i):
    (e) republish with no change -> drop silently, no new audit
    (f) magnitude bump up -> Update
    (g) delay double (>=2x) -> Update
    (h) icon change -> Update
    (i) 8h heartbeat -> Update

  state_511 / itd_511 EventType branching (j-m):
    (j) state_511_atis incident parses
    (k) state_511_atis closure parses
    (l) state_511_atis special_event parses (synthetic)
    (m) itd_511 incident parses

  Decoupled callback (n) and traffic_events UPSERT (o):
    (n) cold-start scenario -- handler runs but callback never fires;
        second pass still emits New: (not Update:)
    (o) existing traffic_events row gets UPSERTed across passes
"""

import re
import time

import pytest

from meshai.central.incident_handler import (
    INCIDENT_BROADCAST_HEARTBEAT_S,
    handle_incident,
)
from meshai.persistence import close_thread_connection, init_db
from meshai.persistence import db as persistence_db


# ---------- fixtures ------------------------------------------------------


@pytest.fixture
def mem_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "incident-test.sqlite")
    monkeypatch.setenv("MESHAI_DB_PATH", db_path)
    persistence_db._initialised.clear()
    close_thread_connection()
    conn = init_db()
    yield conn
    close_thread_connection()
    persistence_db._initialised.discard(db_path)


@pytest.fixture
def no_photon(monkeypatch):
    import meshai.central_normalizer as cn
    monkeypatch.setattr(cn, "_photon_reverse_places", lambda lat, lon: [])
    if hasattr(cn, "_H3_NEAREST_CACHE"):
        cn._H3_NEAREST_CACHE.clear()


# ---------- tomtom envelope builder --------------------------------------


_TTI_A = "cfb0c03f-9ab9-46f9-ac21-9b17d0f715f2"
_TTI_B = "13ca4176-4eea-428e-a807-49bee662159a"
_TTI_C = "3573b54b-9e55-4aff-83d3-253048825e77"


def _tomtom_env(*, tti=_TTI_A,
                  icon_category=6,
                  magnitude=2,
                  delay=412,
                  time_validity="present",
                  description="Queuing traffic on I-84 Westbound from Orchard St to ID-55. ",
                  road_numbers=("I-84",),
                  from_loc="Orchard St/Exit 52 (I-84)",
                  to_loc="ID-55/Exit 46 (I-84)",
                  start_time=None,
                  end_time=None,
                  lat=43.5833926533, lon=-116.2598321532,
                  state_code="ID", bbox_name="treasure_valley_ext",
                  geocoder_city="Boise"):
    inner_id = f"ID:tomtom:TTI-{tti}-TTR{int(time.time()*1000)}"
    geocoder = {"city": geocoder_city, "county": "Ada", "state": "ID",
                 "country": "United States", "landclass": None}
    return {
        "id": inner_id,
        "subject": "central.traffic.incident.id",
        "data": {
            "id": inner_id, "adapter": "tomtom_incidents",
            "category": "incident.tomtom_incidents", "severity": 1,
            "geo": {"centroid": [lon, lat], "primary_region": "US-ID"},
            "data": {
                "id": inner_id,
                "description": description,
                "event_code": 108,
                "from": from_loc, "to": to_loc,
                "magnitude_of_delay": magnitude,
                "icon_category": icon_category,
                "length": 6112.13,
                "delay": delay,
                "road_numbers": list(road_numbers),
                "start_time": start_time, "end_time": end_time,
                "time_validity": time_validity,
                "state_code": state_code, "bbox_name": bbox_name,
                "latitude": lat, "longitude": lon,
                "_enriched": {"geocoder": geocoder},
            },
        },
    }


def _state_511_env(*, layer="Incidents", category_prefix="incident",
                     event_sub_type="crash",
                     roadway="US-95", direction="Both",
                     is_full_closure=False,
                     external_id="ID:Incidents:33948",
                     lat=48.5295, lon=-116.4293,
                     geocoder_city="Naples", county="Boundary"):
    return {
        "id": external_id,
        "subject": f"central.traffic.{category_prefix}.id",
        "data": {
            "id": external_id, "adapter": "state_511_atis",
            "category": f"{category_prefix}.state_511_atis", "severity": 1,
            "geo": {"centroid": [lon, lat], "primary_region": "US-WA"},
            "data": {
                "roadway_name": roadway, "direction": direction,
                "event_sub_type": event_sub_type,
                "description": "test event",
                "is_full_closure": is_full_closure,
                "layer": layer,
                # v0.5.9 GAMMA: default to non-ID neighbor state because
                # state_511_atis no longer covers Idaho (itd_511 took over).
                # Tests that want to exercise the ID-skip path override these.
                "county": county, "state": "Washington", "state_code": "WA",
                "start_date": None,
                "last_updated": None,
                "latitude": lat, "longitude": lon,
                "_enriched": {"geocoder": {
                    "city": geocoder_city, "county": county, "state": "ID",
                    "country": "United States", "landclass": None,
                }},
            },
        },
    }


def _itd_511_env(*, category_prefix="incident",
                   event_type_short="incident",
                   event_sub_type="crash",
                   roadway="I-84", direction="East",
                   is_full_closure=False,
                   external_id="ITD:469:17",
                   lat=43.6486, lon=-116.4870,
                   geocoder_city="Caldwell"):
    return {
        "id": external_id,
        "subject": f"central.traffic.{category_prefix}.us.id",
        "data": {
            "id": external_id, "adapter": "itd_511",
            "category": f"{category_prefix}.itd_511", "severity": 1,
            "geo": {"centroid": [lon, lat], "primary_region": "US-ID"},
            "data": {
                "event_type_short": event_type_short,
                "event_sub_type": event_sub_type,
                "roadway_name": roadway, "direction": direction,
                "description": "test itd event",
                "lanes_affected": "All lanes affected",
                "is_full_closure": is_full_closure,
                "itd_severity": "None",
                "comment": "", "cause": "roadwork",
                "organization": "ERS",
                "recurrence_text": "", "recurrence_schedules": [],
                "restrictions": {}, "encoded_polyline": "",
                "id_internal": 17, "source_id": "469",
                "reported_epoch": None,
                "last_updated_epoch": None,
                "start_epoch": None,
                "planned_end_epoch": None,
                "latitude": lat, "longitude": lon,
                "_enriched": {"geocoder": {
                    "city": geocoder_city, "county": "Canyon", "state": "ID",
                }},
            },
        },
    }


def _commit(data, committed_at):
    cb = data.get("_on_broadcast_committed")
    assert callable(cb), "handler must attach commit callback"
    cb(committed_at)


# ============================================================================
# (a) tomtom parsing -- all five icon categories render correctly
# ============================================================================


@pytest.mark.parametrize("icon, expected_emoji, expected_phrase", [
    (1, "🚨", "crash"),
    (6, "🚗", "jam"),
    (7, "🟠", "lane closed"),
    (8, "🚫", "road closed"),
    (9, "🚧", "road works"),
])
def test_a_tomtom_icon_renders(mem_db, no_photon, icon, expected_emoji, expected_phrase):
    env = _tomtom_env(icon_category=icon, magnitude=2, delay=300)
    data = {}
    wire = handle_incident(env, env["subject"], data=data, now=1_000_000)
    assert wire is not None
    assert wire.startswith(f"{expected_emoji} New: I-84")
    assert expected_phrase in wire
    assert "near Boise" in wire
    assert "5 min delay" in wire
    assert "@ 43.583,-116.260" in wire


# ============================================================================
# (b) tomtom magnitude_of_delay == 0 events filtered
# ============================================================================


def test_b_tomtom_magnitude_zero_filtered(mem_db, no_photon):
    env = _tomtom_env(icon_category=6, magnitude=0, delay=10)
    data = {}
    wire = handle_incident(env, env["subject"], data=data, now=1_000_000)
    assert wire is None
    # filtered envelope leaves no traffic_events row
    n_rows = mem_db.execute("SELECT COUNT(*) AS n FROM traffic_events").fetchone()["n"]
    assert n_rows == 0
    # but the event IS logged to event_log handled=0 for accounting
    n_log = mem_db.execute(
        "SELECT COUNT(*) AS n FROM event_log WHERE source='tomtom_incidents'"
    ).fetchone()["n"]
    assert n_log == 1
    assert "_on_broadcast_committed" not in data


# ============================================================================
# (c) tomtom delay == null events render WITHOUT delay segment
# ============================================================================


def test_c_tomtom_delay_null_no_delay_segment(mem_db, no_photon):
    env = _tomtom_env(icon_category=1, magnitude=3, delay=None)
    data = {}
    wire = handle_incident(env, env["subject"], data=data, now=1_000_000)
    assert wire is not None
    assert "crash" in wire
    assert "min delay" not in wire   # no delay segment
    assert "@ 43.583,-116.260" in wire


# ============================================================================
# (d) tomtom time_validity past/future filtered
# ============================================================================


@pytest.mark.parametrize("validity", ["past", "future"])
def test_d_tomtom_time_validity_filtered(mem_db, no_photon, validity):
    env = _tomtom_env(icon_category=6, magnitude=2, delay=300,
                       time_validity=validity)
    data = {}
    wire = handle_incident(env, env["subject"], data=data, now=1_000_000)
    assert wire is None
    n_rows = mem_db.execute(
        "SELECT COUNT(*) AS n FROM traffic_events").fetchone()["n"]
    assert n_rows == 0


# ============================================================================
# (e) per-incident dedup -- republish with no change drops silently
# ============================================================================


def test_e_per_incident_dedup_no_change(mem_db, no_photon):
    env = _tomtom_env(icon_category=6, magnitude=2, delay=300)
    data1 = {}
    wire1 = handle_incident(env, env["subject"], data=data1, now=1_000_000)
    assert wire1 is not None
    _commit(data1, 1_000_001)

    # Re-publish 5 minutes later, same magnitude/delay/icon.
    data2 = {}
    wire2 = handle_incident(env, env["subject"], data=data2, now=1_000_300)
    assert wire2 is None  # no change, no broadcast


# ============================================================================
# (f) magnitude bump triggers Update
# ============================================================================


def test_f_magnitude_bump_triggers_update(mem_db, no_photon):
    env1 = _tomtom_env(icon_category=6, magnitude=2, delay=300)
    data1 = {}
    handle_incident(env1, env1["subject"], data=data1, now=1_000_000)
    _commit(data1, 1_000_001)

    # v0.5.9 REVISED gate (A): magnitude bump no longer fires Update.
    # State still flips in traffic_events, but no wire string returns.
    env2 = _tomtom_env(icon_category=6, magnitude=3, delay=300)
    data2 = {}
    wire2 = handle_incident(env2, env2["subject"], data=data2, now=1_000_300)
    assert wire2 is None
    # Current magnitude tracked in the row.
    row = mem_db.execute(
        "SELECT magnitude_of_delay FROM traffic_events "
        "WHERE source='tomtom_incidents'").fetchone()
    assert row["magnitude_of_delay"] == 3


# ============================================================================
# (g) delay double triggers Update
# ============================================================================


def test_g_delay_double_triggers_update(mem_db, no_photon):
    env1 = _tomtom_env(icon_category=6, magnitude=2, delay=300)
    data1 = {}
    handle_incident(env1, env1["subject"], data=data1, now=1_000_000)
    _commit(data1, 1_000_001)

    # v0.5.9 REVISED gate (A): delay double no longer fires Update.
    env2 = _tomtom_env(icon_category=6, magnitude=2, delay=700)
    data2 = {}
    wire2 = handle_incident(env2, env2["subject"], data=data2, now=1_000_300)
    assert wire2 is None
    row = mem_db.execute(
        "SELECT delay_seconds FROM traffic_events "
        "WHERE source='tomtom_incidents'").fetchone()
    assert row["delay_seconds"] == 700


def test_g_delay_below_double_no_update(mem_db, no_photon):
    """delay 300 -> 500 (1.67x) should NOT trigger broadcast."""
    env1 = _tomtom_env(icon_category=6, magnitude=2, delay=300)
    data1 = {}
    handle_incident(env1, env1["subject"], data=data1, now=1_000_000)
    _commit(data1, 1_000_001)

    env2 = _tomtom_env(icon_category=6, magnitude=2, delay=500)
    data2 = {}
    wire2 = handle_incident(env2, env2["subject"], data=data2, now=1_000_300)
    assert wire2 is None


# ============================================================================
# (h) icon change triggers Update
# ============================================================================


def test_h_icon_change_triggers_update(mem_db, no_photon):
    env1 = _tomtom_env(icon_category=6, magnitude=2, delay=300)
    data1 = {}
    handle_incident(env1, env1["subject"], data=data1, now=1_000_000)
    _commit(data1, 1_000_001)

    # v0.5.9 REVISED gate (A): icon change no longer fires Update.
    env2 = _tomtom_env(icon_category=8, magnitude=2, delay=300)
    data2 = {}
    wire2 = handle_incident(env2, env2["subject"], data=data2, now=1_000_300)
    assert wire2 is None
    row = mem_db.execute(
        "SELECT icon_category FROM traffic_events "
        "WHERE source='tomtom_incidents'").fetchone()
    assert row["icon_category"] == "road_closed"


# ============================================================================
# (i) 8h heartbeat triggers Update
# ============================================================================


def test_i_8h_heartbeat_triggers_update(mem_db, no_photon):
    env = _tomtom_env(icon_category=6, magnitude=2, delay=300)
    data1 = {}
    handle_incident(env, env["subject"], data=data1, now=1_000_000)
    _commit(data1, 1_000_001)

    # v0.5.9 REVISED gate (A): heartbeat no longer fires Update.
    later = 1_000_001 + INCIDENT_BROADCAST_HEARTBEAT_S
    data2 = {}
    wire2 = handle_incident(env, env["subject"], data=data2, now=later)
    assert wire2 is None


# ============================================================================
# (j) state_511_atis incident parses
# ============================================================================


def test_j_state_511_incident_parses(mem_db, no_photon):
    env = _state_511_env(layer="Incidents", category_prefix="incident",
                          event_sub_type="crash")
    data = {}
    wire = handle_incident(env, env["subject"], data=data, now=1_000_000)
    assert wire is not None
    assert wire.startswith("🚨 New: US-95")  # crash -> 🚨
    assert "crash" in wire
    assert "near Naples" in wire
    row = mem_db.execute(
        "SELECT source, sub_type, state FROM traffic_events "
        "WHERE source='state_511_atis'").fetchone()
    assert row["sub_type"] == "accident"
    # v0.5.9 GAMMA: state_511_atis is non-ID only -- helper now defaults to WA.
    assert row["state"] == "WA"


# ============================================================================
# (k) state_511_atis closure parses
# ============================================================================


def test_k_state_511_closure_parses(mem_db, no_photon):
    env = _state_511_env(layer="Closures", category_prefix="closure",
                          event_sub_type="roadConstruction",
                          is_full_closure=True,
                          external_id="ID:Closures:33950")
    data = {}
    wire = handle_incident(env, env["subject"], data=data, now=1_000_000)
    assert wire is not None
    # roadConstruction -> road_works -> 🚧
    assert wire.startswith("🚧 New:")
    assert "all lanes closed" in wire


# ============================================================================
# (l) state_511_atis special_event parses
# ============================================================================


def test_l_state_511_special_event_parses(mem_db, no_photon):
    env = _state_511_env(layer="Special Events",
                          category_prefix="special_event",
                          event_sub_type="parade",
                          external_id="ID:SpecialEvents:42")
    data = {}
    wire = handle_incident(env, env["subject"], data=data, now=1_000_000)
    assert wire is not None
    # parade -> 🎪
    assert wire.startswith("🎪 New:")
    assert "parade" in wire


# ============================================================================
# (m) itd_511 incident parses
# ============================================================================


def test_m_itd_511_incident_parses(mem_db, no_photon):
    env = _itd_511_env(category_prefix="incident",
                        event_type_short="incident",
                        event_sub_type="crash")
    data = {}
    wire = handle_incident(env, env["subject"], data=data, now=1_000_000)
    assert wire is not None
    assert wire.startswith("🚨 New:")
    row = mem_db.execute(
        "SELECT source, state FROM traffic_events "
        "WHERE source='itd_511'").fetchone()
    assert row["state"] == "ID"


# ============================================================================
# (n) decoupled callback -- cold-start scenario still emits New: on second pass
# ============================================================================


def test_n_cold_start_then_resume_still_new(mem_db, no_photon):
    env = _tomtom_env(icon_category=6, magnitude=2, delay=300)
    data1 = {}
    wire1 = handle_incident(env, env["subject"], data=data1, now=1_000_000)
    assert wire1.startswith("🚗 New:")
    # Cold-start grace drops the broadcast -- DO NOT call _commit().

    # 5 minutes later, same incident republishes.
    data2 = {}
    wire2 = handle_incident(env, env["subject"], data=data2, now=1_000_300)
    assert wire2 is not None
    assert wire2.startswith("🚗 New:"), \
        "must still be New: until commit callback fires"

    row = mem_db.execute(
        "SELECT last_broadcast_at, last_broadcast_magnitude "
        "FROM traffic_events WHERE source='tomtom_incidents'").fetchone()
    assert row["last_broadcast_at"] is None
    assert row["last_broadcast_magnitude"] is None


# ============================================================================
# (o) traffic_events row gets UPSERTed on each pass; event_log handled flips
# ============================================================================


def test_o_traffic_events_upsert_and_event_log_handled_flip(mem_db, no_photon):
    env1 = _tomtom_env(icon_category=6, magnitude=2, delay=300)
    data1 = {}
    handle_incident(env1, env1["subject"], data=data1, now=1_000_000)

    # event_log row exists with handled=0 BEFORE callback.
    el_pre = mem_db.execute(
        "SELECT handled FROM event_log "
        "WHERE source='tomtom_incidents' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert el_pre["handled"] == 0

    _commit(data1, 1_000_001)

    el_post = mem_db.execute(
        "SELECT handled FROM event_log "
        "WHERE source='tomtom_incidents' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert el_post["handled"] == 1

    fr_post = mem_db.execute(
        "SELECT last_broadcast_at, last_broadcast_magnitude, "
        "last_broadcast_delay_seconds, last_broadcast_icon_category "
        "FROM traffic_events WHERE source='tomtom_incidents'"
    ).fetchone()
    assert fr_post["last_broadcast_at"] == 1_000_001
    assert fr_post["last_broadcast_magnitude"] == 2
    assert fr_post["last_broadcast_delay_seconds"] == 300
    assert fr_post["last_broadcast_icon_category"] == "jam"

    # Re-publish: UPSERT updates current_* but doesn't touch last_broadcast_*.
    env2 = _tomtom_env(icon_category=6, magnitude=2, delay=500)
    data2 = {}
    wire2 = handle_incident(env2, env2["subject"], data=data2, now=1_000_300)
    # v0.5.9 REVISED: no Update broadcasts regardless of delta size --
    # under the OLD rule this was 'delay 1.67x not enough', now it's
    # 'we never re-broadcast'.
    assert wire2 is None
    fr2 = mem_db.execute(
        "SELECT delay_seconds, last_broadcast_delay_seconds "
        "FROM traffic_events WHERE source='tomtom_incidents'"
    ).fetchone()
    assert fr2["delay_seconds"] == 500    # UPSERT happened
    assert fr2["last_broadcast_delay_seconds"] == 300  # last broadcast unchanged


# ============================================================================
# v0.5.9 REVISED -- conservative gates
# ============================================================================


def test_p_known_id_all_changed_no_broadcast(mem_db, no_photon):
    """Regression guard for the new no-Update rule. Republish the SAME
    external_id with magnitude AND delay AND icon all changed. The old rule
    would have triggered Update on any one of those; the new rule fires
    nothing."""
    env1 = _tomtom_env(icon_category=6, magnitude=2, delay=300)
    data1 = {}
    wire1 = handle_incident(env1, env1["subject"], data=data1, now=1_000_000)
    assert wire1.startswith("🚗 New:")
    _commit(data1, 1_000_001)

    env2 = _tomtom_env(icon_category=8, magnitude=4, delay=700)  # ALL changed
    data2 = {}
    wire2 = handle_incident(env2, env2["subject"], data=data2, now=1_000_300)
    assert wire2 is None, "no Update broadcasts under the v0.5.9 REVISED rule"

    # State still tracks the latest field values.
    row = mem_db.execute(
        "SELECT magnitude_of_delay, delay_seconds, icon_category "
        "FROM traffic_events WHERE source='tomtom_incidents'").fetchone()
    assert row["magnitude_of_delay"] == 4
    assert row["delay_seconds"] == 700
    assert row["icon_category"] == "road_closed"


# ============================================================================
# Freshness gate (q/r/s)
# ============================================================================


def _now_anchor_relative(start_age_seconds: int):
    """Choose (now, start_time_iso) so that `now - parse(start_time) ==
    start_age_seconds`. Used by the freshness-gate tests."""
    import datetime as _dt
    now = 2_000_000_000  # arbitrary fixed epoch >>1970
    start_iso = _dt.datetime.fromtimestamp(
        now - start_age_seconds, tz=_dt.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    return now, start_iso


def test_q_fresh_event_15min_ago_broadcasts(mem_db, no_photon):
    """Event started 15 min ago -- WITHIN the 30-min fresh window. New: fires."""
    now, start_iso = _now_anchor_relative(15 * 60)
    env = _tomtom_env(icon_category=6, magnitude=2, delay=300,
                       start_time=start_iso)
    data = {}
    wire = handle_incident(env, env["subject"], data=data, now=now)
    assert wire is not None
    assert wire.startswith("🚗 New:")
    n_rows = mem_db.execute(
        "SELECT COUNT(*) AS n FROM traffic_events").fetchone()["n"]
    assert n_rows == 1


def test_r_stale_event_45min_ago_dropped_no_row(mem_db, no_photon):
    """Event started 45 min ago -- OUTSIDE the 30-min window. Drop AT
    handler entrance, no UPSERT into traffic_events, event_log handled=0."""
    now, start_iso = _now_anchor_relative(45 * 60)
    env = _tomtom_env(icon_category=6, magnitude=2, delay=300,
                       start_time=start_iso)
    data = {}
    wire = handle_incident(env, env["subject"], data=data, now=now)
    assert wire is None

    n_rows = mem_db.execute(
        "SELECT COUNT(*) AS n FROM traffic_events").fetchone()["n"]
    assert n_rows == 0, "no UPSERT for stale incidents"

    n_log = mem_db.execute(
        "SELECT COUNT(*) AS n FROM event_log WHERE handled=0 "
        "AND source='tomtom_incidents'").fetchone()["n"]
    assert n_log == 1, "stale envelope must still be logged (handled=0)"


def test_s_null_start_time_default_allow(mem_db, no_photon):
    """start_time missing -> default-allow (treat as fresh and broadcast)."""
    env = _tomtom_env(icon_category=6, magnitude=2, delay=300,
                       start_time=None)
    data = {}
    wire = handle_incident(env, env["subject"], data=data, now=1_000_000)
    assert wire is not None
    assert wire.startswith("🚗 New:")


# ============================================================================
# Per-source startTime field path (t)
# ============================================================================


def test_t_state_511_start_date_path(mem_db, no_photon):
    """state_511_atis pulls start_time from inner.data.start_date ('5/28/26,
    10:45 PM' format). Construct fresh (within 30 min) and stale (>30 min)
    cases and confirm gate behavior is per-source-correct."""
    # 5 min ago in the 511-date format.
    import datetime as _dt
    now = int(_dt.datetime(2026, 6, 4, 12, 0, tzinfo=_dt.timezone.utc).timestamp())
    fresh_date = _dt.datetime.fromtimestamp(now - 5 * 60,
                                              tz=_dt.timezone.utc).strftime(
        "%-m/%-d/%y, %-I:%M %p")
    env = _state_511_env(layer="Incidents", category_prefix="incident",
                          event_sub_type="crash")
    # Replace the default start_date with a fresh one.
    env["data"]["data"]["start_date"] = fresh_date
    wire = handle_incident(env, env["subject"], data={}, now=now)
    assert wire is not None
    assert wire.startswith("🚨 New:")

    # Stale variant (45 min ago).
    stale_date = _dt.datetime.fromtimestamp(now - 45 * 60,
                                              tz=_dt.timezone.utc).strftime(
        "%-m/%-d/%y, %-I:%M %p")
    env2 = _state_511_env(layer="Incidents", category_prefix="incident",
                            event_sub_type="crash",
                            external_id="ID:Incidents:99999")
    env2["data"]["data"]["start_date"] = stale_date
    wire2 = handle_incident(env2, env2["subject"], data={}, now=now)
    assert wire2 is None


def test_t_itd_511_start_epoch_path(mem_db, no_photon):
    """itd_511 pulls start_time from inner.data.start_epoch (Unix int)."""
    now = 1_700_000_000
    # Fresh (5 min ago) variant.
    env = _itd_511_env(category_prefix="incident",
                        event_type_short="incident",
                        event_sub_type="crash")
    env["data"]["data"]["start_epoch"] = now - 5 * 60
    wire = handle_incident(env, env["subject"], data={}, now=now)
    assert wire is not None
    assert wire.startswith("🚨 New:")

    # Stale variant (45 min ago).
    env2 = _itd_511_env(category_prefix="incident",
                         event_type_short="incident",
                         event_sub_type="crash",
                         external_id="ITD:469:99999")
    env2["data"]["data"]["start_epoch"] = now - 45 * 60
    wire2 = handle_incident(env2, env2["subject"], data={}, now=now)
    assert wire2 is None


def test_t_tomtom_start_time_path(mem_db, no_photon):
    """tomtom pulls start_time from inner.data.start_time (ISO-8601)."""
    now, fresh_iso = _now_anchor_relative(5 * 60)
    env = _tomtom_env(icon_category=6, magnitude=2, delay=300,
                       start_time=fresh_iso)
    wire = handle_incident(env, env["subject"], data={}, now=now)
    assert wire is not None
    assert wire.startswith("🚗 New:")

    _, stale_iso = _now_anchor_relative(45 * 60)
    env2 = _tomtom_env(icon_category=6, magnitude=2, delay=300,
                        start_time=stale_iso,
                        tti="11111111-2222-3333-4444-555555555555")
    wire2 = handle_incident(env2, env2["subject"], data={}, now=now)
    assert wire2 is None


# ============================================================================
# v0.5.9 GAMMA -- two-sided freshness gate + state_511 ID skip
# ============================================================================


def test_u_future_scheduled_event_dropped(mem_db, no_photon):
    """itd_511 work_zone envelopes can carry start_epoch in the future
    (scheduled construction). The v0.5.9 REVISED one-sided gate let those
    slip through. The GAMMA fix rejects negative ages too."""
    import datetime as _dt
    now = 2_000_000_000
    # start_epoch 8 hours in the future
    env = _itd_511_env(category_prefix="incident",
                        event_type_short="incident",
                        event_sub_type="crash",
                        external_id="ITD:future:1")
    env["data"]["data"]["start_epoch"] = now + 8 * 3600
    wire = handle_incident(env, env["subject"], data={}, now=now)
    assert wire is None
    n_rows = mem_db.execute(
        "SELECT COUNT(*) AS n FROM traffic_events").fetchone()["n"]
    assert n_rows == 0
    n_log = mem_db.execute(
        "SELECT COUNT(*) AS n FROM event_log WHERE handled=0 "
        "AND source='itd_511'").fetchone()["n"]
    assert n_log == 1


def test_v_state_511_id_skipped_at_handler_entrance(mem_db, no_photon):
    """state_511_atis with state_code='ID' is skipped at handler entrance --
    no parse, no traffic_events row, event_log records the skip."""
    env = _state_511_env(layer="Incidents", category_prefix="incident",
                          event_sub_type="crash")
    # Override defaults (post-GAMMA helper defaults to WA) to ID for this test.
    env["data"]["data"]["state_code"] = "ID"
    env["data"]["data"]["state"] = "Idaho"
    env["data"]["geo"]["primary_region"] = "US-ID"
    wire = handle_incident(env, env["subject"], data={}, now=1_000_000)
    assert wire is None
    n_rows = mem_db.execute(
        "SELECT COUNT(*) AS n FROM traffic_events").fetchone()["n"]
    assert n_rows == 0
    row = mem_db.execute(
        "SELECT category, handled FROM event_log "
        "WHERE source='state_511_atis' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert "|skip_id" in row["category"]
    assert row["handled"] == 0


def test_v_state_511_non_id_still_processed(mem_db, no_photon):
    """Regression guard: state_511_atis with state_code='WA' (or anything
    that's not ID) keeps going through the handler. After Idaho cutover
    we still need state_511 for neighbor coverage."""
    env = _state_511_env(layer="Incidents", category_prefix="incident",
                          event_sub_type="crash", county="Spokane")
    # Override state_code to WA.
    env["data"]["data"]["state_code"] = "WA"
    env["data"]["geo"]["primary_region"] = "US-WA"
    wire = handle_incident(env, env["subject"], data={}, now=1_000_000)
    assert wire is not None
    assert wire.startswith("🚨 New:")
    # traffic_events row written for WA event.
    row = mem_db.execute(
        "SELECT state FROM traffic_events WHERE source='state_511_atis'"
    ).fetchone()
    assert row is not None
    assert row["state"] == "WA"


def test_w_itd_511_future_scheduled_dropped_via_start_epoch(mem_db, no_photon):
    """Direct exercise of the itd_511 start_epoch field path with a
    future-scheduled value (the Phase-1 leak source). Confirms the gate
    really uses start_epoch and the new two-sided check catches it."""
    import meshai.central.incident_handler as h
    env = _itd_511_env(category_prefix="incident",
                        event_type_short="incident",
                        event_sub_type="crash",
                        external_id="ITD:future:work")
    env["data"]["data"]["start_epoch"] = 99_000_000_000  # year ~5108 -- definitely future
    se = h._extract_start_time_epoch(env, "itd_511")
    assert se == 99_000_000_000
    now = 2_000_000_000
    wire = handle_incident(env, env["subject"], data={}, now=now)
    assert wire is None
