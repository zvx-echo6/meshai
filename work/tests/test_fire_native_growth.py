"""Native WFIGS fire -> Phase-3 growth-decider path (F1).

These tests cover the native fire migration: env/fires.py now emits canonical
`data` the shared `gating.fire.decide` reads, env/store.py routes native fires
through the decider (bypassing the received-delta `_seen` gate) with an
unconditional `fires` state-write + a cold-start silent-seed pre-pass, and the
composer renders them via the shared fire formatter. The shared decider and
formatter are REUSED unchanged.

Scenarios (mirror the gate-sequence intent of test_fire_refactor.py):
  1. Cold-start silent-seed: ANY first-sight fire on the FIRST poll (undated OR
     recently declared) -> NO broadcast, `fires` row seeded already-broadcast.
     The first full-state sweep after boot must produce ZERO broadcasts.
  2. Growth: seeded MORA (last_bcast 2410 @ now-9h), poll 3000 -> Update; after
     the deferred commit runs, last_broadcast_acres == 3000.
  3. Containment rise past cooldown -> Update.
  4. Unchanged OR within cooldown -> suppress (no emit).
  5. Later-poll ignition: a fire absent at boot that FIRST appears on a later
     poll (source already seeded) -> New; does NOT re-spam on the next unchanged
     poll (the state-write latched last_broadcast_*).
  6. Native event carries _dedup_suffix + _severity_override +
     _on_broadcast_committed onto the emitted Event; to_event stamps canonical
     `data`; the composer renders it via the fire formatter with no env var.
"""
from __future__ import annotations

import pytest

from meshai.config import EnvironmentalConfig, NICFFiresConfig
from meshai.env.fires import NICFFiresAdapter
from meshai.env.store import EnvironmentalStore
from meshai.notifications.pipeline.bus import EventBus
from meshai.notifications.renderers.composer import compose_mesh_message
from meshai.persistence import close_thread_connection, init_db
from meshai.persistence import db as persistence_db

_NOW = 1_800_000_000.0  # pinned base epoch
_IRWIN = "IRWIN-MORA-0001"


class _Clock:
    """Mutable time seam so a test can advance between polls."""

    def __init__(self, t: float):
        self.t = t

    def now(self) -> float:
        return self.t


@pytest.fixture
def env(monkeypatch, tmp_path):
    db_path = str(tmp_path / "fire-native.sqlite")
    monkeypatch.setenv("MESHAI_DB_PATH", db_path)
    persistence_db._initialised.clear()
    close_thread_connection()
    conn = init_db()
    from meshai.adapter_config import adapter_config as _ac
    _ac.invalidate()
    clk = _Clock(_NOW)
    monkeypatch.setattr("meshai.notifications.clock.now", clk.now)
    yield conn, clk
    close_thread_connection()
    persistence_db._initialised.discard(db_path)


class _FakeFires:
    """Native NIFC stand-in: controllable batch, REAL to_event mapping."""

    def __init__(self):
        self._batch: list = []
        self._real = NICFFiresAdapter(NICFFiresConfig())

    def set_batch(self, evts: list) -> None:
        self._batch = evts

    def tick(self) -> bool:
        return True

    def get_events(self) -> list:
        return list(self._batch)

    def to_event(self, evt: dict):
        return self._real.to_event(evt)


def _raw_fire(*, name="MORA", irwin=_IRWIN, acres=2410, contained=10,
              declared=None, lat=44.0, lon=-115.0, state="US-ID") -> dict:
    """Mirror the raw internal dict env/fires.py::_fetch builds per fire."""
    eid = f"nifc_{name.replace(' ', '_').lower()}_{state}"
    return {
        "source": "nifc",
        "event_id": eid,
        "event_type": "Wildfire",
        "name": name,
        "irwin_id": irwin,
        "acres": acres,
        "pct_contained": contained,
        "contained_pct": contained,
        "declared_at_epoch": declared,
        "county": None,
        "lat": lat,
        "lon": lon,
        "distance_km": None,
        "nearest_anchor": None,
        "severity": "routine",
        "state": state,
        "fetched_at": _NOW,
        "expires": _NOW + 21600,
    }


