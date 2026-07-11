"""v0.7-fire-tracker-2 tests.

Coverage map (vs user-provided scope item 8 + an integration probe):
  - 2-pass attribution with pass2 1.0 mi N of pass1 -> fire_passes row,
    drift_mi=1.0, drift_direction='N', drift_mi_per_hour computed,
    wildfire_growth wire returned with correct movement vector.
  - last_pass_at 14h ago + no new pixels -> halt detector fires once,
    halt_broadcast_at stamped.
  - Re-run halt detector with no state change -> NO second broadcast.
  - Drift below threshold (0.3 mi) -> NO wildfire_growth broadcast.

Plus:
  - Bearing/direction helper sanity.
  - Pass-aggregate fields (centroid/count/total_frp/started/ended) match.
  - Halt re-eligibility after a halted fire receives a new pixel.
  - categories + adapter_config seed verification.
"""
from __future__ import annotations

import time
import uuid

import pytest


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / f"meshai-{uuid.uuid4().hex}.sqlite")
    monkeypatch.setenv("MESHAI_DB_PATH", db_path)
    from meshai.persistence import db as pdb
    pdb.close_thread_connection()
    pdb._initialised.discard(db_path)
    from meshai.persistence import init_db
    init_db(db_path)
    yield db_path
    pdb.close_thread_connection()
    pdb._initialised.discard(db_path)


def _seed_fire(*, irwin_id, lat, lon, name="Stub Fire"):
    from meshai.persistence import get_db
    conn = get_db()
    conn.execute(
        "INSERT INTO fires(irwin_id, incident_name, lat, lon, last_event_at) "
        "VALUES (?,?,?,?,?)",
        (irwin_id, name, lat, lon, int(time.time())),
    )


def _envelope(*, lat, lon, acq_date="2026-06-06", acq_time="1200",
              frp=20.0, satellite="N20"):
    return {
        "data": {
            "adapter": "firms",
            "category": "wildfire_hotspot",
            "severity": "routine",
            "data": {
                "latitude": lat, "longitude": lon, "frp": frp,
                "bright_ti4": 320.0, "satellite": satellite,
                "instrument": "VIIRS", "confidence": "high",
                "acq_date": acq_date, "acq_time": acq_time,
                "daynight": "D", "version": "2.0NRT",
            },
        }
    }


# ---------------------------------------------------------------------------
# (a) 2-pass growth broadcast.
# ---------------------------------------------------------------------------


