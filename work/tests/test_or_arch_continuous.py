"""v0.6-tail-3 tests: env_routes 404 + config_routes restart_required diff."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meshai.config import Config


# ============================================================================
# Gap 1 -- env_routes lookup_usgs_site 404 when feed_source != native
# ============================================================================


def _build_app_with_env_store(env_store):
    """Mount the env router on a minimal FastAPI app w/ env_store on state."""
    from meshai.dashboard.api.env_routes import router as env_router
    app = FastAPI()
    app.state.env_store = env_store
    app.state.config = Config()
    app.include_router(env_router, prefix="/api")
    return app


def test_usgs_lookup_404_when_no_env_store():
    app = _build_app_with_env_store(env_store=None)
    client = TestClient(app)
    r = client.get("/api/env/usgs/lookup/13139510")
    assert r.status_code == 404


def test_usgs_lookup_404_when_no_native_usgs_adapter():
    """Central-feed mode: env_store exists but adapters['usgs'] is missing."""
    env_store = SimpleNamespace(_adapters={})  # no usgs adapter
    app = _build_app_with_env_store(env_store)
    client = TestClient(app)
    r = client.get("/api/env/usgs/lookup/13139510")
    assert r.status_code == 404
    body = r.json()
    # Body must explain the central-feed mode reason so the UI can switch
    # the form to manual-entry mode.
    assert "central" in body.get("detail", "").lower()
    assert "manual" in body.get("detail", "").lower()


def test_usgs_lookup_calls_native_adapter_when_available():
    """When the env_store has a native usgs adapter, lookup proxies to it."""
    fake_adapter = MagicMock()
    fake_adapter.lookup_site = MagicMock(return_value={
        "site_id": "13139510",
        "name": "Big Lost River near Mackay",
        "lat": 43.91,
        "lon": -113.62,
    })
    env_store = SimpleNamespace(_adapters={"usgs": fake_adapter})
    app = _build_app_with_env_store(env_store)
    client = TestClient(app)
    r = client.get("/api/env/usgs/lookup/13139510")
    assert r.status_code == 200
    assert r.json()["name"] == "Big Lost River near Mackay"
    fake_adapter.lookup_site.assert_called_once_with("13139510")


def test_usgs_lookup_502_on_adapter_exception():
    """A failure inside the native adapter surfaces as 502, not 200+error."""
    fake_adapter = MagicMock()
    fake_adapter.lookup_site = MagicMock(side_effect=RuntimeError("upstream timeout"))
    env_store = SimpleNamespace(_adapters={"usgs": fake_adapter})
    app = _build_app_with_env_store(env_store)
    client = TestClient(app)
    r = client.get("/api/env/usgs/lookup/13139510")
    assert r.status_code == 502


# ============================================================================
# Gap 2 -- config_routes RESTART_REQUIRED_SECTIONS + changed_keys diff
# ============================================================================


@pytest.fixture
def config_app(tmp_path, monkeypatch):
    """FastAPI app w/ config_routes mounted; uses a tmp config dir."""
    from meshai.dashboard.api.config_routes import router as config_router

    # save_section needs a real config dir to write to.
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "config.yaml").write_text("# stub\n")

    app = FastAPI()
    app.state.config = Config()
    app.state.config_path = str(cfg_dir / "config.yaml")
    app.include_router(config_router, prefix="/api")
    return app


def _config_app_with_store(tmp_path):
    """config_routes app with a REAL EnvironmentalStore on app.state so the
    hot-reload path (apply_config) actually runs on a PUT. Mirrors config_app
    but adds the store main.py stashes at boot."""
    from meshai.dashboard.api.config_routes import router as config_router
    from meshai.env.store import EnvironmentalStore
    from meshai.config import EnvironmentalConfig

    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "config.yaml").write_text("# stub\n")

    app = FastAPI()
    app.state.config = Config()
    app.state.config_path = str(cfg_dir / "config.yaml")
    app.state.env_store = EnvironmentalStore(EnvironmentalConfig(), event_bus=None)
    app.include_router(config_router, prefix="/api")
    return app


def test_environmental_not_in_restart_required_sections():
    # v0.16-env-hot-reload: environmental (and generic_sources) are now
    # hot-applied via EnvironmentalStore.apply_config() rather than demanding a
    # restart. Only a per-adapter feed_source->central flip still needs one, and
    # that is reported per-adapter (see below), not by section membership.
    from meshai.dashboard.api.config_routes import RESTART_REQUIRED_SECTIONS
    assert "environmental" not in RESTART_REQUIRED_SECTIONS
    assert "generic_sources" not in RESTART_REQUIRED_SECTIONS


def test_put_environmental_feed_source_to_central_still_requires_restart(tmp_path):
    """The one surviving restart case: flipping an adapter's feed_source to
    "central" is boot-only (CentralConsumer subscribe). apply_config reports it
    per-adapter as "restart_required", which the PUT handler surfaces."""
    client = TestClient(_config_app_with_store(tmp_path))

    cur = client.get("/api/config/environmental")
    assert cur.status_code == 200
    body = cur.json()
    # Mutate firms.feed_source from "native" (default) to "central".
    body["firms"]["feed_source"] = "central"

    r = client.put("/api/config/environmental", json=body)
    assert r.status_code == 200
    result = r.json()
    assert result["saved"] is True
    assert result["restart_required"] is True
    assert result["adapter_results"]["firms"] == "restart_required"
    assert any(k.endswith("firms.feed_source") for k in result["changed_keys"]), (
        f"changed_keys missing firms.feed_source: {result['changed_keys']}"
    )


def test_put_environmental_native_field_change_hot_reloads_no_restart(tmp_path):
    """A normal native-adapter field edit (nws.tick_seconds) hot-reloads that
    adapter in place: restart_required=false, adapter reported "reloaded"."""
    client = TestClient(_config_app_with_store(tmp_path))

    cur = client.get("/api/config/environmental")
    assert cur.status_code == 200
    body = cur.json()
    # nws is enabled+native by default; bump its poll cadence.
    body["nws"]["tick_seconds"] = int(body["nws"].get("tick_seconds", 60)) + 60

    r = client.put("/api/config/environmental", json=body)
    assert r.status_code == 200
    result = r.json()
    assert result["restart_required"] is False
    assert result["adapter_results"]["nws"] == "reloaded"
    assert any(k.endswith("nws.tick_seconds") for k in result["changed_keys"])


def test_put_non_restart_section_returns_restart_required_false(config_app):
    """A PUT to bot (not restart-required) returns restart_required=false."""
    client = TestClient(config_app)
    cur = client.get("/api/config/bot").json()
    cur["name"] = "NEW_NAME"
    r = client.put("/api/config/bot", json=cur)
    assert r.status_code == 200
    result = r.json()
    assert result["restart_required"] is False
    assert "bot.name" in result["changed_keys"]


def test_put_with_no_changes_returns_empty_changed_keys(config_app):
    """A no-op PUT returns restart_required=false + empty changed_keys."""
    client = TestClient(config_app)
    cur = client.get("/api/config/bot").json()
    r = client.put("/api/config/bot", json=cur)
    assert r.status_code == 200
    result = r.json()
    assert result["restart_required"] is False
    assert result["changed_keys"] == []


def test_put_environmental_no_change_does_not_flag_restart(config_app):
    """Even though environmental is restart-required, an unchanged PUT must
    not show restart_required=true. The restart-required state is
    contingent on real diff."""
    client = TestClient(config_app)
    cur = client.get("/api/config/environmental").json()
    r = client.put("/api/config/environmental", json=cur)
    assert r.status_code == 200
    result = r.json()
    assert result["restart_required"] is False
    assert result["changed_keys"] == []


def test_diff_keys_nested():
    """Spot-check the _diff_keys helper on nested dicts + lists."""
    from meshai.dashboard.api.config_routes import _diff_keys
    before = {"a": 1, "b": {"x": 1, "y": 2}, "c": [1, 2, 3]}
    after  = {"a": 1, "b": {"x": 9, "y": 2}, "c": [1, 2, 3, 4]}
    keys = _diff_keys(before, after, prefix="root")
    assert "root.b.x" in keys
    assert "root.c" in keys           # list length differs -> bracketless
    assert "root.a" not in keys


def test_diff_keys_list_element_changes():
    from meshai.dashboard.api.config_routes import _diff_keys
    before = {"a": [1, 2, 3]}
    after  = {"a": [1, 9, 3]}
    keys = _diff_keys(before, after, prefix="cfg")
    assert "cfg.a[1]" in keys
