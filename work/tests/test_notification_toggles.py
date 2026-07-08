"""v0.5 Section 1: NotificationToggle dispatch routing tests.

Also covers the per-mesh delivery type routing introduced in
feat/meshcore-first-class-delivery (meshcore_broadcast, meshcore_dm).
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from meshai.config import Config, NotificationToggle
from meshai.notifications.pipeline.dispatcher import Dispatcher
from meshai.notifications.events import make_event
from meshai.notifications.channels import create_channel


class RecChannel:
    def __init__(self, rec):
        self.rec = rec

    async def deliver(self, payload, rule):
        self.rec.append({
            "delivery_type": rule.delivery_type,
            "name": rule.name,
            "broadcast_channel": rule.broadcast_channel,
            "node_ids": list(rule.node_ids),
        })
        return True


def _dispatch(cfg, event):
    rec = []
    # v0.6-2: wipe dedup state between calls so each _dispatch is an
    # independent "what happens if this event arrives now?" check.
    # The pre-v0.6-2 in-memory dedup naturally reset per Dispatcher
    # instance; the new persisted dedup carries across instances unless
    # we clear it here.
    try:
        from meshai.persistence import get_db
        conn = get_db()
        conn.execute("DELETE FROM dispatcher_dedup")
        conn.execute("DELETE FROM dispatcher_cooldowns")
        conn.execute(
            "UPDATE dispatcher_state SET cold_start_anchor=NULL, "
            "stale_dropped=0, cooldown_dropped=0, dedup_dropped=0, "
            "cold_start_dropped=0 WHERE id=1"
        )
    except Exception:
        pass
    d = Dispatcher(cfg, lambda rule, conn: RecChannel(rec), connector=None)
    asyncio.run(d.dispatch(event))
    return rec


def _cfg(enable="weather", **kw):
    cfg = Config()
    cfg.notifications.rules = []
    cfg.notifications.cold_start_grace_seconds = 0  # v0.5.8b: legacy tests
    t = cfg.notifications.toggles[enable]
    t.enabled = True
    t.min_severity = kw.get("min_severity", "priority")
    t.regions = kw.get("regions", [])
    t.severity_channels = kw.get("severity_channels", {"priority": ["mesh_broadcast"]})
    return cfg


def _ev(severity="priority", category="weather_warning", region=None, regions=None):
    return make_event(source="nws", category=category, severity=severity,
                      region=region, regions=regions or [], title="t")


def test_disabled_toggle_no_dispatch():
    cfg = Config(); cfg.notifications.rules = []  # weather disabled by default
    cfg.notifications.cold_start_grace_seconds = 0
    assert _dispatch(cfg, _ev()) == []


def test_enabled_toggle_dispatches():
    rec = _dispatch(_cfg(), _ev(severity="priority"))
    assert len(rec) == 1 and rec[0]["delivery_type"] == "mesh_broadcast"
    assert rec[0]["name"] == "toggle:weather"


def test_region_empty_allows_all():
    rec = _dispatch(_cfg(regions=[]), _ev(region="Boise"))
    assert len(rec) == 1


def test_region_populated_blocks_mismatch():
    cfg = _cfg(regions=["Magic Valley"])
    assert _dispatch(cfg, _ev(region="Boise")) == []
    assert len(_dispatch(cfg, _ev(region="Magic Valley"))) == 1


def test_region_matches_via_regions_list():
    cfg = _cfg(regions=["Magic Valley"])
    assert len(_dispatch(cfg, _ev(region=None, regions=["Magic Valley", "X"]))) == 1


def test_severity_threshold():
    cfg = _cfg(min_severity="priority",
               severity_channels={"routine": ["mesh_broadcast"], "priority": ["mesh_broadcast"],
                                  "immediate": ["mesh_broadcast"]})
    assert _dispatch(cfg, _ev(severity="routine")) == []     # below threshold
    assert len(_dispatch(cfg, _ev(severity="priority"))) == 1
    assert len(_dispatch(cfg, _ev(severity="immediate"))) == 1


def test_per_severity_channel_routing():
    cfg = _cfg(min_severity="routine",
               severity_channels={"priority": ["mesh_broadcast"],
                                  "immediate": ["mesh_broadcast", "mesh_dm"]})
    assert len(_dispatch(cfg, _ev(severity="priority"))) == 1
    imm = _dispatch(cfg, _ev(severity="immediate"))
    assert {r["delivery_type"] for r in imm} == {"mesh_broadcast", "mesh_dm"}


def test_digest_channel_skipped_in_live_dispatch():
    cfg = _cfg(severity_channels={"priority": ["digest", "mesh_broadcast"]})
    rec = _dispatch(cfg, _ev(severity="priority"))
    assert [r["delivery_type"] for r in rec] == ["mesh_broadcast"]  # digest not live-dispatched


def test_category_maps_to_correct_family():
    # seismic family toggle handles earthquake_event via get_toggle fallback
    cfg = Config(); cfg.notifications.rules = []
    cfg.notifications.cold_start_grace_seconds = 0  # v0.5.8b: legacy test
    cfg.notifications.toggles["seismic"].enabled = True
    cfg.notifications.toggles["seismic"].severity_channels = {"priority": ["mesh_broadcast"]}
    rec = _dispatch(cfg, _ev(severity="priority", category="earthquake_event"))
    assert len(rec) == 1 and rec[0]["name"] == "toggle:seismic"


def test_rules_and_toggles_both_fire():
    from meshai.config import NotificationRuleConfig
    cfg = _cfg()
    cfg.notifications.rules = [NotificationRuleConfig(
        name="legacy", enabled=True, trigger_type="condition",
        categories=["weather_warning"], min_severity="routine",
        delivery_type="mesh_broadcast")]
    rec = _dispatch(cfg, _ev(severity="priority"))
    names = {r["name"] for r in rec}
    assert "legacy" in names and "toggle:weather" in names  # parallel paths both fire


# ============================================================
# Per-mesh delivery type routing tests (feat/meshcore-first-class-delivery)
# ============================================================

def _wipe_db():
    """Wipe dispatcher persistence so each test is independent."""
    try:
        from meshai.persistence import get_db
        conn = get_db()
        conn.execute("DELETE FROM dispatcher_dedup")
        conn.execute("DELETE FROM dispatcher_cooldowns")
        conn.execute(
            "UPDATE dispatcher_state SET cold_start_anchor=NULL, "
            "stale_dropped=0, cooldown_dropped=0, dedup_dropped=0, "
            "cold_start_dropped=0 WHERE id=1"
        )
    except Exception:
        pass


def _dispatch_with_connector(cfg, event, connector):
    """Dispatch event, using a real connector so send_message calls are captured."""
    _wipe_db()
    delivered_rules = []

    def _factory(rule, conn):
        ch = create_channel(rule, connector)
        # Wrap to record rule metadata too.
        original_deliver = ch.deliver

        async def _record_deliver(payload, r):
            result = await original_deliver(payload, r)
            delivered_rules.append({
                "delivery_type": r.delivery_type,
                "meshcore_channel": getattr(r, "meshcore_channel", None),
                "meshcore_dm_contacts": list(getattr(r, "meshcore_dm_contacts", []) or []),
                "node_ids": list(getattr(r, "node_ids", []) or []),
            })
            return result

        ch.deliver = _record_deliver
        return ch

    d = Dispatcher(cfg, _factory, connector=connector)
    asyncio.run(d.dispatch(event))
    return delivered_rules


def test_meshcore_broadcast_routes_to_meshcore_child_only():
    """meshcore_broadcast in severity_channels → send_message called with
    transport='meshcore' and the family's meshcore_channel name.
    The Meshtastic child must NOT be called for this type."""
    meshtastic_child = MagicMock()
    meshtastic_child.connected = True
    meshtastic_child.transport_name = "meshtastic"
    meshtastic_child.send_message.return_value = True
    meshtastic_child.send_message_async = AsyncMock(
        side_effect=lambda *a, **kw: meshtastic_child.send_message(*a, **kw)
    )

    meshcore_child = MagicMock()
    meshcore_child.connected = True
    meshcore_child.transport_name = "meshcore"
    meshcore_child.send_message.return_value = True
    meshcore_child.send_message_async = AsyncMock(
        side_effect=lambda *a, **kw: meshcore_child.send_message(*a, **kw)
    )

    from meshai.transport.composite_transport import CompositeTransport
    connector = CompositeTransport([meshtastic_child, meshcore_child])
    # Simulate that the connector has a meshcore child (for capability check in channel).
    connector._by_name = {"meshtastic": meshtastic_child, "meshcore": meshcore_child}

    cfg = Config()
    cfg.notifications.rules = []
    cfg.notifications.cold_start_grace_seconds = 0
    t = cfg.notifications.toggles["fire"]
    t.enabled = True
    t.min_severity = "immediate"
    t.severity_channels = {"immediate": ["meshcore_broadcast"]}
    t.broadcast_channel = 0
    t.meshcore_channel = "AIDA"

    event = make_event(
        source="wfigs", category="fire_perimeter",
        severity="immediate", title="fire alert",
    )

    rules = _dispatch_with_connector(cfg, event, connector)
    assert len(rules) == 1
    assert rules[0]["delivery_type"] == "meshcore_broadcast"
    assert rules[0]["meshcore_channel"] == "AIDA"

    # MeshCore child received the call with the channel NAME on the correct kwarg.
    assert meshcore_child.send_message.called
    mc_kwargs = meshcore_child.send_message.call_args.kwargs
    assert mc_kwargs.get("destination") is None
    # Regression guard for DEFECT 1: channel NAME must be routed via meshcore_channel=.
    assert mc_kwargs.get("meshcore_channel") == "AIDA"
    # The old broken code passed AIDA via channel=; that must NOT be the routing mechanism.
    assert mc_kwargs.get("channel") != "AIDA"

    # Meshtastic child must NOT have been called.
    meshtastic_child.send_message.assert_not_called()


def test_mesh_broadcast_routes_to_meshtastic_child_only():
    """mesh_broadcast → send_message with transport='meshtastic' and
    the Meshtastic channel index. MeshCore child must NOT be called."""
    meshtastic_child = MagicMock()
    meshtastic_child.connected = True
    meshtastic_child.transport_name = "meshtastic"
    meshtastic_child.send_message.return_value = True
    meshtastic_child.send_message_async = AsyncMock(
        side_effect=lambda *a, **kw: meshtastic_child.send_message(*a, **kw)
    )

    meshcore_child = MagicMock()
    meshcore_child.connected = True
    meshcore_child.transport_name = "meshcore"
    meshcore_child.send_message.return_value = True
    meshcore_child.send_message_async = AsyncMock(
        side_effect=lambda *a, **kw: meshcore_child.send_message(*a, **kw)
    )

    from meshai.transport.composite_transport import CompositeTransport
    connector = CompositeTransport([meshtastic_child, meshcore_child])
    connector._by_name = {"meshtastic": meshtastic_child, "meshcore": meshcore_child}

    cfg = Config()
    cfg.notifications.rules = []
    cfg.notifications.cold_start_grace_seconds = 0
    t = cfg.notifications.toggles["weather"]
    t.enabled = True
    t.min_severity = "priority"
    t.severity_channels = {"priority": ["mesh_broadcast"]}
    t.broadcast_channel = 3

    event = make_event(
        source="nws", category="weather_warning",
        severity="priority", title="weather alert",
    )

    rules = _dispatch_with_connector(cfg, event, connector)
    assert len(rules) == 1
    assert rules[0]["delivery_type"] == "mesh_broadcast"

    # Meshtastic child received the call.
    assert meshtastic_child.send_message.called
    mt_kwargs = meshtastic_child.send_message.call_args.kwargs
    assert mt_kwargs.get("destination") is None
    assert mt_kwargs.get("channel") == 3

    # MeshCore child must NOT have been called.
    meshcore_child.send_message.assert_not_called()


def test_meshcore_dm_routes_to_meshcore_contacts():
    """meshcore_dm → connector.send_message called per meshcore_dm_contacts
    entry with transport='meshcore'."""
    from meshai.notifications.channels import MeshCoreDMChannel

    mock_connector = MagicMock()
    mock_connector._by_name = {"meshcore": MagicMock(), "meshtastic": MagicMock()}
    mock_connector.send_message.return_value = True
    mock_connector.send_message_async = AsyncMock(
        side_effect=lambda *a, **kw: mock_connector.send_message(*a, **kw)
    )

    from meshai.config import NotificationRuleConfig
    import time as _time
    from meshai.notifications.events import NotificationPayload

    rule = NotificationRuleConfig(
        name="toggle:mesh_health",
        delivery_type="meshcore_dm",
        meshcore_dm_contacts=["alice", "bob"],
    )

    channel = create_channel(rule, mock_connector)
    assert isinstance(channel, MeshCoreDMChannel)

    payload = NotificationPayload(
        message="dm alert",
        category="mesh_health",
        severity="immediate",
        timestamp=_time.time(),
        chunk_index=0,
    )

    result = asyncio.run(channel.deliver(payload, rule))
    assert result is True

    # One send_message call per contact.
    assert mock_connector.send_message.call_count == 2
    destinations = [
        call.kwargs.get("destination")
        for call in mock_connector.send_message.call_args_list
    ]
    assert set(destinations) == {"alice", "bob"}
    for call in mock_connector.send_message.call_args_list:
        assert call.kwargs.get("transport") == "meshcore"


def test_meshcore_broadcast_noop_when_no_meshcore_transport():
    """meshcore_broadcast with transport=meshtastic (no MeshCore child) →
    deliver returns False, no exception raised."""
    from meshai.notifications.channels import MeshCoreBroadcastChannel
    from meshai.config import NotificationRuleConfig
    import time as _time
    from meshai.notifications.events import NotificationPayload

    # Connector has NO meshcore child (transport=meshtastic scenario).
    mock_connector = MagicMock()
    # _by_name exists but has only meshtastic.
    mock_connector._by_name = {"meshtastic": MagicMock()}

    rule = NotificationRuleConfig(
        name="toggle:fire",
        delivery_type="meshcore_broadcast",
        meshcore_channel="AIDA",
    )

    channel = create_channel(rule, mock_connector)
    assert isinstance(channel, MeshCoreBroadcastChannel)

    payload = NotificationPayload(
        message="fire alert",
        category="fire",
        severity="immediate",
        timestamp=_time.time(),
        chunk_index=0,
    )

    # Must not raise; returns False (no-op).
    result = asyncio.run(channel.deliver(payload, rule))
    assert result is False
    # send_message must NOT have been called (no accidental Meshtastic send).
    mock_connector.send_message.assert_not_called()


def test_config_round_trip_meshcore_fields():
    """NotificationToggle with meshcore types in severity_channels and
    meshcore_dm_contacts survives _dataclass_to_dict / _dict_to_dataclass."""
    from meshai.config import _dataclass_to_dict, _dict_to_dataclass, NotificationToggle

    tog = NotificationToggle(
        name="fire",
        enabled=True,
        min_severity="immediate",
        severity_channels={
            "priority": ["meshcore_broadcast"],
            "immediate": ["mesh_broadcast", "meshcore_broadcast", "meshcore_dm"],
        },
        broadcast_channel=1,
        meshcore_channel="AIDA",
        meshcore_dm_contacts=["alice", "bob"],
        node_ids=["!deadbeef"],
    )

    d = _dataclass_to_dict(tog)
    assert d["meshcore_dm_contacts"] == ["alice", "bob"]
    assert d["meshcore_channel"] == "AIDA"
    assert "meshcore_broadcast" in d["severity_channels"]["priority"]
    assert "meshcore_dm" in d["severity_channels"]["immediate"]

    restored = _dict_to_dataclass(NotificationToggle, d)
    assert restored.meshcore_dm_contacts == ["alice", "bob"]
    assert restored.meshcore_channel == "AIDA"
    assert "meshcore_broadcast" in restored.severity_channels["priority"]
    assert "meshcore_dm" in restored.severity_channels["immediate"]
    assert restored.node_ids == ["!deadbeef"]


def test_meshtastic_only_config_unchanged():
    """Existing configs with only mesh_broadcast/mesh_dm and
    transport=meshtastic behave identically to pre-MeshCore behavior."""
    mock_connector = MagicMock()
    # Simulate a plain MeshtasticTransport (no _by_name, transport_name=meshtastic).
    mock_connector.transport_name = "meshtastic"
    mock_connector.send_message.return_value = True
    mock_connector.send_message_async = AsyncMock(
        side_effect=lambda *a, **kw: mock_connector.send_message(*a, **kw)
    )
    # No _by_name attribute (not a CompositeTransport).
    del mock_connector._by_name

    cfg = Config()
    cfg.notifications.rules = []
    cfg.notifications.cold_start_grace_seconds = 0
    t = cfg.notifications.toggles["weather"]
    t.enabled = True
    t.min_severity = "priority"
    t.severity_channels = {
        "priority": ["mesh_broadcast"],
        "immediate": ["mesh_broadcast", "mesh_dm"],
    }
    t.broadcast_channel = 0
    t.node_ids = ["!deadbeef"]

    event = make_event(
        source="nws", category="weather_warning",
        severity="priority", title="weather alert",
    )
    rules = _dispatch_with_connector(cfg, event, mock_connector)
    assert len(rules) == 1
    assert rules[0]["delivery_type"] == "mesh_broadcast"

    # send_message called with Meshtastic channel and transport hint.
    assert mock_connector.send_message.called
    kwargs = mock_connector.send_message.call_args.kwargs
    assert kwargs.get("transport") == "meshtastic"
    assert kwargs.get("channel") == 0
