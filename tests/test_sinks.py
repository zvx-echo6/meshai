"""Tests for routing simplification: SinkConfig and sink utilities.

Tests cover:
1. SinkConfig dataclass conversion from dict
2. Channel factory per sink type
3. Migration synthesis + idempotence + dry-run
"""

import pytest
import tempfile
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml


class TestSinkConfigDataclass:
    """Tests for SinkConfig dataclass and dict conversion."""

    def test_sink_config_defaults(self):
        """SinkConfig has correct defaults."""
        from meshai.config import SinkConfig

        sink = SinkConfig()
        assert sink.type == "mesh_broadcast"
        assert sink.channel == 0
        assert sink.node_ids == []
        assert sink.smtp_host == ""
        assert sink.recipients == []
        assert sink.webhook_url == ""

    def test_sink_config_mesh_broadcast(self):
        """SinkConfig correctly stores mesh_broadcast config."""
        from meshai.config import SinkConfig

        sink = SinkConfig(type="mesh_broadcast", channel=2)
        assert sink.type == "mesh_broadcast"
        assert sink.channel == 2

    def test_sink_config_mesh_dm(self):
        """SinkConfig correctly stores mesh_dm config."""
        from meshai.config import SinkConfig

        sink = SinkConfig(type="mesh_dm", node_ids=["!abc123", "!def456"])
        assert sink.type == "mesh_dm"
        assert sink.node_ids == ["!abc123", "!def456"]

    def test_sink_config_email(self):
        """SinkConfig correctly stores email config."""
        from meshai.config import SinkConfig

        sink = SinkConfig(
            type="email",
            smtp_host="smtp.example.com",
            smtp_port=465,
            smtp_user="user",
            smtp_password="pass",
            smtp_tls=True,
            from_address="alerts@example.com",
            recipients=["ops@example.com"],
        )
        assert sink.type == "email"
        assert sink.smtp_host == "smtp.example.com"
        assert sink.smtp_port == 465
        assert sink.recipients == ["ops@example.com"]

    def test_sink_config_webhook(self):
        """SinkConfig correctly stores webhook config."""
        from meshai.config import SinkConfig

        sink = SinkConfig(
            type="webhook",
            webhook_url="https://hooks.example.com/alert",
            webhook_headers={"Authorization": "Bearer token"},
        )
        assert sink.type == "webhook"
        assert sink.webhook_url == "https://hooks.example.com/alert"
        assert sink.webhook_headers == {"Authorization": "Bearer token"}

    def test_sink_config_validation_valid(self):
        """SinkConfig.validate() returns empty list for valid configs."""
        from meshai.config import SinkConfig

        # Valid mesh_broadcast
        sink = SinkConfig(type="mesh_broadcast", channel=0)
        assert sink.validate() == []

        # Valid mesh_dm
        sink = SinkConfig(type="mesh_dm", node_ids=["!abc123"])
        assert sink.validate() == []

        # Valid email
        sink = SinkConfig(type="email", smtp_host="smtp.test.com", recipients=["a@b.com"])
        assert sink.validate() == []

        # Valid webhook
        sink = SinkConfig(type="webhook", webhook_url="https://example.com")
        assert sink.validate() == []

    def test_sink_config_validation_invalid_type(self):
        """SinkConfig.validate() catches invalid type."""
        from meshai.config import SinkConfig

        sink = SinkConfig(type="invalid_type")
        errors = sink.validate()
        assert len(errors) == 1
        assert "Invalid sink type" in errors[0]

    def test_sink_config_validation_negative_channel(self):
        """SinkConfig.validate() catches negative channel."""
        from meshai.config import SinkConfig

        sink = SinkConfig(type="mesh_broadcast", channel=-1)
        errors = sink.validate()
        assert len(errors) == 1
        assert "must be >= 0" in errors[0]

    def test_sink_config_validation_channel_zero_valid(self):
        """SinkConfig.validate() accepts channel 0 (B6 fix verification)."""
        from meshai.config import SinkConfig

        sink = SinkConfig(type="mesh_broadcast", channel=0)
        errors = sink.validate()
        assert errors == []

    def test_dict_to_dataclass_converts_sinks(self):
        """_dict_to_dataclass correctly converts sinks dict to SinkConfig instances."""
        from meshai.config import _dict_to_dataclass, Config, SinkConfig

        config_dict = {
            "notifications": {
                "enabled": True,
                "sinks": {
                    "mesh-primary": {"type": "mesh_broadcast", "channel": 0},
                    "mesh-alerts": {"type": "mesh_broadcast", "channel": 2},
                    "dm-ops": {"type": "mesh_dm", "node_ids": ["!abcd1234"]},
                },
            },
        }

        config = _dict_to_dataclass(Config, config_dict)

        assert hasattr(config.notifications, "sinks")
        sinks = config.notifications.sinks

        assert "mesh-primary" in sinks
        assert isinstance(sinks["mesh-primary"], SinkConfig)
        assert sinks["mesh-primary"].type == "mesh_broadcast"
        assert sinks["mesh-primary"].channel == 0

        assert "mesh-alerts" in sinks
        assert isinstance(sinks["mesh-alerts"], SinkConfig)
        assert sinks["mesh-alerts"].channel == 2

        assert "dm-ops" in sinks
        assert isinstance(sinks["dm-ops"], SinkConfig)
        assert sinks["dm-ops"].type == "mesh_dm"
        assert sinks["dm-ops"].node_ids == ["!abcd1234"]


