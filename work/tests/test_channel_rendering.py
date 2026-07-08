"""Tests for channel-renderer integration (Phase 2.5b)."""

import asyncio
import time
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from meshai.notifications.events import NotificationPayload
from meshai.notifications.channels import (
    MeshBroadcastChannel,
    MeshDMChannel,
    EmailChannel,
    WebhookChannel,
)


def _mock_conn():
    """Create a MagicMock connector with send_message_async wired to send_message.

    Channels now call send_message_async (async) instead of send_message (sync).
    side_effect delegates to send_message so existing call_count / call_args
    assertions remain valid.
    """
    c = MagicMock()
    c.send_message_async = AsyncMock(side_effect=lambda *a, **kw: c.send_message(*a, **kw))
    return c


# ============================================================
# MESH CHANNEL RENDERING TESTS
# ============================================================

def test_mesh_channel_uses_mesh_renderer():
    """MeshBroadcastChannel renders long messages to multiple chunks."""
    mock_connector = _mock_conn()

    channel = MeshBroadcastChannel(
        connector=mock_connector,
        channel_index=0,
    )

    # Build a long message that will require chunking
    long_message = "This is a very long alert message that exceeds the character limit. " * 5

    payload = NotificationPayload(
        message=long_message,
        category="weather_warning",
        severity="priority",
        timestamp=time.time(),
        event_type="weather_warning",
    )

    asyncio.run(channel.deliver(payload, None))

    # Should have called send_message multiple times (once per chunk)
    assert mock_connector.send_message.call_count >= 2

    # Each call's text should be <= 200 chars
    for call in mock_connector.send_message.call_args_list:
        text = call.kwargs.get("text", call.args[0] if call.args else "")
        assert len(text) <= 200


def test_mesh_channel_uses_payload_message_directly_when_chunk_metadata_set():
    """Pre-chunked payloads (from digest) skip re-rendering."""
    mock_connector = _mock_conn()

    channel = MeshBroadcastChannel(
        connector=mock_connector,
        channel_index=0,
    )

    # Payload with chunk metadata set (from digest scheduler)
    payload = NotificationPayload(
        message="pre-chunked text",
        category="digest",
        severity="routine",
        timestamp=time.time(),
        chunk_index=1,
        chunk_total=3,
    )

    asyncio.run(channel.deliver(payload, None))

    # Should have called send_message exactly once
    assert mock_connector.send_message.call_count == 1
    # Should use the message directly
    call = mock_connector.send_message.call_args
    text = call.kwargs.get("text", call.args[0] if call.args else "")
    assert text == "pre-chunked text"


def test_mesh_dm_channel_uses_mesh_renderer():
    """MeshDMChannel renders long messages to chunks for each recipient."""
    mock_connector = _mock_conn()

    channel = MeshDMChannel(
        connector=mock_connector,
        node_ids=["!node1", "!node2"],
    )

    long_message = "This is a long DM message that should be chunked. " * 4

    payload = NotificationPayload(
        message=long_message,
        category="test",
        severity="routine",
        timestamp=time.time(),
    )

    asyncio.run(channel.deliver(payload, None))

    # Should have called send_message multiple times
    # (chunks * nodes)
    assert mock_connector.send_message.call_count >= 2


def test_mesh_dm_channel_uses_payload_message_directly_when_chunk_metadata_set():
    """Pre-chunked DM payloads skip re-rendering."""
    mock_connector = _mock_conn()

    channel = MeshDMChannel(
        connector=mock_connector,
        node_ids=["!node1"],
    )

    payload = NotificationPayload(
        message="pre-chunked DM",
        category="digest",
        severity="routine",
        timestamp=time.time(),
        chunk_index=2,
        chunk_total=5,
    )

    asyncio.run(channel.deliver(payload, None))

    # Should use message directly, once per node
    assert mock_connector.send_message.call_count == 1
    call = mock_connector.send_message.call_args
    text = call.kwargs.get("text", call.args[0] if call.args else "")
    assert text == "pre-chunked DM"


# ============================================================
# EMAIL CHANNEL RENDERING TESTS
# ============================================================

def test_email_channel_uses_email_renderer():
    """EmailChannel uses renderer for subject and body."""
    channel = EmailChannel(
        smtp_host="localhost",
        smtp_port=25,
        smtp_user="",
        smtp_password="",
        smtp_tls=False,
        from_address="test@example.com",
        recipients=["user@example.com"],
    )

    payload = NotificationPayload(
        message="Test alert message",
        category="weather_warning",
        severity="immediate",
        timestamp=time.time(),
        event_type="weather_warning",
    )

    # Mock the _send_email method
    with patch.object(channel, "_send_email") as mock_send:
        asyncio.run(channel.deliver(payload, None))

        # Should have been called with renderer output
        mock_send.assert_called_once()
        call_args = mock_send.call_args
        subject = call_args.args[0]
        body = call_args.args[1]

        # Renderer format checks
        assert "[MeshAI]" in subject
        assert "IMMEDIATE" in subject
        assert "Test alert message" in body
        assert "Severity:" in body


