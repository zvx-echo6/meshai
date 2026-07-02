"""Per-family MeshCore channel routing tests.

Tests the independent meshcore_channel field on NotificationToggle /
NotificationRuleConfig and how it threads through the notification pipeline.

Covers:
  1. Config round-trip: meshcore_channel serialises/deserialises correctly.
  2. _toggle_to_rule: carries meshcore_channel from toggle into rule.
  3. MeshBroadcastChannel: both channel and meshcore_channel passed to connector.
  4. meshcore_channel=None: connector receives meshcore_channel=None (transport
     layer is responsible for the skip; channel-level just passes it through).
  5. transport=meshtastic (default): connector called with just channel; no
     breakage from the new optional meshcore_channel kwarg.
  6. End-to-end via Dispatcher with mock children that track per-transport calls.
"""

import asyncio
import time
from unittest.mock import MagicMock, call

import pytest

from meshai.config import (
    Config,
    NotificationRuleConfig,
    NotificationToggle,
    _dataclass_to_dict,
    _dict_to_dataclass,
)
from meshai.notifications.channels import MeshBroadcastChannel, create_channel
from meshai.notifications.events import NotificationPayload, make_event
from meshai.notifications.pipeline.dispatcher import Dispatcher


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_payload(text="test alert") -> NotificationPayload:
    return NotificationPayload(
        message=text,
        category="weather_warning",
        severity="priority",
        timestamp=time.time(),
        event_type="weather_warning",
    )


def _make_rule(broadcast_channel=1, meshcore_channel=None) -> NotificationRuleConfig:
    return NotificationRuleConfig(
        name="test-rule",
        enabled=True,
        trigger_type="condition",
        delivery_type="mesh_broadcast",
        broadcast_channel=broadcast_channel,
        meshcore_channel=meshcore_channel,
    )


# ---------------------------------------------------------------------------
# 1. Config round-trip
# ---------------------------------------------------------------------------

class TestConfigRoundTrip:
    def test_toggle_meshcore_channel_serialises(self):
        tog = NotificationToggle(
            name="weather",
            enabled=True,
            broadcast_channel=1,
            meshcore_channel=3,
        )
        d = _dataclass_to_dict(tog)
        assert d["meshcore_channel"] == 3

    def test_toggle_meshcore_channel_deserialises(self):
        data = {
            "name": "weather",
            "enabled": True,
            "broadcast_channel": 1,
            "meshcore_channel": 3,
        }
        tog = _dict_to_dataclass(NotificationToggle, data)
        assert tog.meshcore_channel == 3

    def test_toggle_meshcore_channel_none_round_trips(self):
        tog = NotificationToggle(name="fire", enabled=False, meshcore_channel=None)
        d = _dataclass_to_dict(tog)
        rt = _dict_to_dataclass(NotificationToggle, d)
        assert rt.meshcore_channel is None

    def test_toggle_meshcore_channel_zero_is_valid(self):
        """Channel index 0 is a valid MeshCore channel (not falsy-ignored)."""
        data = {"name": "weather", "enabled": True, "meshcore_channel": 0}
        tog = _dict_to_dataclass(NotificationToggle, data)
        assert tog.meshcore_channel == 0

    def test_full_notifications_config_round_trips(self):
        """NotificationToggle inside a full NotificationsConfig round-trips meshcore_channel."""
        cfg = Config()
        cfg.notifications.cold_start_grace_seconds = 0
        cfg.notifications.toggles["weather"].meshcore_channel = 5

        d = _dataclass_to_dict(cfg)
        rt_cfg = _dict_to_dataclass(Config, d)
        assert rt_cfg.notifications.toggles["weather"].meshcore_channel == 5

    def test_rule_config_meshcore_channel_round_trips(self):
        rule = NotificationRuleConfig(
            name="r",
            delivery_type="mesh_broadcast",
            broadcast_channel=2,
            meshcore_channel=7,
        )
        d = _dataclass_to_dict(rule)
        assert d["meshcore_channel"] == 7
        rt = _dict_to_dataclass(NotificationRuleConfig, d)
        assert rt.meshcore_channel == 7


# ---------------------------------------------------------------------------
# 2. _toggle_to_rule carries meshcore_channel
# ---------------------------------------------------------------------------

