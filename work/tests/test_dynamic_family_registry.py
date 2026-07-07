"""Integration Phase A: dynamic category/family registry.

A generic data source registers its category as a first-class family with its
own (default-disabled) toggle, so its events resolve to that family and become
routable instead of being dropped as "other". Existing built-in categories are
unchanged.
"""

import pytest

from meshai.notifications import categories
from meshai.notifications.categories import (
    register_category,
    register_family,
    get_toggle,
    all_toggles,
    registered_families,
    VALID_TOGGLES,
)
from meshai.notifications.events import make_event
from meshai.notifications.pipeline.toggle_filter import ToggleFilter
from meshai.config import Config, NotificationToggle, ensure_family_toggles


@pytest.fixture(autouse=True)
def _clean_registry():
    """The dynamic registry is module-global; isolate every test."""
    categories._DYNAMIC_CATEGORIES.clear()
    categories._DYNAMIC_FAMILIES.clear()
    yield
    categories._DYNAMIC_CATEGORIES.clear()
    categories._DYNAMIC_FAMILIES.clear()


class TestRegistry:
    def test_registered_category_resolves_to_its_own_family(self):
        register_category("power_outage", family="power_outage")
        assert get_toggle("power_outage") == "power_outage"

    def test_register_category_registers_family(self):
        register_category("power_outage", family="power_outage")
        assert "power_outage" in all_toggles()
        assert "power_outage" in registered_families()

    def test_register_family_idempotent_and_label(self):
        register_family("power_outage", "Power Outages")
        assert registered_families()["power_outage"] == "Power Outages"
        # Re-register with no label leaves the label intact.
        register_family("power_outage")
        assert registered_families()["power_outage"] == "Power Outages"

    def test_register_family_default_label_title_cased(self):
        register_family("power_outage")
        assert registered_families()["power_outage"] == "Power Outage"

    def test_category_can_route_to_a_distinct_family(self):
        register_category("idaho_power_outage", family="power", name="Idaho Power")
        assert get_toggle("idaho_power_outage") == "power"
        assert "power" in all_toggles()

    def test_unregistered_still_falls_back_as_before(self):
        # No registration -> unknown category has no toggle (ToggleFilter -> other).
        assert get_toggle("totally_unknown_xyz") is None

    def test_prefix_fallback_still_applies_when_unregistered(self):
        # weather-prefixed unknown still resolves via _TOGGLE_PREFIX_FALLBACK.
        assert get_toggle("weather_special_bulletin") == "weather"

    @pytest.mark.parametrize("category,expected", [
        ("weather_warning", "weather"),
        ("wildfire_incident", "fire"),
        ("traffic_congestion", "roads"),
        ("earthquake_event", "seismic"),
        ("infra_offline", "mesh_health"),
        ("avalanche_warning", "avalanche"),
        ("rf_ducting_enhancement", "rf_propagation"),
    ])
    def test_existing_categories_unchanged(self, category, expected):
        # With the dynamic registry populated by an unrelated family, every
        # built-in category must still resolve to its original toggle.
        register_category("power_outage", family="power_outage")
        assert get_toggle(category) == expected

    def test_all_toggles_includes_static_base_and_dynamic(self):
        register_category("power_outage", family="power_outage")
        toggles = all_toggles()
        assert VALID_TOGGLES <= toggles          # static base preserved
        assert "power_outage" in toggles          # dynamic added
        # VALID_TOGGLES frozenset itself is not mutated.
        assert "power_outage" not in VALID_TOGGLES


class TestEnsureFamilyToggles:
    def test_injects_disabled_toggle_for_new_family(self):
        config = Config()
        assert "power_outage" not in config.notifications.toggles
        ensure_family_toggles(config, ["power_outage"])
        tog = config.notifications.toggles["power_outage"]
        assert isinstance(tog, NotificationToggle)
        assert tog.name == "power_outage"
        assert tog.enabled is False
        # Mirrors _default_toggles defaults.
        assert tog.min_severity == "priority"
        assert tog.severity_channels == {
            "priority": ["mesh_broadcast"],
            "immediate": ["mesh_broadcast", "mesh_dm"],
        }

    def test_does_not_clobber_existing_toggle(self):
        config = Config()
        # Operator has enabled the built-in weather family.
        config.notifications.toggles["weather"].enabled = True
        ensure_family_toggles(config, ["weather", "power_outage"])
        assert config.notifications.toggles["weather"].enabled is True
        assert config.notifications.toggles["power_outage"].enabled is False


class TestToggleFilterWithDynamicFamily:
    def _generic_event(self):
        return make_event(
            source="generic:idaho_power",
            category="power_outage",
            severity="priority",
            title="Outage",
        )

    def test_registered_generic_family_enabled_passes(self):
        register_category("power_outage", family="power_outage")
        received = []
        filt = ToggleFilter(
            next_handler=received.append,
            enabled_toggles={"power_outage"},
        )
        filt.handle(self._generic_event())
        assert len(received) == 1

    def test_registered_generic_family_disabled_drops(self):
        register_category("power_outage", family="power_outage")
        received = []
        # Some other family enabled, but NOT power_outage.
        filt = ToggleFilter(
            next_handler=received.append,
            enabled_toggles={"weather"},
        )
        filt.handle(self._generic_event())
        # Dropped as its own disabled family — NOT the silent "other" path,
        # and NOT buried in mesh_health.
        assert len(received) == 0

    def test_regression_weather_still_routes(self):
        # A normal built-in category is unaffected by the dynamic registry.
        register_category("power_outage", family="power_outage")
        received = []
        filt = ToggleFilter(
            next_handler=received.append,
            enabled_toggles={"weather"},
        )
        filt.handle(make_event(
            source="nws", category="weather_warning",
            severity="priority", title="Weather",
        ))
        assert len(received) == 1
