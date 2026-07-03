"""Tests for the MeshTransport abstraction and derive-from-config factory.

These tests exercise:
  - MeshtasticTransport is a concrete subclass of MeshTransport
  - build_transport derives active transports from meshcore_host, not a flag
  - MeshMessage has the expected transport-routing fields with correct defaults

No real radio, socket, or asyncio loop is required.
"""

import pytest

from meshai.transport.base import MeshTransport
from meshai.transport.factory import build_transport
from meshai.connector import MeshtasticTransport, MeshConnector, MeshMessage
from meshai.config import ConnectionConfig


# ---------------------------------------------------------------------------
# MeshtasticTransport hierarchy
# ---------------------------------------------------------------------------


class TestMeshtasticTransportABC:
    def test_is_subclass_of_mesh_transport(self):
        assert issubclass(MeshtasticTransport, MeshTransport)

    def test_backward_compat_alias_is_same_class(self):
        """MeshConnector must still be MeshtasticTransport (alias, not a copy)."""
        assert MeshConnector is MeshtasticTransport

    def test_implements_abstract_surface(self):
        """MeshtasticTransport must not leave any abstract methods unimplemented."""
        abstract_methods = getattr(MeshTransport, "__abstractmethods__", set())
        # Build the set of methods MeshtasticTransport provides
        provided = set(vars(MeshtasticTransport))
        # Every abstract method must be overridden (present in the class dict
        # or resolvable via MRO without being abstract itself)
        for method_name in abstract_methods:
            attr = getattr(MeshtasticTransport, method_name, None)
            assert attr is not None, f"abstract method {method_name!r} not implemented"
            # The attribute must NOT still be abstract on the concrete class
            assert not getattr(attr, "__isabstractmethod__", False), (
                f"{method_name!r} is still abstract on MeshtasticTransport"
            )


# ---------------------------------------------------------------------------
# Factory — transports derived from meshcore_host, not a transport flag
# ---------------------------------------------------------------------------


class TestBuildTransport:
    def _config_no_meshcore(self):
        """Default config: meshcore_host is blank → Meshtastic only."""
        return ConnectionConfig()

    def _config_with_meshcore(self, host="1.2.3.4"):
        """Config with a non-empty meshcore_host → composite."""
        cfg = ConnectionConfig()
        cfg.meshcore_host = host
        return cfg

    def test_blank_host_returns_meshtastic_only(self):
        """Empty meshcore_host → MeshtasticTransport, no composite."""
        cfg = self._config_no_meshcore()
        assert cfg.meshcore_host == ""
        transport = build_transport(cfg)
        assert isinstance(transport, MeshtasticTransport)
        # Must NOT be a composite when MeshCore is not configured.
        from meshai.transport.composite_transport import CompositeTransport
        assert not isinstance(transport, CompositeTransport)

    def test_meshcore_host_set_returns_composite(self):
        """Non-empty meshcore_host → CompositeTransport with both children."""
        from meshai.transport.composite_transport import CompositeTransport
        from meshai.transport.meshcore_transport import MeshCoreTransport
        cfg = self._config_with_meshcore("1.2.3.4")
        t = build_transport(cfg)
        assert isinstance(t, CompositeTransport)
        assert len(t.children) == 2
        assert isinstance(t.children[0], MeshtasticTransport)
        assert isinstance(t.children[1], MeshCoreTransport)

    def test_whitespace_only_host_is_treated_as_blank(self):
        """Whitespace-only meshcore_host is treated as blank → Meshtastic only."""
        cfg = ConnectionConfig()
        cfg.meshcore_host = "   "
        transport = build_transport(cfg)
        assert isinstance(transport, MeshtasticTransport)
        from meshai.transport.composite_transport import CompositeTransport
        assert not isinstance(transport, CompositeTransport)

    def test_config_has_no_transport_field(self):
        """ConnectionConfig must not have a transport attribute after the refactor."""
        cfg = ConnectionConfig()
        assert not hasattr(cfg, "transport"), (
            "ConnectionConfig.transport was not removed; the refactor is incomplete."
        )

    def test_stray_transport_key_in_yaml_loads_without_error(self):
        """A config dict with a stale 'transport' key must load cleanly."""
        from meshai.config import _dict_to_dataclass, ConnectionConfig as CC
        raw = {
            "type": "tcp",
            "tcp_host": "10.0.0.1",
            "transport": "both",  # stale key — must be ignored
            "meshcore_host": "192.168.1.253",
        }
        cfg = _dict_to_dataclass(CC, raw)
        # The stale key must be silently dropped; no AttributeError.
        assert not hasattr(cfg, "transport")
        assert cfg.meshcore_host == "192.168.1.253"

    def test_serialized_config_has_no_transport_field(self):
        """_dataclass_to_dict must not emit a 'transport' key."""
        from meshai.config import _dataclass_to_dict
        cfg = ConnectionConfig()
        d = _dataclass_to_dict(cfg)
        assert "transport" not in d


# ---------------------------------------------------------------------------
# MeshMessage additive fields
# ---------------------------------------------------------------------------


class TestMeshMessageAdditiveFields:
    def _make_message(self, **overrides):
        defaults = dict(
            sender_id="!aabbccdd",
            sender_name="TestNode",
            text="hello",
            channel=0,
            is_dm=False,
        )
        defaults.update(overrides)
        return MeshMessage(**defaults)

    def test_transport_defaults_to_meshtastic(self):
        msg = self._make_message()
        assert msg.transport == "meshtastic"

    def test_transport_can_be_overridden(self):
        msg = self._make_message(transport="meshcore")
        assert msg.transport == "meshcore"

    def test_packet_defaults_to_none(self):
        msg = self._make_message()
        assert msg.packet is None

    def test_packet_can_be_set(self):
        pkt = {"from": 12345, "decoded": {"text": "hi"}}
        msg = self._make_message(packet=pkt)
        assert msg.packet is pkt

    def test_existing_fields_unchanged(self):
        """All pre-existing fields must still be present and functional."""
        msg = self._make_message(
            sender_id="!11223344",
            sender_name="Alpha",
            text="test",
            channel=3,
            is_dm=True,
            packet={"raw": True},
        )
        assert msg.sender_id == "!11223344"
        assert msg.sender_name == "Alpha"
        assert msg.text == "test"
        assert msg.channel == 3
        assert msg.is_dm is True
        assert msg.packet == {"raw": True}

    def test_sender_position_property_still_works(self):
        msg = self._make_message()
        assert msg.sender_position is None
        msg._position = (43.5, -114.2)
        assert msg.sender_position == (43.5, -114.2)
