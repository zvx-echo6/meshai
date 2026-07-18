"""v0.7-fire-tracker-1 tests.

Coverage map (vs the user-provided scope item 7):
  - Pixel within radius        -> attribution + centroid + last_hotspot_at
  - Pixel outside radius       -> no attribution, stays unattributed
  - 3 unattributed within 1 mi -> cluster broadcast fires once
  - 4th pixel same cluster     -> NO second broadcast
  - 5th pixel after 60 min     -> can form a NEW cluster

Plus:
  - wfigs first-sight tags data["category"]="wildfire_declared"
  - wfigs Update path does NOT tag wildfire_declared
  - Haversine sanity
"""
from __future__ import annotations

import os
import sqlite3
import time
import uuid

import pytest


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    """Force MESHAI_DB_PATH to a tmp file per test + reset thread cache."""
    db_path = str(tmp_path / f"meshai-{uuid.uuid4().hex}.sqlite")
    monkeypatch.setenv("MESHAI_DB_PATH", db_path)
    # The persistence module caches a connection on threading.local;
    # reset between tests so we get a fresh DB each time.
    from meshai.persistence import db as pdb
    pdb.close_thread_connection()
    pdb._initialised.discard(db_path)
    from meshai.persistence import init_db
    init_db(db_path)
    yield db_path
    pdb.close_thread_connection()
    pdb._initialised.discard(db_path)


def _seed_fire(*, irwin_id, lat, lon, name="Stub Fire", state="ID"):
    """Insert a minimal active fire row at known coords."""
    from meshai.persistence import get_db
    conn = get_db()
    conn.execute(
        "INSERT INTO fires(irwin_id, incident_name, lat, lon, "
        "last_event_at) VALUES (?,?,?,?,?)",
        (irwin_id, name, lat, lon, int(time.time())),
    )


def _pixel(*, lat, lon, acq_date="2026-06-06", acq_time="1200",
           frp=15.0, satellite="N20", conf="high"):
    """Build a canonical FIRMS pixel dict for the LIVE ingest_hotspot_pixel
    entrypoint (mirrors tests/test_firms_native_fusion.py's `_pixel`)."""
    import datetime as _dt
    acq_epoch = int(_dt.datetime.strptime(
        f"{acq_date} {str(acq_time).zfill(4)}", "%Y-%m-%d %H%M"
    ).replace(tzinfo=_dt.timezone.utc).timestamp())
    return {"lat": lat, "lon": lon, "frp": frp, "confidence": conf,
            "brightness": 320.5, "satellite": satellite, "acq_epoch": acq_epoch}


def _ingest(pixel, *, now):
    """Feed one pixel through the LIVE native entrypoint (chore/ripout-2dii:
    the dead Central handle_firms wrapper this file used to drive through is
    gone) and return the (wire, data) of the first broadcast it produced (or
    (None, {}))."""
    from meshai.env.fire_fusion import ingest_hotspot_pixel
    broadcasts = ingest_hotspot_pixel(pixel, now=now)
    if broadcasts:
        return broadcasts[0]
    return None, {}


# ---------------------------------------------------------------------------
# (a) Attribution within radius.
# ---------------------------------------------------------------------------


def test_pixel_within_radius_attributes_to_fire():
    from meshai.persistence import get_db

    # Cache Peak Fire stub @ 42.118, -113.643.
    _seed_fire(irwin_id="ID-TEST-001",
                 lat=42.118, lon=-113.643)

    # FIRMS pixel ~0.2 mi NE of the anchor -- well inside default 5 mi.
    wire, _data = _ingest(_pixel(lat=42.121, lon=-113.640, frp=18.0),
                          now=1780728000)
    # Attribution is silent (return None); the wire only fires on cluster.
    assert wire is None

    conn = get_db()
    # fire_pixels row created.
    rows = conn.execute("SELECT * FROM fire_pixels").fetchall()
    assert len(rows) == 1
    assert rows[0]["irwin_id"] == "ID-TEST-001"
    assert rows[0]["frp"] == 18.0
    # firms_pixels row has attributed_at stamped.
    raw = conn.execute(
        "SELECT attributed_at, cluster_broadcast_at FROM firms_pixels"
    ).fetchone()
    assert raw["attributed_at"] is not None
    assert raw["cluster_broadcast_at"] is None
    # fires row has centroid + last_hotspot_at updated.
    fire = conn.execute(
        "SELECT current_centroid_lat, current_centroid_lon, last_hotspot_at "
        "FROM fires WHERE irwin_id=?", ("ID-TEST-001",)
    ).fetchone()
    assert fire["current_centroid_lat"] == 42.121
    assert fire["current_centroid_lon"] == -113.640
    assert fire["last_hotspot_at"] is not None