class TestToggleToRule:
    def _make_dispatcher(self):
        """Dispatcher with no-op channel factory; returns (dispatcher, rec)."""
        rec = []
        cfg = Config()
        cfg.notifications.cold_start_grace_seconds = 0
        cfg.notifications.rules = []

        class RecChannel:
            def __init__(self, rec_):
                self._rec = rec_

            async def deliver(self, payload, rule):
                self._rec.append({
                    "broadcast_channel": rule.broadcast_channel,
                    "meshcore_channel": rule.meshcore_channel,
                })
                return True

        d = Dispatcher(
            cfg,
            lambda rule, conn: RecChannel(rec),
            connector=None,
        )
        return d, rec, cfg

    def test_toggle_to_rule_carries_meshcore_channel_set(self):
        d, rec, cfg = self._make_dispatcher()
        tog = cfg.notifications.toggles["weather"]
        tog.enabled = True
        tog.min_severity = "routine"
        tog.severity_channels = {"priority": ["mesh_broadcast"]}
        tog.broadcast_channel = 1
        tog.meshcore_channel = 3

        ev = make_event(source="nws", category="weather_warning",
                        severity="priority", title="t")
        asyncio.run(d.dispatch(ev))

        assert len(rec) == 1
        assert rec[0]["broadcast_channel"] == 1
        assert rec[0]["meshcore_channel"] == 3

    def test_toggle_to_rule_carries_meshcore_channel_none(self):
        d, rec, cfg = self._make_dispatcher()
        tog = cfg.notifications.toggles["weather"]
        tog.enabled = True
        tog.min_severity = "routine"
        tog.severity_channels = {"priority": ["mesh_broadcast"]}
        tog.broadcast_channel = 1
        tog.meshcore_channel = None  # explicit None

        ev = make_event(source="nws", category="weather_warning",
                        severity="priority", title="t")
        asyncio.run(d.dispatch(ev))

        assert len(rec) == 1
        assert rec[0]["broadcast_channel"] == 1
        assert rec[0]["meshcore_channel"] is None


# ---------------------------------------------------------------------------
# 3 & 4. MeshBroadcastChannel.deliver passes both channels to connector
# ---------------------------------------------------------------------------

class TestMeshBroadcastChannelRouting:
    def test_both_channels_passed_to_connector(self):
        """connector.send_message receives channel=1 and meshcore_channel=3."""
        mock_connector = MagicMock()
        channel = MeshBroadcastChannel(
            connector=mock_connector,
            channel_index=1,
            meshcore_channel=3,
        )
        asyncio.run(channel.deliver(_make_payload(), rule=None))

        assert mock_connector.send_message.called
        for call_ in mock_connector.send_message.call_args_list:
            kw = call_.kwargs
            assert kw.get("channel") == 1
            assert kw.get("meshcore_channel") == 3

    def test_meshcore_channel_none_passed_to_connector(self):
        """When meshcore_channel=None, connector receives meshcore_channel=None."""
        mock_connector = MagicMock()
        channel = MeshBroadcastChannel(
            connector=mock_connector,
            channel_index=1,
            meshcore_channel=None,
        )
        asyncio.run(channel.deliver(_make_payload(), rule=None))

        assert mock_connector.send_message.called
        for call_ in mock_connector.send_message.call_args_list:
            kw = call_.kwargs
            assert kw.get("meshcore_channel") is None

    def test_pre_chunked_payload_also_passes_both_channels(self):
        """Pre-chunked digest payloads also carry meshcore_channel."""
        mock_connector = MagicMock()
        channel = MeshBroadcastChannel(
            connector=mock_connector,
            channel_index=2,
            meshcore_channel=5,
        )
        payload = NotificationPayload(
            message="pre-chunked",
            category="weather_warning",
            severity="priority",
            timestamp=time.time(),
            event_type="weather_warning",
            chunk_index=0,
        )
        asyncio.run(channel.deliver(payload, rule=None))

        mock_connector.send_message.assert_called_once_with(
            text="pre-chunked",
            destination=None,
            channel=2,
            meshcore_channel=5,
        )


# ---------------------------------------------------------------------------
# 5. create_channel propagates meshcore_channel from rule
# ---------------------------------------------------------------------------

class TestCreateChannelPropagation:
    def test_create_channel_propagates_meshcore_channel(self):
        """create_channel passes rule.meshcore_channel to MeshBroadcastChannel."""
        mock_connector = MagicMock()
        rule = _make_rule(broadcast_channel=1, meshcore_channel=3)
        ch = create_channel(rule, connector=mock_connector)

        assert isinstance(ch, MeshBroadcastChannel)
        assert ch._meshcore_channel == 3
        assert ch._channel == 1

    def test_create_channel_meshcore_channel_none(self):
        mock_connector = MagicMock()
        rule = _make_rule(broadcast_channel=1, meshcore_channel=None)
        ch = create_channel(rule, connector=mock_connector)
        assert ch._meshcore_channel is None

    def test_create_channel_meshcore_channel_zero(self):
        """meshcore_channel=0 is a valid channel index (not falsy-defaulted)."""
        mock_connector = MagicMock()
        rule = _make_rule(broadcast_channel=1, meshcore_channel=0)
        ch = create_channel(rule, connector=mock_connector)
        assert ch._meshcore_channel == 0


# ---------------------------------------------------------------------------
# 6. transport=meshtastic: old connector API unchanged (meshcore_channel accepted)
# ---------------------------------------------------------------------------