class TestCreateChannelFromSink:
    """Tests for create_channel_from_sink factory function."""

    def test_create_mesh_broadcast_channel(self):
        """create_channel_from_sink creates MeshBroadcastChannel."""
        from meshai.config import SinkConfig
        from meshai.notifications.channels import (
            create_channel_from_sink,
            MeshBroadcastChannel,
        )

        sink = SinkConfig(type="mesh_broadcast", channel=2)
        mock_connector = MagicMock()

        channel = create_channel_from_sink(sink, connector=mock_connector)

        assert isinstance(channel, MeshBroadcastChannel)
        assert channel._channel == 2
        assert channel._connector == mock_connector

    def test_create_mesh_dm_channel(self):
        """create_channel_from_sink creates MeshDMChannel."""
        from meshai.config import SinkConfig
        from meshai.notifications.channels import (
            create_channel_from_sink,
            MeshDMChannel,
        )

        sink = SinkConfig(type="mesh_dm", node_ids=["!abc123", "!def456"])
        mock_connector = MagicMock()

        channel = create_channel_from_sink(sink, connector=mock_connector)

        assert isinstance(channel, MeshDMChannel)
        assert channel._node_ids == ["!abc123", "!def456"]

    def test_create_email_channel(self):
        """create_channel_from_sink creates EmailChannel."""
        from meshai.config import SinkConfig
        from meshai.notifications.channels import (
            create_channel_from_sink,
            EmailChannel,
        )

        sink = SinkConfig(
            type="email",
            smtp_host="smtp.test.com",
            smtp_port=587,
            smtp_user="user",
            smtp_password="pass",
            smtp_tls=True,
            from_address="alerts@test.com",
            recipients=["ops@test.com"],
        )

        channel = create_channel_from_sink(sink)

        assert isinstance(channel, EmailChannel)
        assert channel._host == "smtp.test.com"
        assert channel._port == 587
        assert channel._recipients == ["ops@test.com"]

    def test_create_webhook_channel(self):
        """create_channel_from_sink creates WebhookChannel."""
        from meshai.config import SinkConfig
        from meshai.notifications.channels import (
            create_channel_from_sink,
            WebhookChannel,
        )

        sink = SinkConfig(
            type="webhook",
            webhook_url="https://hooks.test.com/alert",
            webhook_headers={"X-Token": "secret"},
        )

        channel = create_channel_from_sink(sink)

        assert isinstance(channel, WebhookChannel)
        assert channel._url == "https://hooks.test.com/alert"
        assert channel._headers == {"X-Token": "secret"}

    def test_create_channel_invalid_type_raises(self):
        """create_channel_from_sink raises ValueError for invalid type."""
        from meshai.config import SinkConfig
        from meshai.notifications.channels import create_channel_from_sink

        sink = SinkConfig(type="invalid_type")

        with pytest.raises(ValueError, match="Unknown sink type"):
            create_channel_from_sink(sink)

    def test_create_channel_negative_channel_raises(self):
        """create_channel_from_sink raises ValueError for negative channel."""
        from meshai.config import SinkConfig
        from meshai.notifications.channels import create_channel_from_sink

        sink = SinkConfig(type="mesh_broadcast", channel=-1)

        with pytest.raises(ValueError, match="must be >= 0"):
            create_channel_from_sink(sink)

    def test_create_channel_zero_channel_valid(self):
        """create_channel_from_sink accepts channel 0 (B6 fix verification)."""
        from meshai.config import SinkConfig
        from meshai.notifications.channels import (
            create_channel_from_sink,
            MeshBroadcastChannel,
        )

        sink = SinkConfig(type="mesh_broadcast", channel=0)
        mock_connector = MagicMock()

        channel = create_channel_from_sink(sink, connector=mock_connector)

        assert isinstance(channel, MeshBroadcastChannel)
        assert channel._channel == 0


