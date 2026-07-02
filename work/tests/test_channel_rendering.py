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


# ============================================================
# MESH CHANNEL RENDERING TESTS
# ============================================================

def test_mesh_channel_uses_mesh_renderer():
    """MeshBroadcastChannel renders long messages to multiple chunks."""
    mock_connector = MagicMock()

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
    mock_connector = MagicMock()

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
    mock_connector = MagicMock()

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
    mock_connector = MagicMock()

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
# (regression guard for the broadcast send-path gap)
# ============================================================

def test_broadcast_threads_meshcore_channel_through_factory():
    """create_channel(rule) -> MeshBroadcastChannel.deliver must pass BOTH
    channel=<broadcast_channel> AND meshcore_channel=<name> to send_message.

    This is the regression guard for the gap where the rule's
    meshcore_channel never reached connector.send_message.
    """
    from meshai.config import NotificationRuleConfig
    from meshai.notifications.channels import create_channel

    mock_connector = MagicMock()
    rule = NotificationRuleConfig(
        name="toggle:fire",
        delivery_type="mesh_broadcast",
        broadcast_channel=1,
        meshcore_channel="AIDA",
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
    # The load-bearing assertion: the name was NOT dropped.
    assert kwargs.get("meshcore_channel") == "AIDA"


def test_broadcast_meshcore_channel_none_passed_through():
    """meshcore_channel=None (family not on MeshCore) => send_message still
    receives meshcore_channel=None (MeshCore child skipped downstream)."""
    from meshai.config import NotificationRuleConfig
    from meshai.notifications.channels import create_channel

    mock_connector = MagicMock()
    rule = NotificationRuleConfig(
        name="toggle:weather",
        delivery_type="mesh_broadcast",
        broadcast_channel=0,
        meshcore_channel=None,
    )

    channel = create_channel(rule, mock_connector)

    payload = NotificationPayload(
        message="weather alert",
        category="weather_warning",
        severity="priority",
        timestamp=time.time(),
        event_type="weather_warning",
        chunk_index=0,
    )

    assert asyncio.run(channel.deliver(payload, rule)) is True

    kwargs = mock_connector.send_message.call_args.kwargs
    assert kwargs.get("channel") == 0
    assert "meshcore_channel" in kwargs
    assert kwargs.get("meshcore_channel") is None


def test_broadcast_render_loop_threads_meshcore_channel():
    """Non-prechunked path (renderer loop) also threads meshcore_channel
    on every chunk send."""
    from meshai.config import NotificationRuleConfig
    from meshai.notifications.channels import create_channel

    mock_connector = MagicMock()
    rule = NotificationRuleConfig(
        name="toggle:fire",
        delivery_type="mesh_broadcast",
        broadcast_channel=2,
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
        assert call.kwargs.get("channel") == 2
        assert call.kwargs.get("meshcore_channel") == "AIDA"
