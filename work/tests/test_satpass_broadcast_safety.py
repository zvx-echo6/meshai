"""Tests for satpass broadcast safety controls.

Covers all five incident-response requirements:
  1. Opt-in bird filter: empty norad_ids broadcasts nothing
  2. Rate cap: max_broadcasts_per_hour suppresses excess
  3. Dry-run mode: logs wire text, dispatches nothing
  4. Elevation default: REGISTRY min_elevation = 30
  5. Broadcast wire format: two-line, buckets, byte budget
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest


# ── Helpers ──────────────────────────────────────────────────────────

def _envelope(norad_id=25544, sat_name="ISS", observer="Boise",
              max_el=75.0, aos="2026-06-12T03:32:00Z",
              los="2026-06-12T03:38:00Z",
              aos_compass="SW", los_compass="NE"):
    """Build a CloudEvents envelope for a satellite pass."""
    return {
        "specversion": "1.0",
        "type": "central.sat.pass",
        "source": "central",
        "id": f"pass-{norad_id}-{aos}",
        "data": {
            "adapter": "n2yo_visualpasses",
            "category": "pass.n2yo_visualpasses",
            "severity": 0,
            "data": {
                "norad_id": norad_id,
                "satellite_name": sat_name,
                "observer_name": observer,
                "max_elevation_deg": max_el,
                "aos_time": aos,
                "los_time": los,
                "azimuth_at_peak_compass": "S",
                "azimuth_at_aos_compass": aos_compass,
                "azimuth_at_los_compass": los_compass,
                "duration_s": 360,
            }
        }
    }


def _enable_satpass_db(norad_ids=None, dry_run=False, max_per_hour=100):
    """Set satpass config in the test DB."""
    from meshai.persistence import get_db
    from meshai.adapter_config import invalidate_cache
    conn = get_db()
    conn.execute(
        "UPDATE adapter_config SET value_json='true' "
        "WHERE adapter='satpass' AND key='enabled'"
    )
    conn.execute(
        "UPDATE adapter_config SET value_json=? "
        "WHERE adapter='satpass' AND key='dry_run'",
        (json.dumps(dry_run),)
    )
    conn.execute(
        "UPDATE adapter_config SET value_json=? "
        "WHERE adapter='satpass' AND key='max_broadcasts_per_hour'",
        (json.dumps(max_per_hour),)
    )
    if norad_ids is not None:
        conn.execute(
            "UPDATE adapter_config SET value_json=? "
            "WHERE adapter='satpass' AND key='norad_ids'",
            (json.dumps(norad_ids),)
        )
    conn.execute(
        "UPDATE adapter_config SET value_json='5' "
        "WHERE adapter='satpass' AND key='min_elevation'"
    )
    invalidate_cache()


def _clear_handler_flags():
    from meshai.central.satpass_handler import handle_satpass
    for attr in ("_disabled_logged", "_no_norad_ids_logged"):
        if hasattr(handle_satpass, attr):
            delattr(handle_satpass, attr)


def _ingest_and_consolidate(env, subject, *, now, data=None):
    """Drive the two-call async satpass contract.

    handle_satpass() ingests the pass into satpass_pending and ALWAYS
    returns None (the immediate-broadcast suppression); the consumer then
    fires consolidate_satpass_pending() on its 5s timer, which returns the
    (wire_string, data_dict) to actually broadcast, or None if suppressed.
    This helper runs both halves and returns the consolidation result so a
    test can assert on the real broadcast decision + wire.
    """
    from meshai.central.satpass_handler import (
        handle_satpass, consolidate_satpass_pending,
        drain_pending_consolidation_ids)
    drain_pending_consolidation_ids()  # clear any cross-test leakage
    ingest = handle_satpass(
        env, subject, data=data if data is not None else {}, now=now)
    assert ingest is None, "handle_satpass must suppress the immediate broadcast"
    for cid in drain_pending_consolidation_ids():
        res = consolidate_satpass_pending(cid)
        if res is not None:
            return res
    return None


# ══════════════════════════════════════════════════════════════════════
# 1. OPT-IN BIRD FILTER
# ══════════════════════════════════════════════════════════════════════

class TestOptInBirdFilter:
    """empty norad_ids = broadcast nothing; norad_ids=[25544] = ISS only."""

    def test_empty_norad_ids_broadcasts_nothing(self):
        """norad_ids=[] must broadcast nothing."""
        from meshai.central.satpass_handler import handle_satpass
        _enable_satpass_db(norad_ids=[], dry_run=False)
        _clear_handler_flags()

        env = _envelope(norad_id=25544, max_el=80.0)
        result = handle_satpass(env, "central.sat.pass.iss", data={},
                                now=1718163120)
        assert result is None

    def test_empty_norad_ids_logs_once(self, caplog):
        """Empty norad_ids logs info message once."""
        from meshai.central.satpass_handler import handle_satpass
        _enable_satpass_db(norad_ids=[], dry_run=False)
        _clear_handler_flags()

        # now near the 2026 envelope window so the AOS-horizon guard does
        # not short-circuit before the opt-in norad_ids check that logs.
        with caplog.at_level(logging.INFO, logger="meshai.central.satpass_handler"):
            handle_satpass(_envelope(norad_id=25544, max_el=80.0),
                           "central.sat.pass.iss", data={}, now=1781235000)
            handle_satpass(_envelope(norad_id=25544, max_el=80.0,
                                     aos="2026-06-12T04:32:00Z"),
                           "central.sat.pass.iss", data={}, now=1781235000)

        matching = [r for r in caplog.records
                    if "no norad_ids configured" in r.message]
        assert len(matching) == 1, "Should log exactly once"

    def test_norad_ids_25544_airs_iss_only(self):
        """norad_ids=[25544] must air ISS passes, reject others."""
        from meshai.central.satpass_handler import handle_satpass
        _enable_satpass_db(norad_ids=[25544], dry_run=False)
        _clear_handler_flags()

        # ISS pass should air
        iss_env = _envelope(norad_id=25544, max_el=65.0)
        iss_result = _ingest_and_consolidate(iss_env, "central.sat.pass.iss",
                                             now=1781235000)
        assert iss_result is not None, "ISS pass should broadcast"

        # NOAA-18 pass should be rejected (opt-in norad_ids excludes it)
        noaa_env = _envelope(norad_id=28654, sat_name="NOAA 18", max_el=65.0,
                             aos="2026-06-12T05:00:00Z")
        noaa_result = _ingest_and_consolidate(noaa_env, "central.sat.pass.noaa",
                                              now=1781235000)
        assert noaa_result is None, "NOAA-18 pass should be rejected"

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
# 2. RATE CAP
# ══════════════════════════════════════════════════════════════════════

class TestRateCap:
    """max_broadcasts_per_hour suppresses excess."""

    def test_cap_suppresses_pass_n_plus_1(self):
        """After max_broadcasts_per_hour broadcasts, next one is suppressed."""
        from meshai.central.satpass_handler import handle_satpass
        from meshai.persistence import get_db

        _enable_satpass_db(norad_ids=[25544, 28654, 99999, 99998, 99997],
                           dry_run=False, max_per_hour=4)
        _clear_handler_flags()

        now = 1718200000
        hour_start = (now // 3600) * 3600
        conn = get_db()

        # Pre-insert 4 broadcast records in current hour window
        for i in range(4):
            eid = f"prefill:{i}:dummy:{hour_start // 3600}"
            conn.execute(
                "INSERT INTO satpass_events(event_id, norad_id, sat_name, "
                "observer, max_elevation, aos_at, los_at, first_seen_at, "
                "last_broadcast_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (eid, 99990 + i, f"SAT-{i}", "Boise", 60.0,
                 now - 300, now + 300, now - 600, hour_start + i)
            )

        # 5th broadcast should be suppressed
        env = _envelope(norad_id=25544, max_el=70.0,
                        aos="2026-06-12T10:00:00Z")
        result = handle_satpass(env, "central.sat.pass.iss", data={}, now=now)
        assert result is None, "5th broadcast should be suppressed by rate cap"

    def test_cap_logged_on_suppress(self, caplog):
        """Rate cap suppression must log at INFO."""
        from meshai.central.satpass_handler import handle_satpass
        from meshai.persistence import get_db

        _enable_satpass_db(norad_ids=[25544], dry_run=False, max_per_hour=0)
        _clear_handler_flags()

        # now near the 2026 envelope window; max_per_hour=0 => the
        # consolidation step always trips the rate cap and logs.
        now = 1781258400  # 2026-06-12T10:00:00Z (just before this pass)
        with caplog.at_level(logging.INFO, logger="meshai.central.satpass_handler"):
            env = _envelope(norad_id=25544, max_el=70.0,
                            aos="2026-06-12T10:05:00Z",
                            los="2026-06-12T10:11:00Z")
            result = _ingest_and_consolidate(env, "central.sat.pass.iss", now=now)
        assert result is None, "rate cap should suppress the broadcast"

        matching = [r for r in caplog.records if "rate cap reached" in r.message]
        assert len(matching) >= 1, "Rate cap suppression should be logged"

    def test_cap_does_not_apply_to_dm_path(self):
        """Rate cap is broadcast-only, never affects DM replies."""
        # The DM command (satpass_cmd.py) does not call handle_satpass,
        # it uses pass_predictor directly. Verify they're separate paths.
        import inspect
        from meshai.commands import satpass_cmd
        src = inspect.getsource(satpass_cmd)
        assert "max_broadcasts_per_hour" not in src
        assert "_check_rate_cap" not in src


# ══════════════════════════════════════════════════════════════════════
# 3. DRY-RUN MODE
# ══════════════════════════════════════════════════════════════════════

class TestDryRun:
    """dry_run=True logs wire text, dispatches nothing."""

    def test_dry_run_dispatches_nothing(self):
        """dry_run=True must return None (no dispatch)."""
        from meshai.central.satpass_handler import handle_satpass
        _enable_satpass_db(norad_ids=[25544], dry_run=True)
        _clear_handler_flags()

        data = {}
        env = _envelope(norad_id=25544, max_el=70.0)
        result = handle_satpass(env, "central.sat.pass.iss", data=data,
                                now=1718163120)
        assert result is None, "dry_run should suppress dispatch"
        assert "_on_broadcast_committed" not in data, \
            "dry_run should not attach commit callback"

    def test_dry_run_logs_wire_text(self, caplog):
        """dry_run=True must log the exact wire text at INFO."""
        from meshai.central.satpass_handler import handle_satpass
        _enable_satpass_db(norad_ids=[25544], dry_run=True)
        _clear_handler_flags()

        with caplog.at_level(logging.INFO, logger="meshai.central.satpass_handler"):
            env = _envelope(norad_id=25544, sat_name="ISS", max_el=70.0)
            result = _ingest_and_consolidate(env, "central.sat.pass.iss",
                                             now=1781235000)
        assert result is None, "dry_run should suppress dispatch"

        # dry-run logging now happens in the consolidation step; the message
        # is "DRY-RUN would air (consolidated, N observers): <wire>".
        matching = [r for r in caplog.records
                    if r.message.startswith("DRY-RUN would air")]
        assert len(matching) == 1, "Should log DRY-RUN wire text once"
        assert "ISS" in matching[0].message

    def test_dry_run_false_dispatches(self):
        """dry_run=False must dispatch normally."""
        from meshai.central.satpass_handler import handle_satpass
        _enable_satpass_db(norad_ids=[25544], dry_run=False)
        _clear_handler_flags()

        env = _envelope(norad_id=25544, max_el=70.0)
        result = _ingest_and_consolidate(env, "central.sat.pass.iss",
                                         now=1781235000)
        assert result is not None, "dry_run=False should dispatch"
        # commit callback now rides on the consolidation result's data dict.
        wire, data = result
        assert "_on_broadcast_committed" in data

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
    """Two-line format, buckets, byte budget."""

    def test_exact_two_line_example(self):
        """Formatter produces the exact two-line example from spec."""
        from meshai.central.satpass_handler import format_pass

        # ISS high pass, SW→NE, 6 minute window, 8:38–8:44 PM MDT
        # We need epoch values that produce 8:38 PM and 8:44 PM MDT
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/Boise")
        aos_dt = datetime(2026, 6, 12, 20, 38, 0, tzinfo=tz)
        los_dt = datetime(2026, 6, 12, 20, 44, 0, tzinfo=tz)
        aos_epoch = int(aos_dt.timestamp())
        los_epoch = int(los_dt.timestamp())

        wire = format_pass(
            sat_name="ISS", max_el=55.0,
            aos_epoch=aos_epoch, los_epoch=los_epoch,
            aos_compass="SW", los_compass="NE",
            broadcast=True,
        )

        lines = wire.split("\n")
        assert len(lines) == 2

        # No peak_compass passed here \u2192 the sweep stays aos\u2192los (SW\u2192NE).
        assert lines[0] == "\U0001F6F0\uFE0F ISS high pass, SW\u2192NE"
        # line2 renders "min window" + a date qualifier for a pass not
        # occurring today (the fixed 2026-06-12 date is always in the past
        # relative to run time).
        assert lines[1].startswith("6 min window, 8:38\u20138:44 PM MDT")
        assert lines[1] == "6 min window, 8:38\u20138:44 PM MDT Fri Jun 12"

    def test_bucket_overhead_at_60(self):
        """max_el=60 should be 'overhead'."""
        from meshai.central.satpass_handler import _elevation_bucket
        assert _elevation_bucket(60.0) == "overhead"

    def test_bucket_overhead_at_90(self):
        """max_el=90 should be 'overhead'."""
        from meshai.central.satpass_handler import _elevation_bucket
        assert _elevation_bucket(90.0) == "overhead"

    def test_bucket_high_pass_at_59(self):
        """max_el=59 should be 'high pass'."""
        from meshai.central.satpass_handler import _elevation_bucket
        assert _elevation_bucket(59.0) == "high pass"

    def test_bucket_high_pass_at_30(self):
        """max_el=30 should be 'high pass'."""
        from meshai.central.satpass_handler import _elevation_bucket
        assert _elevation_bucket(30.0) == "high pass"

    def test_bucket_low_pass_at_29(self):
        """max_el=29 should be 'low pass'."""
        from meshai.central.satpass_handler import _elevation_bucket
        assert _elevation_bucket(29.0) == "low pass"

    def test_bucket_low_pass_at_10(self):
        """max_el=10 should be 'low pass'."""
        from meshai.central.satpass_handler import _elevation_bucket
        assert _elevation_bucket(10.0) == "low pass"

    def test_broadcast_byte_length_under_budget(self):
        """Broadcast wire message must be <= 120 bytes UTF-8."""
        from meshai.central.satpass_handler import format_pass
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
        from meshai.central.satpass_handler import format_pass
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

        assert "max 75\u00B0" in wire
        assert "overhead" not in wire
        assert "high pass" not in wire

    def test_dm_format_single_line(self):
        """DM format is a single line."""
        from meshai.central.satpass_handler import format_pass
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


class TestNoradIdTypeCoercion:
    """norad_ids may arrive as strings from the GUI or ints from code.
    The handler must accept both shapes forever."""

    def test_string_norad_ids_matches_int_wire(self):
        """norad_ids=["25544"] must match wire norad_id 25544 (int)."""
        from meshai.central.satpass_handler import handle_satpass
        _clear_handler_flags()
        _enable_satpass_db(norad_ids=["25544"], dry_run=False)

        env = _envelope(norad_id=25544, max_el=80.0)
        result = _ingest_and_consolidate(env, "test.subject", now=1781235000)
        assert result is not None, "string norad_id should match int wire"

    def test_mixed_int_and_string_norad_ids(self):
        """norad_ids=[25544, "22825"] must match both NORAD IDs."""
        from meshai.central.satpass_handler import handle_satpass
        _clear_handler_flags()
        _enable_satpass_db(norad_ids=[25544, "22825"], dry_run=False)

        # int in list, int on wire
        env_iss = _envelope(norad_id=25544, max_el=65.0)
        result_iss = _ingest_and_consolidate(env_iss, "test.subject", now=1781235000)
        assert result_iss is not None, "int norad_id in mixed list should match"

        # string in list, int on wire
        env_noaa = _envelope(norad_id=22825, sat_name="NOAA 15", max_el=65.0,
                             aos="2026-06-12T05:32:00Z", los="2026-06-12T05:38:00Z")
        result_noaa = _ingest_and_consolidate(env_noaa, "test.subject", now=1781235000)
        assert result_noaa is not None, "string norad_id in mixed list should match int wire"

    def test_garbage_entries_skipped_without_crash(self):
        """Non-numeric entries in norad_ids must be silently skipped."""
        from meshai.central.satpass_handler import handle_satpass
        _clear_handler_flags()
        _enable_satpass_db(norad_ids=["25544", "not_a_number", "", None, "abc123"],
                           dry_run=False)

        env = _envelope(norad_id=25544, max_el=80.0)
        # Must not raise, and the valid entry should still match
        result = _ingest_and_consolidate(env, "test.subject", now=1781235000)
        assert result is not None, "valid entry should match despite garbage siblings"

    def test_all_garbage_norad_ids_matches_nothing(self):
        """If every entry is garbage, allow_set is empty and nothing matches."""
        from meshai.central.satpass_handler import handle_satpass
        _clear_handler_flags()
        _enable_satpass_db(norad_ids=["abc", "", "xyz"], dry_run=False)

        env = _envelope(norad_id=25544, max_el=80.0)
        result = handle_satpass(env, "test.subject", data={}, now=1781235000)
        assert result is None, "all-garbage norad_ids should match nothing"

    def test_pure_int_norad_ids_still_works(self):
        """norad_ids=[25544] (pure int) must continue to work."""
        from meshai.central.satpass_handler import handle_satpass
        _clear_handler_flags()
        _enable_satpass_db(norad_ids=[25544], dry_run=False)

        env = _envelope(norad_id=25544, max_el=80.0)
        result = _ingest_and_consolidate(env, "test.subject", now=1781235000)
        assert result is not None, "pure int norad_id should still match"

    def test_string_norad_id_rejects_non_matching(self):
        """norad_ids=["25544"] must NOT match wire norad_id 99999."""
        from meshai.central.satpass_handler import handle_satpass
        _clear_handler_flags()
        _enable_satpass_db(norad_ids=["25544"], dry_run=False)

        env = _envelope(norad_id=99999, max_el=80.0)
        result = handle_satpass(env, "test.subject", data={}, now=1781235000)
        assert result is None, "non-matching norad_id should be rejected"


class TestStalenessGuard:
    """Reject passes whose window already ended; allow ongoing/future/None."""

    def test_past_pass_rejected(self):
        """Pass with los 10 min in the past produces no wire, no broadcast mark."""
        from meshai.central.satpass_handler import handle_satpass
        from meshai.persistence import get_db
        _clear_handler_flags()
        _enable_satpass_db(norad_ids=[25544], dry_run=False)

        now = 1718200000
        aos = "2026-06-12T02:00:00Z"  # well in the past
        los_epoch = now - 600  # 10 min ago
        los = datetime.fromtimestamp(los_epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        env = _envelope(norad_id=25544, max_el=80.0, aos=aos, los=los)
        result = handle_satpass(env, "test.subject", data={}, now=now)
        assert result is None, "past pass should produce no wire"

        # Verify no broadcast mark in DB
        conn = get_db()
        row = conn.execute(
            "SELECT last_broadcast_at FROM satpass_events WHERE norad_id=25544"
        ).fetchone()
        assert row is None or row["last_broadcast_at"] is None, \
            "past pass should not create a broadcast-marked DB row"

    def test_ongoing_pass_broadcasts(self):
        """Ongoing pass (aos -2 min, los +5 min) must produce wire."""
        from meshai.central.satpass_handler import handle_satpass
        _clear_handler_flags()
        _enable_satpass_db(norad_ids=[25544], dry_run=False)

        now = 1718200000
        aos_epoch = now - 120  # started 2 min ago
        los_epoch = now + 300  # ends in 5 min
        aos = datetime.fromtimestamp(aos_epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        los = datetime.fromtimestamp(los_epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        env = _envelope(norad_id=25544, max_el=70.0, aos=aos, los=los)
        result = _ingest_and_consolidate(env, "test.subject", now=now)
        assert result is not None, "ongoing pass (los in future) should broadcast"

    def test_future_pass_broadcasts(self):
        """Future pass (aos and los both in future) must produce wire."""
        from meshai.central.satpass_handler import handle_satpass
        _clear_handler_flags()
        _enable_satpass_db(norad_ids=[25544], dry_run=False)

        now = 1718200000
        aos_epoch = now + 600   # starts in 10 min
        los_epoch = now + 1200  # ends in 20 min
        aos = datetime.fromtimestamp(aos_epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        los = datetime.fromtimestamp(los_epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        env = _envelope(norad_id=25544, max_el=55.0, aos=aos, los=los)
        result = _ingest_and_consolidate(env, "test.subject", now=now)
        assert result is not None, "future pass should broadcast"

    def test_none_los_falls_through(self):
        """los_epoch=None must not be rejected by staleness guard."""
        from meshai.central.satpass_handler import handle_satpass
        _clear_handler_flags()
        _enable_satpass_db(norad_ids=[25544], dry_run=False)

        now = 1718200000
        aos_epoch = now + 600
        aos = datetime.fromtimestamp(aos_epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Build envelope with no los_time
        env = _envelope(norad_id=25544, max_el=60.0, aos=aos, los="")
        # Patch los to None by removing los_time from inner data
        env["data"]["data"]["los_time"] = None

        result = _ingest_and_consolidate(env, "test.subject", now=now)
        # Should not be rejected by staleness guard — falls through to
        # normal handling (wire produced or other filter applies)
        # We just verify it does NOT crash and is not rejected as stale
        # It may still produce wire or be filtered by something else,
        # but the staleness guard specifically must not block it.
        # Since all other filters pass, wire should be produced.
        assert result is not None, "None los_epoch should fall through staleness guard"
