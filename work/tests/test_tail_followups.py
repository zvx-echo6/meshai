"""v0.6-tail tests: 5 follow-ups."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meshai.adapter_config import adapter_config, invalidate_cache
from meshai.persistence import get_db


# ============================================================================
# Item 1 -- auto-refresh ToggleFilter on PUT /api/config/notifications
# ============================================================================


def test_auto_refresh_middleware_fires_on_notifications_put():
    """The middleware calls ToggleFilter.refresh() on a successful PUT
    that touches the notifications section."""
    from meshai.dashboard.api import config_routes
    from types import SimpleNamespace

    refreshed = {"n": 0}

    class _StubTF:
        def refresh(self, config):
            refreshed["n"] += 1

    app = FastAPI()
    app.state.config = SimpleNamespace()  # any truthy stand-in
    bus = SimpleNamespace()
    bus._pipeline_components = {"toggle_filter": _StubTF()}
    app.state.bus = bus

    config_routes.register_config_routes_hooks(app)

    @app.put("/api/config/notifications")
    async def _put(): return {"ok": True}

    client = TestClient(app)
    client.put("/api/config/notifications", json={"enabled": True})
    assert refreshed["n"] == 1


def test_auto_refresh_does_not_fire_on_other_section():
    from meshai.dashboard.api import config_routes
    from types import SimpleNamespace

    refreshed = {"n": 0}

    class _StubTF:
        def refresh(self, config):
            refreshed["n"] += 1

    app = FastAPI()
    app.state.config = SimpleNamespace()
    bus = SimpleNamespace()
    bus._pipeline_components = {"toggle_filter": _StubTF()}
    app.state.bus = bus

    config_routes.register_config_routes_hooks(app)

    @app.put("/api/config/llm")
    async def _put(): return {"ok": True}

    client = TestClient(app)
    client.put("/api/config/llm", json={})
    assert refreshed["n"] == 0


def test_create_app_registers_auto_refresh_middleware():
    """Regression: create_app() must actually WIRE the auto-refresh middleware.
    It was defined in register_config_routes_hooks() but never called from
    create_app(), so in prod a saved toggle never refreshed the live filter and
    had to be poked with POST /api/notifications/refresh-toggles by hand."""
    from meshai.dashboard.server import create_app

    app = create_app()
    dispatches = []
    for mw in app.user_middleware:
        fn = (getattr(mw, "kwargs", {}) or {}).get("dispatch")
        if fn is not None:
            dispatches.append(getattr(fn, "__qualname__", ""))
    assert any("_auto_refresh_toggle_filter" in q for q in dispatches), (
        "create_app() did not register the toggle auto-refresh middleware"
    )


# ============================================================================
# Item 2 -- env_reporter cap from adapter_config
# ============================================================================


def test_env_reporter_default_cap_3000():
    invalidate_cache()
    from meshai.notifications.env_reporter import _block_cap, _DEFAULT_BLOCK_MAX_CHARS
    assert _block_cap() == 3000
    assert _DEFAULT_BLOCK_MAX_CHARS == 3000


def test_env_reporter_cap_respects_config_mutation():
    """PUT-equivalent: change the row, invalidate, next call returns new cap."""
    invalidate_cache()
    conn = get_db()
    conn.execute(
        "UPDATE adapter_config SET value_json=? "
        "WHERE adapter='pipeline' AND key='env_reporter_block_chars'",
        ("500",),
    )
    invalidate_cache()
    from meshai.notifications.env_reporter import _block_cap
    assert _block_cap() == 500


# ============================================================================
# Item 3 -- gauge_sites bulk import (CSV path)
# ============================================================================


@pytest.fixture
def client():
    from meshai.dashboard.api.gauge_sites_import import router as imp_router
    from meshai.dashboard.api.curation_routes import router as cur_router
    app = FastAPI()
    app.include_router(imp_router, prefix="/api")
    app.include_router(cur_router, prefix="/api")
    return TestClient(app)


def test_csv_import_inserts_new_rows(client):
    csv_data = (
        "site_id,gauge_name,lat,lon,action_ft,flood_minor_ft,"
        "flood_moderate_ft,flood_major_ft\n"
        "USGS-NEW1,Bellevue Creek,43.467,-114.255,3.0,4.5,,\n"
        "USGS-NEW2,Phantom River,42.0,-114.0,2.0,3.0,4.0,5.0\n"
    )
    r = client.post("/api/gauge-sites/import", json={
        "format": "csv", "data": csv_data,
    })
    assert r.status_code == 200, r.text
    assert r.json()["inserted"] == 2

    r2 = client.get("/api/gauge-sites/USGS-NEW1")
    assert r2.status_code == 200
    assert r2.json()["gauge_name"] == "Bellevue Creek"


def test_csv_import_updates_existing(client):
    """Re-importing the same site updates rather than dupes."""
    csv1 = "site_id,gauge_name,lat,lon\nUSGS-UPSERT,Original,43,-115\n"
    r = client.post("/api/gauge-sites/import", json={"format": "csv", "data": csv1})
    assert r.json()["inserted"] == 1

    csv2 = "site_id,gauge_name,lat,lon\nUSGS-UPSERT,Renamed,43.5,-115.5\n"
    r2 = client.post("/api/gauge-sites/import", json={"format": "csv", "data": csv2})
    assert r2.json()["updated"] == 1
    assert r2.json()["inserted"] == 0

    r3 = client.get("/api/gauge-sites/USGS-UPSERT")
    assert r3.json()["gauge_name"] == "Renamed"


def test_csv_import_skips_bad_rows(client):
    csv_data = (
        "site_id,gauge_name,lat,lon\n"
        "USGS-GOOD,Good Gauge,43,-115\n"
        ",NoSiteId,42,-114\n"
        "USGS-BAD,Bad Coords,not_a_number,oops\n"
    )
    r = client.post("/api/gauge-sites/import", json={
        "format": "csv", "data": csv_data,
    })
    body = r.json()
    assert body["inserted"] == 1
    assert body["skipped"] == 2


def test_csv_import_rejects_missing_required(client):
    csv_data = "gauge_name,lat,lon\nNo Site Id Column,43,-115\n"
    r = client.post("/api/gauge-sites/import", json={
        "format": "csv", "data": csv_data,
    })
    assert r.status_code == 400


def test_import_rejects_bad_format(client):
    r = client.post("/api/gauge-sites/import", json={
        "format": "yaml", "data": "x: 1",
    })
    assert r.status_code == 400


# ---- AHPS parsing (unit-level, no live HTTP) ---------------------------


def test_ahps_index_parses_gauge_links():
    from meshai.dashboard.api.gauge_sites_import import _ahps_parse_index
    html = """
        <html><body>
        <a href="hydrograph.php?gage=hyiq2&prog=foo">HYIQ2 Cache Peak Gauge</a>
        <a href="hydrograph.php?gage=bldz2">BLDZ2 Boise River</a>
        <a href="other.php?gage=ignored">ignore me</a>
        </body></html>
    """
    gauges = _ahps_parse_index(html)
    assert ("hyiq2", "HYIQ2 Cache Peak Gauge") in gauges
    assert ("bldz2", "BLDZ2 Boise River") in gauges
    assert len(gauges) == 2


def test_ahps_detail_extracts_thresholds():
    from meshai.dashboard.api.gauge_sites_import import _ahps_parse_detail
    html = """
        Latitude: 43.690
        Longitude: -116.200
        Action Stage         8.0 ft
        Minor Flood Stage   10.5 ft
        Moderate Flood Stage 12.0 ft
        Major Flood Stage   14.5 ft
    """
    parsed = _ahps_parse_detail(html)
    assert parsed["lat"] == 43.690
    assert parsed["lon"] == -116.200
    assert parsed["action_ft"] == 8.0
    assert parsed["flood_minor_ft"] == 10.5
    assert parsed["flood_moderate_ft"] == 12.0
    assert parsed["flood_major_ft"] == 14.5


# ============================================================================
# Item 3b -- gauge_sites import route is registered on the real app
#
# The endpoint logic above is fully covered, but until server.create_app()
# calls app.include_router(gauge_sites_import_router, ...) the route is
# unreachable in production -- the raw-router `client` fixture above can't
# catch that because it builds its own bare FastAPI app. This test drives
# the actual dashboard app the server process serves.
# ============================================================================


def test_gauge_sites_import_reachable_through_real_app():
    from meshai.dashboard.server import create_app

    app = create_app()
    real_client = TestClient(app)

    res = real_client.post(
        "/api/gauge-sites/import",
        json={
            "format": "csv",
            "data": "site_id,gauge_name,lat,lon\nUSGS-REAL1,Real App Creek,43.5,-114.5\n",
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["inserted"] == 1
    assert body["updated"] == 0

    # And it actually landed -- round-trip through the sibling curation
    # router (also mounted on the real app) to prove the 200 reflects a
    # real DB write through the real app, not a false positive.
    listed = real_client.get("/api/gauge-sites").json()
    assert any(r["site_id"] == "USGS-REAL1" for r in listed)


# ============================================================================
# Item 4 -- WFIGS tombstone column + reminder behavior
# ============================================================================


def test_fires_has_tombstoned_at_column():
    conn = get_db()
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(fires)").fetchall()}
    assert "tombstoned_at" in cols


def test_wfigs_tombstone_stamps_column():
    """A tombstone envelope sets fires.tombstoned_at."""
    from meshai.central.wfigs_handler import handle_wfigs
    conn = get_db()
    # Seed an active fire row.
    irwin = "TOMB-1"
    now = int(time.time())
    conn.execute(
        "INSERT INTO fires(irwin_id, incident_name, incident_type, "
        "current_acres, current_contained_pct, lat, lon, county, state, "
        "declared_at, last_event_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (irwin, "Test", "WF", 100, 10, 43.6, -116.2, "Ada", "ID", now - 3600, now),
    )
    n = {"_kind": "wfigs_tombstone", "irwin_id": irwin}
    envelope = {"data": {"adapter": "fires", "category": "fire.incident.removed",
                          "id": irwin}}
    handle_wfigs(n, envelope, "central.fire.incident.removed.id",
                  data=None, now=now)
    row = conn.execute("SELECT tombstoned_at FROM fires WHERE irwin_id=?",
                        (irwin,)).fetchone()
    assert row["tombstoned_at"] is not None


def _enable_wfigs_reminders():
    """Enable wfigs reminders (default is disabled in adapter_config)."""
    conn = get_db()
    conn.execute(
        "UPDATE adapter_config SET default_json='true' "
        "WHERE adapter='reminders_wfigs' AND key='enabled'"
    )
    conn.execute(
        "UPDATE adapter_config SET value_json='true' "
        "WHERE adapter='reminders_wfigs' AND key='enabled'"
    )
    from meshai.adapter_config import adapter_config as _ac
    _ac.invalidate()


def test_reminder_skipped_when_fire_tombstoned():
    """ReminderScheduler treats fires.tombstoned_at NOT NULL as terminated."""
    from meshai.notifications.reminders import ReminderScheduler
    conn = get_db()
    now = 1_780_000_000
    irwin = "REM-TOMB"
    last = now - 10 * 3600
    # Active fire 10h past last broadcast (would otherwise fire)
    conn.execute(
        "INSERT INTO fires(irwin_id, incident_name, incident_type, "
        "current_acres, current_contained_pct, lat, lon, county, state, "
        "declared_at, last_event_at, first_broadcast_at, last_broadcast_at, "
        "tombstoned_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (irwin, "T", "WF", 100, 10, 43.6, -116.2, "Ada", "ID",
         last, now, last, last, now - 100),  # tombstoned
    )
    dispatcher = MagicMock()
    dispatcher.dispatch_scheduled_broadcast = AsyncMock(return_value=True)
    sch = ReminderScheduler(dispatcher, clock=lambda: now)
    import asyncio
    fired = asyncio.run(sch.tick_once())
    assert fired == 0
    dispatcher.dispatch_scheduled_broadcast.assert_not_called()


def test_reminder_fires_when_fire_not_tombstoned():
    """Same shape but tombstoned_at IS NULL -> reminder fires."""
    from meshai.notifications.reminders import ReminderScheduler
    _enable_wfigs_reminders()
    conn = get_db()
    now = 1_780_000_000
    irwin = "REM-LIVE"
    last = now - 10 * 3600
    conn.execute(
        "INSERT INTO fires(irwin_id, incident_name, incident_type, "
        "current_acres, current_contained_pct, lat, lon, county, state, "
        "declared_at, last_event_at, first_broadcast_at, last_broadcast_at, "
        "tombstoned_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (irwin, "L", "WF", 100, 10, 43.6, -116.2, "Ada", "ID",
         last, now, last, last, None),
    )
    dispatcher = MagicMock()
    dispatcher.dispatch_scheduled_broadcast = AsyncMock(return_value=True)
    # wfigs reminders go out via dispatch_scheduled_fire_broadcast, a
    # distinct method from the generic dispatch_scheduled_broadcast (see
    # meshai/notifications/reminders/__init__.py); both must be mocked as
    # AsyncMock or the un-mocked plain-MagicMock attribute raises
    # "object MagicMock can't be used in 'await' expression" (test_reminders.py
    # mocks both for the same reason).
    dispatcher.dispatch_scheduled_fire_broadcast = AsyncMock(return_value=True)
    sch = ReminderScheduler(dispatcher, clock=lambda: now)
    import asyncio
    fired = asyncio.run(sch.tick_once())
    assert fired == 1