def _make_store(coverage_areas=None, coverage_excluded=None):
    bus = EventBus()
    captured: list = []
    bus.subscribe(lambda e: captured.append(e))
    store = EnvironmentalStore(
        EnvironmentalConfig(), event_bus=bus,
        coverage_areas=coverage_areas,
        coverage_excluded=coverage_excluded,
    )
    adapter = _FakeFires()
    store._adapters["nifc"] = adapter
    return store, adapter, captured


# A coverage area covering SW Idaho (the default _raw_fire lat/lon 44.0,-115.0
# sits inside this box; a fire near 0,0 or on the US east coast is outside).
_SW_IDAHO_AREA = {
    "name": "sw-id", "west": -117.0, "south": 42.0, "east": -114.0, "north": 45.0,
}


def _seed_row(conn, *, acres, contained, last_bcast_at):
    """Manually insert an already-broadcast fires row (skips cold-start)."""
    conn.execute(
        "INSERT INTO fires(irwin_id, incident_name, current_acres, "
        "current_contained_pct, lat, lon, state, last_event_at, "
        "last_broadcast_at, last_broadcast_acres, last_broadcast_contained) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (_IRWIN, "MORA", acres, contained, 44.0, -115.0, "US-ID",
         int(_NOW), int(last_bcast_at), acres, contained),
    )


# ── 1. cold-start silent-seed ────────────────────────────────────────────────
def test_cold_start_silent_seed_no_broadcast(env):
    conn, _clk = env
    store, adapter, captured = _make_store()
    # First-sight, undated, on the FIRST fires poll (source not yet seeded).
    adapter.set_batch([_raw_fire(acres=2410, contained=10, declared=None)])
    store._ingest("nifc", adapter)

    assert captured == [], "cold-start must broadcast NOTHING"
    assert store._fires_seeded is True, "first non-empty poll marks fires seeded"
    row = conn.execute(
        "SELECT last_broadcast_acres, last_broadcast_at, "
        "last_broadcast_contained FROM fires WHERE irwin_id=?",
        (_IRWIN,)).fetchone()
    assert row is not None, "cold-start must seed a fires row"
    assert row["last_broadcast_acres"] == 2410  # seeded == current
    assert row["last_broadcast_contained"] == 10
    assert row["last_broadcast_at"] is not None


# ── 1b. cold-start seeds a RECENTLY-declared fire silently too (no 48h dump) ──
def test_cold_start_recent_fire_still_silent(env):
    conn, _clk = env
    store, adapter, captured = _make_store()
    # Declared just 1h ago — under the OLD 48h window this first-sight fire
    # would have broadcast New. On cold start it must now seed SILENT anyway:
    # a fresh deploy must never dump fires discovered in the last 48h.
    adapter.set_batch([_raw_fire(name="FRESH", irwin="IRWIN-FRESH-9",
                                 acres=120, contained=0,
                                 declared=_NOW - 3600)])
    store._ingest("nifc", adapter)

    assert captured == [], "cold-start must seed EVERY fire silent, even recent"
    row = conn.execute(
        "SELECT last_broadcast_acres, last_broadcast_at FROM fires "
        "WHERE irwin_id=?", ("IRWIN-FRESH-9",)).fetchone()
    assert row is not None and row["last_broadcast_acres"] == 120
    assert row["last_broadcast_at"] is not None
    assert store._fires_seeded is True


# ── 2. growth after cooldown -> Update + commit latches ─────────────────────
def test_growth_update_and_commit(env):
    conn, clk = env
    store, adapter, captured = _make_store()
    _seed_row(conn, acres=2410, contained=10, last_bcast_at=_NOW - 9 * 3600)

    adapter.set_batch([_raw_fire(acres=3000, contained=10)])
    store._ingest("nifc", adapter)

    assert len(captured) == 1, "growth past cooldown must broadcast Update"
    ev = captured[0]
    assert ev.data.get("is_update") is True
    assert ev.data.get("last_bcast_acres") == 2410

    commit = ev.data.get("_on_broadcast_committed")
    assert callable(commit), "Update must arm the deferred commit"
    commit(clk.now())
    row = conn.execute(
        "SELECT last_broadcast_acres FROM fires WHERE irwin_id=?",
        (_IRWIN,)).fetchone()
    assert row["last_broadcast_acres"] == 3000, "commit must latch new acres"