# ============================================================
# WEBHOOK CHANNEL RENDERING TESTS
# ============================================================

def test_webhook_channel_uses_webhook_renderer():
    """WebhookChannel uses renderer for JSON payload."""
    channel = WebhookChannel(
        url="https://example.com/webhook",
        headers={},
    )

    payload = NotificationPayload(
        message="Test webhook message",
        category="test",
        severity="priority",
        timestamp=time.time(),
        event_type="battery_warning",
    )

    # Mock httpx
    with patch("meshai.notifications.channels.httpx.AsyncClient") as mock_client_class:
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        asyncio.run(channel.deliver(payload, None))

        # Check the POST was called
        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args.kwargs

        # Should have JSON payload with schema_version
        json_payload = call_kwargs.get("json", {})
        assert "schema_version" in json_payload
        assert json_payload["schema_version"] == "1.0"
        assert json_payload["message"] == "Test webhook message"


# ============================================================
# PER-FAMILY MESHCORE ROUTING — end-to-end threading guard
# Updated for the explicit-per-mesh model (meshcore_broadcast/mesh_broadcast)
# ============================================================

def test_mesh_broadcast_routes_to_meshtastic_only():
    """mesh_broadcast passes transport='meshtastic' and channel index to
    send_message. meshcore_channel is NOT passed (auto-fan removed).

    Regression guard: before this model, mesh_broadcast also threaded
    meshcore_channel through; now it is Meshtastic-only.
    """
    from meshai.config import NotificationRuleConfig
    from meshai.notifications.channels import create_channel

    mock_connector = _mock_conn()
    rule = NotificationRuleConfig(
        name="toggle:fire",
        delivery_type="mesh_broadcast",
        broadcast_channel=1,
        meshcore_channel="AIDA",  # present in config but must NOT flow to send_message
    )

    channel = create_channel(rule, mock_connector)

    # Pre-chunked payload => exactly one deterministic send_message call.
    payload = NotificationPayload(
        message="fire alert",
        category="fire",
        severity="immediate",
        timestamp=time.time(),
        event_type="fire",
        chunk_index=0,
    )

    assert asyncio.run(channel.deliver(payload, rule)) is True

    mock_connector.send_message.assert_called_once()
    kwargs = mock_connector.send_message.call_args.kwargs
    assert kwargs.get("channel") == 1
    assert kwargs.get("transport") == "meshtastic"
    # meshcore_channel must NOT be present (no auto-fan).
    assert "meshcore_channel" not in kwargs or kwargs.get("meshcore_channel") is None


def test_meshcore_broadcast_routes_to_meshcore_only():
    """meshcore_broadcast passes meshcore_channel=name and transport='meshcore'
    to send_message. This is the explicit MeshCore-only delivery path."""
    from meshai.config import NotificationRuleConfig
    from meshai.notifications.channels import MeshCoreBroadcastChannel, create_channel

    # Simulate a CompositeTransport connector with a meshcore child.
    mock_connector = _mock_conn()
    mock_connector._by_name = {"meshcore": MagicMock(), "meshtastic": MagicMock()}
    mock_connector.send_message.return_value = True

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
        timestamp=time.time(),
        event_type="fire",
        chunk_index=0,
    )

    assert asyncio.run(channel.deliver(payload, rule)) is True

    mock_connector.send_message.assert_called_once()
    kwargs = mock_connector.send_message.call_args.kwargs
    assert kwargs.get("meshcore_channel") == "AIDA"
    assert kwargs.get("transport") == "meshcore"
    assert kwargs.get("destination") is None


def test_broadcast_render_loop_threads_meshcore_channel():
    """Non-prechunked path (renderer loop) for meshcore_broadcast threads
    meshcore_channel on every chunk send."""
    from meshai.config import NotificationRuleConfig
    from meshai.notifications.channels import create_channel

    mock_connector = _mock_conn()
    # Connector has a meshcore child so the no-op guard passes.
    mock_connector._by_name = {"meshcore": MagicMock(), "meshtastic": MagicMock()}
    mock_connector.send_message.return_value = True

    rule = NotificationRuleConfig(
        name="toggle:fire",
        delivery_type="meshcore_broadcast",
        meshcore_channel="AIDA",
    )
    channel = create_channel(rule, mock_connector)

    long_message = "This is a very long alert message that exceeds the limit. " * 5
    payload = NotificationPayload(
        message=long_message,
        category="fire",
        severity="immediate",
        timestamp=time.time(),
        event_type="fire",
    )

    assert asyncio.run(channel.deliver(payload, rule)) is True
    assert mock_connector.send_message.call_count >= 2
    for call in mock_connector.send_message.call_args_list:
        assert call.kwargs.get("meshcore_channel") == "AIDA"
        assert call.kwargs.get("transport") == "meshcore"
