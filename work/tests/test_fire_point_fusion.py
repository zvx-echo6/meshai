"""Off-air tests for WFIGS incident-point + perimeter fusion (feat/wfigs-incident-point-fusion).

T1 — merge dedup by IrwinID:
    Points return A+B+C, perimeters return A+B → merged has 3;
    A and B carry polygon + perimeter-derived lat/lon; C has no polygon.

T2 — point-only fire surfaces post-seed:
    _fires_seeded=True, empty fires table, 25-ac point-only fire →
    decider emits "new", _kind="wfigs_incident", correct irwin_id/lat/lon.

T3 — perimeter geometry preferred over point geometry for same IrwinID.

T4 — cold-start silent-seed:
    _fires_seeded=False, empty table, 6-fire merged batch →
    all 6 rows inserted with last_broadcast_at NOT NULL, ZERO EventBus emits,
    flag flips True.

T5 — FIRMS attribution:
    FIRMSAdapter._get_known_fires() sees point-only fires in the merged set
    (proximity match works on their lat/lon).

No mesh wiring, no hand-written fires rows, no network calls.
"""
from __future__ import annotations

import json
import time
from io import BytesIO
from typing import List, Tuple
from unittest.mock import MagicMock, patch

import pytest

from meshai.config import EnvironmentalConfig, NICFFiresConfig
from meshai.env.fires import NICFFiresAdapter
from meshai.env.store import EnvironmentalStore
from meshai.notifications.pipeline.bus import EventBus
from meshai.persistence import close_thread_connection, init_db
from meshai.persistence import db as persistence_db

_NOW = 1_800_000_000.0


# ── Time seam ─────────────────────────────────────────────────────────────────

class _Clock:
    def __init__(self, t: float = _NOW):
        self.t = t

    def now(self) -> float:
        return self.t


# ── Response builders ─────────────────────────────────────────────────────────

def _perim_geojson(fires: List[Tuple]) -> bytes:
    """GeoJSON perimeter response.

    fires = list of (irwin_id, name, acres, lat, lon)
    Each entry gets a triangular Polygon so _compute_centroid yields ~(lat, lon).
    """
    features = []
    for irwin_id, name, acres, lat, lon in fires:
        features.append({
            "properties": {
                "attr_IrwinID": irwin_id,
                "attr_IncidentName": name,
                "attr_IncidentSize": acres,
                "attr_PercentContained": 10,
                "attr_FireDiscoveryDateTime": None,
                "attr_POOState": "US-ID",
                "poly_GISAcres": acres,
            },
            "geometry": {
                "type": "Polygon",
                # Triangle whose centroid averages to (lat, lon)
                "coordinates": [[
                    [lon - 0.1, lat - 0.1],
                    [lon + 0.2, lat - 0.1],
                    [lon - 0.1, lat + 0.2],
                    [lon - 0.1, lat - 0.1],
                ]],
            },
        })
    return json.dumps({"features": features}).encode()


def _points_arcgis(fires: List[Tuple]) -> bytes:
    """ArcGIS JSON (f=json) points response.

    fires = list of (irwin_id, name, acres, lat, lon)
    """
    features = []
    for irwin_id, name, acres, lat, lon in fires:
        features.append({
            "attributes": {
                "IrwinID": irwin_id,
                "UniqueFireIdentifier": None,
                "IncidentName": name,
                "IncidentSize": acres,
                "PercentContained": 10,
                "FireDiscoveryDateTime": None,
                "POOState": "US-ID",
                "POOCounty": "Test County",
                "InitialLatitude": lat,
                "InitialLongitude": lon,
                "IncidentTypeCategory": "WF",
            },
            "geometry": {"x": lon, "y": lat},
        })
    return json.dumps({"features": features}).encode()


def _ctx(body: bytes):
    """Minimal context-manager mock for urlopen(...) with body bytes."""
    m = MagicMock()
    m.__enter__ = lambda self: self
    m.__exit__ = MagicMock(return_value=False)
    m.read.return_value = body
    return m


# ── Adapter fixture ───────────────────────────────────────────────────────────

@pytest.fixture
def adapter():
    cfg = MagicMock()
    cfg.state = "US-ID"
    cfg.tick_seconds = 600
    return NICFFiresAdapter(cfg)


# ── Store + fake fires helper (mirrors test_fire_native_growth.py) ─────────────

class _FakeFires:
    """Controllable batch that uses the REAL to_event from NICFFiresAdapter."""

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


def _raw_fire(
    *,
    name="TESTFIRE",
    irwin="IRWIN-TEST-001",
    acres=100,
    contained=0,
    declared=None,
    lat=44.0,
    lon=-115.0,
    state="US-ID",
    county="Test County",
    polygon=None,
) -> dict:
    """Raw internal event dict as produced by the merged _fetch path."""
    eid = f"nifc_{name.replace(' ', '_').lower()}_{state}"
    evt = {
        "source": "nifc",
        "event_id": eid,
        "event_type": "Wildfire",
        "name": name,
        "irwin_id": irwin,
        "acres": acres,
        "pct_contained": contained,
        "contained_pct": contained,
        "declared_at_epoch": declared,
        "county": county,
        "lat": lat,
        "lon": lon,
        "distance_km": None,
        "nearest_anchor": None,
        "severity": "routine",
        "state": state,
        "fetched_at": _NOW,
        "expires": _NOW + 21600,
    }
    if polygon is not None:
        evt["polygon"] = polygon
    return evt