def test_two_pass_drift_emits_growth_with_direction_and_speed():
    """Pass 1 (N20 bucket A), pass 2 (N20 bucket B, ~6h later, centroid
    1.0 mi N of pass 1). Drift should be ~1.0 mi N, broadcast must fire."""
    from meshai.central.firms_handler import handle_firms
    from meshai.persistence import get_db

    _seed_fire(irwin_id="ID-GROWTH-001",
                 lat=42.000, lon=-114.000,
                 name="Pine Gulch")

    # Pass A epoch: 2026-06-06 12:00 UTC = 1780747200
    # Pass B epoch: 2026-06-06 18:00 UTC = 1780768800  (6h later)
    pass_a_lat = 42.000
    pass_b_lat = pass_a_lat + (1.0 / 69.0)  # 1.0 mi N: 1 deg ~ 69 mi

    # Pass A pixels (5 pixels tightly clustered around (42.000, -114.000)).
    for i in range(5):
        env = _envelope(
            lat=pass_a_lat + 0.0001 * i,
            lon=-114.000 + 0.0001 * (i - 2),
            acq_date="2026-06-06", acq_time=f"{12:02d}{0 + i:02d}",
            frp=20.0 + i, satellite="N20",
        )
        handle_firms(env, subject="central.fire.hotspot.N20.high.us.id",
                     data={}, now=1780747200 + i)

    # First pixel of pass B fires the growth broadcast.
    env_b_first = _envelope(
        lat=pass_b_lat, lon=-114.000,
        acq_date="2026-06-06", acq_time="1800",
        frp=22.0, satellite="N20",
    )
    data_b = {}
    wire = handle_firms(
        env_b_first, subject="central.fire.hotspot.N20.high.us.id",
        data=data_b, now=1780768800,
    )
    assert wire is not None, "pass-B boundary should fire growth broadcast"
    assert "Moving N" in wire, f"expected N direction, got: {wire}"
    assert data_b.get("category") == "wildfire_growth"
    assert data_b.get("_severity_override") == "immediate"
    assert wire.startswith("🔥 Pine Gulch")

    conn = get_db()
    passes = conn.execute(
        "SELECT * FROM fire_passes WHERE irwin_id=? ORDER BY pass_ended_at",
        ("ID-GROWTH-001",),
    ).fetchall()
    assert len(passes) == 2
    # Pass B has drift filled in.
    pass_b = passes[1]
    assert pass_b["drift_mi_from_prev"] == pytest.approx(1.0, rel=0.05)
    assert pass_b["drift_direction"] == "N"
    # Speed = 1 mi / 6 hours = 0.166... mph
    assert pass_b["drift_mi_per_hour"] == pytest.approx(1.0 / 6.0, rel=0.05)
    # The fire's last_pass_id updated to pass B's bucket.
    fires_row = conn.execute(
        "SELECT last_pass_id, current_centroid_lat, current_centroid_lon "
        "FROM fires WHERE irwin_id=?", ("ID-GROWTH-001",),
    ).fetchone()
    assert fires_row["last_pass_id"] == pass_b["pass_id"]
    # current_centroid_* now reflects pass B (overrides Phase 1 24h median).
    assert fires_row["current_centroid_lat"] == pytest.approx(pass_b_lat,
                                                                rel=1e-4)


def test_drift_below_threshold_does_not_emit_growth():
    """0.3 mi drift between consecutive passes -- below the 0.5 mi
    default -- must NOT broadcast wildfire_growth."""
    from meshai.central.firms_handler import handle_firms
    from meshai.persistence import get_db

    _seed_fire(irwin_id="ID-DRIFT-001",
                 lat=43.000, lon=-115.000,
                 name="Quiet Fire")

    # Pass A: 3 pixels.
    for i in range(3):
        env = _envelope(lat=43.000 + 0.0001 * i, lon=-115.000,
                        acq_time=f"{12:02d}{i:02d}",
                        frp=15.0, satellite="N20")
        handle_firms(env, subject="central.fire.hotspot.N20.high.us.id",
                     data={}, now=1780747200 + i)

    # Pass B: 0.3 mi N (below threshold).
    pass_b_lat = 43.000 + (0.3 / 69.0)
    env_b = _envelope(lat=pass_b_lat, lon=-115.000,
                      acq_time="1800", frp=15.0, satellite="N20")
    data_b = {}
    wire = handle_firms(env_b, subject="central.fire.hotspot.N20.high.us.id",
                        data=data_b, now=1780768800)
    assert wire is None, f"sub-threshold drift should NOT broadcast: {wire}"
    assert data_b.get("category") != "wildfire_growth"
    # The pass row still exists with the (sub-threshold) drift recorded.
    pass_b = get_db().execute(
        "SELECT drift_mi_from_prev, drift_direction FROM fire_passes "
        "WHERE irwin_id=? ORDER BY pass_ended_at DESC LIMIT 1",
        ("ID-DRIFT-001",),
    ).fetchone()
    assert pass_b["drift_mi_from_prev"] == pytest.approx(0.3, rel=0.1)
    assert pass_b["drift_direction"] == "N"


# ---------------------------------------------------------------------------
# (b) Halt detection.
# ---------------------------------------------------------------------------


