"""NWS refactor tests — formatter+gater architecture verification.

Originally four test groups; groups 1-3 below were golden-parity tests
against the now-deleted Central NATS-consumer bridge
(meshai.central.nws_handler / handle_nws / _render). That bridge is dead —
production runs the native formatter+gater path exclusively — so byte-parity
and old-vs-new comparisons against it no longer have anything to compare
against and were deleted (git history preserves the original handler and
the parity tests that proved the rewrite matched it). What remains exercises
the LIVE native path only:

1. Formatter golden: formatters.nws.format() renders the expected wire text
   for real fixtures and pathological synthetic cases (SVR path-sampling,
   dangling-separator regression, TOR/FFW branches). These goldens are
   hardcoded literals, NOT computed by importing the deleted handler.  They
   were derived by temporarily restoring the pre-excision
   meshai.central.nws_handler._render() from git history (ca751fb5^) in a
   throwaway script, confirming it produced byte-identical output to the
   current native format() for every case below, and pinning the resulting
   string as the literal.  That verification script/module was never
   committed; only the confirmed-matching literals live here.  See "golden
   verified against pre-excision _render()" comments below.

2. Gate-sequence: replay a synthetic 4-step lifecycle (first→dup<3h→
   dup>3h→Cancel) through gating.nws.decide(), and a reference-triggered
   "Update" prefix case — both against native code only.

3. Schema-conformance: env/nws.py _fetch() emits all canonical schema keys;
   description is not truncated; to_event() produces a canonical event.data.

4. Formatter/gater registration: formatters/__init__ and gating/__init__
   register the NWS categories against the native format()/decide().
"""
from __future__ import annotations

import time
from datetime import datetime

import pytest

from meshai.notifications.formatters._budget import budget_for
from meshai.notifications.formatters.nws import format as nws_format
from meshai.notifications.gating.nws import decide as nws_decide
from meshai.persistence import close_thread_connection, init_db
from meshai.persistence import db as persistence_db
from tests.harness.goldens import assert_byte_identical, load_fixtures, pinned_tz

# ── DB fixture ────────────────────────────────────────────────────────────────

@pytest.fixture
def mem_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "nws-refactor-test.sqlite")
    monkeypatch.setenv("MESHAI_DB_PATH", db_path)
    persistence_db._initialised.clear()
    close_thread_connection()
    conn = init_db()
    yield conn
    close_thread_connection()
    persistence_db._initialised.discard(db_path)


class _FakeEvent:
    """Minimal fake Event for calling the formatter without the full pipeline."""
    def __init__(self, data: dict):
        self.data = data


def _canonical(event_type, *, same_code="", area_desc="Twin Falls County",
               county="Twin Falls", state="ID", expires_epoch=1_751_400_000,
               certainty="Observed", parameters=None, description="",
               prefix="") -> dict:
    """Build a canonical event.data dict as the native adapter's to_event()
    (or the decider's data_patch) would produce it, for feeding directly to
    nws_format()."""
    return {
        "cap_id": "test",
        "event": event_type,
        "same_code": same_code,
        "cap_severity": None,
        "certainty": certainty,
        "expires_at": expires_epoch,
        "area_desc": area_desc,
        "geocoder": {"city": None, "county": county, "state": state},
        "description": description,
        "parameters": parameters or {},
        "msgType": "Alert",
        "references": [],
        "category": "",
        "headline": None,
        "_nws_prefix": prefix,
    }


# =============================================================================
# 1. Formatter golden — native format() wire text
# =============================================================================

