"""Phase-2 NWS refactor tests — formatter+gater architecture verification.

Four test groups:

1. Golden byte-parity (tier-a): for each NWS fixture, run the OLD _render()
   under pinned_time+pinned_tz, then run the NEW format() from canonical
   data built by the Central bridge, and assert_byte_identical.  This MUST
   be exactly equal — any difference is a regression.

2. Cross-source identity: the native adapter's to_event() canonical dict
   (for a synthetic fixture) produces the same formatter output as the
   Central-bridge canonical dict built from the same alert data.

3. Gate-sequence: replay a synthetic 4-step lifecycle (first→dup<3h→
   dup>3h→Cancel) through the OLD handle_nws gating and the NEW
   gating.nws.decide(), and assert broadcast/suppress match at every step.

4. Schema-conformance: env/nws.py _fetch() emits all canonical schema keys;
   description is not truncated; to_event() produces a canonical event.data.
"""
from __future__ import annotations

import json
import os
import pathlib
import time

import pytest

from meshai.central.nws_handler import _render, handle_nws
from meshai.central.budget import budget_for
from meshai.notifications.formatters.nws import format as nws_format
from meshai.notifications.gating.nws import decide as nws_decide
from meshai.notifications.gating.base import GateResult
from meshai.persistence import close_thread_connection, get_db, init_db
from meshai.persistence import db as persistence_db
from tests.harness.goldens import (
    assert_byte_identical,
    load_fixtures,
    pinned_time,
    pinned_tz,
    run_gate_sequence,
)

# ── Shared epoch for deterministic renders ────────────────────────────────────
_AT = 1_783_206_513.0   # captured_epoch for fixture 0000


# ── Minimal fake Event for calling formatter without full pipeline ────────────

class _FakeEvent:
    def __init__(self, data: dict):
        self.data = data


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


# ── Helper: build canonical data from a Central fixture ──────────────────────

def _canonical_from_fixture(fix: dict) -> dict:
    """Extract canonical event.data dict from a Central NWS fixture.

    Mirrors exactly what handle_nws (cutover path) writes into data dict.
    Used in formatter golden tests without going through the full handler.
    """
    envelope = fix["envelope"]
    inner = envelope.get("data") or {}
    d = inner.get("data") or {}
    geo = inner.get("geo") or {}
    ge = (d.get("_enriched") or {}).get("geocoder") or {}
    category_raw = inner.get("category") or ""

    from meshai.central.nws_handler import _category_to_event_type, _parse_iso

    cap_id = d.get("id") or inner.get("id")
    event_type = d.get("event") or _category_to_event_type(category_raw)
    area_desc = d.get("areaDesc")
    headline = d.get("headline")
    description = d.get("description")
    cap_severity = d.get("severity")
    county = d.get("areaDesc") or ge.get("county")
    state = ge.get("state") or d.get("state")
    expires_epoch = _parse_iso(d.get("expires"))
    same_code = ((d.get("eventCode") or {}).get("SAME") or [""])[0]
    certainty = d.get("certainty") or ""
    references = d.get("references") or []
    parameters = d.get("parameters") or {}
    msg_type = d.get("msgType")

    return {
        "cap_id": cap_id,
        "event": event_type,
        "same_code": same_code,
        "cap_severity": cap_severity,
        "certainty": certainty,
        "expires_at": expires_epoch,
        "area_desc": area_desc,
        "geocoder": {
            "city": ge.get("city"),
            "county": county,
            "state": state,
        },
        "description": description,
        "parameters": parameters,
        "msgType": msg_type,
        "references": references,
        "category": category_raw,
        "headline": headline,
        # prefix injected by gater: "" for first sighting (no references)
        "_nws_prefix": "",
    }


def _old_render_from_fixture(fix: dict) -> str:
    """Call old _render() from a Central NWS fixture with pinned clock+tz.

    Returns the wire string.
    """
    envelope = fix["envelope"]
    inner = envelope.get("data") or {}
    d = inner.get("data") or {}
    geo = inner.get("geo") or {}
    ge = (d.get("_enriched") or {}).get("geocoder") or {}
    category_raw = inner.get("category") or ""

    from meshai.central.nws_handler import _category_to_event_type, _parse_iso

    event_type = d.get("event") or _category_to_event_type(category_raw)
    area_desc = d.get("areaDesc")
    cap_severity = d.get("severity")
    county = d.get("areaDesc") or ge.get("county")
    state = ge.get("state") or d.get("state")
    expires_epoch = _parse_iso(d.get("expires"))

    lat = lon = None
    cent = geo.get("centroid") or []
    if isinstance(cent, list) and len(cent) >= 2:
        lon, lat = cent[0], cent[1]

    epoch = float(fix.get("captured_epoch", _AT))
    return _render(
        event_type=event_type, area_desc=area_desc,
        geocoder_city=ge.get("city"), county=county, state=state,
        expires_epoch=expires_epoch, lat=lat, lon=lon,
        now=epoch, prefix="", d=d,
    )


