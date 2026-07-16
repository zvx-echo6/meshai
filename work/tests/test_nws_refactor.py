"""NWS refactor tests — formatter+gater architecture verification.

Originally four test groups; groups 1-3 below were golden-parity tests
against the now-deleted Central NATS-consumer bridge
(meshai.central.nws_handler / handle_nws / _render). That bridge is dead —
production runs the native formatter+gater path exclusively — so byte-parity
and old-vs-new comparisons against it no longer have anything to compare
against and were deleted (git history preserves the original handler and
the parity tests that proved the rewrite matched it). What remains exercises
the LIVE native path only:

1. Gate-sequence: replay a synthetic 4-step lifecycle (first→dup<3h→
   dup>3h→Cancel) through gating.nws.decide(), and a reference-triggered
   "Update" prefix case — both against native code only.

2. Schema-conformance: env/nws.py _fetch() emits all canonical schema keys;
   description is not truncated; to_event() produces a canonical event.data.

3. Formatter/gater registration: formatters/__init__ and gating/__init__
   register the NWS categories against the native format()/decide().
"""
from __future__ import annotations

import time
from datetime import datetime

import pytest

from meshai.notifications.formatters.nws import format as nws_format
from meshai.notifications.gating.nws import decide as nws_decide
from meshai.persistence import close_thread_connection, init_db
from meshai.persistence import db as persistence_db

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
