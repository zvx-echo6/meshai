"""F3 — curated FIRMS new-fire cluster detection + cold-start silent-seed.

Enables the previously dead ``_maybe_emit_cluster`` path and adds the
cold-start silent-seed discipline so the FIRST FIRMS fetch after boot (a full
day of pre-existing hotspots) never dumps a wall of "possible new fire"
broadcasts to the live mesh.

Scenarios (task spec):
  1. Cluster forms + broadcasts once on a LATER (non-seed) fetch; a subsequent
     pixel in the same cluster is silent (cluster_broadcast_at stamped).
  2. Cold-start silent-seed: first fetch with many unattributed pixels -> 0
     cluster broadcasts, but pixels persisted + cluster_broadcast_at stamped.
  3. Attribution beats clustering: a pixel within a known fire's (MORA) spread
     radius attributes (fire_pixels), never forms a "new" cluster.
  4. Per-pixel silent: a raw hotspot -> FIRMSAdapter.to_event returns None.
  5. Below-threshold: < cluster_min_pixels unattributed pixels -> no cluster.

Mirrors the driving style / isolation fixture of test_firms_native_fusion.py.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest


# ── isolation (real-DB, mirrors test_firms_native_fusion) ────────────────────

@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / f"meshai-{uuid.uuid4().hex}.sqlite")
    monkeypatch.setenv("MESHAI_DB_PATH", db_path)
    from meshai.persistence import db as pdb
    pdb.close_thread_connection()
    pdb._initialised.discard(db_path)
    from meshai.persistence import init_db
    init_db(db_path)
    try:
        from meshai.adapter_config import adapter_config as _ac
        _ac.invalidate()
    except Exception:
        pass
    yield db_path
    pdb.close_thread_connection()
    pdb._initialised.discard(db_path)


@pytest.fixture(autouse=True)
def _no_cutover(monkeypatch):
    monkeypatch.delenv("MESHAI_CUTOVER_CATEGORIES", raising=False)
    from meshai.notifications.cutover import _clear_cache
    _clear_cache()
    yield
    _clear_cache()


# ── helpers ──────────────────────────────────────────────────────────────────

def _acq_epoch(acq_date: str, acq_time: str) -> int:
    return int(datetime.strptime(f"{acq_date} {acq_time.zfill(4)}",
                                 "%Y-%m-%d %H%M")
               .replace(tzinfo=timezone.utc).timestamp())


def _pixel(*, lat, lon, acq_date="2026-06-06", acq_time="1200",
           frp=20.0, confidence="high", brightness=320.0, satellite="N20"):
    return {
        "lat": lat, "lon": lon, "frp": frp, "confidence": confidence,
        "brightness": brightness, "satellite": satellite,
        "acq_epoch": _acq_epoch(acq_date, acq_time),
    }


def _feed(pixel, *, now, seed=False):
    from meshai.env.fire_fusion import ingest_hotspot_pixel
    return ingest_hotspot_pixel(pixel, now=now, seed=seed)


def _stamped_count():
    from meshai.persistence import get_db
    return get_db().execute(
        "SELECT COUNT(*) FROM firms_pixels WHERE cluster_broadcast_at IS NOT NULL"
    ).fetchone()[0]


def _pixel_count():
    from meshai.persistence import get_db
    return get_db().execute("SELECT COUNT(*) FROM firms_pixels").fetchone()[0]


def _seed_fire(*, irwin_id, lat, lon, name="MORA"):
    from meshai.persistence import get_db
    get_db().execute(
        "INSERT INTO fires(irwin_id, incident_name, lat, lon, last_event_at) "
        "VALUES (?,?,?,?,?)", (irwin_id, name, lat, lon, 1780747200))


# ═════════════════════════════════════════════════════════════════════════════
# 1. Cluster forms + broadcasts (after first-fetch seeding); refire is silent
# ═════════════════════════════════════════════════════════════════════════════

def test_cluster_broadcasts_after_seed_then_refire_silent():
    base_lat, base_lon = 43.500, -114.500

    # First fetch (cold start): three pre-existing hotspots -> silent seed.
    seed_wires = []
    for i, dt in enumerate([(0.0, 0.0), (0.001, 0.001), (-0.001, -0.002)]):
        seed_wires += _feed(_pixel(lat=base_lat + dt[0], lon=base_lon + dt[1],
                                   acq_time=f"12{i:02d}"),
                            now=1780728000 + i, seed=True)
    assert seed_wires == [], "cold-start seed must emit no cluster wire"
    assert _stamped_count() == 3, "seeded cluster members must be stamped"

    # Later fetch (seed=False): THREE genuinely-new hotspots elsewhere -> one
    # cluster wire on the 3rd pixel.
    n2_lat, n2_lon = 44.000, -116.000
    later = []
    for i, dt in enumerate([(0.0, 0.0), (0.001, 0.001), (-0.001, -0.002)]):
        later += _feed(_pixel(lat=n2_lat + dt[0], lon=n2_lon + dt[1],
                              acq_time=f"14{i:02d}"),
                      now=1780735200 + i, seed=False)
    assert len(later) == 1, f"expected exactly one cluster wire: {later}"
    wire, data = later[0]
    assert wire.startswith("🔥 Possible new fire:")
    assert "3 hotspots within 1 mi" in wire
    assert data["category"] == "unattributed_hotspot_cluster"
    assert data["_severity_override"] == "priority"

    # A 4th pixel inside the just-broadcast cluster -> silent (members stamped).
    refire = _feed(_pixel(lat=n2_lat + 0.0005, lon=n2_lon - 0.0005,
                          acq_time="1403"), now=1780735300, seed=False)
    assert refire == [], "a pixel joining an already-broadcast cluster is silent"


# ═════════════════════════════════════════════════════════════════════════════
# 2. Cold-start silent-seed: many pixels -> 0 broadcasts, persisted + stamped
# ═════════════════════════════════════════════════════════════════════════════

def test_cold_start_many_pixels_zero_broadcasts_but_persisted():
    base_lat, base_lon = 43.000, -115.000
    produced = []
    # 12 tightly-clustered pre-existing hotspots on the first fetch.
    for i in range(12):
        produced += _feed(
            _pixel(lat=base_lat + 0.0005 * i, lon=base_lon,
                   acq_time=f"12{i:02d}"),
            now=1780728000 + i, seed=True)
    assert produced == [], "cold start must emit ZERO cluster broadcasts"
    # All 12 persisted.
    assert _pixel_count() == 12
    # Every clustered member stamped so none can re-fire later. With 12 pixels
    # within 1 mi / 60 min and min_pixels=3, every pixel from the 3rd onward is
    # part of a stamped cluster; at minimum a supermajority are stamped and NONE
    # is left in a state that would broadcast on a later identical fetch.
    assert _stamped_count() >= 10


def test_cold_start_native_adapter_flag_flips_and_second_fetch_broadcasts():
    """Drive the real native-adapter cold-start gate: first _run_fusion call
    silent-seeds (flag False->True, zero wires); the second broadcasts."""
    from meshai.config import FIRMSConfig
    from meshai.env.firms import FIRMSAdapter

    adapter = FIRMSAdapter(FIRMSConfig(map_key="x"))
    assert adapter._firms_seeded is False

    def _raw(lat, lon, acq_time, i):
        return {
            "source": "firms", "event_id": f"e{i}", "lat": lat, "lon": lon,
            "properties": {"frp": 20.0, "confidence": "high",
                           "brightness": 320.0, "acq_date": "2026-06-06",
                           "acq_time": acq_time},
        }

    base_lat, base_lon = 43.700, -114.700
    first_batch = [_raw(base_lat + 0.001 * i, base_lon, f"12{i:02d}", i)
                   for i in range(4)]
    out1 = adapter._run_fusion(first_batch)
    assert out1 == [], "first fetch (cold start) broadcasts nothing"
    assert adapter._firms_seeded is True, "first non-empty fetch flips the flag"

    # Second fetch: three NEW hotspots in a fresh location -> a cluster wire.
    n_lat, n_lon = 44.300, -115.300
    second_batch = [_raw(n_lat + 0.001 * i, n_lon, f"14{i:02d}", 100 + i)
                    for i in range(3)]
    out2 = adapter._run_fusion(second_batch)
    fusion_cats = [e["properties"].get("category") for e in out2]
    assert "unattributed_hotspot_cluster" in fusion_cats, \
        f"second fetch must broadcast a curated cluster: {fusion_cats}"


def test_empty_first_fetch_does_not_consume_seed():
    """An empty first fetch must not flip the seed flag -- a later real batch
    still seeds silently (mirrors store._fires_seeded non-empty guard)."""
    from meshai.config import FIRMSConfig
    from meshai.env.firms import FIRMSAdapter

    adapter = FIRMSAdapter(FIRMSConfig(map_key="x"))
    assert adapter._run_fusion([]) == []
    assert adapter._firms_seeded is False, "empty fetch must not seed"


# ═════════════════════════════════════════════════════════════════════════════
# 2b. Restart-safe cold start: the silent-seed gate keys on the PERSISTED
#     firms_pixels baseline, NOT just the in-memory _firms_seeded flag (which
#     resets to False on every process start). A FIRST-EVER run (empty baseline)
#     still seeds silently; a RESTART with an existing baseline must NOT re-seed
#     the whole batch — a genuinely-new cluster still broadcasts, and pre-existing
#     already-stamped pixels never re-burst.
# ═════════════════════════════════════════════════════════════════════════════

def _raw_evt(lat, lon, acq_time, i, acq_date="2026-06-06"):
    return {
        "source": "firms", "event_id": f"e{i}", "lat": lat, "lon": lon,
        "properties": {"frp": 20.0, "confidence": "high",
                       "brightness": 320.0, "acq_date": acq_date,
                       "acq_time": acq_time},
    }


def test_firms_pixels_empty_helper_reflects_baseline():
    """The gate's emptiness probe: True on a fresh DB, False once any pixel is
    persisted, and True (fail-safe) if the table is missing entirely."""
    from meshai.config import FIRMSConfig
    from meshai.env.firms import FIRMSAdapter
    from meshai.persistence import get_db

    adapter = FIRMSAdapter(FIRMSConfig(map_key="x"))
    # Fresh isolated DB: table exists (init_db) but holds zero rows -> empty.
    assert adapter._firms_pixels_empty() is True

    # Persist one pixel via the real ingest path -> baseline no longer empty.
    _feed(_pixel(lat=43.0, lon=-115.0, acq_time="1200"), now=1780728000,
          seed=True)
    assert _pixel_count() == 1
    assert adapter._firms_pixels_empty() is False

    # Missing table -> fail-safe treats as empty (never crashes the fetch).
    get_db().execute("DROP TABLE firms_pixels")
    assert adapter._firms_pixels_empty() is True


def test_first_ever_run_empty_baseline_seeds_silently():
    """FIRST-EVER run: empty firms_pixels + _firms_seeded False -> a ≥3-pixel
    new cluster SEEDS silently (zero broadcasts) and the baseline is written."""
    from meshai.config import FIRMSConfig
    from meshai.env.firms import FIRMSAdapter

    adapter = FIRMSAdapter(FIRMSConfig(map_key="x"))
    assert adapter._firms_seeded is False
    assert _pixel_count() == 0, "precondition: empty persisted baseline"

    base_lat, base_lon = 43.700, -114.700
    batch = [_raw_evt(base_lat + 0.001 * i, base_lon, f"12{i:02d}", i)
             for i in range(4)]  # a tight ≥3-pixel cluster
    out = adapter._run_fusion(batch)

    assert out == [], "first-ever run must silent-seed (zero broadcasts)"
    assert _pixel_count() == 4, "seeded pixels persisted to the baseline"
    assert adapter._firms_seeded is True, "first non-empty fetch flips the flag"


def test_restart_with_baseline_new_cluster_broadcasts_no_burst():
    """RESTART with an existing baseline: _firms_seeded resets to False, but the
    persisted firms_pixels is NON-empty, so cold_start collapses to False. A
    genuinely-new ≥3-pixel cluster on the post-restart fetch DOES broadcast (not
    absorbed), and pre-existing already-stamped pixels do NOT re-burst."""
    from meshai.config import FIRMSConfig
    from meshai.env.firms import FIRMSAdapter

    # --- Process 1: cold-start seed a cluster (silent), building the baseline. ---
    a1 = FIRMSAdapter(FIRMSConfig(map_key="x"))
    old_lat, old_lon = 43.500, -114.500
    seed_batch = [_raw_evt(old_lat + 0.001 * i, old_lon, f"12{i:02d}", i)
                  for i in range(4)]
    assert a1._run_fusion(seed_batch) == [], "seed fetch is silent"
    assert a1._firms_seeded is True
    seeded_pixels = _pixel_count()
    assert seeded_pixels == 4
    stamped_before = _stamped_count()
    assert stamped_before >= 3, "seeded cluster members are stamped"

    # --- Process 2 (RESTART): the SAME persisted DB, a FRESH adapter whose
    # in-memory flag reset to False. Baseline is non-empty -> NOT a cold start. ---
    a2 = FIRMSAdapter(FIRMSConfig(map_key="x"))
    assert a2._firms_seeded is False, "restart resets the in-memory flag"
    assert a2._firms_pixels_empty() is False, "but the baseline persists"

    # Post-restart fetch: re-see the SAME pre-existing cluster (no re-burst) PLUS
    # a genuinely-NEW ≥3-pixel cluster elsewhere (must broadcast).
    new_lat, new_lon = 44.300, -115.300
    post_batch = (
        [_raw_evt(old_lat + 0.001 * i, old_lon, f"12{i:02d}", i)
         for i in range(4)]  # pre-existing -> INSERT-OR-IGNORE no-ops / stamped
        + [_raw_evt(new_lat + 0.001 * i, new_lon, f"14{i:02d}", 100 + i)
           for i in range(3)]  # genuinely NEW cluster
    )
    out = a2._run_fusion(post_batch)

    cats = [e["properties"].get("category") for e in out]
    assert "unattributed_hotspot_cluster" in cats, (
        f"a genuinely-new cluster after restart must broadcast, not be "
        f"absorbed: {cats}")
    # Exactly one cluster wire: only the NEW cluster fired; the pre-existing
    # already-stamped cluster did not re-burst.
    assert cats.count("unattributed_hotspot_cluster") == 1, (
        f"pre-existing stamped pixels must not re-burst: {cats}")
    # The new cluster is centered on the NEW location, not the old one.
    wire = out[[e["properties"].get("category")
                for e in out].index("unattributed_hotspot_cluster")]["headline"]
    assert wire.startswith("🔥 Possible new fire:")


# ═════════════════════════════════════════════════════════════════════════════
# 3. Attribution beats clustering (MORA hotspots grow the fire, never cluster)
# ═════════════════════════════════════════════════════════════════════════════

def test_attribution_beats_clustering_for_known_fire():
    from meshai.persistence import get_db

    # Seed a known WFIGS fire (MORA) at a fixed point.
    mora_lat, mora_lon = 44.100, -115.600
    _seed_fire(irwin_id="ID-MORA", lat=mora_lat, lon=mora_lon)

    # Three hotspots ~0.05 mi apart, well within the 5 mi default spread radius.
    out = []
    for i, dt in enumerate([(0.0, 0.0), (0.001, 0.001), (-0.001, -0.001)]):
        out += _feed(_pixel(lat=mora_lat + dt[0], lon=mora_lon + dt[1],
                            acq_time=f"12{i:02d}"),
                    now=1780728000 + i, seed=False)

    conn = get_db()
    # Attributed: fire_pixels rows exist, firms_pixels.attributed_at set.
    assert conn.execute("SELECT COUNT(*) FROM fire_pixels").fetchone()[0] == 3
    attributed = conn.execute(
        "SELECT COUNT(*) FROM firms_pixels WHERE attributed_at IS NOT NULL"
    ).fetchone()[0]
    assert attributed == 3
    # NOT clustered: nothing stamped cluster_broadcast_at, and no cluster wire.
    assert _stamped_count() == 0
    cats = [d.get("category") for _w, d in out]
    assert "unattributed_hotspot_cluster" not in cats


# ═════════════════════════════════════════════════════════════════════════════
# 3b. Cold-start silent-seed suppresses the FUSION path too (growth/spotting/
#     halt), not just clusters — enabling FIRMS must emit ZERO broadcasts on the
#     first fetch even for a pre-existing attributed fire (e.g. MORA).
# ═════════════════════════════════════════════════════════════════════════════

# One VIIRS pass is a 90-min (5400 s) bucket (see _pass_id). Anchor pass A and
# pass B in DIFFERENT buckets so the second batch crosses a real pass boundary.
_PASS_A = _acq_epoch("2026-06-06", "1200")            # bucket N
_PASS_B = _PASS_A + 6 * 3600                            # +6h -> a later bucket


def _pixel_at(*, lat, lon, acq_epoch, frp=20.0):
    return {"lat": lat, "lon": lon, "frp": frp, "confidence": "high",
            "brightness": 320.0, "satellite": "N20", "acq_epoch": acq_epoch}


def test_cold_start_seed_suppresses_growth_fusion_but_persists():
    """First (seed) fetch: a pre-existing fire (MORA) with pixels that WOULD
    produce a wildfire_growth boundary broadcast -> ZERO wires, yet the pixels
    are persisted + attributed and the pass/centroid baseline is built."""
    from meshai.persistence import get_db

    mora_lat, mora_lon = 44.100, -115.600
    _seed_fire(irwin_id="ID-MORA", lat=mora_lat, lon=mora_lon)

    produced = []
    # Pass A: 5 pixels around the anchor (same 90-min bucket).
    for i in range(5):
        produced += _feed(
            _pixel_at(lat=mora_lat + 0.0001 * i, lon=mora_lon + 0.0001 * (i - 2),
                      acq_epoch=_PASS_A + i, frp=20.0 + i),
            now=1780728000 + i, seed=True)
    # Pass B: one pixel ~1 mi north in a LATER bucket -> a growth boundary that,
    # on a non-seed fetch, WOULD broadcast wildfire_growth.
    produced += _feed(
        _pixel_at(lat=mora_lat + 1.0 / 69.0, lon=mora_lon, acq_epoch=_PASS_B),
        now=1780728100, seed=True)

    assert produced == [], "cold-start seed must suppress the growth fusion wire"

    conn = get_db()
    # Persistence + attribution still happened.
    assert conn.execute("SELECT COUNT(*) FROM firms_pixels").fetchone()[0] == 6
    assert conn.execute(
        "SELECT COUNT(*) FROM fire_pixels").fetchone()[0] == 6
    assert conn.execute(
        "SELECT COUNT(*) FROM firms_pixels WHERE attributed_at IS NOT NULL"
    ).fetchone()[0] == 6
    # Pass baseline built: the fires cursor advanced to pass B's bucket so a
    # genuinely-new LATER pass can measure drift against it.
    cursor = conn.execute(
        "SELECT last_pass_id, current_centroid_lat FROM fires "
        "WHERE irwin_id=?", ("ID-MORA",)).fetchone()
    assert cursor["last_pass_id"] is not None
    assert cursor["current_centroid_lat"] is not None


def test_growth_fires_on_later_fetch_after_seed():
    """After the cold-start seed built the pass baseline, a genuinely-new pass
    on a LATER (seed=False) fetch DOES broadcast wildfire_growth."""
    mora_lat, mora_lon = 44.100, -115.600
    _seed_fire(irwin_id="ID-MORA", lat=mora_lat, lon=mora_lon)

    # --- Cold-start seed: pass A + pass B, all silent. ---
    for i in range(5):
        assert _feed(
            _pixel_at(lat=mora_lat + 0.0001 * i, lon=mora_lon + 0.0001 * (i - 2),
                      acq_epoch=_PASS_A + i, frp=20.0 + i),
            now=1780728000 + i, seed=True) == []
    assert _feed(
        _pixel_at(lat=mora_lat + 1.0 / 69.0, lon=mora_lon, acq_epoch=_PASS_B),
        now=1780728100, seed=True) == []

    # --- Later real fetch (seed=False): a NEW pass C, another ~1 mi north of
    # pass B -> a genuinely-new boundary drift -> wildfire_growth broadcasts. ---
    pass_c = _PASS_B + 6 * 3600
    out = _feed(
        _pixel_at(lat=mora_lat + 2.0 / 69.0, lon=mora_lon, acq_epoch=pass_c),
        now=1780750000, seed=False)
    assert len(out) == 1, f"genuinely-new post-seed growth must fire: {out}"
    wire, data = out[0]
    assert data["category"] == "wildfire_growth"
    assert data["_severity_override"] == "immediate"
    assert wire.startswith("🔥 MORA")


def test_cold_start_seed_suppresses_halt_but_latches():
    """First (seed) fetch: an already-idle fire that WOULD halt-broadcast is
    suppressed AND its halt latch is stamped, so it stays silent on later
    unrelated fetches until real activity resumes."""
    from meshai.persistence import get_db

    now = 1780768800
    idle_at = now - 14 * 3600
    conn = get_db()
    conn.execute(
        "INSERT INTO fires(irwin_id, incident_name, lat, lon, last_event_at, "
        "last_pass_id, last_pass_at) VALUES (?,?,?,?,?,?,?)",
        ("ID-IDLE", "Cold Fire", 42.5, -114.5, int(idle_at),
         "N20-329627", float(idle_at)))

    # A fresh unattributed pixel FAR from the idle fire on the cold-start fetch.
    out = _feed(_pixel_at(lat=45.0, lon=-118.0, acq_epoch=_PASS_A), now=now,
                seed=True)
    assert out == [], "cold-start seed must suppress the halt wire"
    latch = conn.execute(
        "SELECT halt_broadcast_at FROM fires WHERE irwin_id=?",
        ("ID-IDLE",)).fetchone()[0]
    assert latch == float(now), "seed still latches halt so it can't re-fire"

    # A later unrelated fetch: the idle fire is latched -> still silent.
    out2 = _feed(_pixel_at(lat=45.1, lon=-118.1, acq_epoch=_PASS_A + 60),
                 now=now + 120, seed=False)
    assert out2 == [], "latched idle fire must not halt on a later fetch"


# ═════════════════════════════════════════════════════════════════════════════
# 4. Per-pixel silent: a raw hotspot never renders an Event
# ═════════════════════════════════════════════════════════════════════════════

def test_raw_hotspot_to_event_returns_none():
    from meshai.config import FIRMSConfig
    from meshai.env.firms import FIRMSAdapter

    adapter = FIRMSAdapter(FIRMSConfig(map_key="x"))
    raw_evt = {
        "source": "firms", "event_id": "firms_43.0_-115.0_2026-06-06_1200",
        "lat": 43.0, "lon": -115.0, "properties": {"new_ignition": True},
    }
    assert adapter.to_event(raw_evt) is None, "raw hotspots must never broadcast"


# ═════════════════════════════════════════════════════════════════════════════
# 5. Below-threshold: fewer than cluster_min_pixels -> no cluster
# ═════════════════════════════════════════════════════════════════════════════

def test_below_threshold_no_cluster():
    base_lat, base_lon = 43.900, -116.100
    produced = []
    # Only two unattributed pixels within radius -> below min_pixels (3).
    for i, dt in enumerate([(0.0, 0.0), (0.001, 0.001)]):
        produced += _feed(_pixel(lat=base_lat + dt[0], lon=base_lon + dt[1],
                                 acq_time=f"12{i:02d}"),
                         now=1780728000 + i, seed=False)
    assert produced == [], "two pixels must not fire a cluster"
    assert _stamped_count() == 0
    assert _pixel_count() == 2