# =============================================================================
# 1. Golden byte-parity (tier-a)
# =============================================================================

class TestGoldenByteParity:
    """formatters/nws.format() is byte-identical to _render() for all fixtures.

    Both the nws/ fixtures (first-sighting, no prefix) and nws_last/ fixtures
    (may have references → "Update" prefix) are tested.
    """

    def _render_and_format(self, fix: dict, prefix: str = ""):
        """Run old _render and new format() under identical pinned clock+tz.

        Returns (golden, new_output).
        """
        epoch = float(fix.get("captured_epoch", _AT))

        canonical = _canonical_from_fixture(fix)
        canonical["_nws_prefix"] = prefix

        budget = budget_for("nws")

        with pinned_tz("America/Boise"):
            with pinned_time(epoch):
                golden = _old_render_from_fixture(fix)
                # Override prefix in _render for parity (handler uses "" for first-sight)
                golden = _render(
                    **{k: canonical.get(k) for k in
                       ("event_type",)},  # we'll call _render directly below
                )
            # Actually call _render directly with same params as _old_render_from_fixture
            golden = _old_render_from_fixture(fix)
            new_out = nws_format(_FakeEvent(canonical), now=epoch, budget=budget)

        return golden, new_out

    @pytest.mark.parametrize("n", list(range(27)))
    def test_fixture_nws_byte_identical(self, n):
        """All 27 nws/ fixtures render byte-identically old vs new."""
        fixes = load_fixtures("nws")
        if n >= len(fixes):
            pytest.skip(f"fixture {n} not found (only {len(fixes)} fixtures)")
        fix = fixes[n]
        epoch = float(fix.get("captured_epoch", _AT))
        canonical = _canonical_from_fixture(fix)
        budget = budget_for("nws")

        with pinned_tz("America/Boise"):
            golden = _old_render_from_fixture(fix)
            new_out = nws_format(_FakeEvent(canonical), now=epoch, budget=budget)

        assert_byte_identical(new_out, golden)

    @pytest.mark.parametrize("n", list(range(10)))
    def test_fixture_nws_last_byte_identical(self, n):
        """All 10 nws_last/ fixtures render byte-identically old vs new."""
        fixes = load_fixtures("nws_last")
        if n >= len(fixes):
            pytest.skip(f"fixture {n} not found (only {len(fixes)} fixtures)")
        fix = fixes[n]
        epoch = float(fix.get("captured_epoch", _AT))
        canonical = _canonical_from_fixture(fix)
        budget = budget_for("nws")

        with pinned_tz("America/Boise"):
            golden = _old_render_from_fixture(fix)
            new_out = nws_format(_FakeEvent(canonical), now=epoch, budget=budget)

        assert_byte_identical(new_out, golden)

    def test_update_prefix_byte_identical(self):
        """'Update:' prefix variant is byte-identical."""
        fixes = load_fixtures("nws_last")
        if not fixes:
            pytest.skip("no nws_last fixtures")
        fix = fixes[0]
        epoch = float(fix.get("captured_epoch", _AT))
        canonical = _canonical_from_fixture(fix)
        canonical["_nws_prefix"] = "Update"
        budget = budget_for("nws")

        envelope = fix["envelope"]
        inner = envelope.get("data") or {}
        d = inner.get("data") or {}
        geo = inner.get("geo") or {}
        ge = (d.get("_enriched") or {}).get("geocoder") or {}
        from meshai.central.nws_handler import _category_to_event_type, _parse_iso
        category_raw = inner.get("category") or ""
        event_type = d.get("event") or _category_to_event_type(category_raw)
        area_desc = d.get("areaDesc")
        county = d.get("areaDesc") or ge.get("county")
        state = ge.get("state") or d.get("state")
        expires_epoch = _parse_iso(d.get("expires"))
        lat = lon = None
        cent = geo.get("centroid") or []
        if isinstance(cent, list) and len(cent) >= 2:
            lon, lat = cent[0], cent[1]

        with pinned_tz("America/Boise"):
            golden = _render(event_type=event_type, area_desc=area_desc,
                             geocoder_city=ge.get("city"), county=county, state=state,
                             expires_epoch=expires_epoch, lat=lat, lon=lon,
                             now=epoch, prefix="Update", d=d)
            new_out = nws_format(_FakeEvent(canonical), now=epoch, budget=budget)

        assert_byte_identical(new_out, golden)

    def test_active_prefix_byte_identical(self):
        """'Active:' prefix variant is byte-identical."""
        fixes = load_fixtures("nws")
        if not fixes:
            pytest.skip("no nws fixtures")
        fix = fixes[0]
        epoch = float(fix.get("captured_epoch", _AT))
        canonical = _canonical_from_fixture(fix)
        canonical["_nws_prefix"] = "Active"
        budget = budget_for("nws")

        envelope = fix["envelope"]
        inner = envelope.get("data") or {}
        d = inner.get("data") or {}
        geo = inner.get("geo") or {}
        ge = (d.get("_enriched") or {}).get("geocoder") or {}
        from meshai.central.nws_handler import _category_to_event_type, _parse_iso
        category_raw = inner.get("category") or ""
        event_type = d.get("event") or _category_to_event_type(category_raw)
        area_desc = d.get("areaDesc")
        county = d.get("areaDesc") or ge.get("county")
        state = ge.get("state") or d.get("state")
        expires_epoch = _parse_iso(d.get("expires"))
        lat = lon = None
        cent = geo.get("centroid") or []
        if isinstance(cent, list) and len(cent) >= 2:
            lon, lat = cent[0], cent[1]

        with pinned_tz("America/Boise"):
            golden = _render(event_type=event_type, area_desc=area_desc,
                             geocoder_city=ge.get("city"), county=county, state=state,
                             expires_epoch=expires_epoch, lat=lat, lon=lon,
                             now=epoch, prefix="Active", d=d)
            new_out = nws_format(_FakeEvent(canonical), now=epoch, budget=budget)

        assert_byte_identical(new_out, golden)