class TestFormatterGolden:
    """formatters.nws.format() renders the expected wire text.

    Fixture-driven cases (golden verified against pre-excision _render(),
    see module docstring) plus hand-built pathological cases that pin
    known-tricky behavior: SVR path-sampling, the "no dangling separator"
    regression, and the TOR/FFW hazard branches.
    """

    def _canonical_from_fixture(self, fix: dict) -> dict:
        """Extract canonical event.data from a Central-style NWS fixture.

        Standalone re-implementation of the field extraction that used to
        live in the deleted meshai.central.nws_handler (event-type fallback
        via category, ISO-to-epoch parsing) — kept here only as test
        scaffolding to turn a raw fixture into a canonical dict.
        """
        envelope = fix["envelope"]
        inner = envelope.get("data") or {}
        d = inner.get("data") or {}
        ge = (d.get("_enriched") or {}).get("geocoder") or {}
        category_raw = inner.get("category") or ""

        event_type = d.get("event") or "Weather Alert"
        area_desc = d.get("areaDesc")
        county = d.get("areaDesc") or ge.get("county")
        state = ge.get("state") or d.get("state")
        expires_epoch = _parse_iso(d.get("expires"))
        same_code = ((d.get("eventCode") or {}).get("SAME") or [""])[0]

        return {
            "cap_id": d.get("id") or inner.get("id"),
            "event": event_type,
            "same_code": same_code,
            "cap_severity": d.get("severity"),
            "certainty": d.get("certainty") or "",
            "expires_at": expires_epoch,
            "area_desc": area_desc,
            "geocoder": {"city": ge.get("city"), "county": county, "state": state},
            "description": d.get("description"),
            "parameters": d.get("parameters") or {},
            "msgType": d.get("msgType"),
            "references": d.get("references") or [],
            "category": category_raw,
            "headline": d.get("headline"),
            "_nws_prefix": "",
        }

    @pytest.mark.parametrize("n,expected", [
        (0, "🌬️ Special Weather Statement\nUntil 5:45 PM MDT — Northern Elko County"
            "\nLandspouts, 40mph gusts, and half inch hail · observed"
            "\nMoving W 23 mph"),
        (8, "⛈️ Severe Thunderstorm Warning\nUntil 4:30 PM MDT — Cassia, ID"
            "\nup to 50mph winds, 1\" hail · radar"
            "\nMoving SW 20 mph"),
        (9, "🌩️ Severe Thunderstorm Warning\nUntil 4:30 PM MDT — Cassia, ID"
            "\n1\" hail · observed"
            "\nMoving SW 20 mph — Oakley Reservoir and Oakley"),
    ])
    def test_fixture_golden(self, n, expected):
        """Real nws/ fixtures render to the pinned wire text.

        golden verified against pre-excision _render() (see module docstring).
        """
        fixes = load_fixtures("nws")
        fix = fixes[n]
        epoch = float(fix.get("captured_epoch", 1_783_206_513.0))
        canonical = self._canonical_from_fixture(fix)
        budget = budget_for("nws")

        with pinned_tz("America/Boise"):
            result = nws_format(_FakeEvent(canonical), now=epoch, budget=budget)

        assert_byte_identical(result, expected)

    def test_svr_long_locations_path_sampled(self):
        """SVR with a long town list renders a PATH SAMPLE (first → last),
        never a tail-drop that silently loses the final town.

        golden verified against pre-excision _render() (see module docstring).
        """
        long_locations = (
            "Buhl, Eden, Hazelton, Murtaugh, Richfield, Dietrich, "
            "Gooding, Hagerman, Wendell, and Shoshone"
        )
        description = (
            "HAZARD...Damaging winds to 60 mph and quarter-size hail.\n\n"
            f"Locations impacted include...{long_locations}"
        )
        canonical = _canonical(
            "Severe Thunderstorm Warning", same_code="SVR",
            certainty="Observed", description=description,
            parameters={
                "maxWindGust": ["60 MPH"], "maxHailSize": ["1.00"],
                "eventMotionDescription": ["2200000T254DEG...35KT 42.5,-114.5"],
            },
        )
        expected = (
            "⛈️ Severe Thunderstorm Warning\nUntil 2:00 PM MDT — Twin Falls County"
            "\n60mph winds, 1\" hail · radar"
            "\nMoving W 40 mph — Buhl → Shoshone"
        )

        with pinned_tz("America/Boise"):
            result = nws_format(_FakeEvent(canonical), now=1_751_400_000,
                                 budget=budget_for("nws"))

        assert len(result) <= 140
        assert "→" in result, "long town list must be path-sampled"
        assert "Buhl" in result and "Shoshone" in result
        assert_byte_identical(result, expected)

    def test_svr_short_locations_shown_in_full(self):
        """SVR with a short town list shows the FULL comma-joined list —
        never the path-sample arrow.

        golden verified against pre-excision _render() (see module docstring).
        """
        description = (
            "HAZARD...Damaging winds to 60 mph and quarter-size hail.\n\n"
            "Locations impacted include...Buhl, Eden, and Hazelton"
        )
        canonical = _canonical(
            "Severe Thunderstorm Warning", same_code="SVR",
            certainty="Observed", description=description,
            parameters={
                "maxWindGust": ["60 MPH"], "maxHailSize": ["1.00"],
                "eventMotionDescription": ["2200000T254DEG...35KT 42.5,-114.5"],
            },
        )
        expected = (
            "⛈️ Severe Thunderstorm Warning\nUntil 2:00 PM MDT — Twin Falls County"
            "\n60mph winds, 1\" hail · radar"
            "\nMoving W 40 mph — Buhl, Eden, Hazelton"
        )

        with pinned_tz("America/Boise"):
            result = nws_format(_FakeEvent(canonical), now=1_751_400_000,
                                 budget=budget_for("nws"))

        assert "→" not in result, "short list must not be path-sampled"
        assert_byte_identical(result, expected)

    def test_sps_no_dangling_separator(self):
        """Regression: an SPS with wind+motion+long town list must never
        collapse to a trailing bare em-dash ('Moving SW 24 mph —…').

        golden verified against pre-excision _render() (see module docstring).
        """
        long_locations = (
            "Twin Falls, Kimberly, Filer, Buhl, Hansen, Murtaugh, Hollister, "
            "Eden, Hazelton, and Rogerson"
        )
        description = (
            "HAZARD...Wind gusts in excess of 45 mph and pea size hail.\n\n"
            "SOURCE...Radar indicated.\n\n"
            f"Locations impacted include...{long_locations}"
        )
        canonical = _canonical(
            "Special Weather Statement", same_code="SPS",
            certainty="Observed", description=description,
            parameters={"eventMotionDescription": ["2200000T225DEG...21KT 42.5,-114.5"]},
        )
        expected = (
            "🌬️ Special Weather Statement\nUntil 2:00 PM MDT — Twin Falls County"
            "\n45mph gusts and 0.25\" hail · observed"
            "\nMoving SW 24 mph — Twin Falls"
        )

        with pinned_tz("America/Boise"):
            result = nws_format(_FakeEvent(canonical), now=1_751_400_000,
                                 budget=budget_for("nws"))

        assert len(result) <= 140
        for line in result.splitlines():
            stripped = line.rstrip()
            assert not stripped.endswith("—"), f"bare trailing em-dash: {line!r}"
            assert "—…" not in stripped and "— …" not in stripped
        assert "45mph gusts" in result, "wind hazard not tightened"
        assert '0.25" hail' in result, "hail not rendered numerically ('pea' -> 0.25\")"
        assert "in excess of" not in result, "filler phrase survived tightening"
        assert_byte_identical(result, expected)

    def test_sps_pathological_towns_degrade_to_motion_only(self):
        """When not even one sampled town fits the remaining budget, line 4
        degrades to motion-only — never a dangling separator.

        golden verified against pre-excision _render() (see module docstring).
        """
        long_town = "Averyverylongimpossibletownnamethatwillnotfitthebudgetatall" * 2
        description = (
            "HAZARD...Wind gusts in excess of 45 mph.\n\n"
            f"Locations impacted include...{long_town}"
        )
        canonical = _canonical(
            "Special Weather Statement", same_code="SPS",
            certainty="Observed", description=description,
            parameters={"eventMotionDescription": ["2200000T225DEG...21KT 42.5,-114.5"]},
        )
        expected = (
            "🌬️ Special Weather Statement\nUntil 2:00 PM MDT — Twin Falls County"
            "\n45mph gusts · observed"
            "\nMoving SW 24 mph"
        )

        with pinned_tz("America/Boise"):
            result = nws_format(_FakeEvent(canonical), now=1_751_400_000,
                                 budget=budget_for("nws"))

        assert len(result) <= 140
        last = result.splitlines()[-1]
        if last.startswith("Moving"):
            assert " — " not in last, f"expected motion-only, got: {last!r}"
        assert_byte_identical(result, expected)

    def test_tor_observed_on_ground_with_damage_threat(self):
        """TOR branch: OBSERVED detection -> 'on ground'; damage threat appended.

        golden verified against pre-excision _render() (see module docstring).
        """
        canonical = _canonical(
            "Tornado Warning", same_code="TOR", certainty="Observed",
            description="TORNADO...OBSERVED\n\nLocations impacted include...Twin Falls.",
            parameters={"tornadoDetection": ["OBSERVED"],
                        "tornadoDamageThreat": ["Considerable"]},
        )
        expected = (
            "🌪️ Tornado Warning\nUntil 2:00 PM MDT — Twin Falls County"
            "\ntornado on ground · considerable damage"
            "\nTwin Falls"
        )

        with pinned_tz("America/Boise"):
            result = nws_format(_FakeEvent(canonical), now=1_751_400_000,
                                 budget=budget_for("nws"))

        assert_byte_identical(result, expected)

    def test_tor_radar_indicated_no_threat(self):
        """TOR branch: non-OBSERVED detection -> 'radar'; no threat segment
        when tornadoDamageThreat is empty.

        golden verified against pre-excision _render() (see module docstring).
        """
        canonical = _canonical(
            "Tornado Warning", same_code="TOR", certainty="Possible",
            description="TORNADO...RADAR INDICATED\n\nLocations impacted include...Buhl.",
            parameters={"tornadoDetection": ["RADAR INDICATED"], "tornadoDamageThreat": []},
        )
        expected = (
            "🌪️ Tornado Warning\nUntil 2:00 PM MDT — Twin Falls County"
            "\ntornado radar"
            "\nBuhl"
        )

        with pinned_tz("America/Boise"):
            result = nws_format(_FakeEvent(canonical), now=1_751_400_000,
                                 budget=budget_for("nws"))

        assert_byte_identical(result, expected)

    def test_ffw_thunderstorm_flood_cause(self):
        """FFW/FLW branch: flood-cause keyword ('thunderstorm') is appended
        as a ' · thunderstorms' segment.

        golden verified against pre-excision _render() (see module docstring).
        """
        canonical = _canonical(
            "Flash Flood Warning", same_code="FFW", certainty="Observed",
            description=("HAZARD...Flash flooding caused by thunderstorms. Excessive "
                          "runoff will result in flooding of small creeks.\n\n"
                          "Locations impacted include...Twin Falls."),
            parameters={},
        )
        expected = (
            "🌊 Flash Flood Warning\nUntil 2:00 PM MDT — Twin Falls County"
            "\nFlash flooding caused by thunderstorms · thunderstorms"
            "\nTwin Falls"
        )

        with pinned_tz("America/Boise"):
            result = nws_format(_FakeEvent(canonical), now=1_751_400_000,
                                 budget=budget_for("nws"))

        assert_byte_identical(result, expected)


