"""Phase-3 hydro (USGS NWIS) refactor tests.

Verifies the formatter+decider for the stream-gauge hazard (gating/hydro.py,
formatters/hydro.py), mirroring test_quake_refactor.py:

1. Golden: formatters.hydro.format() renders the expected wire, for a
   stage-only crossing, a paired flow(00060)+stage(00065) reading, and every
   threshold label.

2. Gate-sequence: an explicit `now`-timeline of readings driven through
   gating.hydro.decide() (upward crossing broadcasts; same-rank + receding
   suppress unless broadcast_on_recede).

The Central `nwis_handler` module (`_render()`, `handle_nwis()`) has been
deleted along with the rest of the Central NATS consumer path. Pure
old-vs-new parity assertions have been removed (original diffs are
preserved in git history); what remains asserts against hand-written
expected strings / broadcast outcomes.

decide() is READ-ONLY over gauge_readings by design (the append-only INSERT
was always caller-owned -- previously the Central handler, inline,
immediately after calling decide()). With the handler gone there is no
current producer for the "stream_flow" category in production (no native
env/ adapter emits it -- meshai.env.usgs.USGSStreamsAdapter is a separate,
older stream-gauge pipeline with different categories/schema). Tests below
that need prior-reading state seed gauge_readings directly via a local SQL
helper that mirrors the deleted handler's INSERT shape, so the gate logic
itself stays under direct, native-only test coverage.

The real registry / cutover key is "stream_flow" — the flat category the
Central nwis path used to produce for every central.hydro.* envelope.
"""
from __future__ import annotations

import pytest

from meshai.persistence import close_thread_connection, init_db
from meshai.persistence import db as persistence_db
from tests.harness.goldens import assert_byte_identical

_AT = 1_783_200_000.0  # pinned epoch (unused by hydro render/gate, kept for parity)


# ── DB fixture (same shape as test_nwis_handler.py) ──────────────────────────

@pytest.fixture
def mem_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "hydro-refactor-test.sqlite")
    monkeypatch.setenv("MESHAI_DB_PATH", db_path)
    persistence_db._initialised.clear()
    close_thread_connection()
    conn = init_db()
    yield conn
    close_thread_connection()
    persistence_db._initialised.discard(db_path)


def _make_fake_event(data: dict):
    class _FakeEvent:
        pass
    e = _FakeEvent()
    e.data = data
    return e


# ─────────────────────────────────────────────────────────────────────────────
# 1. Golden byte-identical — formatter reproduces _render() exactly
# ─────────────────────────────────────────────────────────────────────────────

class TestFormatterGolden:
    """formatters.hydro.format() renders the expected wire for canonical data."""

    def _fmt_new(self, canonical: dict) -> str:
        from meshai.notifications.formatters.hydro import format as hfmt
        return hfmt(_make_fake_event(canonical), now=_AT, budget=140)

    def test_stage_only_crossing_action(self):
        """Stage-only (00065) reading at action stage, no companion flow."""
        canonical = {
            "gauge_name": "Snake River at Heise",
            "threshold_state": "action",
            "stage_ft": 12.5,
            "flow_cfs": None,
            "unit": "ft",
            "lat": 43.612,
            "lon": -111.654,
        }
        new = self._fmt_new(canonical)
        assert_byte_identical(
            new, "🌊 New: Snake River at Heise: action stage 12.5 ft, @ 43.612,-111.654"
        )

    def test_paired_flow_and_stage(self):
        """00060 discharge back-looked onto a 00065 stage: flow segment present."""
        canonical = {
            "gauge_name": "Boise River",
            "threshold_state": "flood_minor",
            "stage_ft": 14.5,
            "flow_cfs": 8400,
            "unit": "ft",
            "lat": 43.600,
            "lon": -116.200,
        }
        new = self._fmt_new(canonical)
        assert_byte_identical(
            new,
            "🌊 New: Boise River: minor flooding 14.5 ft, flow 8,400 cfs, @ 43.600,-116.200",
        )

    @pytest.mark.parametrize(
        "state,label",
        [
            ("action", "action stage"),
            ("flood_minor", "minor flooding"),
            ("flood_moderate", "moderate flooding"),
            ("flood_major", "major flooding"),
        ],
    )
    def test_every_threshold_label(self, state, label):
        """Each threshold_state maps to the correct label."""
        canonical = {
            "gauge_name": "Test Gauge",
            "threshold_state": state,
            "stage_ft": 20.0,
            "flow_cfs": None,
            "unit": "ft",
            "lat": 44.0,
            "lon": -114.0,
        }
        new = self._fmt_new(canonical)
        assert_byte_identical(
            new, f"🌊 New: Test Gauge: {label} 20.0 ft, @ 44.000,-114.000"
        )

    def test_missing_coords_drops_at_tail(self):
        """No coords → no @ segment."""
        canonical = {
            "gauge_name": "No Coords Gauge",
            "threshold_state": "action",
            "stage_ft": 10.0,
            "flow_cfs": None,
            "unit": "ft",
            "lat": None,
            "lon": None,
        }
        new = self._fmt_new(canonical)
        assert_byte_identical(new, "🌊 New: No Coords Gauge: action stage 10.0 ft")


