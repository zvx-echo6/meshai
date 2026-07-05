"""v0.6-3c API tests for adapter_config + adapter_meta routes.

Uses FastAPI TestClient against a tmp DB seeded by the conftest autouse
fixture. Covers: GET (list, per-adapter, single), PUT (incl. type
validation), POST reset, GET/PUT meta, cache invalidation propagation
to the accessor.
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meshai.adapter_config import adapter_config, invalidate_cache
from meshai.dashboard.api.adapter_config_routes import router


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router, prefix="/api")
    return TestClient(app)


# ============================================================================
# GET /api/adapter-config (grouped)
# ============================================================================


def test_list_returns_all_59_keys(client):
    r = client.get("/api/adapter-config")
    assert r.status_code == 200
    body = r.json()
    # 14 adapters with at least one key (itd_511 has zero -- not in the
    # grouped dict because the SQL only returns rows that exist).
    total = sum(len(v) for v in body.values())
    # was 96; config-schema cleanup removed the two deprecated nws keys
    # (broadcast_severities, warning_suffix_promotes) -> 94.
    assert total == 94


def test_list_grouped_by_adapter(client):
    r = client.get("/api/adapter-config")
    body = r.json()
    assert "wfigs" in body
    keys = {row["key"] for row in body["wfigs"]}
    assert keys == {"cooldown_seconds", "anchor_max_mi", "freshness_seconds",
                     "broadcast_on_acres", "broadcast_on_contained"}


def test_list_includes_type_value_default_description(client):
    r = client.get("/api/adapter-config")
    body = r.json()
    row = next(row for row in body["wfigs"] if row["key"] == "cooldown_seconds")
    assert row["value"] == 28800
    assert row["default"] == 28800
    assert row["type"] == "int"
    assert row["description"]


# ============================================================================
# GET /api/adapter-config/{adapter}
# ============================================================================


def test_per_adapter_list(client):
    r = client.get("/api/adapter-config/usgs_quake")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    keys = {row["key"] for row in body}
    assert keys == {
        "regional_centroid", "regional_radius_mi",
        "broadcast_pager_alerts", "global_mag_floor",
        "regional_mag_floor", "escalate_mag_floor",
    }


def test_per_adapter_empty_for_itd_511(client):
    """itd_511 has zero config keys post-3a.1; returns empty list, not 404."""
    r = client.get("/api/adapter-config/itd_511")
    assert r.status_code == 200
    assert len(r.json()) > 0  # itd_511 has adapter_config keys now


# ============================================================================
# GET /api/adapter-config/{adapter}/{key}
# ============================================================================


def test_get_single_key(client):
    r = client.get("/api/adapter-config/usgs_quake/global_mag_floor")
    assert r.status_code == 200
    body = r.json()
    assert body["value"] == 3.0
    assert body["default"] == 3.0
    assert body["type"] == "float"


def test_get_unknown_key_404(client):
    r = client.get("/api/adapter-config/wfigs/no_such_key")
    assert r.status_code == 404


# ============================================================================
# PUT /api/adapter-config/{adapter}/{key}
# ============================================================================


def test_put_updates_value(client):
    r = client.put(
        "/api/adapter-config/usgs_quake/global_mag_floor",
        json={"value": 2.8},
    )
    assert r.status_code == 200
    assert r.json()["value"] == 2.8
    # GET reflects the new value.
    g = client.get("/api/adapter-config/usgs_quake/global_mag_floor")
    assert g.json()["value"] == 2.8


def test_put_invalidates_accessor_cache(client):
    """The handler-side accessor reads the new value WITHOUT a restart."""
    # Prime the cache with the default.
    assert adapter_config.usgs_quake.global_mag_floor == 3.0
    # Mutate via API.
    client.put(
        "/api/adapter-config/usgs_quake/global_mag_floor",
        json={"value": 2.5},
    )
    # Accessor returns the new value -- cache invalidation worked.
    assert adapter_config.usgs_quake.global_mag_floor == 2.5


def test_put_int_validation(client):
    """int field rejects string body."""
    r = client.put(
        "/api/adapter-config/wfigs/cooldown_seconds",
        json={"value": "two hours"},
    )
    assert r.status_code == 400


def test_put_int_rejects_float_with_fraction(client):
    r = client.put(
        "/api/adapter-config/wfigs/cooldown_seconds",
        json={"value": 3600.5},
    )
    assert r.status_code == 400


def test_put_int_accepts_float_with_integer_value(client):
    r = client.put(
        "/api/adapter-config/wfigs/cooldown_seconds",
        json={"value": 3600.0},
    )
    assert r.status_code == 200
    assert r.json()["value"] == 3600


def test_put_float_accepts_int(client):
    r = client.put(
        "/api/adapter-config/usgs_quake/global_mag_floor",
        json={"value": 4},
    )
    assert r.status_code == 200
    assert r.json()["value"] == 4.0


def test_put_bool_rejects_int(client):
    r = client.put(
        "/api/adapter-config/wfigs/broadcast_on_acres",
        json={"value": 1},
    )
    assert r.status_code == 400


def test_put_str_validation(client):
    r = client.put(
        "/api/adapter-config/geocoder/photon_url",
        json={"value": 42},
    )
    assert r.status_code == 400


def test_put_json_accepts_list(client):
    # broadcast_severities was removed in the config-schema cleanup; use the
    # surviving nws json-list key tombstone_msgtypes to exercise list PUTs.
    r = client.put(
        "/api/adapter-config/nws/tombstone_msgtypes",
        json={"value": ["Cancel"]},
    )
    assert r.status_code == 200
    assert r.json()["value"] == ["Cancel"]


def test_put_json_accepts_dict(client):
    r = client.put(
        "/api/adapter-config/central/severity_thresholds",
        json={"value": {"routine_max": 0, "priority_max": 1, "immediate_min": 2}},
    )
    assert r.status_code == 200


def test_put_json_accepts_none(client):
    r = client.put(
        "/api/adapter-config/firms/bbox",
        json={"value": None},
    )
    assert r.status_code == 200
    assert r.json()["value"] is None


def test_put_unknown_key_404(client):
    r = client.put(
        "/api/adapter-config/wfigs/no_such_key",
        json={"value": 1},
    )
    assert r.status_code == 404


def test_put_missing_value_field(client):
    r = client.put(
        "/api/adapter-config/wfigs/cooldown_seconds",
        json={},
    )
    assert r.status_code == 400


# ============================================================================
# POST /api/adapter-config/{adapter}/{key}/reset
# ============================================================================


def test_reset_restores_default(client):
    # Mutate.
    client.put(
        "/api/adapter-config/usgs_quake/global_mag_floor",
        json={"value": 2.8},
    )
    assert client.get("/api/adapter-config/usgs_quake/global_mag_floor").json()["value"] == 2.8

    # Reset.
    r = client.post("/api/adapter-config/usgs_quake/global_mag_floor/reset")
    assert r.status_code == 200
    assert r.json()["value"] == 3.0
    # Accessor too.
    invalidate_cache()
    assert adapter_config.usgs_quake.global_mag_floor == 3.0


def test_reset_invalidates_cache(client):
    """Reset must invalidate the accessor cache the same as PUT."""
    client.put(
        "/api/adapter-config/usgs_quake/global_mag_floor",
        json={"value": 2.8},
    )
    # Prime cache with the post-PUT value.
    assert adapter_config.usgs_quake.global_mag_floor == 2.8
    client.post("/api/adapter-config/usgs_quake/global_mag_floor/reset")
    # Cache cleared -- next read returns the default.
    assert adapter_config.usgs_quake.global_mag_floor == 3.0


def test_reset_unknown_key_404(client):
    r = client.post("/api/adapter-config/wfigs/no_such_key/reset")
    assert r.status_code == 404


# ============================================================================
# GET /api/adapter-meta
# ============================================================================


def test_list_meta(client):
    r = client.get("/api/adapter-meta")
    assert r.status_code == 200
    body = r.json()
    assert "wfigs" in body
    assert body["wfigs"]["include_in_llm_context"] is True
    # central / geocoder default to False
    assert body["central"]["include_in_llm_context"] is False
    assert body["geocoder"]["include_in_llm_context"] is False


# ============================================================================
# PUT /api/adapter-meta/{adapter}
# ============================================================================


def test_put_meta_toggles_llm_context(client):
    r = client.put(
        "/api/adapter-meta/itd_511",
        json={"include_in_llm_context": False},
    )
    assert r.status_code == 200
    assert r.json()["include_in_llm_context"] is False
    # GET reflects the change.
    g = client.get("/api/adapter-meta").json()
    assert g["itd_511"]["include_in_llm_context"] is False


def test_put_meta_updates_display_name(client):
    r = client.put(
        "/api/adapter-meta/wfigs",
        json={"display_name": "Active wildfires (WFIGS)"},
    )
    assert r.status_code == 200
    assert r.json()["display_name"] == "Active wildfires (WFIGS)"


def test_put_meta_partial_update(client):
    """Only the fields in the body change; others survive."""
    original = client.get("/api/adapter-meta").json()["wfigs"]
    r = client.put(
        "/api/adapter-meta/wfigs",
        json={"include_in_llm_context": False},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["display_name"] == original["display_name"]  # unchanged
    assert body["include_in_llm_context"] is False


def test_put_meta_rejects_bad_bool(client):
    r = client.put(
        "/api/adapter-meta/wfigs",
        json={"include_in_llm_context": "yes"},
    )
    assert r.status_code == 400


def test_put_meta_rejects_empty_display_name(client):
    r = client.put(
        "/api/adapter-meta/wfigs",
        json={"display_name": "   "},
    )
    assert r.status_code == 400


def test_put_meta_unknown_adapter_404(client):
    r = client.put(
        "/api/adapter-meta/nonexistent_adapter",
        json={"include_in_llm_context": False},
    )
    assert r.status_code == 404
