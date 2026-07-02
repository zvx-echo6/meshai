"""Phase 2.16.1: lock in notification-rule coercion in the config loader path.

Regression guard for the bug where the generic nested-dataclass handler in
_dict_to_dataclass shadowed the explicit 'notifications' branch, leaving
cfg.notifications.rules as raw dicts (which crashed Dispatcher._matching_rules
on rule.enabled). config_loader.load_config uses this same _dict_to_dataclass.
"""

from meshai.config import (
    Config,
    NotificationRuleConfig,
    NotificationToggle,
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
