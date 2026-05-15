"""Tests for Phase 2.5b per-channel-type renderers."""

import json
import re
import time

import pytest

from meshai.notifications.events import NotificationPayload, make_event, make_payload_from_event
from meshai.notifications.renderers import MeshRenderer, EmailRenderer, WebhookRenderer


# ============================================================
# MESH RENDERER TESTS
# ============================================================

def test_mesh_render_short_message_single_chunk():
    """Short message produces a single chunk."""
    payload = NotificationPayload(
        message="Test alert",
        category="test",
        severity="routine",
        timestamp=time.time(),
    )
    renderer = MeshRenderer()
    chunks = renderer.render(payload)
    assert len(chunks) == 1
    assert "Test alert" in chunks[0]


def test_mesh_render_event_type_prefix():
    """Known event type adds toggle label prefix."""
    payload = NotificationPayload(
        message="Severe storm",
        category="weather_warning",
        severity="priority",
        timestamp=time.time(),
        event_type="weather_warning",
    )
    renderer = MeshRenderer()
    chunks = renderer.render(payload)
    assert len(chunks) == 1
    assert chunks[0].startswith("[Weather]")


def test_mesh_render_unknown_event_type_no_prefix():
    """Unknown event type does not add a prefix."""
    payload = NotificationPayload(
        message="Hello",
        category="made_up_thing",
        severity="routine",
        timestamp=time.time(),
        event_type="made_up_thing",
    )
    renderer = MeshRenderer()
    chunks = renderer.render(payload)
    assert len(chunks) == 1
    assert not chunks[0].startswith("[")


def test_mesh_render_long_message_chunks():
    """Long message splits into multiple chunks with counters."""
    # Build a ~500 char message
    long_message = "This is a very long alert message that should definitely exceed the two hundred character limit. " * 5
    payload = NotificationPayload(
        message=long_message,
        category="weather_warning",
        severity="priority",
        timestamp=time.time(),
        event_type="weather_warning",
    )
    renderer = MeshRenderer()
    chunks = renderer.render(payload)

    assert len(chunks) >= 2
    for chunk in chunks:
        assert len(chunk) <= 200, f"Chunk exceeds limit: {len(chunk)} chars"
    # Check each chunk ends with "(k/N)" counter
    for chunk in chunks:
        assert re.search(r"\(\d+/\d+\)$", chunk), f"Missing counter: {chunk}"


def test_mesh_render_chunks_preserve_full_content():
    """Chunking preserves all words from the original message."""
    # Build a message with distinct tokens
    words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf",
             "hotel", "india", "juliet", "kilo", "lima", "mike", "november",
             "oscar", "papa", "quebec", "romeo", "sierra", "tango", "uniform",
             "victor", "whiskey", "xray", "yankee", "zulu"]
    long_message = " ".join(words * 3)  # Repeat to span multiple chunks

    payload = NotificationPayload(
        message=long_message,
        category="test",
        severity="routine",
        timestamp=time.time(),
    )
    renderer = MeshRenderer()
    chunks = renderer.render(payload)

    # Join all chunks and remove counters
    combined = " ".join(chunks)
    combined = re.sub(r"\s*\(\d+/\d+\)", "", combined)

    # Every original word should appear
    for word in words:
        assert word in combined, f"Missing word: {word}"


def test_mesh_render_no_event_type():
    """Payload without event_type has no prefix."""
    payload = NotificationPayload(
        message="Plain message",
        category="test",
        severity="routine",
        timestamp=time.time(),
        event_type=None,
    )
    renderer = MeshRenderer()
    chunks = renderer.render(payload)
    assert len(chunks) == 1
    assert chunks[0] == "Plain message"


# ============================================================
# EMAIL RENDERER TESTS
# ============================================================

def test_email_render_subject_includes_severity_and_type():
    """Subject line includes severity and event type."""
    payload = NotificationPayload(
        message="Storm approaching",
        category="weather_warning",
        severity="immediate",
        timestamp=time.time(),
        event_type="weather_warning",
    )
    renderer = EmailRenderer()
    rendered = renderer.render(payload)

    assert "IMMEDIATE" in rendered["subject"]
    assert "Weather Warning" in rendered["subject"]