def test_halt_detector_fires_once_after_12h_idle():
    """Fire with last_pass_at 14h ago + no new pixels in that fire
    triggers halt on the next FIRMS pixel arrival (for any fire)."""
    from meshai.central.firms_handler import handle_firms, _maybe_emit_halt
    from meshai.persistence import get_db

    now_epoch = 1780768800   # 2026-06-06 18:00 UTC
    fourteen_h_ago = now_epoch - (14 * 3600)
    conn = get_db()
    # Stale fire.
    conn.execute(
        "INSERT INTO fires(irwin_id, incident_name, lat, lon, "
        "last_event_at, last_pass_id, last_pass_at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("ID-HALT-001", "Cold Fire", 42.500, -114.500,
         int(fourteen_h_ago), "N20-329627", float(fourteen_h_ago)),
    )

    data = {}
    wire = _maybe_emit_halt(conn, data=data, now=now_epoch)
    assert wire is not None
    assert "Cold Fire" in wire
    assert "no growth in 14h" in wire
    assert data.get("category") == "wildfire_halted"
    assert data.get("_severity_override") == "routine"

    # halt_broadcast_at stamped.
    halt_at = conn.execute(
        "SELECT halt_broadcast_at FROM fires WHERE irwin_id=?",
        ("ID-HALT-001",),
    ).fetchone()[0]
    assert halt_at == float(now_epoch)


def test_halt_detector_no_second_broadcast_for_same_fire():
    """Once halt_broadcast_at is stamped, the detector skips that fire."""
    from meshai.central.firms_handler import _maybe_emit_halt
    from meshai.persistence import get_db

    now_epoch = 1780768800
    fourteen_h_ago = now_epoch - (14 * 3600)
    conn = get_db()
    conn.execute(
        "INSERT INTO fires(irwin_id, incident_name, lat, lon, "
        "last_event_at, last_pass_id, last_pass_at, halt_broadcast_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("ID-HALT-002", "Already Halted", 42.500, -114.500,
         int(fourteen_h_ago), "N20-329627", float(fourteen_h_ago),
         float(now_epoch - 600)),  # halt fired 10 min ago
    )

    data = {}
    wire = _maybe_emit_halt(conn, data=data, now=now_epoch)
    assert wire is None, f"halt latched fire should NOT re-fire: {wire}"


def test_halt_eligibility_returns_after_new_pass_arrives():
    """A previously halted fire that receives a new pixel becomes eligible
    for halt again if it goes idle a second time. The detector filter is
    halt_broadcast_at IS NULL OR halt_broadcast_at < last_pass_at."""
    from meshai.central.firms_handler import _maybe_emit_halt
    from meshai.persistence import get_db

    now_epoch = 1780768800
    conn = get_db()
    # Fire was halted yesterday, then last_pass_at advanced 14h ago.
    conn.execute(
        "INSERT INTO fires(irwin_id, incident_name, lat, lon, "
        "last_event_at, last_pass_id, last_pass_at, halt_broadcast_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("ID-HALT-003", "Resurrected", 42.500, -114.500,
         int(now_epoch), "N20-329640",
         float(now_epoch - 14 * 3600),  # last pass 14h ago
         float(now_epoch - 24 * 3600)),  # halt stamped 24h ago
    )
    # halt_broadcast_at (24h ago) < last_pass_at (14h ago) -> eligible.
    data = {}
    wire = _maybe_emit_halt(conn, data=data, now=now_epoch)
    assert wire is not None
    assert "Resurrected" in wire


# ---------------------------------------------------------------------------
# (c) Helper sanity.
# ---------------------------------------------------------------------------


def test_bearing_and_direction_round_trip():
    """Bearing helper + 8-way mapping cover all cardinals/intercardinals."""
    from meshai.central.firms_handler import _bearing, _direction_8
    # Source point.
    s_lat, s_lon = 42.0, -114.0
    # Each cardinal/intercardinal direction we test by walking ~1 mi.
    delta_deg = 1.0 / 69.0  # ~1 mi in latitude degrees
    cases = [
        ("N",  s_lat + delta_deg, s_lon),
        ("NE", s_lat + delta_deg, s_lon + delta_deg),
        ("E",  s_lat,             s_lon + delta_deg),
        ("SE", s_lat - delta_deg, s_lon + delta_deg),
        ("S",  s_lat - delta_deg, s_lon),
        ("SW", s_lat - delta_deg, s_lon - delta_deg),
        ("W",  s_lat,             s_lon - delta_deg),
        ("NW", s_lat + delta_deg, s_lon - delta_deg),
    ]
    for expected, t_lat, t_lon in cases:
        b = _bearing(s_lat, s_lon, t_lat, t_lon)
        d = _direction_8(b)
        assert d == expected, f"expected {expected} from bearing {b:.1f}, got {d}"