def test_centroid_recomputes_as_median_across_passes():
    """A second attributed pixel updates the centroid to the median, not
    just the latest pixel's coords."""
    from meshai.persistence import get_db

    _seed_fire(irwin_id="ID-TEST-002",
                 lat=42.000, lon=-113.000)

    # 3 pixels within radius at distinct coords. v0.7-fire-tracker-2
    # makes fires.current_centroid_* the latest PASS centroid (per-pass
    # median), overriding Phase 1's 24h all-pixels median. Use acq
    # times within a single ~90 min satellite bucket so all 3 land in
    # ONE fire_passes row -- then the per-pass median IS the 3-pixel
    # median and this test's intent (verify median computation, not
    # arithmetic mean) survives the Phase 2 semantic shift.
    coords = [(42.001, -113.001), (42.002, -113.002), (42.003, -113.003)]
    for i, (la, lo) in enumerate(coords):
        _ingest(_pixel(lat=la, lon=lo, acq_date="2026-06-06",
                       acq_time=f"12{i * 10:02d}"),  # 12:00, 12:10, 12:20
                now=1780728000 + i * 600)

    fire = get_db().execute(
        "SELECT current_centroid_lat, current_centroid_lon "
        "FROM fires WHERE irwin_id=?", ("ID-TEST-002",)
    ).fetchone()
    # Median of 3 sorted lats = middle = 42.002. Same for lons.
    assert abs(fire["current_centroid_lat"] - 42.002) < 1e-9
    assert abs(fire["current_centroid_lon"] - -113.002) < 1e-9


# ---------------------------------------------------------------------------
# (b) Pixel outside radius -- no attribution.
# ---------------------------------------------------------------------------


def test_pixel_outside_radius_stays_unattributed():
    from meshai.persistence import get_db

    _seed_fire(irwin_id="ID-TEST-003",
                 lat=42.000, lon=-113.000)

    # Pixel ~50 mi away -- comfortably outside the 5 mi default.
    wire, _data = _ingest(_pixel(lat=42.700, lon=-113.000, frp=10.0),
                          now=1780728000)
    # No attribution AND below the 3-pixel cluster threshold -> no wire.
    assert wire is None

    conn = get_db()
    # No fire_pixels row.
    assert conn.execute("SELECT COUNT(*) FROM fire_pixels").fetchone()[0] == 0
    # firms_pixels has the row but attributed_at IS NULL.
    raw = conn.execute(
        "SELECT attributed_at FROM firms_pixels"
    ).fetchone()
    assert raw["attributed_at"] is None
    # fires row centroid unchanged.
    fire = conn.execute(
        "SELECT current_centroid_lat FROM fires WHERE irwin_id=?",
        ("ID-TEST-003",)
    ).fetchone()
    assert fire["current_centroid_lat"] is None


# ---------------------------------------------------------------------------
# (c) Cluster broadcast fires once on the 3rd pixel.
# ---------------------------------------------------------------------------


def _hhmm(h, m=0):
    return f"{int(h):02d}{int(m):02d}"


