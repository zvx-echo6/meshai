"""API tests for GET /api/health's `recommendations` field.

Covers the dashboard-recommendations wiring: mesh_reporter is exposed on
app.state (mirroring the existing health_engine/data_store/etc. pattern in
dashboard/server.py) and mesh_routes.py's health endpoint now returns real
recommendations from MeshReporter.recommendations_list("mesh") instead of
the old hardcoded `[]`.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meshai.dashboard.api.mesh_routes import router
from meshai.mesh_health import HealthScore, MeshHealth


def _client(health_engine, mesh_reporter=None):
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.state.health_engine = health_engine
    app.state.mesh_reporter = mesh_reporter
    return TestClient(app)


def _health_engine(mesh_health):
    engine = MagicMock()
    engine.mesh_health = mesh_health
    return engine


def test_health_endpoint_returns_recommendations():
    """recommendations_list("mesh") output reaches the REST /api/health body,
    flagged as `recommendations_available: True` (engine ran successfully)."""
    mesh_health = MeshHealth(score=HealthScore())
    engine = _health_engine(mesh_health)

    reporter = MagicMock()
    reporter.recommendations_list.return_value = [
        "Coverage gap in TestRegion: 3 nodes only reach 1 gateway.",
        "No MQTT uplinks in TestRegion. Enable on at least one infrastructure node.",
    ]

    client = _client(engine, mesh_reporter=reporter)
    r = client.get("/api/health")

    assert r.status_code == 200
    body = r.json()
    assert body["recommendations"] == [
        "Coverage gap in TestRegion: 3 nodes only reach 1 gateway.",
        "No MQTT uplinks in TestRegion. Enable on at least one infrastructure node.",
    ]
    assert body["recommendations_available"] is True
    reporter.recommendations_list.assert_called_once_with("mesh")


def test_health_endpoint_empty_recommendations_is_marked_available():
    """A genuinely healthy mesh: empty list AND recommendations_available=True.

    This is the "healthy" state — it must be distinguishable from the
    error/unwired states below, which also produce an empty list but with
    recommendations_available=False.
    """
    mesh_health = MeshHealth(score=HealthScore())
    engine = _health_engine(mesh_health)

    reporter = MagicMock()
    reporter.recommendations_list.return_value = []

    client = _client(engine, mesh_reporter=reporter)
    r = client.get("/api/health")

    assert r.status_code == 200
    body = r.json()
    assert body["recommendations"] == []
    assert body["recommendations_available"] is True


def test_health_endpoint_no_mesh_reporter_configured():
    """mesh_reporter can be None (e.g. Meshtastic not configured) — no crash,
    but this must NOT be indistinguishable from "healthy": empty list with
    recommendations_available=False, not True.
    """
    mesh_health = MeshHealth(score=HealthScore())
    engine = _health_engine(mesh_health)

    client = _client(engine, mesh_reporter=None)
    r = client.get("/api/health")

    assert r.status_code == 200
    body = r.json()
    assert body["recommendations"] == []
    assert body["recommendations_available"] is False


def test_health_endpoint_recommendations_error_is_swallowed_but_flagged(caplog):
    """A raising mesh_reporter must not break the health endpoint (the other
    fields are still useful), but the failure must be (a) logged, so it's
    traceable, and (b) surfaced via recommendations_available=False, so the
    UI never renders a crashed engine as "mesh is healthy"."""
    mesh_health = MeshHealth(score=HealthScore())
    engine = _health_engine(mesh_health)

    reporter = MagicMock()
    reporter.recommendations_list.side_effect = RuntimeError("boom")

    client = _client(engine, mesh_reporter=reporter)
    with caplog.at_level("ERROR"):
        r = client.get("/api/health")

    assert r.status_code == 200
    body = r.json()
    assert body["recommendations"] == []
    assert body["recommendations_available"] is False
    # The other fields on the response are unaffected by the recommendations
    # failure — a 500 must not take down the whole health endpoint.
    assert body["score"] == round(HealthScore().composite, 1)
    assert body["tier"] == HealthScore().tier
    assert any("recommendations_list failed" in rec.message for rec in caplog.records)


def test_health_endpoint_no_health_data_yet():
    """health_engine.mesh_health is None (not computed yet) — unaffected by recommendations wiring."""
    engine = _health_engine(None)
    reporter = MagicMock()

    client = _client(engine, mesh_reporter=reporter)
    r = client.get("/api/health")

    assert r.status_code == 200
    body = r.json()
    assert body["message"] == "Health engine not ready"
    reporter.recommendations_list.assert_not_called()
