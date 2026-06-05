"""Tests for v0.5.12 usgs_nwis handler."""
import pytest

# v0.6-4: IDAHO_CURATED_SITES dict moved to gauge_sites SQLite table;
# import the seed data from the curation module as a back-compat alias.
from meshai.persistence.curation import _GAUGE_SITES_SEED as IDAHO_CURATED_SITES
from meshai.central.nwis_handler import handle_nwis
from meshai.persistence import close_thread_connection, init_db
from meshai.persistence import db as persistence_db


@pytest.fixture
def mem_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "nwis-test.sqlite")
    monkeypatch.setenv("MESHAI_DB_PATH", db_path)
    persistence_db._initialised.clear()
    close_thread_connection()
    conn = init_db()
    yield conn
    close_thread_connection()
    persistence_db._initialised.discard(db_path)


def _nwis_env(*, site_id="USGS-13186000",
                parameter_code="00065", value=13.0,
                unit="ft", time_iso="2026-06-05T15:00:00Z",
                lat=43.612, lon=-111.654,
                envelope_id=None):
    envelope_id = envelope_id or f"nwis_{site_id}_{time_iso}"
    return {
        "id": envelope_id, "subject": f"central.hydro.{parameter_code}.usgs.{site_id}.us.id",
        "data": {
            "id": envelope_id, "adapter": "nwis",
            "category": f"hydro.{parameter_code}", "severity": 0,
            "geo": {"centroid": [lon, lat], "primary_region": "US-ID"},
            "data": {
                "id": envelope_id,
                "monitoring_location_id": site_id,
                "parameter_code": parameter_code,
                "time": time_iso,
                "value": value,
                "unit_of_measure": unit,
                "latitude": lat, "longitude": lon,
                "_enriched": {"geocoder": {
                    "name": IDAHO_CURATED_SITES.get(site_id, {}).get(
                        "gauge_name", "?"),
                }},
            },
        },
    }


def _commit(data, t):
    data["_on_broadcast_committed"](float(t))


# ---- (a) curated site at action stage triggers broadcast -----------------


def test_a_curated_site_action_stage_triggers(mem_db):
    # Snake River at Heise: action=12.0ft, broadcast at 12.5ft.
    env = _nwis_env(site_id="USGS-13186000", parameter_code="00065",
                     value=12.5)
    data = {}
    wire = handle_nwis(env, env["subject"], data=data, now=1_000_000)
    assert wire is not None
    assert wire.startswith("🌊 New:")
    assert "Snake River at Heise" in wire
    assert "action stage 12.5 ft" in wire


# ---- (b) non-curated site no broadcast + event_log handled=0 ------------


def test_b_non_curated_site_dropped(mem_db):
    env = _nwis_env(site_id="USGS-99999999", value=99.0)
    data = {}
    wire = handle_nwis(env, env["subject"], data=data, now=1_000_000)
    assert wire is None
    n_rows = mem_db.execute(
        "SELECT COUNT(*) AS n FROM gauge_readings").fetchone()["n"]
    assert n_rows == 0
    n_log = mem_db.execute(
        "SELECT COUNT(*) AS n FROM event_log WHERE source='nwis' AND handled=0"
    ).fetchone()["n"]
    assert n_log == 1


# ---- (c) curated site at normal stage no broadcast ----------------------


def test_c_curated_site_normal_stage_no_broadcast(mem_db):
    # Heise normal is below 12.0ft.
    env = _nwis_env(site_id="USGS-13186000", value=8.0)
    data = {}
    wire = handle_nwis(env, env["subject"], data=data, now=1_000_000)
    assert wire is None
    # The reading WAS persisted (time-series).
    row = mem_db.execute(
        "SELECT threshold_state FROM gauge_readings WHERE site_id=?",
        ("USGS-13186000",)).fetchone()
    assert row["threshold_state"] == "normal"


# ---- (d) upward threshold crossing (normal -> action) triggers ---------


def test_d_upward_crossing_normal_to_action_triggers(mem_db):
    # First reading at normal.
    env1 = _nwis_env(site_id="USGS-13186000", value=8.0,
                      time_iso="2026-06-05T10:00:00Z")
    handle_nwis(env1, env1["subject"], data={}, now=1_000_000)
    # Now rises to action.
    env2 = _nwis_env(site_id="USGS-13186000", value=12.5,
                      time_iso="2026-06-05T10:15:00Z",
                      envelope_id="env_2")
    data = {}
    wire = handle_nwis(env2, env2["subject"], data=data, now=1_000_900)
    assert wire is not None
    assert "action stage 12.5 ft" in wire


# ---- (e) downward crossing (action -> normal) does NOT broadcast -------


def test_e_downward_crossing_does_not_broadcast(mem_db):
    env_high = _nwis_env(site_id="USGS-13186000", value=12.5,
                           time_iso="2026-06-05T10:00:00Z")
    handle_nwis(env_high, env_high["subject"], data={}, now=1_000_000)
    env_low = _nwis_env(site_id="USGS-13186000", value=8.0,
                          time_iso="2026-06-05T11:00:00Z",
                          envelope_id="env_drop")
    wire = handle_nwis(env_low, env_low["subject"], data={}, now=1_003_600)
    assert wire is None


def test_e_same_threshold_no_re_broadcast(mem_db):
    """Repeated readings at the same threshold (action -> action -> action)
    must NOT re-broadcast every 15-min poll."""
    env = _nwis_env(site_id="USGS-13186000", value=12.5,
                     time_iso="2026-06-05T10:00:00Z")
    wire1 = handle_nwis(env, env["subject"], data={}, now=1_000_000)
    assert wire1 is not None

    env2 = _nwis_env(site_id="USGS-13186000", value=12.8,
                      time_iso="2026-06-05T10:15:00Z",
                      envelope_id="env_p2")
    wire2 = handle_nwis(env2, env2["subject"], data={}, now=1_000_900)
    assert wire2 is None  # still in action band