# ─────────────────────────────────────────────────────────────────────────────
# 2. Gate-sequence parity — old handle_nwis vs new gating.hydro.decide()
# ─────────────────────────────────────────────────────────────────────────────

def _nwis_env(*, site_id="USGS-13186000", parameter_code="00065", value=13.0,
              unit="ft", time_iso="2026-06-05T15:00:00Z",
              lat=43.612, lon=-111.654, envelope_id=None):
    envelope_id = envelope_id or f"nwis_{site_id}_{time_iso}"
    return {
        "id": envelope_id,
        "subject": f"central.hydro.{parameter_code}.usgs.{site_id}.us.id",
        "data": {
            "id": envelope_id, "adapter": "nwis",
            "category": f"hydro.{parameter_code}", "severity": 0,
            "geo": {"centroid": [lon, lat], "primary_region": "US-ID"},
            "data": {
                "id": envelope_id,
                "monitoring_location_id": site_id,
                "parameter_code": parameter_code,
                "time": time_iso,
                "value": value,
                "unit_of_measure": unit,
                "latitude": lat, "longitude": lon,
            },
        },
    }


def _parse_iso_epoch(s):
    from datetime import datetime
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def _insert_reading(conn, *, site_id, gauge_name, value, unit,
                    threshold_state, flow_cfs, reading_time, lat, lon):
    """Directly seed a gauge_readings row.

    Mirrors the schema the deleted Central nwis_handler used to INSERT
    inline, immediately after calling decide(). decide() is read-only over
    this table by design (see gating/hydro.py docstring) -- the INSERT was
    always caller-owned, so tests seed state directly instead of reaching
    into the deleted handler.
    """
    conn.execute(
        "INSERT INTO gauge_readings(site_id, gauge_name, reading_value, "
        "reading_unit, threshold_state, flow_cfs, reading_time, lat, lon) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (site_id, gauge_name, value, unit, threshold_state, flow_cfs,
         reading_time, lat, lon),
    )