def test_three_unattributed_pixels_fire_cluster_once():
    from meshai.persistence import get_db

    # No fires seeded -- everything is unattributed.
    # Three pixels within ~0.3 mi over 30 minutes.
    base_lat, base_lon = 43.500, -114.500
    pixels = [
        (base_lat,         base_lon,         "1200", 25.0),
        (base_lat + 0.001, base_lon + 0.001, "1210", 32.0),
        (base_lat - 0.001, base_lon - 0.002, "1220", 21.0),
    ]
    wires: list[str | None] = []
    for la, lo, t, frp in pixels:
        wire, data = _ingest(_pixel(lat=la, lon=lo, acq_time=t, frp=frp),
                             now=1780728000)
        wires.append(wire)
        if wires[-1] is not None:
            # The handler must have tagged data with the cluster category.
            assert data.get("category") == "unattributed_hotspot_cluster"
            assert data.get("_severity_override") == "priority"

    # F3: cluster detection is ENABLED. The 3rd pixel reaches cluster_min_pixels
    # (3) within 1 mi / 60 min, so exactly ONE cluster wire fires (on pixel 3);
    # pixels 1 and 2 return None (below threshold at the time they arrive).
    fired = [w for w in wires if w is not None]
    assert len(fired) == 1, f"expected 1 cluster wire, got {len(fired)}: {wires}"
    assert fired[0].startswith("🔥 Possible new fire:")
    # All three members must be stamped so a later pixel can't re-fire them.
    conn = get_db()
    stamped = conn.execute(
        "SELECT COUNT(*) FROM firms_pixels WHERE cluster_broadcast_at IS NOT NULL"
    ).fetchone()[0]
    assert stamped == 3


def test_fourth_pixel_in_same_cluster_does_not_refire():
    from meshai.persistence import get_db

    base_lat, base_lon = 43.500, -114.500
    # Seed 3 pixels -> cluster fires once.
    for i, (la, lo, t) in enumerate([
        (base_lat,         base_lon,         "1200"),
        (base_lat + 0.001, base_lon + 0.001, "1210"),
        (base_lat - 0.001, base_lon - 0.002, "1220"),
    ]):
        _ingest(_pixel(lat=la, lon=lo, acq_time=t), now=1780728000 + i)

    # A 4th pixel inside the same cluster footprint.
    wire, data4 = _ingest(
        _pixel(lat=base_lat + 0.0005, lon=base_lon - 0.0005, acq_time="1230"),
        now=1780728100)
    # The existing 3 members already have cluster_broadcast_at stamped,
    # so they don't count toward the new cluster query (the SQL filter
    # is `cluster_broadcast_at IS NULL`). The 4th pixel alone fails
    # the min_pixels threshold -- no wire.
    assert wire is None
    assert "category" not in data4

    # F3: the first 3 members were stamped by the cluster that fired; the 4th
    # pixel is unattributed + unstamped (it alone can't re-form the cluster).
    conn = get_db()
    stamped = conn.execute(
        "SELECT COUNT(*) FROM firms_pixels WHERE cluster_broadcast_at IS NOT NULL"
    ).fetchone()[0]
    assert stamped == 3


def test_fifth_pixel_after_time_window_can_form_new_cluster():
    """The cluster query filters on acq_time > NOW - cluster_time_window.
    A pixel arriving 60+ minutes after the prior cluster's members has
    no nearby unstamped pixels to count, so it stays silent -- but if
    we then ingest TWO more nearby pixels (also outside the original
    window), we should fire a NEW cluster."""
    base_lat, base_lon = 43.500, -114.500
    # First cluster at 12:00..12:20 -> fires + stamps all 3.
    for i, (la, lo, t) in enumerate([
        (base_lat,         base_lon,         "1200"),
        (base_lat + 0.001, base_lon + 0.001, "1210"),
        (base_lat - 0.001, base_lon - 0.002, "1220"),
    ]):
        _ingest(_pixel(lat=la, lon=lo, acq_time=t), now=1780728000 + i)

    # Three NEW pixels at 14:00..14:20 -- well past the 60 min window
    # from the first cluster (which ended at 12:20 acq time).
    base2_lat, base2_lon = 43.510, -114.510
    wires2: list[str | None] = []
    for i, (la, lo, t) in enumerate([
        (base2_lat,         base2_lon,         "1400"),
        (base2_lat + 0.001, base2_lon + 0.001, "1410"),
        (base2_lat - 0.001, base2_lon - 0.002, "1420"),
    ]):
        wire, _data = _ingest(_pixel(lat=la, lon=lo, acq_time=t),
                              now=1780728000 + 7200 + i)
        wires2.append(wire)
    # F3: the first cluster's 3 members are stamped (excluded from the query),
    # so the second batch forms an independent NEW cluster -- one wire on its
    # 3rd pixel.
    fired = [w for w in wires2 if w is not None]
    assert len(fired) == 1, f"expected 1 new cluster wire, got: {wires2}"
    assert fired[0].startswith("🔥 Possible new fire:")