def test_direction_8_boundary_cases():
    from meshai.central.firms_handler import _direction_8
    # Bearings on the boundary -- check the +22.5 offset rounds correctly.
    assert _direction_8(0.0) == "N"
    assert _direction_8(22.4) == "N"
    assert _direction_8(22.6) == "NE"
    assert _direction_8(67.4) == "NE"
    assert _direction_8(67.6) == "E"
    assert _direction_8(359.9) == "N"


# ---------------------------------------------------------------------------
# (d) Adapter_config + categories.
# ---------------------------------------------------------------------------


def test_adapter_config_seeds_phase2_keys():
    from meshai.persistence import get_db
    conn = get_db()
    rows = {
        (r["adapter"], r["key"]): r["default_json"]
        for r in conn.execute(
            "SELECT adapter, key, default_json FROM adapter_config "
            "WHERE (adapter, key) IN ( "
            " ('fires','growth_drift_threshold_mi'), "
            " ('fires','halt_passes_threshold'), "
            " ('fires','halt_minimum_seconds') )"
        )
    }
    assert rows[("fires", "growth_drift_threshold_mi")] == "0.5"
    assert rows[("fires", "halt_passes_threshold")] == "2"
    assert rows[("fires", "halt_minimum_seconds")] == "43200"


def test_phase2_categories_registered():
    from meshai.notifications.categories import ALERT_CATEGORIES
    assert ALERT_CATEGORIES["wildfire_growth"]["default_severity"] == "priority"
    assert ALERT_CATEGORIES["wildfire_halted"]["default_severity"] == "routine"
    for cat in ("wildfire_growth", "wildfire_halted"):
        assert ALERT_CATEGORIES[cat]["toggle"] == "fire"


# ---------------------------------------------------------------------------
# (e) Pass aggregate correctness.
# ---------------------------------------------------------------------------


def test_pass_row_aggregates_match_member_pixels():
    """5 pixels attributed in the same pass yield ONE fire_passes row
    with pixel_count=5, total_frp = sum, pass_started_at = min(acq),
    pass_ended_at = max(acq), centroid = median."""
    from meshai.central.firms_handler import handle_firms
    from meshai.persistence import get_db

    _seed_fire(irwin_id="ID-AGG-001",
                 lat=42.000, lon=-114.000,
                 name="Aggregator")

    pixels = [
        (42.000, -114.000, "1200", 10.0),
        (42.001, -114.001, "1205", 20.0),
        (42.002, -114.002, "1210", 30.0),
        (42.003, -114.003, "1215", 40.0),
        (42.004, -114.004, "1220", 50.0),
    ]
    for la, lo, t, frp in pixels:
        env = _envelope(lat=la, lon=lo, acq_time=t, frp=frp,
                        satellite="N20")
        handle_firms(env, subject="central.fire.hotspot.N20.high.us.id",
                     data={}, now=1780747200)

    row = get_db().execute(
        "SELECT pixel_count, total_frp, pass_centroid_lat, "
        "pass_centroid_lon, pass_started_at, pass_ended_at "
        "FROM fire_passes WHERE irwin_id=?", ("ID-AGG-001",),
    ).fetchone()
    assert row["pixel_count"] == 5
    assert row["total_frp"] == pytest.approx(150.0)
    # Median of 5 sorted lats = middle = 42.002.
    assert row["pass_centroid_lat"] == pytest.approx(42.002, abs=1e-6)
    # pass_started_at corresponds to acq 1200 = 2026-06-06 12:00 = 1780747200
    assert row["pass_started_at"] == 1780747200.0
    assert row["pass_ended_at"] == 1780747200.0 + 20 * 60  # +20 minutes