class TestGateSequenceParity:
    """gating.hydro.decide() broadcast/suppress across a reading timeline."""

    @pytest.fixture(autouse=True)
    def _db(self, mem_db):
        self.db = mem_db

    def _canonical(self, fixture):
        """Build canonical data from a Central-style fixture (as the deleted
        handler used to) via the still-live idaho_gauge_sites helpers."""
        from meshai.central.idaho_gauge_sites import (
            compute_threshold_state, lookup_site, normalize_site_id,
        )
        env = fixture["envelope"]
        d = env["data"]["data"]
        raw_site = d.get("monitoring_location_id")
        site_id = normalize_site_id(raw_site)
        site_meta = lookup_site(raw_site)
        pc = d.get("parameter_code")
        value = float(d.get("value"))
        reading_time = _parse_iso_epoch(d.get("time"))
        stage_ft = value if pc == "00065" else None
        flow_cfs = value if pc == "00060" else None
        threshold_state = "normal"
        if pc == "00065":
            threshold_state = compute_threshold_state(stage_ft, site_meta)
        canonical = {
            "site_id": site_id,
            "gauge_name": site_meta["gauge_name"],
            "stage_ft": stage_ft,
            "flow_cfs": flow_cfs,
            "unit": d.get("unit_of_measure"),
            "threshold_state": threshold_state,
            "reading_time": reading_time,
            "lat": d.get("latitude"),
            "lon": d.get("longitude"),
            "parameter_code": pc,
        }
        return canonical, value

    def _decide(self, fixture, *, now):
        """decide() only (no persist) — for assertions on a single reading."""
        from meshai.notifications.gating.hydro import decide
        canonical, _value = self._canonical(fixture)
        return decide(canonical, source="nwis", now=float(now))

    def _decide_and_persist(self, fixture, *, now):
        """decide() then INSERT the resolved reading — mirrors the deleted
        handler's decide-then-insert ordering, so later steps in a sequence
        see accumulated prior state exactly as production would."""
        from meshai.notifications.gating.hydro import decide
        canonical, value = self._canonical(fixture)
        gate = decide(canonical, source="nwis", now=float(now))
        threshold_state = gate.data_patch.get("threshold_state", canonical["threshold_state"])
        stage_ft = gate.data_patch.get("stage_ft", canonical["stage_ft"])
        _insert_reading(
            self.db,
            site_id=canonical["site_id"], gauge_name=canonical["gauge_name"],
            value=value, unit=canonical["unit"], threshold_state=threshold_state,
            flow_cfs=canonical["flow_cfs"], reading_time=canonical["reading_time"],
            lat=canonical["lat"], lon=canonical["lon"],
        )
        return gate

    def test_gate_sequence_matches(self):
        """Timeline of Heise readings: decide() agrees with expectations at every step.

        Heise (USGS-13186000): action=12.0ft.
          [0] 8.0 ft  normal  (first reading, no prior)        → suppress
          [1] 12.5 ft action  (normal → action upward)          → broadcast
          [2] 12.8 ft action  (action → action same rank)       → suppress
          [3] 14.5 ft f_minor (action → flood_minor upward)     → broadcast
          [4] 8.0 ft  normal  (flood_minor → normal receding)   → suppress
        """
        base = _parse_iso_epoch("2026-06-05T10:00:00Z")
        specs = [
            (8.0,  "2026-06-05T10:00:00Z", "s0"),
            (12.5, "2026-06-05T10:15:00Z", "s1"),
            (12.8, "2026-06-05T10:30:00Z", "s2"),
            (14.5, "2026-06-05T10:45:00Z", "s3"),
            (8.0,  "2026-06-05T11:00:00Z", "s4"),
        ]
        ordered = [
            {"envelope": _nwis_env(value=v, time_iso=t, envelope_id=eid)}
            for (v, t, eid) in specs
        ]
        timeline = [float(base + i * 900) for i in range(len(specs))]

        results = [
            self._decide_and_persist(fx, now=t)
            for fx, t in zip(ordered, timeline)
        ]

        assert results[0].broadcast is False, "normal first reading suppressed"
        assert results[1].broadcast is True,  "normal→action broadcasts"
        assert results[2].broadcast is False, "action→action suppressed"
        assert results[3].broadcast is True,  "action→flood_minor broadcasts"
        assert results[4].broadcast is False, "receding suppressed (no toggle)"

    def test_00060_backlook_inherits_stage_band(self, mem_db):
        """A 00060 discharge reading inherits the last 00065 stage band.

        Seed an action-stage 00065 reading, then feed a 00060 discharge: the
        decider's back-look must resolve threshold_state=action + the prior
        stage_ft, and (same rank as the seeded action) suppress the discharge.
        """
        env_stage = _nwis_env(parameter_code="00065", value=12.5,
                              time_iso="2026-06-05T10:00:00Z", envelope_id="seed")
        self._decide_and_persist({"envelope": env_stage}, now=1_000_000)

        # Now decide on a 00060 discharge — should back-look the action band.
        env_flow = _nwis_env(parameter_code="00060", value=8400, unit="ft^3/s",
                             time_iso="2026-06-05T10:05:00Z", envelope_id="q")
        gate = self._decide({"envelope": env_flow}, now=1_000_300)
        assert gate.data_patch["threshold_state"] == "action"
        assert gate.data_patch["stage_ft"] == 12.5
        # action → action (same rank) → suppress
        assert gate.broadcast is False

    def test_recede_toggle_enables_broadcast(self, mem_db):
        """With broadcast_on_recede set, a receding crossing broadcasts."""
        from meshai.adapter_config._accessor import set_runtime_override, _overrides
        # Seed an action reading.
        env_high = _nwis_env(parameter_code="00065", value=12.5,
                            time_iso="2026-06-05T10:00:00Z", envelope_id="hi")
        self._decide_and_persist({"envelope": env_high}, now=1_000_000)

        # Force the recede toggle on for the decision only (runtime override,
        # since adapter_config accessors are read-only).
        set_runtime_override("usgs_nwis", "broadcast_on_recede", True)
        try:
            env_low = _nwis_env(parameter_code="00065", value=8.0,
                               time_iso="2026-06-05T11:00:00Z", envelope_id="lo")
            gate = self._decide({"envelope": env_low}, now=1_003_600)
        finally:
            _overrides.pop(("usgs_nwis", "broadcast_on_recede"), None)
        assert gate.broadcast is True, "receding must broadcast when toggle is on"
        assert gate.data_patch["threshold_state"] == "normal"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Registration — stream_flow resolves to hydro formatter + decider
# ─────────────────────────────────────────────────────────────────────────────

class TestRegistration:
    def test_stream_flow_formatter_registered(self):
        from meshai.notifications.formatters import get_formatter
        from meshai.notifications.formatters.hydro import format as hfmt
        assert get_formatter("stream_flow") is hfmt

    def test_stream_flow_decider_registered(self):
        from meshai.notifications.gating import get_decider
        from meshai.notifications.gating.hydro import decide
        assert get_decider("stream_flow") is decide
