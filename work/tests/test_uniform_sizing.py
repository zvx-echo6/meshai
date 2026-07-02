"""Tests for Phase 3 uniform mesh-packet sizing.

Verifies that every size-sensitive code path uses the active transport's
max_chars rather than a hardcoded 200.  Meshtastic default stays 200
(byte-identical); MeshCore uses 140.
"""

import sys
import types
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Fake meshcore module (must precede production imports that lazy-import it)
# ---------------------------------------------------------------------------

def _build_fake_meshcore():
    mod = types.ModuleType("meshcore")

    class EventType:
        CONTACT_MSG_RECV = "CONTACT_MSG_RECV"
        CHANNEL_MSG_RECV = "CHANNEL_MSG_RECV"
        DISCONNECTED = "DISCONNECTED"
        CONNECTED = "CONNECTED"

    mod.EventType = EventType

    class _FakeMeshCore:
        self_info = {"public_key": "aabbccdd1122", "name": "FakeNode"}
        contacts = {}

        async def start_auto_message_fetching(self):
            pass

        async def stop_auto_message_fetching(self):
            pass

        async def disconnect(self):
            pass

        def subscribe(self, event_type, callback):
            pass

        def get_contact_by_key_prefix(self, prefix):
            return None

        @classmethod
        async def create_tcp(cls, host, port,
                             auto_reconnect=True, max_reconnect_attempts=5):
            return cls()

        class commands:
            @staticmethod
            async def send_chan_msg(chan_idx, text):
                result = MagicMock()
                result.is_error.return_value = False
                return result

            @staticmethod
            async def send_msg(dst, text):
                result = MagicMock()
                result.is_error.return_value = False
                return result

    mod.MeshCore = _FakeMeshCore
    return mod


sys.modules.setdefault("meshcore", _build_fake_meshcore())

# ---------------------------------------------------------------------------
# Production imports
# ---------------------------------------------------------------------------

from meshai.config import ConnectionConfig                        # noqa: E402
from meshai.connector import MeshtasticTransport                  # noqa: E402
from meshai.transport.meshcore_transport import MeshCoreTransport  # noqa: E402
from meshai.transport.factory import build_transport               # noqa: E402
from meshai.notifications.channels import (                        # noqa: E402
    MeshBroadcastChannel,
    MeshDMChannel,
)
from meshai.notifications.pipeline import build_pipeline           # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mt_config(**overrides):
    """Return a ConnectionConfig wired for meshtastic (default)."""
    cfg = ConnectionConfig(transport="meshtastic")
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _mc_config(**overrides):
    """Return a ConnectionConfig wired for meshcore."""
    cfg = ConnectionConfig(
        transport="meshcore",
        meshcore_host="127.0.0.1",
        meshcore_port=5050,
        meshcore_channel_index=0,
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _fake_connector(max_chars_val: int):
    """Minimal fake connector exposing only max_chars."""
    conn = MagicMock()
    conn.max_chars = max_chars_val
    return conn


def _minimal_config():
    """Minimal full Config for build_pipeline calls."""
    from meshai.config import Config
    return Config()


# ---------------------------------------------------------------------------
# 1. Transport max_chars property
# ---------------------------------------------------------------------------

class TestTransportMaxChars:
    def test_meshtastic_default_is_200(self):
        cfg = _mt_config()
        t = MeshtasticTransport(cfg)
        assert t.max_chars == 200

    def test_meshtastic_respects_config_field(self):
        cfg = _mt_config(meshtastic_max_chars=160)
        t = MeshtasticTransport(cfg)
        assert t.max_chars == 160

    def test_meshcore_default_is_140(self):
        cfg = _mc_config()
        t = MeshCoreTransport(cfg)
        assert t.max_chars == 140

    def test_meshcore_respects_config_field(self):
        cfg = _mc_config(meshcore_max_chars=120)
        t = MeshCoreTransport(cfg)
        assert t.max_chars == 120

    def test_build_transport_meshtastic_max_chars(self):
        t = build_transport(_mt_config())
        assert t.max_chars == 200

    def test_build_transport_meshcore_max_chars(self):
        t = build_transport(_mc_config())
        assert t.max_chars == 140


# ---------------------------------------------------------------------------
# 2. MeshRenderer char_limit propagation via channels
# ---------------------------------------------------------------------------

class TestChannelRendererBudget:
    def test_broadcast_channel_with_meshcore_connector_uses_140(self):
        conn = _fake_connector(140)
        ch = MeshBroadcastChannel(connector=conn, channel_index=0)
        assert ch._renderer._limit == 140

    def test_broadcast_channel_with_meshtastic_connector_uses_200(self):
        conn = _fake_connector(200)
        ch = MeshBroadcastChannel(connector=conn, channel_index=0)
        assert ch._renderer._limit == 200

    def test_dm_channel_with_meshcore_connector_uses_140(self):
        conn = _fake_connector(140)
        ch = MeshDMChannel(connector=conn, node_ids=["!aabbccdd"])
        assert ch._renderer._limit == 140

    def test_dm_channel_with_meshtastic_connector_uses_200(self):
        conn = _fake_connector(200)
        ch = MeshDMChannel(connector=conn, node_ids=["!aabbccdd"])
        assert ch._renderer._limit == 200


# ---------------------------------------------------------------------------
# 3. DigestAccumulator mesh_char_limit propagation via build_pipeline
# ---------------------------------------------------------------------------

class TestBuildPipelineMeshCharLimit:
    def test_no_connector_uses_200(self):
        cfg = _minimal_config()
        bus = build_pipeline(cfg, llm_backend=None, connector=None)
        acc = bus._pipeline_components["accumulator"]
        assert acc._mesh_char_limit == 200

    def test_meshcore_connector_uses_140(self):
        cfg = _minimal_config()
        conn = _fake_connector(140)
        bus = build_pipeline(cfg, llm_backend=None, connector=conn)
        acc = bus._pipeline_components["accumulator"]
        assert acc._mesh_char_limit == 140

    def test_meshtastic_connector_uses_200(self):
        cfg = _minimal_config()
        conn = _fake_connector(200)
        bus = build_pipeline(cfg, llm_backend=None, connector=conn)
        acc = bus._pipeline_components["accumulator"]
        assert acc._mesh_char_limit == 200


# ---------------------------------------------------------------------------
# 4. Runtime-override durability: survives adapter_config cache invalidation
# ---------------------------------------------------------------------------

class TestRuntimeOverrideDurability:
    """Verify set_runtime_override writes are not cleared by invalidate_cache().

    We test at the _resolve/_overrides seam directly because the full
    adapter_config.nws.single_packet_max_chars read path hits the DB, which
    is not available in this hermetic test environment.  Testing via _resolve
    is equivalent: it is the exact call site used by _AdapterSection.__getattr__.
    """

    def test_override_survives_cache_invalidation(self):
        from meshai.adapter_config._accessor import (
            _overrides,
            _resolve,
            set_runtime_override,
            invalidate_cache,
            _SENTINEL,
        )
        key = ("nws", "single_packet_max_chars")
        try:
            set_runtime_override("nws", "single_packet_max_chars", 140)

            # Override is visible through the resolution path.
            assert _resolve("nws", "single_packet_max_chars") == 140

            # Cache invalidation must NOT wipe the override.
            invalidate_cache()
            assert _resolve("nws", "single_packet_max_chars") == 140
        finally:
            # Clean up so this test cannot pollute subsequent tests.
            _overrides.pop(key, None)