# ---- (f) flow_cfs included for 00060, dropped for 00065-only -----------


def test_f_flow_cfs_segment_from_companion_discharge(mem_db):
    # First seed a stage reading at action.
    env_stage = _nwis_env(site_id="USGS-13186000",
                            parameter_code="00065", value=12.5,
                            time_iso="2026-06-05T10:00:00Z")
    wire1 = handle_nwis(env_stage, env_stage["subject"], data={}, now=1_000_000)
    assert wire1 is not None
    assert "flow" not in wire1  # no companion discharge yet

    # Now a discharge reading arrives -- the handler should pick up the
    # prior stage_ft for threshold context AND emit flow if upward crossing.
    # In this case the stage didn't change, so no broadcast.
    env_flow = _nwis_env(site_id="USGS-13186000",
                          parameter_code="00060", value=8400,
                          unit="ft^3/s",
                          time_iso="2026-06-05T10:01:00Z",
                          envelope_id="env_q")
    wire2 = handle_nwis(env_flow, env_flow["subject"], data={}, now=1_000_060)
    assert wire2 is None  # same threshold; no re-broadcast


# ---- (g) site missing coords drops @ tail ------------------------------


def test_g_missing_coords_drops_at_tail(mem_db):
    # Build a Heise envelope but blank out the latitude in inner.data so
    # the handler must fall back to the curated coords (which DO exist).
    env = _nwis_env(site_id="USGS-13186000", value=12.5)
    env["data"]["data"]["latitude"] = None
    env["data"]["data"]["longitude"] = None
    wire = handle_nwis(env, env["subject"], data={}, now=1_000_000)
    assert wire is not None
    # Curated coords kick in -> @ segment still present.
    assert "@ 43.612,-111.654" in wire


# ---- (h) IDAHO_CURATED_SITES has all 9 starter sites populated ---------


def test_h_curated_sites_count_and_required_fields():
    assert len(IDAHO_CURATED_SITES) == 9
    required_keys = {"gauge_name", "lat", "lon", "action_ft", "flood_minor_ft"}
    for site_id, meta in IDAHO_CURATED_SITES.items():
        assert site_id.startswith("USGS-"), site_id
        missing = required_keys - set(meta.keys())
        assert not missing, f"{site_id} missing {missing}"
        assert isinstance(meta["action_ft"], (int, float))
        assert isinstance(meta["flood_minor_ft"], (int, float))


def test_h_curated_sites_listed_starter_set():
    """Spot-check the 9 starter sites are exactly what spec listed."""
    expected = {
        "USGS-13139510", "USGS-13186000", "USGS-13037500",
        "USGS-13135500", "USGS-13205000", "USGS-13247500",
        "USGS-13057000", "USGS-13162225", "USGS-13083000",
    }
    assert set(IDAHO_CURATED_SITES.keys()) == expected


# ---- commit callback flips event_log.handled = 1 -----------------------


def test_commit_callback_flips_event_log(mem_db):
    env = _nwis_env(site_id="USGS-13186000", value=12.5)
    data = {}
    wire = handle_nwis(env, env["subject"], data=data, now=1_000_000)
    assert wire is not None
    pre = mem_db.execute(
        "SELECT handled FROM event_log WHERE source='nwis' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert pre["handled"] == 0
    _commit(data, 1_000_001)
    post = mem_db.execute(
        "SELECT handled FROM event_log WHERE source='nwis' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert post["handled"] == 1


# ---- threshold escalation triggers a new broadcast --------------------


def test_action_to_flood_minor_triggers_re_broadcast(mem_db):
    """Reading rises action -> flood_minor: this is an upward crossing,
    re-broadcast with the higher threshold label."""
    env1 = _nwis_env(site_id="USGS-13186000", value=12.5,
                      time_iso="2026-06-05T10:00:00Z")
    wire1 = handle_nwis(env1, env1["subject"], data={}, now=1_000_000)
    assert wire1 is not None
    assert "action stage" in wire1

    env2 = _nwis_env(site_id="USGS-13186000", value=14.5,
                      time_iso="2026-06-05T11:00:00Z",
                      envelope_id="env_fm")
    wire2 = handle_nwis(env2, env2["subject"], data={}, now=1_003_600)
    assert wire2 is not None
    assert "minor flooding" in wire2


# ---- precipitation events skipped (parameter_code=00045) --------------


def test_precip_parameter_skipped(mem_db):
    env = _nwis_env(site_id="USGS-13186000", parameter_code="00045",
                     value=0.5, unit="in")
    wire = handle_nwis(env, env["subject"], data={}, now=1_000_000)
    assert wire is None
    # No gauge_readings row written for precip.
    n_rows = mem_db.execute(
        "SELECT COUNT(*) AS n FROM gauge_readings").fetchone()["n"]
    assert n_rows == 0


# ---- site_id normalization -----------------------------------------------


def test_site_id_normalization_accepts_bare_id(mem_db):
    """'13186000' without USGS- prefix should still resolve to Heise."""
    env = _nwis_env(site_id="13186000", value=12.5)
    env["data"]["data"]["monitoring_location_id"] = "13186000"
    wire = handle_nwis(env, env["subject"], data={}, now=1_000_000)
    assert wire is not None
    assert "Snake River at Heise" in wire
