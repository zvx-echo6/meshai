"""PUT /api/config/meshcore_context must hot-apply to the LIVE MeshCoreTransport.

Known stale-reference bug (2026-09-24 AIDA triple-DM diagnosis, item 3):
MeshCoreTransport.__init__ captures ``self._mc_context`` as a direct
reference to the MeshCoreContextConfig instance handed to it at
construction. update_config_section() (config_routes.py) replaces
``app.state.config.meshcore_context`` with a brand-new instance on every
save (never mutates the old one in place) -- so the transport's cached
reference goes stale immediately and every meshcore_context setting
(respond_to_dms, ignore_contacts, enable_passive_context, observe_channels,
addme_*) keeps enforcing whatever was live at boot until a restart, even
though the section is NOT in RESTART_REQUIRED_SECTIONS and the dashboard
tells the operator the change is live.

Fix: config_routes._refresh_meshcore_context() calls the transport's
existing (previously dead) ``set_context_config()`` after every successful
meshcore_context PUT -- mirroring the "context" section's
_refresh_mesh_context() hook.
"""
from __future__ import annotations

import importlib
import sys
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

for _mod in ("openai", "aiosqlite", "anthropic", "google", "google.genai"):
    if _mod not in sys.modules:
        try:
            importlib.import_module(_mod)
        except ImportError:
            sys.modules[_mod] = MagicMock()

from meshai.config import Config, MeshCoreContextConfig  # noqa: E402
from meshai.dashboard.api.config_routes import router  # noqa: E402


class _FakeMeshCoreChild:
    """Stand-in for MeshCoreTransport: records every set_context_config() call."""

    def __init__(self, initial=None):
        self.set_context_config_calls: list = []
        self._mc_context = initial

    def set_context_config(self, cfg) -> None:
        self.set_context_config_calls.append(cfg)
        self._mc_context = cfg


class _FakeCompositeConnector:
    """Stand-in for CompositeTransport: exposes meshcore_child()."""

    def __init__(self, mc_child):
        self._mc_child = mc_child

    def meshcore_child(self):
        return self._mc_child


@pytest.fixture
def config_dir(tmp_path):
    (tmp_path / "config.yaml").write_text("timezone: America/Boise\n")
    return tmp_path


@pytest.fixture
def app_and_child(config_dir):
    app = FastAPI()
    app.include_router(router, prefix="/api")

    config = Config()
    mc_child = _FakeMeshCoreChild(initial=config.meshcore_context)
    connector = _FakeCompositeConnector(mc_child)

    app.state.config = config
    app.state.config_path = str(config_dir / "config.yaml")
    app.state.connector = connector

    return app, mc_child


def test_meshcore_context_put_calls_set_context_config(app_and_child):
    app, mc_child = app_and_child
    client = TestClient(app)

    resp = client.put(
        "/api/config/meshcore_context",
        json={"respond_to_dms": False, "addme_enabled": False},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["restart_required"] is False

    assert len(mc_child.set_context_config_calls) == 1
    new_cfg = mc_child.set_context_config_calls[0]
    assert isinstance(new_cfg, MeshCoreContextConfig)
    assert new_cfg.respond_to_dms is False
    assert new_cfg.addme_enabled is False


def test_meshcore_context_put_result_reaches_transport_object(app_and_child):
    """The transport's own _mc_context (what mc_context_allows()/
    _is_addme_trigger() actually read) reflects the PUT, not the boot-time
    object -- this is the specific stale-copy bug being fixed."""
    app, mc_child = app_and_child
    client = TestClient(app)

    stale_ref = mc_child._mc_context
    assert stale_ref.observe_channels == []

    resp = client.put(
        "/api/config/meshcore_context",
        json={"observe_channels": ["#aida", "#general"]},
    )
    assert resp.status_code == 200, resp.text

    # The OLD object the transport started with is untouched (proves the
    # section really is replaced wholesale, not mutated in place)...
    assert stale_ref.observe_channels == []
    # ...but the transport's live _mc_context now points at the new one.
    assert mc_child._mc_context.observe_channels == ["#aida", "#general"]


def test_meshcore_context_put_does_not_call_set_context_config_when_no_meshcore_child(
    config_dir,
):
    """Meshtastic-only deployment (no MeshCore child): the PUT still
    succeeds; the refresh hook is just a no-op."""
    app = FastAPI()
    app.include_router(router, prefix="/api")

    config = Config()
    connector = _FakeCompositeConnector(mc_child=None)

    app.state.config = config
    app.state.config_path = str(config_dir / "config.yaml")
    app.state.connector = connector

    client = TestClient(app)
    resp = client.put("/api/config/meshcore_context", json={"respond_to_dms": False})
    assert resp.status_code == 200, resp.text


def test_meshcore_context_put_no_connector_does_not_raise(config_dir):
    """No connector on app.state at all (early startup/tests) -- PUT must
    not blow up."""
    app = FastAPI()
    app.include_router(router, prefix="/api")

    config = Config()
    app.state.config = config
    app.state.config_path = str(config_dir / "config.yaml")
    # app.state.connector deliberately absent.

    client = TestClient(app)
    resp = client.put("/api/config/meshcore_context", json={"respond_to_dms": False})
    assert resp.status_code == 200, resp.text


def test_mention_channels_already_hot_applied_via_shared_config(app_and_child):
    """Regression guard: meshcore_context.mention_channels is read by
    MessageRouter directly off the shared Config object (router.config.
    meshcore_context.mention_channels), NOT off the transport's private
    copy -- so it was ALREADY hot-applying before this fix, and must keep
    doing so. This only asserts the PUT updates the shared config object
    that a router would be reading."""
    app, mc_child = app_and_child
    client = TestClient(app)

    resp = client.put(
        "/api/config/meshcore_context",
        json={"mention_channels": ["#ops"]},
    )
    assert resp.status_code == 200, resp.text
    assert app.state.config.meshcore_context.mention_channels == ["#ops"]
