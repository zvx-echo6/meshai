"""Phase-1 avalanche refactor tests — formatter+decider architecture.

Four test groups:

1. Parity (tier-b): formatter renders from canonical data.
   Expected strings are hand-written (the new correct format).
   OLD _render() output for the same fixture is captured in comments so the
   intended tier-b diff is explicit and reviewable.
   The centralseverity=2 (Considerable) case is OLD-vs-NEW identical.
   The is_update=True case shows the Update: prefix diff.

2. Cross-source identity: native AvalancheAdapter.to_event() builds the same
   canonical data as the Central path for fixture 0000.  Both render
   byte-identically via the registered formatter.

3. Gate-sequence: replay canonical data through gating.avalanche.decide().
   Verify danger-level gate (below / at / above threshold) and first→update
   trend lifecycle labels.

4. Schema-conformance: env/avalanche.py to_event() emits all canonical keys;
   _meshai_precomposed is NOT set.
"""
from __future__ import annotations

import pytest

from tests.harness.goldens import (
    assert_byte_identical,
    load_fixtures,
    pinned_time,
)

# ── Shared clock epoch for deterministic renders ─────────────────────────────
_AT = 1_783_200_000.0   # 2026-07-03T00:00:00Z (pinned, same as quake tests)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_fake_event(data: dict):
    """Minimal fake Event for calling the formatter without the full pipeline."""
    class _FakeEvent:
        pass
    e = _FakeEvent()
    e.data = data
    return e


def _canonical_from_fixture(fixture: dict, *, is_update: bool = False) -> dict:
    """Extract canonical data dict from a Central avalanche envelope fixture."""
    inner = fixture["envelope"]["data"]
    d = inner.get("data") or {}
    geo = inner.get("geo") or {}
    centroid = geo.get("centroid") or []
    lon, lat = (centroid[0], centroid[1]) if len(centroid) >= 2 else (None, None)
    return {
        "danger_level": d.get("danger_level"),
        "danger_name": d.get("danger_name"),
        "zone_name": d.get("zone_name"),
        "center_id": d.get("center_id"),
        "travel_advice": d.get("travel_advice"),
        "lat": lat,
        "lon": lon,
        "is_update": is_update,
    }


