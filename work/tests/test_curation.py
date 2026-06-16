"""v0.6-4 curation tests: schema, seed, accessors, REST API."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meshai.persistence.curation import (
    invalidate_curation_cache,
    lookup_gauge_site,
    lookup_town_anchor,
    seed_gauge_sites,
    seed_town_anchors,
    _GAUGE_SITES_SEED,
    _TOWN_ANCHORS_SEED,
)
from meshai.persistence import get_db
from meshai.dashboard.api.curation_routes import router as curation_router


# ============================================================================
# Schema + seed
# ============================================================================


def test_v8_v9_tables_present():
    conn = get_db()
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "gauge_sites" in tables
    assert "town_anchors" in tables


def test_gauge_sites_seeded_at_init():
    conn = get_db()
    n = conn.execute("SELECT COUNT(*) FROM gauge_sites").fetchone()[0]
    assert n == len(_GAUGE_SITES_SEED)


def test_town_anchors_seeded_at_init():
    conn = get_db()
    n = conn.execute("SELECT COUNT(*) FROM town_anchors").fetchone()[0]
    assert n == len(_TOWN_ANCHORS_SEED)


def test_seed_idempotent():
    conn = get_db()
    a = seed_gauge_sites(conn)
    b = seed_town_anchors(conn)
    assert a == 0 and b == 0


def test_seed_does_not_overwrite_user_edits():
    conn = get_db()
    conn.execute(
        "UPDATE gauge_sites SET action_ft=99 WHERE site_id='USGS-13186000'"
    )
    seed_gauge_sites(conn)
    r = conn.execute(
        "SELECT action_ft FROM gauge_sites WHERE site_id='USGS-13186000'"
    ).fetchone()
    assert r["action_ft"] == 99


# ============================================================================
# Accessors
# ============================================================================


def test_lookup_gauge_site_hits():
    invalidate_curation_cache()
    r = lookup_gauge_site("USGS-13186000")
    assert r is not None
    assert r["gauge_name"] == "Snake River at Heise"
    assert r["action_ft"] == 12.0


def test_lookup_gauge_site_miss():
    invalidate_curation_cache()
    assert lookup_gauge_site("USGS-99999") is None


def test_lookup_gauge_disabled_row_invisible():
    conn = get_db()
    conn.execute("UPDATE gauge_sites SET enabled=0 WHERE site_id='USGS-13186000'")
    invalidate_curation_cache()
    assert lookup_gauge_site("USGS-13186000") is None


def test_lookup_town_anchor_hits():
    invalidate_curation_cache()
    coord = lookup_town_anchor("Boise")
    assert coord is not None
    assert coord[0] == pytest.approx(43.6150)
    assert coord[1] == pytest.approx(-116.2023)


def test_lookup_town_anchor_case_insensitive():
    invalidate_curation_cache()
    a = lookup_town_anchor("MERIDIAN")
    b = lookup_town_anchor("meridian")
    assert a == b


def test_lookup_town_anchor_miss():
    invalidate_curation_cache()
    assert lookup_town_anchor("xxxx") is None


# ============================================================================
# REST API: gauge_sites
# ============================================================================


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(curation_router, prefix="/api")
    return TestClient(app)


def test_api_list_gauges(client):
    r = client.get("/api/gauge-sites")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == len(_GAUGE_SITES_SEED)


def test_api_get_one_gauge(client):
    r = client.get("/api/gauge-sites/USGS-13186000")
    assert r.status_code == 200
    assert r.json()["gauge_name"] == "Snake River at Heise"


def test_api_get_404(client):
    r = client.get("/api/gauge-sites/USGS-99999")
    assert r.status_code == 404


def test_api_post_add_gauge(client):
    r = client.post("/api/gauge-sites", json={
        "site_id": "USGS-NEW1", "gauge_name": "Test Gauge",
        "lat": 44.0, "lon": -115.0, "action_ft": 5.0,
    })
    assert r.status_code == 200, r.text
    assert r.json()["site_id"] == "USGS-NEW1"
    # Accessor sees it.
    invalidate_curation_cache()
    assert lookup_gauge_site("USGS-NEW1") is not None


def test_api_put_updates_gauge(client):
    r = client.put("/api/gauge-sites/USGS-13186000",
                    json={"action_ft": 15.5})
    assert r.status_code == 200
    assert r.json()["action_ft"] == 15.5
    invalidate_curation_cache()
    assert lookup_gauge_site("USGS-13186000")["action_ft"] == 15.5


def test_api_delete_gauge(client):
    r = client.delete("/api/gauge-sites/USGS-13186000")
    assert r.status_code == 200
    invalidate_curation_cache()
    assert lookup_gauge_site("USGS-13186000") is None


def test_api_post_missing_field_400(client):
    r = client.post("/api/gauge-sites", json={"gauge_name": "X"})
    assert r.status_code == 400


# ============================================================================
# REST API: town_anchors
# ============================================================================


def test_api_list_towns(client):
    r = client.get("/api/town-anchors")
    assert r.status_code == 200
    assert len(r.json()) == len(_TOWN_ANCHORS_SEED)


def test_api_post_add_town(client):
    r = client.post("/api/town-anchors", json={
        "name": "Bellevue", "lat": 43.4670, "lon": -114.2557, "state": "ID",
    })
    assert r.status_code == 200
    assert r.json()["name"] == "bellevue"
    invalidate_curation_cache()
    coord = lookup_town_anchor("bellevue")
    assert coord is not None


def test_api_put_town(client):
    # Find Boise's anchor_id first.
    r = client.get("/api/town-anchors")
    boise = next(t for t in r.json() if t["name"] == "boise")
    r2 = client.put(f"/api/town-anchors/{boise['anchor_id']}",
                     json={"enabled": False})
    assert r2.status_code == 200
    assert r2.json()["enabled"] is False
    invalidate_curation_cache()
    assert lookup_town_anchor("boise") is None


def test_api_delete_town(client):
    r = client.get("/api/town-anchors")
    nampa = next(t for t in r.json() if t["name"] == "nampa")
    r2 = client.delete(f"/api/town-anchors/{nampa['anchor_id']}")
    assert r2.status_code == 200
    invalidate_curation_cache()
    assert lookup_town_anchor("nampa") is None