class TestMeshtasticOnlyUnchanged:
    def test_connector_called_with_correct_channel(self):
        """Meshtastic-only connector receives its channel; meshcore_channel=None is a no-op."""
        mock_connector = MagicMock()
        channel = MeshBroadcastChannel(
            connector=mock_connector,
            channel_index=5,
            # No meshcore_channel set → defaults to None
        )
        asyncio.run(channel.deliver(_make_payload(), rule=None))

        assert mock_connector.send_message.called
        for call_ in mock_connector.send_message.call_args_list:
            kw = call_.kwargs
            assert kw.get("channel") == 5
            # meshcore_channel=None passed through; old MeshConnector accepts and ignores it.
            assert kw.get("meshcore_channel") is None

    def test_dispatcher_meshtastic_family_no_meshcore_channel(self):
        """End-to-end: toggle without meshcore_channel routes to channel=1, meshcore_channel=None."""
        rec = []

        class RecChannel:
            async def deliver(self, payload, rule):
                rec.append({
                    "broadcast_channel": rule.broadcast_channel,
                    "meshcore_channel": rule.meshcore_channel,
                })
                return True

        cfg = Config()
        cfg.notifications.cold_start_grace_seconds = 0
        cfg.notifications.rules = []
        tog = cfg.notifications.toggles["weather"]
        tog.enabled = True
        tog.min_severity = "routine"
        tog.severity_channels = {"priority": ["mesh_broadcast"]}
        tog.broadcast_channel = 1
        # meshcore_channel NOT set → remains None

        d = Dispatcher(cfg, lambda rule, conn: RecChannel(), connector=None)
        ev = make_event(source="nws", category="weather_warning",
                        severity="priority", title="t")
        asyncio.run(d.dispatch(ev))

        assert len(rec) == 1
        assert rec[0]["broadcast_channel"] == 1
        assert rec[0]["meshcore_channel"] is None


# ---------------------------------------------------------------------------
# 7. Mock-composite end-to-end: both + None child routing
# ---------------------------------------------------------------------------

class FakeMeshChild:
    """Minimal connector mock that tracks send_message calls."""

    def __init__(self, name: str):
        self.name = name
        self.calls: list[dict] = []

    def send_message(self, text, destination=None, channel=0, meshcore_channel=None):
        self.calls.append({
            "text": text,
            "destination": destination,
            "channel": channel,
            "meshcore_channel": meshcore_channel,
        })
        return True


class FakeCompositeConnector:
    """Simulates CompositeTransport's per-family routing at the connector level.

    Replicates the exact logic in CompositeTransport._broadcast:
      - Meshtastic child: receives ``channel``
      - MeshCore child: receives ``meshcore_channel``; skipped if None.
    """

    def __init__(self, mt_child: FakeMeshChild, mc_child: FakeMeshChild):
        self.mt = mt_child
        self.mc = mc_child

    def send_message(self, text, destination=None, channel=0, meshcore_channel=None):
        # Meshtastic always gets the Meshtastic channel.
        self.mt.send_message(text, destination=destination, channel=channel)
        # MeshCore only gets a call when meshcore_channel is set.
        if meshcore_channel is not None:
            self.mc.send_message(text, destination=destination, channel=meshcore_channel)
        return True


class TestMockCompositeBothTransport:
    def _make_connector(self):
        mt = FakeMeshChild("meshtastic")
        mc = FakeMeshChild("meshcore")
        return FakeCompositeConnector(mt, mc), mt, mc

    def test_broadcast_channel_1_meshcore_channel_3(self):
        """meshcore_channel=3, broadcast_channel=1: MT on ch 1, MC on ch 3."""
        composite, mt, mc = self._make_connector()
        channel = MeshBroadcastChannel(
            connector=composite,
            channel_index=1,
            meshcore_channel=3,
        )
        asyncio.run(channel.deliver(_make_payload("alert text"), rule=None))

        assert len(mt.calls) >= 1
        assert all(c["channel"] == 1 for c in mt.calls)

        assert len(mc.calls) >= 1
        assert all(c["channel"] == 3 for c in mc.calls)

    def test_meshcore_channel_none_skips_meshcore_child(self):
        """meshcore_channel=None: MT receives broadcast, MC child NOT called."""
        composite, mt, mc = self._make_connector()
        channel = MeshBroadcastChannel(
            connector=composite,
            channel_index=1,
            meshcore_channel=None,
        )
        asyncio.run(channel.deliver(_make_payload("alert"), rule=None))

        assert len(mt.calls) >= 1     # Meshtastic got it
        assert len(mc.calls) == 0     # MeshCore was skipped

    def test_meshtastic_only_unchanged(self):
        """transport=meshtastic (single connector): channel routing unchanged."""
        mt = FakeMeshChild("meshtastic")
        channel = MeshBroadcastChannel(
            connector=mt,  # single connector, no composite
            channel_index=5,
            # No meshcore_channel → defaults to None
        )
        asyncio.run(channel.deliver(_make_payload("single"), rule=None))

        assert len(mt.calls) >= 1
        assert all(c["channel"] == 5 for c in mt.calls)