@pytest.fixture
def env(monkeypatch, tmp_path):
    db_path = str(tmp_path / "fire-pts-test.sqlite")
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


def _make_store():
    bus = EventBus()
    captured: list = []
    bus.subscribe(lambda e: captured.append(e))
    store = EnvironmentalStore(EnvironmentalConfig(), event_bus=bus)
    adapter = _FakeFires()
    store._adapters["nifc"] = adapter
    return store, adapter, captured


# ═════════════════════════════════════════════════════════════════════════════
# T1 — merge dedup by IrwinID: A+B in perimeter, A+B+C in points → 3 merged
# ═════════════════════════════════════════════════════════════════════════════

@patch("meshai.env.fires.urlopen")
def test_t1_merge_dedup_by_irwin(mock_urlopen, adapter):
    """Points A+B+C merged with perimeters A+B → 3 events; A/B have polygon; C does not."""
    fires_ab = [
        ("IRWIN-A", "Alpha Fire", 500, 43.0, -115.0),
        ("IRWIN-B", "Beta Fire",  300, 43.5, -115.5),
    ]
    fires_abc = [
        ("IRWIN-A", "Alpha Fire", 500, 43.01, -115.01),   # point coords differ from perim
        ("IRWIN-B", "Beta Fire",  300, 43.51, -115.51),
        ("IRWIN-C", "Gamma Fire", 250, 44.0,  -116.0),    # point-only
    ]

    mock_urlopen.side_effect = [
        _ctx(_perim_geojson(fires_ab)),
        _ctx(_points_arcgis(fires_abc)),
    ]

    adapter._fetch()

    events = adapter.get_events()
    assert len(events) == 3, f"Expected 3 merged fires, got {len(events)}: {[e['name'] for e in events]}"

    by_irwin = {e["irwin_id"]: e for e in events}

    # A and B: must have polygon (perimeter-backed)
    assert "polygon" in by_irwin["IRWIN-A"], "Alpha Fire (perimeter-backed) must carry polygon"
    assert "polygon" in by_irwin["IRWIN-B"], "Beta Fire (perimeter-backed) must carry polygon"

    # C: must NOT have polygon (point-only)
    assert "polygon" not in by_irwin["IRWIN-C"], "Gamma Fire (point-only) must not carry polygon"

    # C must still have valid lat/lon from point geometry
    assert by_irwin["IRWIN-C"]["lat"] == pytest.approx(44.0, abs=0.01)
    assert by_irwin["IRWIN-C"]["lon"] == pytest.approx(-116.0, abs=0.01)


# ═════════════════════════════════════════════════════════════════════════════
# T2 — point-only fire surfaces post-seed (decider emits "new")
# ═════════════════════════════════════════════════════════════════════════════

def test_t2_point_only_fire_surfaces(env):
    """Post-seed: a point-only fire (25 ac) triggers decider 'new' → one emit."""
    _conn, _clk = env
    store, fires_adapter, captured = _make_store()

    # Mark already seeded so this is NOT cold-start
    store._fires_seeded = True

    fires_adapter.set_batch([
        _raw_fire(
            name="Elk Point Fire",
            irwin="IRWIN-PT-ONLY-001",
            acres=25,
            contained=0,
            lat=43.8,
            lon=-115.2,
            polygon=None,   # point-only — no polygon
        )
    ])
    store._ingest("nifc", fires_adapter)

    assert len(captured) == 1, (
        f"Expected 1 broadcast for new point-only fire, got {len(captured)}"
    )
    ev = captured[0]
    assert ev.data.get("_kind") == "wfigs_incident"
    assert ev.data.get("irwin_id") == "IRWIN-PT-ONLY-001"
    assert ev.lat == pytest.approx(43.8, abs=0.001)
    assert ev.lon == pytest.approx(-115.2, abs=0.001)


# ═════════════════════════════════════════════════════════════════════════════
# T3 — perimeter geometry preferred over point geometry for same IrwinID
# ═════════════════════════════════════════════════════════════════════════════