# ── 3. containment rise past cooldown -> Update ─────────────────────────────
def test_containment_rise_update(env):
    conn, _clk = env
    store, adapter, captured = _make_store()
    _seed_row(conn, acres=2410, contained=10, last_bcast_at=_NOW - 9 * 3600)

    adapter.set_batch([_raw_fire(acres=2410, contained=55)])
    store._ingest("nifc", adapter)

    assert len(captured) == 1, "containment rise past cooldown must broadcast"
    assert captured[0].data.get("is_update") is True


# ── 4. unchanged OR within cooldown -> suppress ─────────────────────────────
def test_within_cooldown_suppresses(env):
    conn, _clk = env
    store, adapter, captured = _make_store()
    # Growth present but last broadcast only 1h ago -> inside 8h cooldown.
    _seed_row(conn, acres=2410, contained=10, last_bcast_at=_NOW - 3600)

    adapter.set_batch([_raw_fire(acres=3000, contained=10)])
    store._ingest("nifc", adapter)

    assert captured == [], "growth inside cooldown must be suppressed"


def test_no_change_suppresses(env):
    conn, _clk = env
    store, adapter, captured = _make_store()
    _seed_row(conn, acres=2410, contained=10, last_bcast_at=_NOW - 9 * 3600)

    adapter.set_batch([_raw_fire(acres=2410, contained=10)])
    store._ingest("nifc", adapter)

    assert captured == [], "no forward change must be suppressed"


# ── 5. later-poll ignition -> New, and no re-spam on the next unchanged poll ──
def test_later_poll_ignition_new_then_no_respam(env):
    conn, clk = env
    store, adapter, captured = _make_store()

    # First (cold-start) poll: an existing fire present at boot -> silent-seed.
    # This marks the fires source seeded; NOTHING broadcasts.
    adapter.set_batch([_raw_fire(acres=2410, contained=10)])
    store._ingest("nifc", adapter)
    assert captured == [], "cold-start poll must broadcast nothing"
    assert store._fires_seeded is True

    # Later poll (well after any grace window): a NEW fire that was NOT present
    # at boot -> genuine ignition -> New broadcast.
    clk.t = _NOW + 20 * 60
    captured.clear()
    fresh = dict(name="FRESH", irwin="IRWIN-FRESH-9", acres=120, contained=0,
                 declared=_NOW - 3600)
    adapter.set_batch([_raw_fire(acres=2410, contained=10),
                       _raw_fire(**fresh)])
    store._ingest("nifc", adapter)
    assert len(captured) == 1, "new fire on a later poll must broadcast New"
    ev = captured[0]
    assert ev.data.get("irwin_id") == "IRWIN-FRESH-9"
    assert ev.data.get("category") == "wildfire_declared"
    assert ev.data.get("is_update") is False
    # Latch the New broadcast.
    ev.data["_on_broadcast_committed"](clk.now())

    # Next poll, unchanged, even well past cooldown -> must NOT re-broadcast.
    clk.t = _NOW + 10 * 3600
    captured.clear()
    adapter.set_batch([_raw_fire(acres=2410, contained=10),
                       _raw_fire(**fresh)])
    store._ingest("nifc", adapter)
    assert captured == [], "latched New must not re-spam on unchanged re-poll"


# ── 6. stamps + canonical data + formatter render ───────────────────────────
def test_event_carries_stamps_and_renders_via_formatter(env):
    conn, _clk = env
    store, adapter, captured = _make_store()
    _seed_row(conn, acres=2410, contained=10, last_bcast_at=_NOW - 9 * 3600)

    adapter.set_batch([_raw_fire(acres=3000, contained=20)])
    store._ingest("nifc", adapter)
    assert len(captured) == 1
    ev = captured[0]

    # decider stamps present
    assert ev.data.get("_dedup_suffix") == "3000|20"
    assert ev.data.get("_severity_override") == "priority"
    assert callable(ev.data.get("_on_broadcast_committed"))
    # canonical data from to_event
    assert ev.data.get("_kind") == "wfigs_incident"
    assert ev.data.get("irwin_id") == _IRWIN
    assert ev.data.get("acres") == 3000
    assert ev.data.get("contained_pct") == 20

    # composer renders native fire via the shared fire formatter (no env var):
    wire = compose_mesh_message(ev)
    assert "MORA" in wire and "Update" in wire and wire.startswith("\U0001f525")