# =============================================================================
# 2. Cross-source identity: native canonical == Central-sourced render
# =============================================================================

class TestCrossSourceIdentity:
    """Native adapter to_event() canonical == Central-bridge canonical for same alert."""

    def _make_native_raw(self, props: dict, onset: float, expires: float) -> dict:
        """Simulate what _fetch() builds for a single NWS API feature."""
        return {
            "source": "nws",
            "event_id": props.get("id", ""),
            "event_type": props.get("event", "Unknown"),
            "severity": (props.get("severity") or "Unknown").lower(),
            "headline": props.get("headline", ""),
            "description": props.get("description") or "",
            "onset": onset,
            "expires": expires,
            "expires_at": expires,
            "areas": (props.get("geocode") or {}).get("UGC", []),
            "area_desc": props.get("areaDesc", ""),
            "fetched_at": time.time(),
            "cap_id": props.get("id", ""),
            "same_code": ((props.get("eventCode") or {}).get("SAME") or [""])[0],
            "cap_severity": props.get("severity", "Unknown"),
            "certainty": props.get("certainty", "Unknown"),
            "parameters": props.get("parameters") or {},
            "msgType": props.get("messageType", "Alert"),
            "references": props.get("references") or [],
        }

    def test_native_and_central_render_identically(self):
        """For a synthetic SVR alert, native and Central canonical render the same wire.

        Both paths must produce byte-identical output when given the same underlying
        alert data.  The key equality constraints are:
          - same expires_at epoch
          - same same_code
          - same area_desc / geocoder.county
          - same parameters (wind/hail)
          - same _nws_prefix (both "")
        """
        from unittest.mock import MagicMock
        from meshai.env.nws import NWSAlertsAdapter
        from meshai.central.nws_handler import _parse_iso

        expires_iso = "2026-07-04T01:00:00-06:00"
        # Derive epoch from the ISO string so both paths use the same value.
        expires_epoch = _parse_iso(expires_iso)  # int

        props = {
            "id": "urn:oid:test.svr.001",
            "event": "Severe Thunderstorm Warning",
            "severity": "Severe",
            "certainty": "Observed",
            "areaDesc": "Twin Falls County",
            "headline": "SVR Warning Twin Falls County",
            "description": "HAZARD...60 MPH winds and 1.00 inch hail.",
            "expires": expires_iso,
            "messageType": "Alert",
            "references": [],
            "parameters": {
                "maxWindGust": ["60 MPH"],
                "maxHailSize": ["1.00"],
            },
            "eventCode": {"SAME": ["SVR"]},
            "geocode": {"UGC": ["IDZ016"]},
        }

        # Native path: build raw → to_event() → event.data
        mock_cfg = MagicMock()
        mock_cfg.areas = ["ID"]
        mock_cfg.user_agent = "(test)"
        mock_cfg.severity_min = "moderate"
        mock_cfg.tick_seconds = 60
        adapter = NWSAlertsAdapter(mock_cfg)

        raw = self._make_native_raw(props, onset=expires_epoch - 7200, expires=expires_epoch)
        native_event = adapter.to_event(raw)
        native_canonical = native_event.data

        # Central path: build canonical manually (same logic as bridge)
        central_canonical = {
            "cap_id": props["id"],
            "event": "Severe Thunderstorm Warning",
            "same_code": "SVR",
            "cap_severity": "Severe",
            "certainty": "Observed",
            "expires_at": expires_epoch,
            "area_desc": "Twin Falls County",
            "geocoder": {"city": None, "county": "Twin Falls County", "state": None},
            "description": props["description"],
            "parameters": props["parameters"],
            "msgType": "Alert",
            "references": [],
            "category": "weather_warning",
            "headline": props["headline"],
            "_nws_prefix": "",
        }

        budget = budget_for("nws")
        with pinned_tz("America/Boise"):
            native_wire = nws_format(_FakeEvent(native_canonical), now=expires_epoch - 100, budget=budget)
            central_wire = nws_format(_FakeEvent(central_canonical), now=expires_epoch - 100, budget=budget)

        assert_byte_identical(native_wire, central_wire)


