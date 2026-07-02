"""Lightweight tests for the Phase-1 MeshTransport abstraction.

These tests exercise the structural contracts introduced by the refactor:
  - MeshtasticTransport is a concrete subclass of MeshTransport
  - build_transport returns the right type for the default config
  - build_transport raises appropriately for unimplemented / unknown transports
  - MeshMessage has the new additive fields with the expected defaults

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
# Factory
# ---------------------------------------------------------------------------


class TestBuildTransport:
    def _default_config(self):
        return ConnectionConfig()  # transport defaults to "meshtastic"

    def _config_with(self, transport_name):
        cfg = ConnectionConfig()
        cfg.transport = transport_name
        return cfg

    def test_default_config_returns_meshtastic_transport(self):
        cfg = self._default_config()
        transport = build_transport(cfg)
        assert isinstance(transport, MeshtasticTransport)

    def test_explicit_meshtastic_returns_meshtastic_transport(self):
        cfg = self._config_with("meshtastic")
        transport = build_transport(cfg)
        assert isinstance(transport, MeshtasticTransport)

    def test_meshcore_returns_meshcore_transport(self):
        # Phase 2: meshcore is now implemented; build_transport returns a
        # MeshCoreTransport instance (MeshTransport subclass).
        from meshai.transport.meshcore_transport import MeshCoreTransport
        cfg = self._config_with("meshcore")
        transport = build_transport(cfg)
        assert isinstance(transport, MeshCoreTransport)
        assert isinstance(transport, MeshTransport)

    def test_both_raises_not_implemented(self):
        cfg = self._config_with("both")
        with pytest.raises(NotImplementedError):
            build_transport(cfg)

    def test_unknown_transport_raises_value_error(self):
        cfg = self._config_with("unknown_transport_xyz")
        with pytest.raises(ValueError):
            build_transport(cfg)


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