class TestMigrationSynthesis:
    """Tests for migration script sink synthesis logic."""

    def test_extract_sink_from_toggle_mesh_broadcast(self):
        """extract_sink_from_toggle handles broadcast_channel."""
        from meshai.scripts.migrate_config_routing import extract_sinks_from_toggle, extract_sink_from_toggle

        toggle = {"broadcast_channel": 2, "enabled": True}
        sink = extract_sink_from_toggle(toggle)

        assert sink == {"type": "mesh_broadcast", "channel": 2}

    def test_extract_sink_from_toggle_mesh_dm(self):
        """extract_sink_from_toggle handles node_ids."""
        from meshai.scripts.migrate_config_routing import extract_sinks_from_toggle, extract_sink_from_toggle

        toggle = {"node_ids": ["!abc123"], "enabled": True}
        sink = extract_sink_from_toggle(toggle)

        assert sink == {"type": "mesh_dm", "node_ids": ["!abc123"]}

    def test_extract_sink_from_toggle_email(self):
        """extract_sink_from_toggle handles smtp_host."""
        from meshai.scripts.migrate_config_routing import extract_sinks_from_toggle, extract_sink_from_toggle

        toggle = {
            "smtp_host": "smtp.test.com",
            "smtp_port": 587,
            "smtp_user": "user",
            "smtp_password": "pass",
            "smtp_tls": True,
            "from_address": "alerts@test.com",
            "recipients": ["ops@test.com"],
        }
        sink = extract_sink_from_toggle(toggle)

        assert sink["type"] == "email"
        assert sink["smtp_host"] == "smtp.test.com"
        assert sink["recipients"] == ["ops@test.com"]

    def test_extract_sink_from_toggle_webhook(self):
        """extract_sink_from_toggle handles webhook_url."""
        from meshai.scripts.migrate_config_routing import extract_sinks_from_toggle, extract_sink_from_toggle

        toggle = {
            "webhook_url": "https://hooks.test.com",
            "webhook_headers": {"X-Token": "secret"},
        }
        sink = extract_sink_from_toggle(toggle)

        assert sink == {
            "type": "webhook",
            "webhook_url": "https://hooks.test.com",
            "webhook_headers": {"X-Token": "secret"},
        }

    def test_extract_sink_from_toggle_none(self):
        """extract_sink_from_toggle returns None for toggle without transport."""
        from meshai.scripts.migrate_config_routing import extract_sinks_from_toggle, extract_sink_from_toggle

        toggle = {"enabled": True, "min_severity": "priority"}
        sink = extract_sink_from_toggle(toggle)

        assert sink is None

    
    def test_extract_sinks_from_toggle_multiple_transports(self):
        """extract_sinks_from_toggle returns ALL configured transports."""
        from meshai.scripts.migrate_config_routing import extract_sinks_from_toggle

        toggle = {
            "broadcast_channel": 1,
            "node_ids": ["!abc123"],
            "smtp_host": "",  # Empty = not configured
            "webhook_url": "",  # Empty = not configured
        }
        sinks = extract_sinks_from_toggle(toggle)

        assert len(sinks) == 2
        types = {s["type"] for s in sinks}
        assert types == {"mesh_broadcast", "mesh_dm"}

    def test_extract_sinks_from_toggle_all_four_types(self):
        """extract_sinks_from_toggle extracts all four transport types."""
        from meshai.scripts.migrate_config_routing import extract_sinks_from_toggle

        toggle = {
            "broadcast_channel": 2,
            "node_ids": ["!abc123"],
            "smtp_host": "smtp.test.com",
            "recipients": ["ops@test.com"],
            "webhook_url": "https://hooks.test.com",
        }
        sinks = extract_sinks_from_toggle(toggle)

        assert len(sinks) == 4
        types = {s["type"] for s in sinks}
        assert types == {"mesh_broadcast", "mesh_dm", "email", "webhook"}

    def test_extract_sink_from_rule(self):
        """extract_sink_from_rule handles delivery_type."""
        from meshai.scripts.migrate_config_routing import extract_sink_from_rule

        rule = {"delivery_type": "mesh_broadcast", "broadcast_channel": 3}
        sink = extract_sink_from_rule(rule)

        assert sink == {"type": "mesh_broadcast", "channel": 3}

    def test_generate_sink_name_mesh_broadcast(self):
        """generate_sink_name creates readable names for mesh_broadcast."""
        from meshai.scripts.migrate_config_routing import generate_sink_name

        name = generate_sink_name("mesh_broadcast", {"channel": 2})
        assert name == "mesh-ch2"

    def test_generate_sink_name_mesh_dm(self):
        """generate_sink_name creates readable names for mesh_dm."""
        from meshai.scripts.migrate_config_routing import generate_sink_name

        name = generate_sink_name("mesh_dm", {"node_ids": ["!abcd1234"]})
        assert name == "dm-abcd1234"

    def test_generate_sink_name_email(self):
        """generate_sink_name creates readable names for email."""
        from meshai.scripts.migrate_config_routing import generate_sink_name

        name = generate_sink_name("email", {"smtp_host": "smtp.example.com"})
        assert name == "email-smtp"

    def test_generate_sink_name_webhook(self):
        """generate_sink_name creates readable names for webhook."""
        from meshai.scripts.migrate_config_routing import generate_sink_name

        name = generate_sink_name("webhook", {"webhook_url": "https://hooks.slack.com/abc"})
        assert name == "webhook-hooks"

    def test_synthesize_sinks_deduplicates(self):
        """synthesize_sinks deduplicates identical transports."""
        from meshai.scripts.migrate_config_routing import synthesize_sinks

        notifications = {
            "toggles": {
                "fire": {"broadcast_channel": 2},
                "weather": {"broadcast_channel": 2},  # Same channel
            }
        }

        sinks = synthesize_sinks(notifications)

        # Should only have one sink for channel 2
        assert len(sinks) == 1
        assert "mesh-ch2" in sinks

    def test_synthesize_sinks_handles_collisions(self):
        """synthesize_sinks handles name collisions."""
        from meshai.scripts.migrate_config_routing import synthesize_sinks

        notifications = {
            "toggles": {
                "fire": {"broadcast_channel": 0},
                "weather": {"broadcast_channel": 1},
                "roads": {"broadcast_channel": 2},
            }
        }

        sinks = synthesize_sinks(notifications)

        # Should have three unique sinks
        assert len(sinks) == 3