@patch("meshai.env.fires.urlopen")
def test_t3_perimeter_geometry_preferred(mock_urlopen, adapter):
    """When both layers carry the same IrwinID, the perimeter centroid wins."""
    # Perimeter: centroid will average to roughly (43.0, -115.0) from the triangle
    perim = [("IRWIN-GEOM", "Geom Fire", 400, 43.0, -115.0)]
    # Points: different initial coords
    pts = [("IRWIN-GEOM", "Geom Fire", 400, 44.9, -119.9)]

    mock_urlopen.side_effect = [
        _ctx(_perim_geojson(perim)),
        _ctx(_points_arcgis(pts)),
    ]

    adapter._fetch()

    events = adapter.get_events()
    assert len(events) == 1
    evt = events[0]

    # Centroid of the triangle [(-115.1, 42.9), (-114.8, 42.9), (-115.1, 43.2), (-115.1, 42.9)]
    # average x = (-115.1 + -114.8 + -115.1 + -115.1) / 4 = (-460.1)/4 ≈ -115.025
    # average y = (42.9 + 42.9 + 43.2 + 42.9) / 4 = 171.9/4 ≈ 42.975
    # Point coords were (44.9, -119.9) — clearly different
    assert evt["lat"] != pytest.approx(44.9, abs=0.5), (
        "perimeter centroid must be used, not point lat"
    )
    assert evt["lon"] != pytest.approx(-119.9, abs=0.5), (
        "perimeter centroid must be used, not point lon"
    )
    # Must be near the perimeter centroid (43.0, -115.0)
    assert evt["lat"] == pytest.approx(43.0, abs=0.2)
    assert evt["lon"] == pytest.approx(-115.0, abs=0.2)

    # Must carry a polygon (from perimeter)
    assert "polygon" in evt


# ═════════════════════════════════════════════════════════════════════════════
# T4 — cold-start silent-seed: 6-fire merged batch → 0 broadcasts, 6 rows
# ═════════════════════════════════════════════════════════════════════════════

def test_t4_cold_start_silent_seed_six_fires(env):
    """Cold-start: 6 fires (mix of perimeter-backed + point-only) → zero broadcasts,
    all 6 inserted with last_broadcast_at NOT NULL, flag flips True."""
    conn, _clk = env
    store, fires_adapter, captured = _make_store()

    assert store._fires_seeded is False, "store must start unseeded"

    batch = [
        _raw_fire(name=f"Fire {i}", irwin=f"IRWIN-SEED-{i:03d}",
                  acres=100 + i * 50, lat=43.0 + i * 0.1, lon=-115.0 + i * 0.1,
                  polygon=([[[-115.0, 43.0], [-114.9, 43.0], [-115.0, 43.1], [-115.0, 43.0]]]
                            if i % 2 == 0 else None))
        for i in range(6)
    ]
    fires_adapter.set_batch(batch)
    store._ingest("nifc", fires_adapter)

    assert captured == [], f"cold-start must produce ZERO broadcasts, got {len(captured)}"
    assert store._fires_seeded is True, "flag must flip after non-empty ingest"

    rows = conn.execute("SELECT irwin_id, last_broadcast_at FROM fires").fetchall()
    assert len(rows) == 6, f"Expected 6 fires rows, got {len(rows)}"
    for row in rows:
        assert row["last_broadcast_at"] is not None, (
            f"cold-start row for {row['irwin_id']} must have last_broadcast_at set"
        )


# ═════════════════════════════════════════════════════════════════════════════
# T5 — FIRMS attribution: _get_known_fires() sees point-only fires
# ═════════════════════════════════════════════════════════════════════════════

def test_t5_firms_attribution_sees_point_only_fires():
    """FIRMSAdapter._get_known_fires() returns point-only fires from the merged set."""
    from meshai.env.firms import FIRMSAdapter

    # Fires adapter with a point-only fire and a perimeter-backed fire
    fires_cfg = MagicMock()
    fires_cfg.state = "US-ID"
    fires_cfg.tick_seconds = 600
    fires_adapter = NICFFiresAdapter(fires_cfg)
    fires_adapter._events = [
        # Perimeter-backed fire (has polygon)
        _raw_fire(name="Perim Fire", irwin="IRWIN-PERIM-001", acres=500,
                  lat=43.5, lon=-115.5,
                  polygon=[[[-115.5, 43.4], [-115.4, 43.4], [-115.5, 43.6], [-115.5, 43.4]]]),
        # Point-only fire (no polygon)
        _raw_fire(name="Point Only Fire", irwin="IRWIN-PTONLY-001", acres=80,
                  lat=44.1, lon=-116.3, polygon=None),
    ]

    firms_cfg = MagicMock()
    firms_cfg.map_key = "test-key"
    firms_cfg.source = "VIIRS_SNPP_NRT"
    firms_cfg.bbox = [-117, 42, -114, 44]
    firms_cfg.day_range = 1
    firms_cfg.tick_seconds = 1800
    firms_cfg.confidence_min = "nominal"
    firms_cfg.proximity_km = 10.0

    firms = FIRMSAdapter(firms_cfg, region_anchors=[], fires_adapter=fires_adapter)
    known = firms._get_known_fires()

    assert len(known) == 2, f"Expected 2 known fires (perim + point-only), got {len(known)}"

    names = {f["name"] for f in known}
    assert "Perim Fire" in names
    assert "Point Only Fire" in names

    # Point-only fire must have correct coordinates for proximity matching
    pt = next(f for f in known if f["name"] == "Point Only Fire")
    assert pt["lat"] == pytest.approx(44.1, abs=0.001)
    assert pt["lon"] == pytest.approx(-116.3, abs=0.001)
