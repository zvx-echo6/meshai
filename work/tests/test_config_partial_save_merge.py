"""Regression tests: PUT /api/config/{section} must MERGE partial payloads.

The outage (2026-07-17 06:46:52 on CT108)
-----------------------------------------
Saving the "Auto-advert interval" dropdown on the MeshCore Companion page PUT a
single-key body to /api/config/connection:

    {"meshcore_advert_interval_seconds": 10800}

`_dict_to_dataclass(ConnectionConfig, body)` builds kwargs ONLY from keys present
in the body, so `return cls(**kwargs)` gave every ABSENT field its dataclass
default. One click rewrote meshtastic.yaml to defaults and took BOTH radios
offline:

    type:                 tcp   -> serial            (Meshtastic offline)
    tcp_host:             192.168.1.100 -> <deleted> (LOCAL_FIELDS, write failed)
    tcp_port:             4404  -> 4403              (wrong meshmonitor vnode)
    meshcore_host:        192.168.1.253 -> ''        (MeshCore off; blank = off)
    meshcore_conn_type:   serial -> tcp              (wrong transport)
    meshcore_serial_port: /dev/meshcore-rak -> ''    (RAK radio lost)

It went unnoticed because `connection` is restart-required: the running process
kept the good in-memory config while the file sat gutted, waiting for any
restart.

This is NOT one page's bug -- the route is destructive on a partial payload for
EVERY section. Other callers only survive because they happen to spread the full
object first.

Fix: the route merges the body over the CURRENT section dict before coercing, so
omitted keys keep their live values while explicitly-sent keys still apply.
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Stub heavy optional deps so config_routes imports without them.
for _mod in ("openai", "aiosqlite", "anthropic", "google", "google.genai"):
    sys.modules.setdefault(_mod, MagicMock())

from meshai.config import (  # noqa: E402
    Config,
    ConnectionConfig,
    NotificationsConfig,
    RegionRouteMatrix,
)
from meshai.dashboard.api.config_routes import router  # noqa: E402


# The live CT108 connection values that the outage destroyed.
LIVE_CONNECTION = {
    "type": "tcp",
    "serial_port": "/dev/ttyUSB0",
    "tcp_host": "192.168.1.100",
    "tcp_port": 4404,
    "mesh_max_chars": 140,
    "meshcore_host": "192.168.1.253",
    "meshcore_port": 5050,
    "meshcore_auto_reconnect": False,
    "meshcore_advert_interval_seconds": 86400,
    "meshcore_conn_type": "serial",
    "meshcore_serial_port": "/dev/meshcore-rak",
    "meshcore_baud": 115200,
    "meshtastic_send_pacing_min_seconds": 2.2,
    "meshtastic_send_pacing_max_seconds": 2.6,
}


@pytest.fixture
def config_dir(tmp_path):
    """A minimal on-disk multi-file config dir with the live connection values."""
    (tmp_path / "config.yaml").write_text(
        "timezone: America/Boise\n"
        "connection: !include meshtastic.yaml\n"
    )
    (tmp_path / "meshtastic.yaml").write_text(
        yaml.safe_dump({"connection": dict(LIVE_CONNECTION)}, sort_keys=False)
    )
    return tmp_path


@pytest.fixture
def client(config_dir):
    """TestClient whose app.state.config carries the LIVE connection values."""
    app = FastAPI()
    app.include_router(router, prefix="/api")

    config = Config()
    config.connection = ConnectionConfig(**LIVE_CONNECTION)

    app.state.config = config
    app.state.config_path = str(config_dir / "config.yaml")
    return TestClient(app)


def _saved_connection(config_dir) -> dict:
    """The EFFECTIVE persisted connection section, across both files it spans.

    save_section() splits the section by LOCAL_FIELDS: `connection.tcp_host` is
    deliberately relocated to local.yaml as `infrastructure.tcp_host`, so it is
    absent from meshtastic.yaml by design. Reassemble the operator-visible view
    so the assertions test the config that actually takes effect.

    That split is also the outage's second act: save_section writes the domain
    file FIRST and local.yaml SECOND, so the gutted meshtastic.yaml hit the disk
    and *then* the local.yaml write died on `[Errno 13] Permission denied`,
    stranding tcp_host in neither file.
    """
    conn = dict(yaml.safe_load((config_dir / "meshtastic.yaml").read_text())["connection"])
    local_path = config_dir / "local.yaml"
    if local_path.exists():
        local = yaml.safe_load(local_path.read_text()) or {}
        tcp_host = (local.get("infrastructure") or {}).get("tcp_host")
        if tcp_host is not None:
            conn["tcp_host"] = tcp_host
    return conn


# ==========================================================================
# STAGE 1 -- the reproduction. Fails on unfixed code.
# ==========================================================================

def test_partial_connection_put_does_not_wipe_other_fields(client, config_dir):
    """THE OUTAGE. The exact payload that took both radios offline.

    A PUT carrying ONLY meshcore_advert_interval_seconds must change ONLY that
    field. Every omitted field must keep its live value -- not its dataclass
    default.
    """
    resp = client.put(
        "/api/config/connection",
        json={"meshcore_advert_interval_seconds": 86400},
    )
    assert resp.status_code == 200, resp.text

    saved = _saved_connection(config_dir)

    # The field we actually sent applied.
    assert saved["meshcore_advert_interval_seconds"] == 86400

    # ...and NOTHING else moved. These are the fields the outage destroyed.
    assert saved["type"] == "tcp", "Meshtastic transport reset to dataclass default"
    assert saved["tcp_host"] == "192.168.1.100", "tcp_host lost"
    assert saved["tcp_port"] == 4404, "tcp_port reset to default vnode"
    assert saved["meshcore_host"] == "192.168.1.253", "MeshCore host blanked (= radio off)"
    assert saved["meshcore_conn_type"] == "serial", "MeshCore transport reset"
    assert saved["meshcore_serial_port"] == "/dev/meshcore-rak", "RAK radio path lost"
    assert saved["meshcore_auto_reconnect"] is False, "auto_reconnect flipped to default"
    assert saved["meshtastic_send_pacing_min_seconds"] == 2.2, "pacing tuning lost"
    assert saved["meshtastic_send_pacing_max_seconds"] == 2.6, "pacing tuning lost"


# ==========================================================================
# STAGE 4 -- merge semantics: multiple sections, and intentional clearing.
# ==========================================================================

def test_partial_connection_put_applies_the_sent_field(client, config_dir):
    """Merge must not make the route a no-op -- a sent field still changes."""
    resp = client.put(
        "/api/config/connection",
        json={"meshcore_advert_interval_seconds": 3600},
    )
    assert resp.status_code == 200, resp.text
    assert _saved_connection(config_dir)["meshcore_advert_interval_seconds"] == 3600


def test_explicit_empty_string_still_clears(client, config_dir):
    """Merge must not prevent INTENTIONAL clearing.

    Blanking meshcore_host is how an operator turns MeshCore off. An explicitly
    sent empty string must still apply -- only OMITTED keys are preserved.
    """
    resp = client.put(
        "/api/config/connection",
        json={"meshcore_host": ""},
    )
    assert resp.status_code == 200, resp.text

    saved = _saved_connection(config_dir)
    assert saved["meshcore_host"] == "", "explicit '' was swallowed by the merge"
    # ...while omitted neighbours still survive.
    assert saved["type"] == "tcp"
    assert saved["meshcore_serial_port"] == "/dev/meshcore-rak"


def test_explicit_false_still_applies(client, config_dir):
    """Falsy-but-present values (False) must apply, not be treated as absent."""
    client.put("/api/config/connection", json={"meshcore_auto_add_contacts": False})
    saved = _saved_connection(config_dir)
    assert saved["meshcore_auto_add_contacts"] is False
    assert saved["meshcore_host"] == "192.168.1.253"


def test_full_object_put_still_works(client, config_dir):
    """Existing callers spread the full object -- merge must not break them."""
    full = dict(LIVE_CONNECTION)
    full["tcp_port"] = 4405
    full["meshcore_host"] = "192.168.1.99"

    resp = client.put("/api/config/connection", json=full)
    assert resp.status_code == 200, resp.text

    saved = _saved_connection(config_dir)
    assert saved["tcp_port"] == 4405
    assert saved["meshcore_host"] == "192.168.1.99"
    assert saved["meshcore_serial_port"] == "/dev/meshcore-rak"


# ==========================================================================
# The bug was never connection-specific -- PUT /api/config/{section} was
# destructive on a partial payload for EVERY section. Prove the fix is general,
# and pin the nested merge semantics.
# ==========================================================================

@pytest.fixture
def notif_client(tmp_path):
    """TestClient with a populated `notifications` section."""
    (tmp_path / "config.yaml").write_text("timezone: America/Boise\n")
    (tmp_path / "notifications.yaml").write_text("enabled: true\n")

    app = FastAPI()
    app.include_router(router, prefix="/api")

    config = Config()
    config.notifications = NotificationsConfig(
        enabled=True,
        cold_start_grace_seconds=90,
        band_conditions_tz="America/Boise",
        region_routes=RegionRouteMatrix(
            mt_enabled=True,
            mc_enabled=False,
            cells={"weather": {"sw-id": {"mt": 3}, "sc-id": {"mt": 2}}},
        ),
    )

    app.state.config = config
    app.state.config_path = str(tmp_path / "config.yaml")
    return TestClient(app), tmp_path


def _saved_notifications(config_dir) -> dict:
    return yaml.safe_load((config_dir / "notifications.yaml").read_text())


def test_partial_notifications_put_keeps_other_fields(notif_client):
    """Same wipe, different section: a one-key PUT must not reset the rest."""
    client, config_dir = notif_client

    resp = client.put("/api/config/notifications", json={"enabled": False})
    assert resp.status_code == 200, resp.text

    saved = _saved_notifications(config_dir)
    assert saved["enabled"] is False, "the sent key must apply"
    # Omitted keys keep their LIVE values, not NotificationsConfig defaults.
    assert saved["cold_start_grace_seconds"] == 90, "reset to default (60)"
    assert saved["region_routes"]["cells"] != {}, "routing matrix wiped"
    assert saved["region_routes"]["mt_enabled"] is True, "mt routing silently disabled"


def test_nested_dataclass_partial_deep_merges(notif_client):
    """dict -> nested DATACLASS field deep-merges.

    region_routes is a RegionRouteMatrix (fixed schema), so flipping mc_enabled
    must not drop its sibling cells/mt_enabled.
    """
    client, config_dir = notif_client

    resp = client.put(
        "/api/config/notifications",
        json={"region_routes": {"mc_enabled": True}},
    )
    assert resp.status_code == 200, resp.text

    rr = _saved_notifications(config_dir)["region_routes"]
    assert rr["mc_enabled"] is True, "the sent key must apply"
    assert rr["mt_enabled"] is True, "sibling field reset to default"
    assert rr["cells"] == {"weather": {"sw-id": {"mt": 3}, "sc-id": {"mt": 2}}}, (
        "cells wiped by a sibling-key save"
    )


def test_dynamic_map_replaces_so_deletion_works(notif_client):
    """dict -> bare `dict` field REPLACES at the key.

    `cells` is a free-form map, so an operator removing a route cell must see it
    GONE. Deep-merging maps would resurrect the deleted key from the live config
    and make deletion impossible -- the mirror-image bug of the one being fixed.
    """
    client, config_dir = notif_client

    resp = client.put(
        "/api/config/notifications",
        json={"region_routes": {"cells": {"weather": {"sw-id": {"mt": 3}}}}},
    )
    assert resp.status_code == 200, resp.text

    rr = _saved_notifications(config_dir)["region_routes"]
    assert rr["cells"] == {"weather": {"sw-id": {"mt": 3}}}, (
        "sc-id was resurrected -- a deleted route cell must stay deleted"
    )
    # ...while the enclosing dataclass's other fields still deep-merge.
    assert rr["mt_enabled"] is True