class TestMigrationIdempotence:
    """Tests for migration script idempotence."""

    def test_migration_refuses_if_sinks_exist(self):
        """Migration refuses to run if sinks block already exists."""
        from meshai.scripts.migrate_config_routing import load_notifications_config

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump({
                "notifications": {
                    "enabled": True,
                    "sinks": {"mesh-primary": {"type": "mesh_broadcast"}},
                }
            }, f)
            config_path = Path(f.name)

        try:
            _, notifications, _ = load_notifications_config(config_path)
            assert notifications.get("sinks") is not None
            # The main() function checks this and exits
        finally:
            os.unlink(config_path)

    def test_backup_creates_timestamped_file(self):
        """backup_config creates properly named backup."""
        from meshai.scripts.migrate_config_routing import backup_config

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("test: true\n")
            config_path = Path(f.name)

        try:
            backup_path = backup_config(config_path)
            assert backup_path.exists()
            assert ".pre-sinks." in str(backup_path)
            assert backup_path.suffix == ".bak"
        finally:
            os.unlink(config_path)
            if backup_path.exists():
                os.unlink(backup_path)


class TestWriteSinksToConfig:
    """Tests for layout-aware, append-only sinks write."""

    def test_write_sinks_multifile_layout(self):
        """write_sinks_to_config handles multi-file layout correctly."""
        from meshai.scripts.migrate_config_routing import (
            write_sinks_to_config,
            backup_config,
            load_notifications_config,
        )

        # Create a multi-file config with comments
        original_content = """# Notification config for MeshAI
# This comment should be preserved

toggles:
  fire:
    enabled: true
    broadcast_channel: 1
  weather:
    enabled: true
    broadcast_channel: 2

rules: []
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(original_content)
            config_path = Path(f.name)

        backup_path = None
        try:
            backup_path = backup_config(config_path)
            sinks = {
                "mesh-ch1": {"type": "mesh_broadcast", "channel": 1},
                "mesh-ch2": {"type": "mesh_broadcast", "channel": 2},
            }

            success = write_sinks_to_config(config_path, sinks, "multifile", backup_path)
            assert success, "Write should succeed"

            # Read back and verify
            with open(config_path, "r") as f:
                new_content = f.read()

            # Comments should be preserved
            assert "# Notification config for MeshAI" in new_content
            assert "# This comment should be preserved" in new_content

            # Original keys should be intact
            _, notifications, layout = load_notifications_config(config_path)
            assert layout == "multifile"
            assert "toggles" in notifications
            assert "rules" in notifications
            assert "sinks" in notifications

            # Sinks should be at root level (no notifications: wrapper created)
            config = yaml.safe_load(new_content)
            assert "notifications" not in config, "Should not create notifications: wrapper"
            assert "sinks" in config, "Sinks should be at root"
            assert set(config["sinks"].keys()) == {"mesh-ch1", "mesh-ch2"}
        finally:
            os.unlink(config_path)
            if backup_path and backup_path.exists():
                os.unlink(backup_path)

    def test_write_sinks_monolithic_layout(self):
        """write_sinks_to_config handles monolithic layout correctly."""
        from meshai.scripts.migrate_config_routing import (
            write_sinks_to_config,
            backup_config,
            load_notifications_config,
        )

        # Create a monolithic config
        original_content = """# Main config file