def test_to_event_stamps_canonical_data(env):
    _conn, _clk = env
    adapter = _FakeFires()
    ev = adapter.to_event(_raw_fire(acres=555, contained=30, declared=_NOW))
    assert ev is not None
    assert ev.category == "wildfire_incident"
    assert ev.data["_kind"] == "wfigs_incident"
    assert ev.data["irwin_id"] == _IRWIN
    assert ev.data["acres"] == 555
    assert ev.data["contained_pct"] == 30
    assert ev.data["declared_at_epoch"] == _NOW
    assert ev.data["lat"] == 44.0 and ev.data["state"] == "US-ID"


# ── 7. Coverage-scope ingest gate ────────────────────────────────────────────
# Fires OUTSIDE every configured coverage area are NEVER stored (so they are
# never tracked / alerted / reminded / re-ingested). The gate mirrors the
# dispatch-level CoverageFilter's set-union membership and applies to BOTH the
# cold-start seed and the live INSERT/UPDATE path. Fail-OPEN when coverage is
# disabled or has no areas (unchanged current behaviour).


def _fires_row(conn, irwin):
    return conn.execute(
        "SELECT irwin_id FROM fires WHERE irwin_id=?", (irwin,)).fetchone()


def test_coverage_gate_inside_area_is_stored(env):
    """A fire INSIDE a coverage box is stored (cold-start seed row present)."""
    conn, _clk = env
    store, adapter, captured = _make_store(coverage_areas=[_SW_IDAHO_AREA])
    # Default coords 44.0,-115.0 are inside _SW_IDAHO_AREA.
    adapter.set_batch([_raw_fire(irwin="IRWIN-IN-1", lat=44.0, lon=-115.0)])
    store._ingest("nifc", adapter)

    assert captured == [], "cold-start seed must broadcast nothing"
    assert _fires_row(conn, "IRWIN-IN-1") is not None, \
        "in-area fire must be stored"


def test_coverage_gate_outside_area_is_not_stored(env):
    """A fire OUTSIDE all coverage boxes is NOT stored (no row) with coverage
    enabled + areas defined — neither the cold-start seed nor a live upsert."""
    conn, _clk = env
    store, adapter, captured = _make_store(coverage_areas=[_SW_IDAHO_AREA])
    # 0,0 (Gulf of Guinea) is far outside the SW-Idaho box.
    adapter.set_batch([_raw_fire(irwin="IRWIN-OUT-1", lat=0.0, lon=0.0)])
    store._ingest("nifc", adapter)

    assert captured == [], "out-of-area fire must broadcast nothing"
    assert _fires_row(conn, "IRWIN-OUT-1") is None, \
        "out-of-area fire must NOT be stored (cold-start seed dropped)"

    # A later (already-seeded) poll must ALSO refuse to INSERT the out-of-area
    # fire via the live path — it must never latch a row.
    store._fires_seeded = True
    store._ingest("nifc", adapter)
    assert _fires_row(conn, "IRWIN-OUT-1") is None, \
        "out-of-area fire must NOT be stored on the live upsert path either"


def test_coverage_gate_fail_open_when_no_areas(env):
    """Coverage DISABLED / no areas -> an out-of-box fire IS stored (fail-open,
    unchanged behaviour)."""
    conn, _clk = env
    store, adapter, captured = _make_store()  # no coverage areas
    assert store._fire_coverage_areas == [], \
        "no coverage areas -> gate is a no-op list"
    adapter.set_batch([_raw_fire(irwin="IRWIN-OPEN-1", lat=0.0, lon=0.0)])
    store._ingest("nifc", adapter)

    assert _fires_row(conn, "IRWIN-OPEN-1") is not None, \
        "with no coverage areas, every fire is stored (fail-open)"


def test_coverage_gate_excluded_adapter_fails_open(env):
    """When 'fires' is on the coverage opt-out list, the gate is disabled even
    with areas configured — mirrors the _coverage_for fetch-scope escape hatch;
    an out-of-box fire IS stored."""
    conn, _clk = env
    store, adapter, captured = _make_store(
        coverage_areas=[_SW_IDAHO_AREA], coverage_excluded=["fires"])
    assert store._fire_coverage_areas == [], \
        "excluded 'fires' adapter -> no gate built"
    adapter.set_batch([_raw_fire(irwin="IRWIN-EXCL-1", lat=0.0, lon=0.0)])
    store._ingest("nifc", adapter)

    assert _fires_row(conn, "IRWIN-EXCL-1") is not None, \
        "excluded adapter falls back to no coverage gating (fire stored)"