def test_email_render_body_includes_message_and_context():
    """Body includes message and structured context fields."""
    fixed_time = 1700000000.0  # Known timestamp
    payload = NotificationPayload(
        message="Test alert message",
        category="battery_warning",
        severity="priority",
        timestamp=fixed_time,
        event_type="battery_warning",
        region="Magic Valley",
        node_name="BLD-MTN",
    )
    renderer = EmailRenderer()
    rendered = renderer.render(payload)
    body = rendered["body"]

    assert "Test alert message" in body
    assert "Magic Valley" in body
    assert "BLD-MTN" in body
    assert "priority" in body
    assert "battery_warning" in body
    # Check formatted date
    assert "2023-11-14" in body  # Date part of timestamp


def test_email_render_omits_missing_context():
    """Body omits lines for missing optional fields."""
    payload = NotificationPayload(
        message="Minimal alert",
        category="test",
        severity="routine",
        timestamp=time.time(),
        # No region, no node, no event_type
    )
    renderer = EmailRenderer()
    rendered = renderer.render(payload)
    body = rendered["body"]

    assert "Minimal alert" in body
    assert "Severity: routine" in body
    assert "Region:" not in body
    assert "Node:" not in body
    assert "Category:" not in body


def test_email_render_includes_source_event():
    """Body includes source event details when present."""
    event = make_event(
        source="weather_adapter",
        category="weather_warning",
        severity="priority",
        title="Severe Storm",
        summary="Severe storm expected",
    )
    payload = make_payload_from_event(event)

    renderer = EmailRenderer()
    rendered = renderer.render(payload)
    body = rendered["body"]

    assert "weather_adapter" in body
    assert "Severe Storm" in body


# ============================================================
# WEBHOOK RENDERER TESTS
# ============================================================

def test_webhook_render_has_schema_version():
    """Output includes schema_version field."""
    payload = NotificationPayload(
        message="Test",
        category="test",
        severity="routine",
        timestamp=time.time(),
    )
    renderer = WebhookRenderer()
    rendered = renderer.render(payload)

    assert rendered["schema_version"] == "1.0"


def test_webhook_render_omits_none_fields():
    """None optional fields are omitted, not set to null."""
    payload = NotificationPayload(
        message="Test message",
        category="test",
        severity="routine",
        timestamp=time.time(),
        # All optional fields default to None
    )
    renderer = WebhookRenderer()
    rendered = renderer.render(payload)

    # Required fields present
    assert "message" in rendered
    assert "severity" in rendered
    assert "timestamp" in rendered
    assert "schema_version" in rendered

    # Optional fields omitted
    assert "node_id" not in rendered
    assert "region" not in rendered
    assert "chunk_index" not in rendered


def test_webhook_render_includes_source_event_when_present():
    """source_event dict included when payload has source event."""
    event = make_event(
        source="test_adapter",
        category="test",
        severity="routine",
        title="Test Event",
    )
    payload = make_payload_from_event(event)

    renderer = WebhookRenderer()
    rendered = renderer.render(payload)

    assert "source_event" in rendered
    assert rendered["source_event"]["id"] == event.id
    assert rendered["source_event"]["source"] == "test_adapter"


def test_webhook_render_is_json_serializable():
    """Rendered output is JSON-serializable (no datetime objects)."""
    event = make_event(
        source="test",
        category="test",
        severity="routine",
        title="Test",
    )
    payload = make_payload_from_event(event)
    payload.chunk_index = 1
    payload.chunk_total = 3
    payload.region = "Test Region"
    payload.node_name = "Test-Node"

    renderer = WebhookRenderer()
    rendered = renderer.render(payload)

    # Should not raise
    json_str = json.dumps(rendered)
    assert isinstance(json_str, str)
    # Verify round-trip
    parsed = json.loads(json_str)
    assert parsed["message"] == payload.message


def test_webhook_render_includes_optional_fields_when_set():
    """Optional fields included when they have values."""
    payload = NotificationPayload(
        message="Test",
        category="test_category",
        severity="priority",
        timestamp=time.time(),
        event_type="battery_warning",
        node_id="!abc123",
        node_name="Test-Node",
        region="Test Region",
        chunk_index=2,
        chunk_total=5,
    )
    renderer = WebhookRenderer()
    rendered = renderer.render(payload)

    assert rendered["category"] == "test_category"
    assert rendered["event_type"] == "battery_warning"
    assert rendered["node_id"] == "!abc123"
    assert rendered["node_name"] == "Test-Node"
    assert rendered["region"] == "Test Region"
    assert rendered["chunk_index"] == 2
    assert rendered["chunk_total"] == 5