# =============================================================================
# 1. Gate-sequence: native gating/nws.decide() only
# =============================================================================

def _parse_iso(s):
    """Parse a CAP ISO datetime string to an epoch int (or None).

    Standalone equivalent of the now-deleted meshai.central.nws_handler
    ._parse_iso, kept here only as test scaffolding for building canonical
    dicts to feed nws_decide().
    """
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


class TestGateSequence:
    """Replay a 4-step lifecycle through the native gating.nws.decide().

    Steps:
      1. First sighting → broadcast
      2. Repeat within 3h window → suppress
      3. Repeat outside 3h window → broadcast (Active prefix)
      4. Cancel/Expire tombstone → suppress
    """

    def _make_envelope(self, cap_id: str, event: str = "Severe Thunderstorm Warning",
                        msg_type: str = "Alert", references=None) -> dict:
        return {
            "envelope": {
                "data": {
                    "adapter": "nws",
                    "category": "wx.alert.severe_thunderstorm_warning",
                    "severity": 3,
                    "geo": {"centroid": [-114.46, 42.5], "primary_region": "US-ID"},
                    "data": {
                        "id": cap_id,
                        "event": event,
                        "severity": "Severe",
                        "certainty": "Observed",
                        "areaDesc": "Twin Falls County",
                        "msgType": msg_type,
                        "headline": f"{event} for Twin Falls County",
                        "description": "HAZARD...60 MPH winds.",
                        "expires": "2026-07-04T03:00:00Z",
                        "references": references or [],
                        "parameters": {"maxWindGust": ["60 MPH"], "maxHailSize": ["0.00"]},
                        "eventCode": {"SAME": ["SVR"]},
                    },
                }
            },
            "subject": "central.wx.alert.us.id.county.0370011",
            "captured_epoch": 1_783_200_000,
        }

    def _make_canonical(self, fixture: dict) -> dict:
        """Build canonical dict from a fixture for nws_decide().

        `_make_envelope` always sets an explicit "event" field, so the
        deleted central _category_to_event_type() fallback is never
        actually exercised here; "Weather Alert" documents that fallback
        without depending on the deleted module.
        """
        env = fixture["envelope"]
        inner = env.get("data") or {}
        d = inner.get("data") or {}
        category_raw = inner.get("category") or ""
        return {
            "cap_id": d.get("id"),
            "event": d.get("event") or "Weather Alert",
            "same_code": ((d.get("eventCode") or {}).get("SAME") or [""])[0],
            "cap_severity": d.get("severity"),
            "certainty": d.get("certainty") or "",
            "expires_at": _parse_iso(d.get("expires")),
            "area_desc": d.get("areaDesc"),
            "geocoder": {"city": None, "county": d.get("areaDesc"), "state": None},
            "description": d.get("description"),
            "parameters": d.get("parameters") or {},
            "msgType": d.get("msgType"),
            "references": d.get("references") or [],
            "category": category_raw,
            "headline": d.get("headline"),
        }

    def test_new_gate_sequence(self, mem_db):
        """4-step lifecycle through NEW nws_decide(): first→dup<3h→dup>3h→Cancel."""
        cap_id = "urn:oid:new.gate.001"
        t0 = 1_783_200_000.0
        t1 = t0 + 1000      # <3h
        t2 = t0 + 11000     # >3h
        t3 = t0 + 12000

        def go(msg_type="Alert", now=t0):
            fix = self._make_envelope(cap_id, msg_type=msg_type)
            canon = self._make_canonical(fix)
            gate = nws_decide(canon, source="nws", now=now)
            if gate.broadcast and gate.commit:
                gate.commit(now)
            return gate

        gate1 = go(now=t0)
        assert gate1.broadcast is True, f"step1: first sighting broadcast, got: {gate1.reason}"
        assert gate1.data_patch.get("_nws_prefix") == "", "step1: first sighting has empty prefix"

        gate2 = go(now=t1)
        assert gate2.broadcast is False, f"step2: dup within 3h suppressed, got: {gate2.reason}"

        gate3 = go(now=t2)
        assert gate3.broadcast is True, f"step3: after 3h rebroadcast, got: {gate3.reason}"
        assert gate3.data_patch.get("_nws_prefix") == "Active", (
            f"step3: rebroadcast prefix 'Active', got: {gate3.data_patch.get('_nws_prefix')!r}"
        )

        gate4 = go("Cancel", now=t3)
        assert gate4.broadcast is False, f"step4: Cancel tombstone suppressed, got: {gate4.reason}"

    def test_update_prefix_on_reference(self, mem_db):
        """A new alert that references a previously-broadcast alert gets 'Update' prefix."""
        parent_id = "urn:oid:parent.001"
        child_id = "urn:oid:child.001"
        t0 = 1_783_200_000.0
        t1 = t0 + 500

        # Broadcast parent first, through the same native nws_decide() path
        # used everywhere else in this class (mirrors test_new_gate_sequence).
        fix_parent = self._make_envelope(parent_id)
        canon_parent = self._make_canonical(fix_parent)
        gate_parent = nws_decide(canon_parent, source="nws", now=t0)
        assert gate_parent.broadcast is True, "parent should broadcast"
        if gate_parent.commit:
            gate_parent.commit(t0)

        # Child references parent
        fix_child = self._make_envelope(
            child_id,
            references=[{"identifier": parent_id, "sent": "2026-07-04T00:00:00Z",
                          "effective": "2026-07-04T00:00:00Z"}],
        )
        canonical = self._make_canonical(fix_child)

        gate_child = nws_decide(canonical, source="nws", now=t1)
        assert gate_child.broadcast is True, f"child should broadcast: {gate_child.reason}"
        assert gate_child.data_patch.get("_nws_prefix") == "Update", (
            f"child referencing a broadcast parent should get 'Update' prefix, "
            f"got {gate_child.data_patch.get('_nws_prefix')!r}"
        )