# =============================================================================
# 3. Gate-sequence: old handle_nws vs new gating/nws.decide()
# =============================================================================

class TestGateSequence:
    """Replay a 4-step lifecycle and assert old/new gating decisions match.

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
        """Build canonical dict from a fixture for nws_decide()."""
        env = fixture["envelope"]
        inner = env.get("data") or {}
        d = inner.get("data") or {}
        category_raw = inner.get("category") or ""
        from meshai.central.nws_handler import _category_to_event_type, _parse_iso
        return {
            "cap_id": d.get("id"),
            "event": d.get("event") or _category_to_event_type(category_raw),
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

    def test_old_gate_sequence(self, mem_db):
        """4-step lifecycle through OLD handle_nws: first→dup<3h→dup>3h→Cancel."""
        cap_id = "urn:oid:old.gate.001"
        t0 = 1_783_200_000
        t1 = t0 + 1000      # <3h
        t2 = t0 + 11000     # >3h (10800s window)
        t3 = t0 + 12000

        def go(msg_type="Alert", now=t0):
            env = self._make_envelope(cap_id, msg_type=msg_type)["envelope"]
            data = {}
            wire = handle_nws(env, "central.wx.alert.us.id", data=data, now=int(now))
            if wire is not None and "_on_broadcast_committed" in data:
                data["_on_broadcast_committed"](float(now))
            return wire is not None

        assert go(now=t0) is True,  "step1: first sighting should broadcast"
        assert go(now=t1) is False, "step2: dup within 3h should suppress"
        assert go(now=t2) is True,  "step3: after 3h should rebroadcast"
        assert go("Cancel", now=t3) is False, "step4: Cancel tombstone should suppress"

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

        # Broadcast parent first
        fix_parent = self._make_envelope(parent_id)
        data_p = {}
        wire_p = handle_nws(fix_parent["envelope"], fix_parent["subject"], data=data_p, now=int(t0))
        assert wire_p is not None, "parent should broadcast"
        if "_on_broadcast_committed" in data_p:
            data_p["_on_broadcast_committed"](t0)

        # Child references parent
        fix_child = self._make_envelope(
            child_id,
            references=[{"identifier": parent_id, "sent": "2026-07-04T00:00:00Z",
                          "effective": "2026-07-04T00:00:00Z"}],
        )

        from meshai.central.nws_handler import _parse_iso, _category_to_event_type
        inner = fix_child["envelope"].get("data") or {}
        d = inner.get("data") or {}
        category_raw = inner.get("category") or ""
        canonical = {
            "cap_id": child_id,
            "event": d.get("event") or _category_to_event_type(category_raw),
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

        gate_child = nws_decide(canonical, source="nws", now=t1)
        assert gate_child.broadcast is True, f"child should broadcast: {gate_child.reason}"
        assert gate_child.data_patch.get("_nws_prefix") == "Update", (
            f"child referencing a broadcast parent should get 'Update' prefix, "
            f"got {gate_child.data_patch.get('_nws_prefix')!r}"
        )


# =============================================================================
# 4. Schema-conformance: native env/nws.py emits canonical schema
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
# 5. Formatter registration: weather_warning + weather_statement registered
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