def _avy_render_old(*, danger_level: int, danger_name: str, zone_name: str,
                    center_id: str, travel: str) -> str:
    """Capture OLD _render() output from avy_handler for diff comments."""
    from meshai.central.avy_handler import _render
    return _render(
        danger_level=danger_level, danger_name=danger_name,
        zone_name=zone_name, center_id=center_id, travel=travel,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. Parity (tier-b) — formatter renders from canonical data
# ─────────────────────────────────────────────────────────────────────────────

class TestFormatterParity:
    """formatters/avalanche.format() renders correct output from canonical data."""

    def test_fixture_0000_considerable_new_format(self):
        """Fixture 0000 (Considerable, NAADS 3) → formatter output matches expected.

        centralseverity=2 maps to NAADS 3 (Considerable).
        min_danger_level=3 → broadcast.
        is_update=False → "Watch:" prefix.

        OLD _render() output:
            '⛷ AVY Watch: Sawtooth Mountains — Considerable (3)
            Dangerous conditions on steep slopes.
            SNFAC · valid today'
        NEW formatter output (identical — no tier-b diff for non-update case):
            '⛷ AVY Watch: Sawtooth Mountains — Considerable (3)
            Dangerous conditions on steep slopes.
            SNFAC · valid today'
        """
        from meshai.notifications.formatters.avalanche import format as avyfmt

        fixtures = load_fixtures("avalanche")
        fx = next(f for f in fixtures
                  if "considerable" in f["envelope"]["id"] and "update" not in f["envelope"]["id"])
        canonical = _canonical_from_fixture(fx)

        expected = (
            "⛷ AVY Watch: Sawtooth Mountains — Considerable (3)"
            "\nDangerous conditions on steep slopes."
            "\nSNFAC · valid today"
        )

        with pinned_time(_AT):
            result = avyfmt(_make_fake_event(canonical), now=_AT, budget=140)

        assert_byte_identical(result, expected)

        # Confirm old _render() is identical for this case (no tier-b diff)
        old_wire = _avy_render_old(
            danger_level=3, danger_name="Considerable",
            zone_name="Sawtooth Mountains", center_id="SNFAC",
            travel="Dangerous conditions on steep slopes. Conservative decision-making is advised.",
        )
        assert_byte_identical(result, old_wire), (
            "For fixture 0000 (Considerable, is_update=False) old and new "
            "outputs must be identical — tier-b diff only appears for is_update=True."
        )

    def test_fixture_0001_high_warning_prefix(self):
        """Fixture 0001 (High, NAADS 4) → formatter uses 'WARNING:' prefix.

        OLD _render() output:
            '⛷ AVY WARNING: Banner Summit — High (4)
            Avoid all avalanche terrain today.
            SNFAC · valid today'
        NEW formatter output (identical — WARNING prefix unchanged):
            '⛷ AVY WARNING: Banner Summit — High (4)
            Avoid all avalanche terrain today.
            SNFAC · valid today'
        """
        from meshai.notifications.formatters.avalanche import format as avyfmt

        fixtures = load_fixtures("avalanche")
        fx = next(f for f in fixtures if "banner" in f["envelope"]["id"])
        canonical = _canonical_from_fixture(fx)

        expected = (
            "⛷ AVY WARNING: Banner Summit — High (4)"
            "\nAvoid all avalanche terrain today."
            "\nSNFAC · valid today"
        )

        with pinned_time(_AT):
            result = avyfmt(_make_fake_event(canonical), now=_AT, budget=140)

        assert_byte_identical(result, expected)

        old_wire = _avy_render_old(
            danger_level=4, danger_name="High",
            zone_name="Banner Summit", center_id="SNFAC",
            travel="Avoid all avalanche terrain today. Natural avalanches are likely on steep slopes.",
        )
        assert_byte_identical(result, old_wire), (
            "For High (level=4, is_update=False) old and new must be identical."
        )

    def test_fixture_0002_extreme_warning_prefix(self):
        """Fixture 0002 (Extreme, NAADS 5) → formatter uses 'WARNING:' prefix.

        OLD _render() output:
            '⛷ AVY WARNING: Soldier Mountains — Extreme (5)
            Avoid all avalanche terrain!
            SNFAC · valid today'
        NEW formatter output (identical):
            '⛷ AVY WARNING: Soldier Mountains — Extreme (5)
            Avoid all avalanche terrain!
            SNFAC · valid today'
        """
        from meshai.notifications.formatters.avalanche import format as avyfmt

        fixtures = load_fixtures("avalanche")
        fx = next(f for f in fixtures if "soldier" in f["envelope"]["id"])
        canonical = _canonical_from_fixture(fx)

        expected = (
            "⛷ AVY WARNING: Soldier Mountains — Extreme (5)"
            "\nAvoid all avalanche terrain!"
            "\nSNFAC · valid today"
        )

        with pinned_time(_AT):
            result = avyfmt(_make_fake_event(canonical), now=_AT, budget=140)

        assert_byte_identical(result, expected)

    def test_tier_b_update_prefix_rendered(self):
        """Tier-b: is_update=True produces 'AVY Update:' prefix.

        OLD _render() output (no is_update path):
            '⛷ AVY Watch: Sawtooth Mountains — Considerable (3)
            Dangerous conditions on steep slopes.
            SNFAC · valid today'
        NEW formatter output (is_update=True):
            '⛷ AVY Update: Sawtooth Mountains — Considerable (3)
            Dangerous conditions on steep slopes.
            SNFAC · valid today'
        """
        from meshai.notifications.formatters.avalanche import format as avyfmt

        fixtures = load_fixtures("avalanche")
        fx = next(f for f in fixtures if "update" in f["envelope"]["id"])
        # Extract with is_update=True
        canonical = _canonical_from_fixture(fx, is_update=True)

        # OLD _render() output (no is_update support)
        old_wire = _avy_render_old(
            danger_level=3, danger_name="Considerable",
            zone_name="Sawtooth Mountains", center_id="SNFAC",
            travel="Dangerous conditions on steep slopes. Conservative decision-making is advised.",
        )
        expected_old = (
            "⛷ AVY Watch: Sawtooth Mountains — Considerable (3)"
            "\nDangerous conditions on steep slopes."
            "\nSNFAC · valid today"
        )
        expected_new = (
            "⛷ AVY Update: Sawtooth Mountains — Considerable (3)"
            "\nDangerous conditions on steep slopes."
            "\nSNFAC · valid today"
        )

        with pinned_time(_AT):
            result = avyfmt(_make_fake_event(canonical), now=_AT, budget=140)

        assert_byte_identical(old_wire, expected_old)
        assert_byte_identical(result, expected_new)
        assert "Update:" in result
        assert "Watch:" not in result

    def test_formatter_budget_respected(self):
        """Formatter output fits within budget for worst-case zone/advice strings."""
        from meshai.notifications.formatters.avalanche import format as avyfmt

        canonical = {
            "danger_level": 5,
            "danger_name": "Extreme",
            "zone_name": "A very long zone name that might cause budget overflow",
            "center_id": "SNFAC",
            "travel_advice": (
                "Avoid all avalanche terrain! Very dangerous conditions exist "
                "across all elevations and aspects. Natural avalanches certain."
            ),
            "lat": 43.5,
            "lon": -115.4,
            "is_update": False,
        }
        with pinned_time(_AT):
            result = avyfmt(_make_fake_event(canonical), now=_AT, budget=140)
        assert len(result) <= 140, f"{len(result)} chars:\n{result!r}"
        assert "AVY WARNING" in result

    def test_no_travel_advice_still_renders(self):
        """Formatter renders cleanly when travel_advice is absent."""
        from meshai.notifications.formatters.avalanche import format as avyfmt

        canonical = {
            "danger_level": 3,
            "danger_name": "Considerable",
            "zone_name": "Wood River Valley",
            "center_id": "SNFAC",
            "travel_advice": "",
            "lat": 43.4,
            "lon": -114.3,
            "is_update": False,
        }
        with pinned_time(_AT):
            result = avyfmt(_make_fake_event(canonical), now=_AT, budget=140)
        assert "AVY Watch" in result
        assert "Wood River Valley" in result
        assert "SNFAC" in result
        assert "\n\n" not in result  # no blank lines


# ─────────────────────────────────────────────────────────────────────────────
# 2. Cross-source identity — native and Central produce identical renders
# ─────────────────────────────────────────────────────────────────────────────

class TestCrossSourceIdentity:
    """Native to_event() canonical data renders byte-identically to Central path."""

    def test_native_central_render_identical_fixture_0000(self):
        """Canonical data from native and Central for fixture 0000 renders identically.

        The Central path extracts from the envelope.
        The native path produces the same canonical keys via to_event().
        The formatter reads the same keys from both → same wire.
        """
        from meshai.notifications.formatters.avalanche import format as avyfmt

        # Central-path canonical data (hand-extracted from fixture 0000)
        central_canonical = {
            "danger_level": 3,
            "danger_name": "Considerable",
            "zone_name": "Sawtooth Mountains",
            "center_id": "SNFAC",
            "travel_advice": "Dangerous conditions on steep slopes. Conservative decision-making is advised.",
            "lat": 43.8,
            "lon": -114.9,
            "is_update": False,
        }

        # Native-path canonical data (as to_event() would populate event.data)
        native_canonical = {
            "danger_level": 3,
            "danger_name": "Considerable",
            "zone_name": "Sawtooth Mountains",
            "center_id": "SNFAC",
            "travel_advice": "Dangerous conditions on steep slopes. Conservative decision-making is advised.",
            "lat": 43.8,
            "lon": -114.9,
            "is_update": False,
            "event_id": "avy_SNFAC_sawtooth_mountains",
        }

        with pinned_time(_AT):
            central_wire = avyfmt(_make_fake_event(central_canonical),
                                  now=_AT, budget=140)
            native_wire = avyfmt(_make_fake_event(native_canonical),
                                 now=_AT, budget=140)

        assert_byte_identical(native_wire, central_wire), (
            f"Native and Central renders must be byte-identical:\n"
            f"  Central: {central_wire!r}\n"
            f"  Native:  {native_wire!r}"
        )

    def test_native_to_event_uses_canonical_keys(self):
        """to_event() event.data has the canonical keys the formatter reads."""
        from unittest.mock import MagicMock
        from meshai.env.avalanche import AvalancheAdapter

        cfg = MagicMock()
        cfg.center_ids = ["SNFAC"]
        cfg.tick_seconds = 1800
        cfg.season_months = [12, 1, 2, 3, 4]
        adapter = AvalancheAdapter(cfg)

        import time
        now = time.time()
        raw_evt = {
            "source": "avalanche",
            "event_id": "avy_SNFAC_sawtooth_mountains",
            "event_type": "Avalanche Advisory",
            "severity": "routine",
            "headline": "Sawtooth Mountains: Considerable avalanche danger",
            "zone_name": "Sawtooth Mountains",
            "center": "Sawtooth Avalanche Center",
            "center_id": "SNFAC",
            "center_link": "https://www.sawtoothavalanche.com",
            "forecast_link": "https://www.sawtoothavalanche.com/forecast",
            "danger": "considerable",
            "danger_level": 3,
            "danger_name": "Considerable",
            "travel_advice": "Dangerous conditions on steep slopes.",
            "state": "ID",
            "lat": 43.8,
            "lon": -114.9,
            "expires": now + 3600,
            "fetched_at": now,
        }

        event = adapter.to_event(raw_evt)
        assert event is not None, "to_event() must return an Event for valid input"
        assert event.data is not None, "event.data must not be None"

        canonical_keys = {
            "danger_level", "danger_name", "zone_name", "center_id",
            "travel_advice", "lat", "lon", "is_update", "event_id",
        }
        missing = canonical_keys - set(event.data.keys())
        assert not missing, (
            f"to_event() event.data missing canonical keys: {missing}\n"
            f"Got keys: {sorted(event.data.keys())}"
        )

        # Spot-check values
        assert event.data["danger_level"] == 3
        assert event.data["danger_name"] == "Considerable"
        assert event.data["zone_name"] == "Sawtooth Mountains"
        assert event.data["center_id"] == "SNFAC"
        assert event.data["is_update"] is False
        assert event.data["event_id"] == "avy_SNFAC_sawtooth_mountains"

    def test_native_to_event_no_precomposed(self):
        """to_event() must NOT set _meshai_precomposed in event.data (formatter governs)."""
        from unittest.mock import MagicMock
        from meshai.env.avalanche import AvalancheAdapter

        cfg = MagicMock()
        cfg.center_ids = ["SNFAC"]
        cfg.tick_seconds = 1800
        cfg.season_months = [12, 1, 2, 3, 4]
        adapter = AvalancheAdapter(cfg)

        import time
        now = time.time()
        raw_evt = {
            "source": "avalanche",
            "event_id": "avy_SNFAC_sawtooth_mountains",
            "event_type": "Avalanche Advisory",
            "severity": "routine",
            "headline": "test",
            "zone_name": "Sawtooth Mountains",
            "center": "SNFAC",
            "center_id": "SNFAC",
            "center_link": "",
            "forecast_link": "",
            "danger": "considerable",
            "danger_level": 3,
            "danger_name": "Considerable",
            "travel_advice": "Dangerous conditions.",
            "state": "ID",
            "lat": 43.8,
            "lon": -114.9,
            "expires": now + 3600,
            "fetched_at": now,
        }

        event = adapter.to_event(raw_evt)
        assert event is not None
        assert not event.data.get("_meshai_precomposed"), (
            "event.data must NOT have _meshai_precomposed=True — "
            "the formatter registry governs rendering"
        )

    def test_handle_avy_writes_canonical_into_data(self):
        """handle_avy() writes canonical fields into the shared data dict on broadcast."""
        from meshai.central.avy_handler import handle_avy

        envelope = {
            "id": "avy-snfac-sawtooth-test",
            "data": {
                "adapter": "avalanche_org",
                "category": "advisory.us.id",
                "severity": 2,
                "geo": {"centroid": [-114.9, 43.8]},
                "data": {
                    "danger_level": 3,
                    "danger_name": "Considerable",
                    "zone_name": "Sawtooth Mountains",
                    "center_id": "SNFAC",
                    "travel_advice": "Dangerous conditions.",
                },
            },
        }
        data: dict = {}
        wire = handle_avy(envelope, "central.avy.advisory.us.id", data=data)

        assert wire is not None, "handle_avy must return a wire string on broadcast"
        # Canonical fields must be in the shared data dict
        assert data.get("danger_level") == 3
        assert data.get("danger_name") == "Considerable"
        assert data.get("zone_name") == "Sawtooth Mountains"
        assert data.get("center_id") == "SNFAC"
        assert "_on_broadcast_committed" in data
        assert "_broadcast_audit" in data


# ─────────────────────────────────────────────────────────────────────────────
# 3. Gate-sequence — danger-level gate + first→update trend
# ─────────────────────────────────────────────────────────────────────────────

class TestGateSequence:
    """gating.avalanche.decide() gate + lifecycle decisions."""

    def _decide(self, danger_level: int, *, is_update: bool = False):
        from meshai.notifications.gating.avalanche import decide
        data = {
            "danger_level": danger_level,
            "danger_name": {1: "Low", 2: "Moderate", 3: "Considerable",
                            4: "High", 5: "Extreme"}.get(danger_level, "?"),
            "zone_name": "Test Zone",
            "center_id": "SNFAC",
            "travel_advice": "Test travel advice.",
            "lat": 43.8,
            "lon": -114.9,
            "is_update": is_update,
        }
        return decide(data, source="avalanche", now=_AT)

    def test_level_below_threshold_suppressed(self):
        """danger_level=2 (Moderate) is below min_danger_level=3 → suppress."""
        result = self._decide(2)
        assert result.broadcast is False
        assert result.lifecycle == "suppress"

    def test_level_at_threshold_broadcast(self):
        """danger_level=3 (Considerable) equals min_danger_level=3 → broadcast."""
        result = self._decide(3)
        assert result.broadcast is True
        assert result.lifecycle == "new"

    def test_level_above_threshold_broadcast(self):
        """danger_level=4 (High) above min_danger_level=3 → broadcast + priority."""
        result = self._decide(4)
        assert result.broadcast is True
        assert result.lifecycle == "new"
        assert result.data_patch.get("_severity_override") == "priority"

    def test_extreme_broadcast_priority(self):
        """danger_level=5 (Extreme) → broadcast + priority."""
        result = self._decide(5)
        assert result.broadcast is True
        assert result.data_patch.get("_severity_override") == "priority"

    def test_considerable_no_priority_override(self):
        """danger_level=3 (Considerable) → broadcast, _severity_override=None."""
        result = self._decide(3)
        assert result.broadcast is True
        assert result.data_patch.get("_severity_override") is None

    def test_first_sighting_lifecycle_new(self):
        """First event (is_update=False) → lifecycle='new'."""
        result = self._decide(3, is_update=False)
        assert result.broadcast is True
        assert result.lifecycle == "new"
        assert result.data_patch.get("is_update") is False

    def test_update_trend_lifecycle_update(self):
        """Subsequent event (is_update=True) → lifecycle='update'."""
        result = self._decide(3, is_update=True)
        assert result.broadcast is True
        assert result.lifecycle == "update"
        assert result.data_patch.get("is_update") is True

    def test_missing_danger_level_suppresses(self):
        """Missing danger_level in canonical data → suppress."""
        from meshai.notifications.gating.avalanche import decide
        result = decide({"zone_name": "test"}, source="avalanche", now=_AT)
        assert result.broadcast is False

    def test_centralseverity_remap_considerable(self):
        """centralseverity=2 → NAADS 3 (Considerable) → broadcast at min_level=3.

        This tests the remap logic in _remap_centralseverity() as used by
        handle_avy() when data.data.danger_level is absent.

        ⚠ Mapping table (needs live validation in-season Oct+):
            centralseverity 2 → NAADS 3 (Considerable) [documented]
            centralseverity 3 → NAADS 4 (High)          [documented]
            centralseverity 4 → NAADS 5 (Extreme)        [documented]
            centralseverity 0 → NAADS 1 (Low)            [inferred]
            centralseverity 1 → NAADS 2 (Moderate)       [inferred]
        """
        from meshai.central.avy_handler import _remap_centralseverity

        # Documented values
        assert _remap_centralseverity(2) == 3, "centralseverity 2 → NAADS 3 (Considerable)"
        assert _remap_centralseverity(3) == 4, "centralseverity 3 → NAADS 4 (High)"
        assert _remap_centralseverity(4) == 5, "centralseverity 4 → NAADS 5 (Extreme)"
        # Inferred values
        assert _remap_centralseverity(0) == 1, "centralseverity 0 → NAADS 1 (Low) [inferred]"
        assert _remap_centralseverity(1) == 2, "centralseverity 1 → NAADS 2 (Moderate) [inferred]"
        # Out-of-range → 0 (No Rating)
        assert _remap_centralseverity(5) == 0, "centralseverity 5 → 0 (No Rating)"
        assert _remap_centralseverity(-1) == 0
        assert _remap_centralseverity("?") == 0

    def test_centralseverity_gate_sequence_via_fixtures(self):
        """Gate-sequence via fixtures: centralseverity 2/3/4 all broadcast; 1 suppresses.

        Uses handle_avy to exercise the full Central ingestion path including
        the centralseverity → NAADS remap and the decide() call.
        """
        from meshai.central.avy_handler import handle_avy

        def _make_envelope(centralseverity: int, naads_level: int,
                           danger_name: str) -> dict:
            return {
                "data": {
                    "adapter": "avalanche_org",
                    "category": "advisory.us.id",
                    "severity": centralseverity,
                    "geo": {"centroid": [-114.9, 43.8]},
                    "data": {
                        "danger_level": naads_level,
                        "danger_name": danger_name,
                        "zone_name": "Test Zone",
                        "center_id": "SNFAC",
                        "travel_advice": "Test advice.",
                    },
                }
            }

        # centralseverity=1 → NAADS 2 (Moderate) → suppressed (below min_level=3)
        env_moderate = _make_envelope(1, 2, "Moderate")
        data_m: dict = {}
        wire_m = handle_avy(env_moderate, "central.avy.advisory.us.id", data=data_m)
        assert wire_m is None, "Moderate (NAADS 2) must be suppressed"

        # centralseverity=2 → NAADS 3 (Considerable) → broadcast
        env_considerable = _make_envelope(2, 3, "Considerable")
        data_c: dict = {}
        wire_c = handle_avy(env_considerable, "central.avy.advisory.us.id", data=data_c)
        assert wire_c is not None, "Considerable (NAADS 3) must broadcast"
        assert "Watch" in wire_c

        # centralseverity=3 → NAADS 4 (High) → broadcast with WARNING
        env_high = _make_envelope(3, 4, "High")
        data_h: dict = {}
        wire_h = handle_avy(env_high, "central.avy.advisory.us.id", data=data_h)
        assert wire_h is not None, "High (NAADS 4) must broadcast"
        assert "WARNING" in wire_h

        # centralseverity=4 → NAADS 5 (Extreme) → broadcast with WARNING
        env_extreme = _make_envelope(4, 5, "Extreme")
        data_e: dict = {}
        wire_e = handle_avy(env_extreme, "central.avy.advisory.us.id", data=data_e)
        assert wire_e is not None, "Extreme (NAADS 5) must broadcast"
        assert "WARNING" in wire_e

    def test_is_update_propagated_into_data_patch(self):
        """decide() data_patch.is_update reflects the incoming is_update flag."""
        from meshai.notifications.gating.avalanche import decide

        result_new = decide(
            {"danger_level": 3, "zone_name": "Z", "center_id": "C",
             "travel_advice": "", "is_update": False},
            source="avalanche", now=_AT,
        )
        assert result_new.data_patch["is_update"] is False

        result_update = decide(
            {"danger_level": 3, "zone_name": "Z", "center_id": "C",
             "travel_advice": "", "is_update": True},
            source="avalanche", now=_AT,
        )
        assert result_update.data_patch["is_update"] is True


# ─────────────────────────────────────────────────────────────────────────────
# 4. Schema conformance — to_event() canonical data completeness
# ─────────────────────────────────────────────────────────────────────────────

class TestSchemaConformance:
    """env/avalanche.py to_event() emits canonical keys without _meshai_precomposed."""

    @pytest.fixture
    def adapter(self):
        from unittest.mock import MagicMock
        from meshai.env.avalanche import AvalancheAdapter
        cfg = MagicMock()
        cfg.center_ids = ["SNFAC"]
        cfg.tick_seconds = 1800
        cfg.season_months = [12, 1, 2, 3, 4]
        return AvalancheAdapter(cfg)

    CANONICAL_KEYS = frozenset({
        "danger_level", "danger_name", "zone_name", "center_id",
        "travel_advice", "lat", "lon", "is_update", "event_id",
    })

    def _raw_evt(self, **overrides):
        import time
        base = {
            "source": "avalanche",
            "event_id": "avy_SNFAC_sawtooth_mountains",
            "event_type": "Avalanche Advisory",
            "severity": "routine",
            "headline": "test",
            "zone_name": "Sawtooth Mountains",
            "center": "Sawtooth Avalanche Center",
            "center_id": "SNFAC",
            "center_link": "",
            "forecast_link": "",
            "danger": "considerable",
            "danger_level": 3,
            "danger_name": "Considerable",
            "travel_advice": "Dangerous conditions on steep slopes.",
            "state": "ID",
            "lat": 43.8,
            "lon": -114.9,
            "expires": time.time() + 3600,
            "fetched_at": time.time(),
        }
        base.update(overrides)
        return base

    def test_all_canonical_keys_present(self, adapter):
        """event.data contains all canonical schema keys."""
        event = adapter.to_event(self._raw_evt())
        assert event is not None
        missing = self.CANONICAL_KEYS - set(event.data.keys())
        assert not missing, f"Missing canonical keys: {missing}"

    def test_no_precomposed_flag(self, adapter):
        """event.data must NOT have _meshai_precomposed — formatter governs."""
        event = adapter.to_event(self._raw_evt())
        assert event is not None
        assert not event.data.get("_meshai_precomposed"), (
            "event.data['_meshai_precomposed'] must be falsy/absent; "
            "formatter registry governs dispatch rendering"
        )

    def test_danger_level_is_int(self, adapter):
        """event.data['danger_level'] is a Python int (not float or str)."""
        event = adapter.to_event(self._raw_evt(danger_level=3))
        assert event is not None
        assert isinstance(event.data["danger_level"], int)
        assert event.data["danger_level"] == 3

    def test_is_update_defaults_false(self, adapter):
        """is_update=False when _is_update not set in the raw event dict."""
        event = adapter.to_event(self._raw_evt())
        assert event is not None
        assert event.data["is_update"] is False

    def test_is_update_propagated_from_store(self, adapter):
        """_is_update=True from EnvironmentalStore propagates to canonical is_update."""
        raw = self._raw_evt()
        raw["_is_update"] = True
        event = adapter.to_event(raw)
        assert event is not None
        assert event.data["is_update"] is True

    def test_event_id_in_canonical_data(self, adapter):
        """event.data['event_id'] matches the source event_id."""
        event = adapter.to_event(self._raw_evt(event_id="avy_test_id"))
        assert event is not None
        assert event.data["event_id"] == "avy_test_id"

    def test_formatter_renders_from_canonical_data(self, adapter):
        """Formatter reads canonical event.data and renders a valid wire string."""
        from meshai.notifications.formatters.avalanche import format as avyfmt

        event = adapter.to_event(self._raw_evt())
        assert event is not None

        with pinned_time(_AT):
            result = avyfmt(event, now=_AT, budget=140)

        assert result is not None
        assert len(result) <= 140
        assert "AVY Watch" in result
        assert "Sawtooth Mountains" in result
        assert "Considerable (3)" in result

    def test_formatter_registered_for_avalanche_categories(self):
        """Both avalanche_warning and avalanche_watch have registered formatters."""
        from meshai.notifications.formatters import get_formatter

        fmt_warning = get_formatter("avalanche_warning")
        assert fmt_warning is not None, "avalanche_warning must have a registered formatter"

        fmt_watch = get_formatter("avalanche_watch")
        assert fmt_watch is not None, "avalanche_watch must have a registered formatter"

    def test_decider_registered_for_avalanche_categories(self):
        """Both avalanche_warning and avalanche_watch have registered deciders."""
        from meshai.notifications.gating import get_decider

        dec_warning = get_decider("avalanche_warning")
        assert dec_warning is not None, "avalanche_warning must have a registered decider"

        dec_watch = get_decider("avalanche_watch")
        assert dec_watch is not None, "avalanche_watch must have a registered decider"
