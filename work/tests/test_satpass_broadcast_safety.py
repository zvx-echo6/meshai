"""Tests for satpass broadcast safety controls.

Covers the incident-response requirements that live in
`meshai.env.satellite.pass_format` (formatting rules) and REGISTRY defaults:
  3. Dry-run default: REGISTRY dry_run default is True
  4. Elevation default: REGISTRY min_elevation = 30
  5. Broadcast wire format: single-line, numeric degrees, byte budget
  6. Clean format: short names, degrees, compass collapse, friendly observers

The Central envelope-ingest path (`handle_satpass`,
`consolidate_satpass_pending`, and the opt-in-bird-filter / rate-cap /
dry-run / staleness-guard / norad-id-coercion logic that lived INSIDE
`handle_satpass`) was retired with the Central NATS consumer and deleted
2026-07. That logic has no live equivalent in the native path -- the native
SatpassAdapter (env/satpass.py) does its own norad/observer filtering at the
adapter level (config-driven, not per-envelope) and its own imminence gate
instead of a staleness guard; both are covered by tests/test_satpass_native.py.
Dedup, rate-cap, and dry-run behavior of the shared
`gate_consolidated_pass` gate are also exercised live via the native adapter
in tests/test_satpass_native.py (`test_second_tick_does_not_rebroadcast_after_commit`,
imminence tests, etc). Tests that only exercised the dead ingest path's
filters (opt-in bird filter mechanics, rate-cap-via-handle_satpass,
dry-run-via-handle_satpass, norad-id string/int coercion, staleness guard)
were deleted rather than ported, since their behavior no longer exists
anywhere to test.
"""
from __future__ import annotations

from datetime import datetime

import pytest


# ══════════════════════════════════════════════════════════════════════
# 1. OPT-IN BIRD FILTER — REGISTRY-only surviving check
# ══════════════════════════════════════════════════════════════════════

class TestOptInBirdFilter:
    def test_dm_command_not_gated_by_norad_ids(self):
        """!satpass DM replies about any bird in TLE cache,
        regardless of broadcast norad_ids being empty."""
        # This test verifies the DM command path doesn't use the
        # broadcast norad_ids filter. The command_norad_ids is separate.
        from meshai.adapter_config.defaults import REGISTRY
        # Verify command_norad_ids is a separate key
        assert ("satpass", "command_norad_ids") in REGISTRY
        assert ("satpass", "norad_ids") in REGISTRY
        assert REGISTRY[("satpass", "command_norad_ids")]["default"] == [25544]
        assert REGISTRY[("satpass", "norad_ids")]["default"] == []


# ══════════════════════════════════════════════════════════════════════
# 2. RATE CAP — DM-path isolation check
# ══════════════════════════════════════════════════════════════════════

class TestRateCap:
    def test_cap_does_not_apply_to_dm_path(self):
        """Rate cap is broadcast-only, never affects DM replies."""
        # The DM command (satpass_cmd.py) does not call gate_consolidated_pass,
        # it uses pass_predictor directly. Verify they're separate paths.
        import inspect
        from meshai.commands import satpass_cmd
        src = inspect.getsource(satpass_cmd)
        assert "max_broadcasts_per_hour" not in src
        assert "_check_rate_cap" not in src


# ══════════════════════════════════════════════════════════════════════
# 3. DRY-RUN MODE — REGISTRY default
# ══════════════════════════════════════════════════════════════════════

class TestDryRun:
    def test_dry_run_default_is_true(self):
        """REGISTRY default for dry_run must be True."""
        from meshai.adapter_config.defaults import REGISTRY
        spec = REGISTRY[("satpass", "dry_run")]
        assert spec["default"] is True
        assert spec["type"] == "bool"


# ══════════════════════════════════════════════════════════════════════
# 4. ELEVATION DEFAULT
# ══════════════════════════════════════════════════════════════════════

class TestElevationDefault:
    """min_elevation default in REGISTRY must be 30."""

    def test_registry_min_elevation_default_30(self):
        from meshai.adapter_config.defaults import REGISTRY
        spec = REGISTRY[("satpass", "min_elevation")]
        assert spec["default"] == 30


# ══════════════════════════════════════════════════════════════════════
# 5. BROADCAST WIRE FORMAT
# ══════════════════════════════════════════════════════════════════════