# =============================================================================
# 2. Schema-conformance: native env/nws.py emits canonical schema
# =============================================================================

class TestSchemaConformance:
    """env/nws.py _fetch() and to_event() emit all canonical schema keys."""

    _CANONICAL_KEYS = {
        "cap_id", "event", "same_code", "cap_severity", "certainty",
        "expires_at", "area_desc", "geocoder", "description", "parameters",
        "msgType", "references", "category", "headline",
    }

    def _make_adapter(self):
        from unittest.mock import MagicMock
        from meshai.env.nws import NWSAlertsAdapter
        cfg = MagicMock()
        cfg.areas = ["ID"]
        cfg.user_agent = "(test)"
        cfg.severity_min = "moderate"
        cfg.tick_seconds = 60
        return NWSAlertsAdapter(cfg)

    def _make_raw(self, description="HAZARD...60 MPH winds.") -> dict:
        """Simulate a _fetch() event dict with all canonical fields."""
        expires = time.time() + 3600
        return {
            "source": "nws",
            "event_id": "urn:oid:schema.test.001",
            "event_type": "Severe Thunderstorm Warning",
            "severity": "severe",
            "headline": "SVR Warning",
            "description": description,
            "onset": time.time(),
            "expires": expires,
            "expires_at": expires,
            "areas": ["IDZ016"],
            "area_desc": "Twin Falls County",
            "fetched_at": time.time(),
            "cap_id": "urn:oid:schema.test.001",
            "same_code": "SVR",
            "cap_severity": "Severe",
            "certainty": "Observed",
            "parameters": {"maxWindGust": ["60 MPH"], "maxHailSize": ["1.00"]},
            "msgType": "Alert",
            "references": [],
        }

    def test_to_event_emits_all_canonical_keys(self):
        """to_event() produces event.data with all canonical schema keys."""
        adapter = self._make_adapter()
        raw = self._make_raw()
        event = adapter.to_event(raw)
        data = event.data

        assert isinstance(data, dict), "event.data must be a dict"
        missing = self._CANONICAL_KEYS - set(data.keys())
        assert not missing, f"event.data missing canonical keys: {missing}"

    def test_description_not_truncated(self):
        """to_event() carries FULL description (not truncated to 500 chars)."""
        adapter = self._make_adapter()
        long_desc = "X" * 1000
        raw = self._make_raw(description=long_desc)
        event = adapter.to_event(raw)
        assert event.data["description"] == long_desc, (
            f"description truncated: expected {len(long_desc)} chars, "
            f"got {len(event.data['description'])}"
        )

    def test_geocoder_structure(self):
        """event.data['geocoder'] has city, county, state keys."""
        adapter = self._make_adapter()
        raw = self._make_raw()
        event = adapter.to_event(raw)
        geo = event.data.get("geocoder") or {}
        assert "city" in geo, "geocoder missing 'city'"
        assert "county" in geo, "geocoder missing 'county'"
        assert "state" in geo, "geocoder missing 'state'"

    def test_same_code_extracted(self):
        """event.data['same_code'] is extracted correctly from raw."""
        adapter = self._make_adapter()
        raw = self._make_raw()
        raw["same_code"] = "SVR"
        event = adapter.to_event(raw)
        assert event.data["same_code"] == "SVR"

    def test_cap_id_present(self):
        """event.data['cap_id'] is the alert identifier."""
        adapter = self._make_adapter()
        raw = self._make_raw()
        event = adapter.to_event(raw)
        assert event.data["cap_id"] == "urn:oid:schema.test.001"

    def test_parameters_passed_through(self):
        """event.data['parameters'] is the full CAP parameters dict."""
        adapter = self._make_adapter()
        raw = self._make_raw()
        event = adapter.to_event(raw)
        params = event.data.get("parameters") or {}
        assert "maxWindGust" in params, "parameters.maxWindGust missing"