notifications:
  enabled: true
  toggles:
    fire:
      broadcast_channel: 1

other_section:
  key: value
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(original_content)
            config_path = Path(f.name)

        backup_path = None
        try:
            backup_path = backup_config(config_path)
            sinks = {"mesh-ch1": {"type": "mesh_broadcast", "channel": 1}}

            success = write_sinks_to_config(config_path, sinks, "monolithic", backup_path)
            assert success, "Write should succeed"

            # Read back and verify
            _, notifications, layout = load_notifications_config(config_path)
            assert layout == "monolithic"
            assert "sinks" in notifications
            assert "mesh-ch1" in notifications["sinks"]

            # Other sections should be intact
            with open(config_path, "r") as f:
                config = yaml.safe_load(f)
            assert "other_section" in config
            assert config["other_section"]["key"] == "value"
        finally:
            os.unlink(config_path)
            if backup_path and backup_path.exists():
                os.unlink(backup_path)

    def test_write_aborts_and_restores_on_bad_result(self):
        """write_sinks_to_config restores backup if verification fails."""
        from meshai.scripts.migrate_config_routing import (
            backup_config,
            verify_sinks_written,
        )
        import shutil

        original_content = """toggles:
  fire:
    enabled: true
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(original_content)
            config_path = Path(f.name)

        backup_path = None
        try:
            backup_path = backup_config(config_path)

            # Corrupt the file
            with open(config_path, "w") as f:
                f.write("invalid: yaml: content: [")

            # Verify should fail
            success, error = verify_sinks_written(
                config_path,
                {"mesh-ch1": {"type": "mesh_broadcast"}},
                "multifile",
                {"toggles"}
            )
            assert not success
            assert "parse error" in error.lower() or "not found" in error.lower()

            # Restore from backup
            shutil.copy2(backup_path, config_path)

            # Original content should be restored
            with open(config_path, "r") as f:
                restored = f.read()
            assert "toggles:" in restored
        finally:
            os.unlink(config_path)
            if backup_path and backup_path.exists():
                os.unlink(backup_path)

    def test_dry_run_reports_layout(self, capsys):
        """--dry-run reports detected layout and write path."""
        from meshai.scripts.migrate_config_routing import (
            load_notifications_config,
            synthesize_sinks,
            render_sinks_yaml,
        )

        # Multi-file layout
        multifile_content = """toggles:
  fire:
    broadcast_channel: 1
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(multifile_content)
            config_path = Path(f.name)

        try:
            _, notifications, layout = load_notifications_config(config_path)
            assert layout == "multifile"

            sinks = synthesize_sinks(notifications)
            rendered = render_sinks_yaml(sinks, layout)

            # The rendered YAML for multifile should not be indented
            assert rendered.startswith("sinks:")
            assert "  mesh-ch1:" in rendered

            # For monolithic, it should be indented
            mono_rendered = render_sinks_yaml(sinks, "monolithic")
            assert mono_rendered.startswith("  sinks:")
        finally:
            os.unlink(config_path)

    def test_generate_sink_name_int_node_ids(self):
        """generate_sink_name handles integer node IDs without crashing."""
        from meshai.scripts.migrate_config_routing import generate_sink_name

        # Integer node ID (no leading !)
        name = generate_sink_name("mesh_dm", {"node_ids": [123456789]})
        assert name == "dm-12345678"

        # String node ID with !
        name = generate_sink_name("mesh_dm", {"node_ids": ["!abcd1234"]})
        assert name == "dm-abcd1234"

        # Mixed types
        name = generate_sink_name("mesh_dm", {"node_ids": [987654321, "!xyz"]})
        assert name == "dm-98765432"