class TestBroadcastWireFormat:
    """Single-line format, numeric degrees, byte budget."""

    def test_exact_single_line_example(self):
        """Formatter produces the exact single-line target format."""
        from meshai.env.satellite.pass_format import format_pass

        # ISS, SW->NE, 6-minute window, rises 8:38 PM MDT, max 55 deg.
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/Boise")
        aos_dt = datetime(2026, 6, 12, 20, 38, 0, tzinfo=tz)
        los_dt = datetime(2026, 6, 12, 20, 44, 0, tzinfo=tz)
        aos_epoch = int(aos_dt.timestamp())
        los_epoch = int(los_dt.timestamp())

        wire = format_pass(
            sat_name="ISS", norad_id=25544, max_el=55.0,
            aos_epoch=aos_epoch, los_epoch=los_epoch,
            aos_compass="SW", los_compass="NE",
            broadcast=True,
        )

        # Single line: absolute local rise time, numeric max elevation, and a
        # date qualifier ("Fri Jun 12" — the fixed date is always in the past).
        assert "\n" not in wire
        assert wire == (
            "\U0001F6F0️ ISS 8:38 PM MDT Fri Jun 12, "
            "max 55° SW→NE (6 min)"
        )

    def test_broadcast_byte_length_under_budget(self):
        """Broadcast wire message must be <= 120 bytes UTF-8."""
        from meshai.env.satellite.pass_format import format_pass
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("America/Boise")

        # Test with longest plausible satellite name
        test_cases = [
            ("ISS", 75.0, "SW", "NE"),
            ("NOAA 18", 45.0, "SE", "NW"),
            ("AMATEUR-SAT-1", 35.0, "S", "N"),
            ("SO-50", 88.0, "NE", "SW"),
        ]

        for sat_name, max_el, aos_c, los_c in test_cases:
            aos_dt = datetime(2026, 6, 12, 20, 38, 0, tzinfo=tz)
            los_dt = datetime(2026, 6, 12, 20, 44, 0, tzinfo=tz)
            wire = format_pass(
                sat_name=sat_name, max_el=max_el,
                aos_epoch=int(aos_dt.timestamp()),
                los_epoch=int(los_dt.timestamp()),
                aos_compass=aos_c, los_compass=los_c,
                broadcast=True,
            )
            byte_len = len(wire.encode("utf-8"))
            assert byte_len <= 120, (
                f"Broadcast for {sat_name} is {byte_len} bytes, exceeds 120: "
                f"{wire!r}"
            )

    def test_dm_format_has_exact_degrees(self):
        """DM format must include exact degree number, not bucket."""
        from meshai.env.satellite.pass_format import format_pass
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("America/Boise")
        aos_dt = datetime(2026, 6, 12, 20, 38, 0, tzinfo=tz)
        los_dt = datetime(2026, 6, 12, 20, 44, 0, tzinfo=tz)

        wire = format_pass(
            sat_name="ISS", max_el=75.3,
            aos_epoch=int(aos_dt.timestamp()),
            los_epoch=int(los_dt.timestamp()),
            aos_compass="SW", los_compass="NE",
            broadcast=False,
        )

        assert "max 75°" in wire
        assert "overhead" not in wire
        assert "high pass" not in wire

    def test_dm_format_single_line(self):
        """DM format is a single line."""
        from meshai.env.satellite.pass_format import format_pass
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("America/Boise")
        aos_dt = datetime(2026, 6, 12, 20, 38, 0, tzinfo=tz)
        los_dt = datetime(2026, 6, 12, 20, 44, 0, tzinfo=tz)

        wire = format_pass(
            sat_name="ISS", max_el=75.0,
            aos_epoch=int(aos_dt.timestamp()),
            los_epoch=int(los_dt.timestamp()),
            aos_compass="SW", los_compass="NE",
            broadcast=False,
        )

        assert "\n" not in wire


# ══════════════════════════════════════════════════════════════════════
# REGISTRY completeness
# ══════════════════════════════════════════════════════════════════════

class TestRegistryKeys:
    """Verify all new adapter_config keys exist."""

    def test_max_broadcasts_per_hour_in_registry(self):
        from meshai.adapter_config.defaults import REGISTRY
        spec = REGISTRY[("satpass", "max_broadcasts_per_hour")]
        assert spec["default"] == 4
        assert spec["type"] == "int"

    def test_dry_run_in_registry(self):
        from meshai.adapter_config.defaults import REGISTRY
        spec = REGISTRY[("satpass", "dry_run")]
        assert spec["default"] is True
        assert spec["type"] == "bool"

    def test_norad_ids_description_says_opt_in(self):
        from meshai.adapter_config.defaults import REGISTRY
        spec = REGISTRY[("satpass", "norad_ids")]
        assert "broadcast nothing" in spec["description"].lower() or \
               "opt-in" in spec["description"].lower()


