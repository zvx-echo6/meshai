"""API tests for /api/announcements (custom scheduled announcements).

Uses FastAPI TestClient, mirroring tests/test_adapter_config_api.py.
Covers CRUD, validation of every bad-input case named in the spec, the
create-always-disabled invariant, and that no code path under this router
ever sends anything (preview is preview-only; there is no send-now route).
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meshai.dashboard.api.announcement_routes import router


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router, prefix="/api")
    return TestClient(app)


def _valid_body(**overrides):
    body = {
        "name": "Morning greeting",
        "message": "Good morning, mesh!",
        "schedule_kind": "daily",
        "time_of_day": "08:00",
        "channels": [{"transport": "meshtastic", "channel": 2}],
    }
    body.update(overrides)
    return body


# ============================================================================
# Create -- always starts disabled
# ============================================================================


def test_create_returns_201_shape_and_starts_disabled(client):
    r = client.post("/api/announcements", json=_valid_body())
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert body["name"] == "Morning greeting"
    assert body["announcement_id"] is not None


def test_create_ignores_client_supplied_enabled_true(client):
    """There is no field in AnnouncementCreateBody for enabled at all --
    a client cannot arm an announcement at creation time."""
    r = client.post("/api/announcements", json={**_valid_body(), "enabled": True})
    assert r.status_code == 200
    assert r.json()["enabled"] is False


# ============================================================================
# List / get / delete
# ============================================================================


def test_list_returns_created_rows(client):
    client.post("/api/announcements", json=_valid_body(name="A"))
    client.post("/api/announcements", json=_valid_body(name="B"))
    r = client.get("/api/announcements")
    assert r.status_code == 200
    names = {row["name"] for row in r.json()}
    assert names == {"A", "B"}


def test_delete_removes_row(client):
    created = client.post("/api/announcements", json=_valid_body()).json()
    aid = created["announcement_id"]
    r = client.delete(f"/api/announcements/{aid}")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert client.get("/api/announcements").json() == []


def test_delete_unknown_id_404s(client):
    r = client.delete("/api/announcements/99999")
    assert r.status_code == 404


def test_update_unknown_id_404s(client):
    r = client.put("/api/announcements/99999", json={"name": "x"})
    assert r.status_code == 404


# ============================================================================
# Update -- can arm (enable) an announcement, partial merge re-validates whole
# ============================================================================


def test_update_can_enable_after_review(client):
    created = client.post("/api/announcements", json=_valid_body()).json()
    aid = created["announcement_id"]
    r = client.put(f"/api/announcements/{aid}", json={"enabled": True})
    assert r.status_code == 200
    assert r.json()["enabled"] is True


def test_update_switching_kind_without_required_field_is_rejected(client):
    """Switching to weekly without supplying dow_mask must fail even though
    dow_mask itself wasn't part of this request -- the MERGED record is
    what gets validated."""
    created = client.post("/api/announcements", json=_valid_body()).json()
    aid = created["announcement_id"]
    r = client.put(f"/api/announcements/{aid}", json={"schedule_kind": "weekly"})
    assert r.status_code == 400


def test_update_message_only_preserves_other_fields(client):
    created = client.post("/api/announcements", json=_valid_body()).json()
    aid = created["announcement_id"]
    r = client.put(f"/api/announcements/{aid}", json={"message": "updated text"})
    assert r.status_code == 200
    body = r.json()
    assert body["message"] == "updated text"
    assert body["schedule_kind"] == "daily"
    assert body["channels"] == [{"transport": "meshtastic", "channel": 2}]


# ============================================================================
# Validation -- each bad input named in the spec
# ============================================================================


def test_rejects_empty_message(client):
    r = client.post("/api/announcements", json=_valid_body(message=""))
    assert r.status_code == 400


def test_rejects_empty_name(client):
    r = client.post("/api/announcements", json=_valid_body(name="  "))
    assert r.status_code == 400


def test_rejects_invalid_hh_mm(client):
    for bad in ("25:00", "08:60", "not-a-time", "8"):
        r = client.post("/api/announcements", json=_valid_body(time_of_day=bad))
        assert r.status_code == 400, f"{bad!r} should have been rejected"


def test_rejects_unknown_schedule_kind(client):
    r = client.post("/api/announcements", json=_valid_body(schedule_kind="hourly"))
    assert r.status_code == 400


def test_rejects_empty_channel_list(client):
    r = client.post("/api/announcements", json=_valid_body(channels=[]))
    assert r.status_code == 400


def test_does_not_reject_a_long_channel_list(client):
    """No cap: a long, mixed-transport channel list is valid."""
    channels = [{"transport": "meshtastic", "channel": i} for i in range(25)]
    channels += [{"transport": "meshcore", "channel": f"#ch{i}"} for i in range(25)]
    r = client.post("/api/announcements", json=_valid_body(channels=channels))
    assert r.status_code == 200
    assert len(r.json()["channels"]) == 50


def test_rejects_day_of_month_out_of_range(client):
    for bad in (0, 32, -1):
        r = client.post("/api/announcements", json=_valid_body(
            schedule_kind="monthly", day_of_month=bad,
        ))
        assert r.status_code == 400, f"day_of_month={bad} should have been rejected"


def test_monthly_accepts_valid_day_of_month(client):
    r = client.post("/api/announcements", json=_valid_body(
        schedule_kind="monthly", day_of_month=31,
    ))
    assert r.status_code == 200


def test_rejects_dow_mask_wrong_length(client):
    r = client.post("/api/announcements", json=_valid_body(
        schedule_kind="weekly", dow_mask=[True, False, True],
    ))
    assert r.status_code == 400


def test_rejects_dow_mask_non_boolean_entries(client):
    r = client.post("/api/announcements", json=_valid_body(
        schedule_kind="weekly", dow_mask=[1, 1, 1, 1, 1, 1, 1],
    ))
    assert r.status_code == 400


def test_weekly_accepts_valid_dow_mask(client):
    r = client.post("/api/announcements", json=_valid_body(
        schedule_kind="weekly", dow_mask=[True, False, False, False, False, False, False],
    ))
    assert r.status_code == 200


def test_rejects_missing_interval_days_for_interval_kind(client):
    r = client.post("/api/announcements", json=_valid_body(schedule_kind="interval_days"))
    assert r.status_code == 400


def test_interval_days_accepts_valid_value(client):
    r = client.post("/api/announcements", json=_valid_body(
        schedule_kind="interval_days", interval_days=2,
    ))
    assert r.status_code == 200


def test_rejects_channel_shape_meshtastic_non_int_channel(client):
    r = client.post("/api/announcements", json=_valid_body(
        channels=[{"transport": "meshtastic", "channel": "2"}],
    ))
    assert r.status_code == 400


def test_rejects_channel_shape_meshcore_empty_channel(client):
    r = client.post("/api/announcements", json=_valid_body(
        channels=[{"transport": "meshcore", "channel": ""}],
    ))
    assert r.status_code == 400


def test_rejects_channel_shape_unknown_transport(client):
    r = client.post("/api/announcements", json=_valid_body(
        channels=[{"transport": "carrier_pigeon", "channel": 1}],
    ))
    assert r.status_code == 400


def test_does_not_hard_reject_meshcore_channel_not_currently_on_radio(client):
    """Shape validation only -- a syntactically valid MeshCore channel name
    is accepted even though this test never provisions a live radio."""
    r = client.post("/api/announcements", json=_valid_body(
        channels=[{"transport": "meshcore", "channel": "#not-provisioned-yet"}],
    ))
    assert r.status_code == 200


# ============================================================================
# Preview -- exact wire text + never sends
# ============================================================================


def test_preview_returns_wire_text_and_counts_without_sending(client, monkeypatch):
    created = client.post("/api/announcements", json=_valid_body(
        message="Good morning, mesh!",
    )).json()
    aid = created["announcement_id"]

    r = client.post(f"/api/announcements/{aid}/preview")
    assert r.status_code == 200
    body = r.json()
    assert body["wire_text"] == "Good morning, mesh!"
    assert body["char_count"] == len("Good morning, mesh!")
    assert body["byte_count"] == len("Good morning, mesh!".encode("utf-8"))
    assert body["truncated"] is False
    assert "budget" in body


def test_preview_truncates_long_message_to_budget(client):
    long_message = "y" * 500
    created = client.post("/api/announcements", json=_valid_body(
        message=long_message,
    )).json()
    aid = created["announcement_id"]

    r = client.post(f"/api/announcements/{aid}/preview")
    assert r.status_code == 200
    body = r.json()
    assert body["char_count"] <= 140
    assert body["truncated"] is True
    assert body["wire_text"] != long_message


def test_preview_unknown_id_404s(client):
    r = client.post("/api/announcements/99999/preview")
    assert r.status_code == 404


# ============================================================================
# No send-anywhere guarantee
# ============================================================================


def test_no_send_endpoint_exists_anywhere_in_router():
    paths = {(getattr(rt, "path", ""), tuple(getattr(rt, "methods", []) or []))
             for rt in router.routes}
    for path, methods in paths:
        assert "send" not in path.lower(), f"unexpected send-shaped route: {path}"


def test_create_does_not_touch_mesh_broadcasts_out(client):
    from meshai.persistence import get_db
    conn = get_db()
    before = conn.execute("SELECT COUNT(*) AS c FROM mesh_broadcasts_out").fetchone()["c"]
    client.post("/api/announcements", json=_valid_body())
    after = conn.execute("SELECT COUNT(*) AS c FROM mesh_broadcasts_out").fetchone()["c"]
    assert after == before


def test_update_does_not_touch_mesh_broadcasts_out(client):
    from meshai.persistence import get_db
    conn = get_db()
    created = client.post("/api/announcements", json=_valid_body()).json()
    aid = created["announcement_id"]
    before = conn.execute("SELECT COUNT(*) AS c FROM mesh_broadcasts_out").fetchone()["c"]
    client.put(f"/api/announcements/{aid}", json={"enabled": True})
    after = conn.execute("SELECT COUNT(*) AS c FROM mesh_broadcasts_out").fetchone()["c"]
    assert after == before


def test_preview_does_not_touch_mesh_broadcasts_out(client):
    from meshai.persistence import get_db
    conn = get_db()
    created = client.post("/api/announcements", json=_valid_body()).json()
    aid = created["announcement_id"]
    before = conn.execute("SELECT COUNT(*) AS c FROM mesh_broadcasts_out").fetchone()["c"]
    client.post(f"/api/announcements/{aid}/preview")
    after = conn.execute("SELECT COUNT(*) AS c FROM mesh_broadcasts_out").fetchone()["c"]
    assert after == before
