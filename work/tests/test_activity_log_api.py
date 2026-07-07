"""API tests for the Activity Log endpoint (GET /api/activity).

The Activity Log reads the FULL mesh_broadcasts_out audit log -- every
category (weather/fires/satpass/band/traffic) across BOTH transports
(meshtastic + meshcore), newest-first, with limit/offset pagination and
OPTIONAL transport/category filters that default to "everything".

Uses FastAPI TestClient against the per-test tmp DB seeded by the conftest
autouse fixture; we insert a handful of mesh_broadcasts_out rows directly.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meshai.dashboard.api.alert_routes import router
from meshai.persistence import get_db


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router, prefix="/api")
    return TestClient(app)


def _seed(rows):
    """Insert (sent_at, recipient, channel, text, table, pk, transport, success)."""
    conn = get_db()
    for sent_at, recipient, channel, text, table, pk, transport, success in rows:
        conn.execute(
            "INSERT INTO mesh_broadcasts_out(sent_at, recipient, channel, "
            "text, source_event_table, source_event_pk, bytes_sent, "
            "ack_received, transport, success) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (sent_at, recipient, channel, text, table, pk,
             len(text.encode()), 0, transport, success),
        )
    conn.commit()


# Deliberately mixed transports + categories, out of chronological order.
SEED = [
    (100, "broadcast", 0, "old weather MT", "nws_alerts", "a", "meshtastic", 1),
    (200, "broadcast", "aida", "weather MC", "nws_alerts", "b", "meshcore", 1),
    (300, "broadcast", 0, "satpass MT", "satpass_events", "c", "meshtastic", 1),
    (400, "broadcast", "fire", "fire MC", "fires", "d", "meshcore", 1),
    (500, "broadcast", 0, "newest band MT", "band_conditions_broadcasts", "e",
     "meshtastic", 1),
]


def test_activity_returns_all_categories_and_both_meshes(client):
    """The default (unfiltered) feed returns EVERY seeded row -- no category
    or transport is dropped -- newest-first."""
    _seed(SEED)
    r = client.get("/api/activity")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == len(SEED)

    # newest-first ordering
    assert [e["sent_at"] for e in body] == [500, 400, 300, 200, 100]

    # both transports present
    assert {e["transport"] for e in body} == {"meshtastic", "meshcore"}
    # weather (nws) present on BOTH meshes -- the categories the stub feed missed
    nws = [e for e in body if e["source_event_table"] == "nws_alerts"]
    assert {e["transport"] for e in nws} == {"meshtastic", "meshcore"}
    # fire on the MeshCore side present
    assert any(
        e["source_event_table"] == "fires" and e["transport"] == "meshcore"
        for e in body
    )


def test_activity_pagination_limit_and_offset(client):
    """limit caps the page; offset walks further back, newest-first."""
    _seed(SEED)
    page1 = client.get("/api/activity?limit=2").json()
    assert [e["sent_at"] for e in page1] == [500, 400]
    page2 = client.get("/api/activity?limit=2&offset=2").json()
    assert [e["sent_at"] for e in page2] == [300, 200]
    page3 = client.get("/api/activity?limit=2&offset=4").json()
    assert [e["sent_at"] for e in page3] == [100]


def test_activity_optional_transport_filter(client):
    """transport is optional and narrows to one mesh when supplied."""
    _seed(SEED)
    mc = client.get("/api/activity?transport=meshcore").json()
    assert {e["transport"] for e in mc} == {"meshcore"}
    assert [e["sent_at"] for e in mc] == [400, 200]


def test_activity_optional_category_filter(client):
    """category is optional and narrows to one source_event_table -- this is
    how an operator surfaces weather when chatty satpass rows dominate."""
    _seed(SEED)
    weather = client.get("/api/activity?category=nws_alerts").json()
    assert {e["source_event_table"] for e in weather} == {"nws_alerts"}
    assert [e["sent_at"] for e in weather] == [200, 100]


def test_activity_empty_when_no_rows(client):
    """No broadcasts yet -> empty list, not an error."""
    r = client.get("/api/activity")
    assert r.status_code == 200
    assert r.json() == []