# ══════════════════════════════════════════════════════════════════════
# 6. CLEAN FORMAT: short names, degrees, compass collapse, friendly obs
# ══════════════════════════════════════════════════════════════════════

class TestCleanBroadcastFormat:
    """The format-cleanup rules (short names, degrees, compass, observers)."""

    @staticmethod
    def _wire(**kw):
        from meshai.env.satellite.pass_format import format_pass
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/Boise")
        base = dict(
            sat_name="X", max_el=50.0,
            aos_epoch=int(datetime(2026, 6, 12, 20, 38, 0, tzinfo=tz).timestamp()),
            los_epoch=int(datetime(2026, 6, 12, 20, 44, 0, tzinfo=tz).timestamp()),
            aos_compass="S", los_compass="N", broadcast=True,
        )
        base.update(kw)
        return format_pass(**base)

    def test_short_name_ao91_from_norad(self):
        wire = self._wire(norad_id=43017, sat_name="RADFXSAT (FOX-1B)")
        assert "AO-91" in wire
        assert "RADFXSAT" not in wire
        assert "FOX-1B" not in wire

    def test_short_name_iss_and_ao27(self):
        assert "ISS" in self._wire(norad_id=25544, sat_name="ISS (ZARYA)")
        assert "AO-27" in self._wire(norad_id=22825, sat_name="EYESAT-1 (AO-27)")

    def test_short_name_substring_fallback(self):
        # No NORAD mapping, but the catalog name is recognizable.
        wire = self._wire(norad_id=99999, sat_name="RADFXSAT (FOX-1B)")
        assert "AO-91" in wire

    def test_unmapped_name_is_cleaned(self):
        # Parenthetical stripped for an unmapped satellite.
        wire = self._wire(norad_id=40000, sat_name="METEOR-M2 (WEATHER)")
        assert "METEOR-M2" in wire
        assert "(WEATHER)" not in wire

    def test_compass_collapse_all_equal(self):
        wire = self._wire(aos_compass="E", peak_compass="E", los_compass="E")
        assert "E→E→E" not in wire
        assert " E " in wire  # a lone collapsed "E"

    def test_compass_collapse_trailing_dup(self):
        wire = self._wire(aos_compass="E", peak_compass="SE", los_compass="SE")
        assert "E→SE" in wire
        assert "E→SE→SE" not in wire

    def test_compass_no_collapse_when_distinct(self):
        wire = self._wire(aos_compass="S", peak_compass="W", los_compass="NW")
        assert "S→W→NW" in wire

    def test_shows_numeric_degrees_not_bucket(self):
        wire = self._wire(max_el=77.0)
        assert "max 77°" in wire
        assert "high pass" not in wire
        assert "overhead" not in wire

    def test_multi_observer_region_shown(self):
        wire = self._wire(entry_observer="Treasure Valley",
                          exit_observer="Magic Valley")
        assert "(Treasure Valley→Magic Valley)" in wire

    def test_single_observer_no_region(self):
        wire = self._wire(entry_observer="Boise", exit_observer="Boise")
        assert "(" not in wire.split("min)")[-1]  # nothing after the (N min)

    def test_coverage_center_region_dropped(self):
        wire = self._wire(entry_observer="coverage_center",
                          exit_observer="Magic Valley")
        assert "coverage_center" not in wire
        assert "Coverage Center" not in wire
        # Also the friendly synthetic label form.
        wire2 = self._wire(entry_observer="Coverage Center",
                           exit_observer="Magic Valley")
        assert "Coverage Center" not in wire2

    def test_golden_line(self):
        # A full golden line matching the target format for a sample pass.
        wire = self._wire(
            norad_id=25544, sat_name="ISS (ZARYA)", max_el=77.0,
            aos_compass="S", peak_compass=None, los_compass="NW",
        )
        assert wire == (
            "\U0001F6F0️ ISS 8:38 PM MDT Fri Jun 12, "
            "max 77° S→NW (6 min)"
        )
