"""Boolean round-trip tests for adapter_config enabled toggle.

Proves:
 1. PUT satpass.enabled=true  -> value_json='true' in DB -> accessor reads Python True
 2. PUT satpass.enabled=false -> value_json='false' in DB -> accessor reads Python False
 3. Same round-trip on a second adapter (wfigs.broadcast_on_acres) -> not satpass-special
 4. PUT rejects non-bool values (string 'true', int 1)
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meshai.adapter_config import adapter_config, invalidate_cache
from meshai.dashboard.api.adapter_config_routes import router
from meshai.persistence import get_db


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router, prefix="/api")
    return TestClient(app)


# -- satpass.enabled boolean round-trip --

def test_satpass_enabled_true_roundtrip(client):
    """PUT enabled=true -> DB value_json='true' -> accessor returns Python True."""
    # Default is false
    assert adapter_config.satpass.enabled is False

    # PUT true
    r = client.put(
        "/api/adapter-config/satpass/enabled",
        json={"value": True},
    )
    assert r.status_code == 200
    assert r.json()["value"] is True

    # DB has value_json='true' (the JSON literal, not Python repr)
    conn = get_db()
    row = conn.execute(
        "SELECT value_json FROM adapter_config WHERE adapter='satpass' AND key='enabled'"
    ).fetchone()
    assert row is not None
    assert row["value_json"] == "true", (
        f"expected value_json='true', got {row['value_json']!r}"
    )

    # Accessor reads Python True (not string, not int)
    val = adapter_config.satpass.enabled
    assert val is True
    assert type(val) is bool


def test_satpass_enabled_false_roundtrip(client):
    """PUT enabled=false after enabling -> DB value_json='false' -> accessor False."""
    # Enable first
    client.put("/api/adapter-config/satpass/enabled", json={"value": True})
    assert adapter_config.satpass.enabled is True

    # Disable
    r = client.put(
        "/api/adapter-config/satpass/enabled",
        json={"value": False},
    )
    assert r.status_code == 200
    assert r.json()["value"] is False

    # DB
    conn = get_db()
    row = conn.execute(
        "SELECT value_json FROM adapter_config WHERE adapter='satpass' AND key='enabled'"
    ).fetchone()
    assert row["value_json"] == "false"

    # Accessor
    assert adapter_config.satpass.enabled is False


def test_satpass_enabled_rejects_string_true(client):
    """String 'true' must be rejected -- only Python bool accepted."""
    r = client.put(
        "/api/adapter-config/satpass/enabled",
        json={"value": "true"},
    )
    assert r.status_code == 400


def test_satpass_enabled_rejects_int_one(client):
    """Integer 1 must be rejected -- only Python bool accepted."""
    r = client.put(
        "/api/adapter-config/satpass/enabled",
        json={"value": 1},
    )
    assert r.status_code == 400


# -- Second adapter boolean round-trip (not satpass-special) --

def test_wfigs_broadcast_on_acres_bool_roundtrip(client):
    """Same round-trip on wfigs.broadcast_on_acres proves bool handling is generic."""
    # Read default
    default_val = adapter_config.wfigs.broadcast_on_acres

    # Flip it
    new_val = not default_val
    r = client.put(
        "/api/adapter-config/wfigs/broadcast_on_acres",
        json={"value": new_val},
    )
    assert r.status_code == 200
    assert r.json()["value"] is new_val

    # DB has correct JSON literal
    conn = get_db()
    row = conn.execute(
        "SELECT value_json FROM adapter_config WHERE adapter='wfigs' AND key='broadcast_on_acres'"
    ).fetchone()
    assert row["value_json"] == json.dumps(new_val)

    # Accessor
    val = adapter_config.wfigs.broadcast_on_acres
    assert val is new_val
    assert type(val) is bool


def test_reminders_wfigs_enabled_bool_roundtrip(client):
    """Third adapter (reminders_wfigs.enabled) -- additional proof of generic handling."""
    # Read default
    default_val = adapter_config.reminders_wfigs.enabled

    # Flip
    new_val = not default_val
    r = client.put(
        "/api/adapter-config/reminders_wfigs/enabled",
        json={"value": new_val},
    )
    assert r.status_code == 200
    assert r.json()["value"] is new_val

    # Accessor returns the correct bool
    assert adapter_config.reminders_wfigs.enabled is new_val
    assert type(adapter_config.reminders_wfigs.enabled) is bool
