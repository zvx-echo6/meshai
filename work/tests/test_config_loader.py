"""Phase 2.16.1: lock in notification-rule coercion in the config loader path.

Regression guard for the bug where the generic nested-dataclass handler in
_dict_to_dataclass shadowed the explicit 'notifications' branch, leaving
cfg.notifications.rules as raw dicts (which crashed Dispatcher._matching_rules
on rule.enabled). config_loader.load_config uses this same _dict_to_dataclass.
"""

import logging

from meshai.config import (
    Config,
    MeshIntelligenceConfig,
    NotificationRuleConfig,
    NotificationToggle,
    RegionRouteMatrix,
    _dataclass_to_dict,
    _dict_to_dataclass,
)


def test_multifile_load_coerces_notification_rules():
    """notifications.rules dicts are coerced to NotificationRuleConfig."""
    data = {
        "notifications": {
            "enabled": True,
            "rules": [
                {
                    "name": "Test Rule",
                    "enabled": True,
                    "trigger_type": "condition",
                    "categories": ["earthquake_event"],
                    "min_severity": "routine",
                    "delivery_type": "mesh_broadcast",
                },
                {
                    "name": "Second Rule",
                    "enabled": False,
                    "trigger_type": "condition",
                    "categories": ["wildfire_incident"],
                    "delivery_type": "email",
                },
            ],
        }
    }
    cfg = _dict_to_dataclass(Config, data)
    rules = cfg.notifications.rules
    assert len(rules) == 2
    # Coerced to the dataclass, NOT left as dicts.
    assert all(isinstance(r, NotificationRuleConfig) for r in rules)
    # Attribute access (what Dispatcher._matching_rules needs) works.
    assert rules[0].enabled is True
    assert rules[0].name == "Test Rule"
    assert rules[1].enabled is False


def test_rules_attribute_access_does_not_raise():
    """Dispatcher-style attribute access on every rule succeeds."""
    data = {
        "notifications": {
            "rules": [
                {"name": "R", "enabled": True, "trigger_type": "condition",
                 "categories": ["earthquake_event"], "min_severity": "immediate"},
            ]
        }
    }
    cfg = _dict_to_dataclass(Config, data)
    for r in cfg.notifications.rules:
        # These are the accesses Dispatcher._matching_rules performs.
        _ = r.enabled
        _ = r.trigger_type
        _ = r.categories
        _ = r.min_severity


def test_toggle_meshcore_channel_name_round_trips():
    """A NotificationToggle's meshcore_channel NAME survives dict round-trip.

    _dict_to_dataclass drops unknown keys, so this guards that the new
    meshcore_channel field is a real dataclass field and persists as a str.
    """
    tog = NotificationToggle(name="fire", enabled=True, meshcore_channel="AIDA")
    d = _dataclass_to_dict(tog)
    assert d["meshcore_channel"] == "AIDA"
    restored = _dict_to_dataclass(NotificationToggle, d)
    assert restored.meshcore_channel == "AIDA"

    # Default stays None when unset.
    default = _dict_to_dataclass(NotificationToggle, {"name": "weather"})
    assert default.meshcore_channel is None


def test_unknown_key_warns_but_does_not_raise(caplog):
    """An unrecognized config key (typo, or a renamed/removed field) logs a
    WARNING naming the key and the dataclass, and the key is silently dropped
    (never raises). Regression guard for the silent-drop bug: previously an
    operator could set a bogus/typo'd key, restart, and get zero feedback."""
    data = {
        "mesh_intelligence": {
            "enabled": True,
            "region_radius_miles": 40.0,  # never a real field -- see config.example.yaml fix
        }
    }
    with caplog.at_level(logging.WARNING, logger="meshai.config"):
        cfg = _dict_to_dataclass(Config, data)

    assert cfg.mesh_intelligence.enabled is True
    assert not hasattr(cfg.mesh_intelligence, "region_radius_miles")
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    assert "region_radius_miles" in msg
    assert "MeshIntelligenceConfig" in msg


def test_legacy_region_routes_enabled_does_not_warn(caplog):
    """region_routes.enabled is a pre-mt/mc-split legacy key, still read
    directly by the explicit region_routes handler in _dict_to_dataclass.
    It intentionally has no RegionRouteMatrix field and must NOT warn --
    warning here would be a false positive for every config still using the
    pre-split single master switch."""
    data = {"notifications": {"region_routes": {"enabled": True, "cells": {}}}}
    with caplog.at_level(logging.WARNING, logger="meshai.config"):
        cfg = _dict_to_dataclass(Config, data)

    assert cfg.notifications.region_routes.mt_enabled is True
    assert not any(
        "region_routes" in r.getMessage() or "'enabled'" in r.getMessage()
        for r in caplog.records
    )


def test_legacy_notifications_channels_does_not_warn(caplog):
    """notifications.channels (pre-v0.5 format) has no NotificationsConfig
    field -- it's consumed directly from the raw dict by
    _migrate_legacy_channels. Must not warn for users mid-migration."""
    data = {
        "notifications": {
            "channels": [{"id": "c1", "type": "mesh_broadcast", "channel_index": 0}],
            "rules": [{"name": "r1", "channel_ids": ["c1"], "categories": ["fire"]}],
        }
    }
    with caplog.at_level(logging.WARNING, logger="meshai.config"):
        cfg = _dict_to_dataclass(Config, data)

    assert len(cfg.notifications.rules) == 1
    assert cfg.notifications.rules[0].broadcast_channel == 0
    assert not any("channels" in r.getMessage() for r in caplog.records)


def test_dynamic_sections_do_not_warn(caplog):
    """Sections that are intentionally free-form/passthrough (generic_sources)
    or use explicit dict-of-dataclass coercion (toggles, destinations) must
    never warn for keys that ARE valid on their actual target shape."""
    data = {
        "notifications": {
            "toggles": {
                "weather": {"name": "weather", "enabled": True, "min_severity": "priority"},
            },
            "destinations": {
                "d1": {"name": "d1", "type": "mesh_broadcast", "broadcast_channel": 0},
            },
        },
        "mesh_sources": [
            {"name": "mv", "type": "meshview", "url": "http://x", "enabled": True},
        ],
        "generic_sources": [
            {"name": "gs1", "enabled": True, "url": "http://x", "not_a_dataclass_field": "fine"},
        ],
    }
    with caplog.at_level(logging.WARNING, logger="meshai.config"):
        cfg = _dict_to_dataclass(Config, data)

    assert caplog.records == []
    assert cfg.generic_sources[0]["not_a_dataclass_field"] == "fine"