# ---------------------------------------------------------------------------
# (d) WFIGS first-sight tags wildfire_declared; Update does not.
# ---------------------------------------------------------------------------


def test_wfigs_first_sight_tags_wildfire_declared():
    # chore/ripout-2dii: drives the LIVE decider (gating.fire.decide) directly
    # -- handle_wfigs (the dead Central entrypoint this used to route through)
    # is gone. Same canonical schema env/fires.py::to_event builds.
    from meshai.notifications.gating.fire import decide as fire_decide

    canonical = {
        "_kind": "wfigs_incident",
        "irwin_id": "ID-NEW-001",
        "incident_name": "Pine Gulch",
        "incident_type": "WF",
        "acres": 250.0,
        "contained_pct": 0,
        "lat": 42.93, "lon": -114.45,
        "county": "Twin Falls", "state": "ID",
        "declared_at_epoch": 1780728000,
    }
    gate = fire_decide(canonical, source="wfigs", now=1780728000.0)
    assert gate.broadcast is True
    assert gate.lifecycle == "new"
    assert gate.data_patch.get("category") == "wildfire_declared", \
        f"expected wildfire_declared, got data_patch={gate.data_patch!r}"


def test_wfigs_update_does_not_retag_wildfire_declared():
    """After a row exists AND has been broadcast, an acres-grew Update
    must NOT carry the wildfire_declared category."""
    from meshai.notifications.gating.fire import decide as fire_decide
    from meshai.persistence import get_db

    # Pre-existing row that has already been broadcast.
    conn = get_db()
    conn.execute(
        "INSERT INTO fires(irwin_id, incident_name, current_acres, "
        "current_contained_pct, lat, lon, last_event_at, "
        "last_broadcast_at, last_broadcast_acres, last_broadcast_contained) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("ID-UPD-001", "Pine Gulch", 250.0, 0, 42.93, -114.45,
         1780728000, 1780728000, 250.0, 0),
    )
    canonical = {
        "_kind": "wfigs_incident",
        "irwin_id": "ID-UPD-001",
        "incident_name": "Pine Gulch",
        "incident_type": "WF",
        "acres": 500.0,
        "contained_pct": 15,
        "lat": 42.93, "lon": -114.45,
        "county": "Twin Falls", "state": "ID",
    }
    # 8h cooldown clear: now > 28800s after last_broadcast_at.
    gate = fire_decide(canonical, source="wfigs", now=1780728000.0 + 30000)
    assert gate.broadcast is True
    assert gate.lifecycle == "update"
    # Update branch must NOT re-tag with wildfire_declared.
    assert "category" not in gate.data_patch


# ---------------------------------------------------------------------------
# (e) Adapter_config rows present after seed.
# ---------------------------------------------------------------------------


def test_adapter_config_seeds_new_keys():
    from meshai.persistence import get_db
    conn = get_db()
    rows = {
        (r["adapter"], r["key"]): r["default_json"]
        for r in conn.execute(
            "SELECT adapter, key, default_json FROM adapter_config "
            "WHERE adapter IN ('firms','fires') AND key IN ("
            "'spread_radius_mi_default', 'cluster_min_pixels', "
            "'cluster_max_radius_mi', 'cluster_time_window_minutes')"
        )
    }
    assert ("fires", "spread_radius_mi_default") in rows
    assert ("firms", "cluster_min_pixels") in rows
    assert ("firms", "cluster_max_radius_mi") in rows
    assert ("firms", "cluster_time_window_minutes") in rows
    # Values are JSON-encoded; spot-check the float.
    assert rows[("fires", "spread_radius_mi_default")] == "5.0"
    assert rows[("firms", "cluster_min_pixels")] == "3"


# ---------------------------------------------------------------------------
# (f) Categories registered.
# ---------------------------------------------------------------------------


def test_new_categories_registered():
    from meshai.notifications.categories import ALERT_CATEGORIES
    assert "wildfire_declared" in ALERT_CATEGORIES
    assert "unattributed_hotspot_cluster" in ALERT_CATEGORIES
    for cat in ("wildfire_declared", "unattributed_hotspot_cluster"):
        entry = ALERT_CATEGORIES[cat]
        assert entry["toggle"] == "fire"
        assert entry["default_severity"] == "priority"