# =============================================================================
# 3. Formatter registration: weather_warning + weather_statement registered
# =============================================================================

class TestFormatterRegistration:
    """formatters/__init__ and gating/__init__ register NWS categories."""

    def test_weather_warning_formatter_registered(self):
        from meshai.notifications.formatters import get_formatter
        fn = get_formatter("weather_warning")
        assert fn is not None, "weather_warning formatter not registered"
        assert fn is nws_format, "weather_warning formatter should be nws.format"

    def test_weather_statement_formatter_registered(self):
        from meshai.notifications.formatters import get_formatter
        fn = get_formatter("weather_statement")
        assert fn is not None, "weather_statement formatter not registered"
        assert fn is nws_format, "weather_statement formatter should be nws.format"

    def test_weather_warning_gater_registered(self):
        from meshai.notifications.gating import get_decider
        fn = get_decider("weather_warning")
        assert fn is not None, "weather_warning gater not registered"
        assert fn is nws_decide, "weather_warning gater should be gating.nws.decide"

    def test_weather_statement_gater_registered(self):
        from meshai.notifications.gating import get_decider
        fn = get_decider("weather_statement")
        assert fn is not None, "weather_statement gater not registered"
        assert fn is nws_decide, "weather_statement gater should be gating.nws.decide"

    def test_pre_existing_formatters_still_registered(self):
        """Existing Phase-1 registrations must still be present (idempotent append)."""
        from meshai.notifications.formatters import get_formatter
        from meshai.notifications.formatters import quake as _q
        from meshai.notifications.formatters import avalanche as _avy
        from meshai.notifications.formatters import swpc as _swpc
        assert get_formatter("earthquake_event") is _q.format
        assert get_formatter("avalanche_warning") is _avy.format
        assert get_formatter("geomagnetic_storm") is _swpc.format

    def test_pre_existing_gaters_still_registered(self):
        """Existing Phase-1 gating registrations must still be present."""
        from meshai.notifications.gating import get_decider
        from meshai.notifications.gating import quake as _q
        from meshai.notifications.gating import avalanche as _avy
        from meshai.notifications.gating import swpc as _swpc
        assert get_decider("earthquake_event") is _q.decide
        assert get_decider("avalanche_warning") is _avy.decide
        assert get_decider("geomagnetic_storm") is _swpc.decide
