"""Phase-1 SWPC refactor tests.

The Central `swpc_handler` module (`_render()`, `handle_swpc()`) has been
deleted — the native path is the only production path now. Pure old-vs-new
parity assertions, the flare_class-string-parsing tests that only existed
to exercise the deleted handler's Central-only mapping, and the
`solar_radiation_storm`/proton "legacy path" test (which imported the now
also-deleted `swpc_handler` -- proton events have no broadcast path at all
post-excision, native or otherwise; flagged for Matt, not fixed here) have
been removed. Original diffs are preserved in git history. What remains
exercises native code directly (hand-written expected strings are kept as
regression pins on the current wire format).

Five test groups:

1. Parity — for a kindex-style fixture and a flare fixture, the formatter
   produces the expected wire text (noting tier-b severity fix).

2. Cross-source identity — same Kp from swpc_kindex and swpc_alerts shares
   the 600s geomag dedup window (committed broadcast suppresses the second).

3. Gate sequence — Kp crossing G1 → G3 → G3-within-600s-window → G5.
   Verifies in-window suppression and G5 passes (different scale_code).

4. Flare R-scale floor — R1/R2 suppressed, R3+ passes.

5. Schema conformance — to_event() emits all required canonical fields.

6. Proton NOT registered — solar_radiation_storm is absent from both
   FORMATTERS and DECIDERS registries.
"""
from __future__ import annotations

import pytest

from meshai.persistence import close_thread_connection, init_db
from meshai.persistence import db as persistence_db
from tests.harness.goldens import pinned_time


# ── Shared clock epoch ───────────────────────────────────────────────────────
_AT = 1_783_200_000.0  # 2026-07-03T00:00:00Z (pinned)

# ── DB fixture ───────────────────────────────────────────────────────────────

@pytest.fixture
def mem_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "swpc-refactor-test.sqlite")
    monkeypatch.setenv("MESHAI_DB_PATH", db_path)
    persistence_db._initialised.clear()
    close_thread_connection()
    conn = init_db()
    # Clear the gating module's geomag window between tests.
    from meshai.notifications.gating import swpc as _swpc_gate
    _swpc_gate._geomag_window.clear()
    yield conn
    close_thread_connection()
    persistence_db._initialised.discard(db_path)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_fake_event(data: dict):
    """Minimal fake Event for calling the formatter without the full pipeline."""
    class _FakeEvent:
        pass
    e = _FakeEvent()
    e.data = data
    return e


def _kindex_env(*, kp: float, event_id: str):
    """Build a Central-style swpc_kindex envelope."""
    return {
        "id": event_id,
        "subject": "central.space.kindex",
        "data": {
            "id": event_id,
            "adapter": "swpc_kindex",
            "category": "space.kindex",
            "severity": 0,
            "geo": {},
            "data": {"id": event_id, "kp_index": kp, "time": "2026-07-04T05:00:00Z"},
        },
    }


def _alert_env(*, event_id: str, kp: float | None = None,
               flare_class: str | None = None):
    """Build a Central-style swpc_alerts envelope."""
    d: dict = {"id": event_id, "product_id": event_id,
               "time": "2026-07-04T05:10:00Z"}
    if kp is not None:
        d["kp_index"] = kp
    if flare_class is not None:
        d["flare_class"] = flare_class
    return {
        "id": event_id,
        "subject": "central.space.alert." + event_id.lower(),
        "data": {
            "id": event_id,
            "adapter": "swpc_alerts",
            "category": "space.alert",
            "severity": 0,
            "geo": {},
            "data": d,
        },
    }


def _commit(data: dict, t: float) -> None:
    cb = data.get("_on_broadcast_committed")
    if cb is not None:
        cb(float(t))


# ─────────────────────────────────────────────────────────────────────────────
# 1. Parity — formatter output matches old _render() (with tier-b severity note)
# ─────────────────────────────────────────────────────────────────────────────

class TestFormatterParity:
    """formatters/swpc.format() renders the expected wire text from canonical data."""

    def test_kindex_g3_parity(self, mem_db):
        """Kp=7 (G3) kindex envelope → formatter output matches hand-written expected.

        Tier-b note: _severity_override (now "priority" instead of
        missing/routine) does NOT affect the wire text.
        """
        from meshai.notifications.formatters.swpc import format as sfmt

        # Canonical data as handle_swpc would build it for Kp=7.
        canonical = {
            "event_id": "kp_parity_g3",
            "driver": "kp",
            "scalar": 7.0,
            "scale_code": "G3",
            "message": "HF degraded, aurora possible",
            "issued_at": "2026-07-04T05:00:00Z",
        }

        expected = (
            "🧲 New: G3 Geomagnetic Storm — Kp7"
            "\nHF degraded, aurora possible"
            "\nSWPC · 2026-07-04 05:00"
        )

        with pinned_time(_AT):
            new_wire = sfmt(_make_fake_event(canonical), now=_AT, budget=140)

        assert "G3" in new_wire, f"scale_code missing from wire: {new_wire!r}"
        assert "Kp7" in new_wire, f"scalar 'Kp7' missing from wire: {new_wire!r}"
        assert "Geomagnetic Storm" in new_wire
        assert new_wire == expected, (
            f"Wire mismatch for G3/Kp7:\n  expected: {expected!r}\n  got: {new_wire!r}"
        )

    def test_flare_x1_r3_parity(self, mem_db):
        """X1.0 flare (R3) alert → formatter output matches hand-written expected.

        Fixture mirrors swpc_last/0003.json (XX0S, X1.0 flare, R3 Strong).
        """
        from meshai.notifications.formatters.swpc import format as sfmt

        canonical = {
            "event_id": "flare_x10_parity",
            "driver": "flare",
            "scalar": "X1.0",
            "scale_code": "R3",
            "message": "HF radio fading, GPS may glitch",
            "issued_at": "2026-06-03T11:59:00Z",
        }

        expected = (
            "☀️ New: X1.0 Solar Flare — R3"
            "\nHF radio fading, GPS may glitch"
            "\nSWPC · 2026-06-03 11:59"
        )

        with pinned_time(_AT):
            new_wire = sfmt(_make_fake_event(canonical), now=_AT, budget=140)

        assert "R3" in new_wire
        assert "X1.0" in new_wire
        assert "Solar Flare" in new_wire
        assert new_wire == expected, (
            f"Wire mismatch for X1.0/R3:\n  expected: {expected!r}\n  got: {new_wire!r}"
        )

    def test_g5_kp9_parity(self, mem_db):
        """Kp=9 (G5) renders correctly — extreme label and scalar."""
        from meshai.notifications.formatters.swpc import format as sfmt

        canonical = {
            "event_id": "kp_g5_parity",
            "driver": "kp",
            "scalar": 9.0,
            "scale_code": "G5",
            "message": "Widespread power disruptions possible",
            "issued_at": "2026-07-04T08:00:00Z",
        }

        expected = (
            "🧲 New: G5 Geomagnetic Storm — Kp9"
            "\nWidespread power disruptions possible"
            "\nSWPC · 2026-07-04 08:00"
        )

        with pinned_time(_AT):
            new_wire = sfmt(_make_fake_event(canonical), now=_AT, budget=140)

        assert "G5" in new_wire
        assert "Kp9" in new_wire
        assert new_wire == expected, (
            f"G5 wire mismatch:\n  expected: {expected!r}\n  got: {new_wire!r}"
        )

    def test_null_scalar_renders_without_dash_tail(self, mem_db):
        """Native path: scalar=None → renders without '— Kp?' tail."""
        from meshai.notifications.formatters.swpc import format as sfmt

        canonical = {
            "event_id": "native_g3",
            "driver": "kp",
            "scalar": None,
            "scale_code": "G3",
            "message": "",
            "issued_at": None,
        }

        with pinned_time(_AT):
            wire = sfmt(_make_fake_event(canonical), now=_AT, budget=140)

        assert "G3" in wire
        assert "Geomagnetic Storm" in wire
        # No "—" dash when scalar is None (no Kp to show)
        assert "Kp" not in wire, f"Unexpected Kp in wire when scalar=None: {wire!r}"


# ─────────────────────────────────────────────────────────────────────────────
# 2. Cross-source identity — geomag 600s window shared across sub-adapters
# ─────────────────────────────────────────────────────────────────────────────

class TestCrossSourceIdentity:
    """Same Kp/scale from two different sub-adapters shares the 600s window."""

    @pytest.fixture(autouse=True)
    def _setup(self, mem_db):
        self.db = mem_db

    def test_kindex_then_alert_same_g3_suppressed(self):
        """swpc_kindex G3 → commit → swpc_alerts G3 within 600s → suppress.

        Uses different event_ids (realistic: kindex and alerts have distinct ids).
        """
        from meshai.notifications.gating.swpc import decide, _geomag_window

        t0 = _AT
        canonical_kindex = {
            "event_id": "ci_kindex_g3",
            "driver": "kp",
            "scalar": 7.0,
            "scale_code": "G3",
            "message": "",
            "issued_at": None,
        }
        canonical_alert = {
            "event_id": "ci_alert_g3",      # different event_id
            "driver": "kp",
            "scalar": 7.0,
            "scale_code": "G3",
            "message": "",
            "issued_at": None,
        }

        # First broadcast: swpc_kindex
        gate1 = decide(canonical_kindex, source="swpc", now=t0)
        assert gate1.broadcast, "First G3 from kindex must broadcast"

        # Commit fires the window stamp
        gate1.commit(t0 + 1.0)
        assert _geomag_window.get("G3") == t0 + 1.0, (
            "Window stamp must be set on commit, not on decision"
        )

        # Second broadcast: swpc_alerts, same G3, within 600s
        gate2 = decide(canonical_alert, source="swpc", now=t0 + 300)
        assert not gate2.broadcast, (
            "Second G3 from alerts within 600s must be suppressed by window"
        )
        assert "geomag dedup" in gate2.reason.lower(), (
            f"Suppression reason must mention geomag dedup: {gate2.reason!r}"
        )

    def test_window_expires_after_600s(self):
        """After 600s the window resets and a new G3 can broadcast."""
        from meshai.notifications.gating.swpc import decide, _geomag_window

        t0 = _AT
        c1 = {"event_id": "window_1", "driver": "kp", "scalar": 7.0,
               "scale_code": "G3", "message": "", "issued_at": None}
        c2 = {"event_id": "window_2", "driver": "kp", "scalar": 7.0,
               "scale_code": "G3", "message": "", "issued_at": None}

        gate1 = decide(c1, source="swpc", now=t0)
        assert gate1.broadcast
        gate1.commit(t0 + 1.0)

        # 601s later — window expired
        gate2 = decide(c2, source="swpc", now=t0 + 601)
        assert gate2.broadcast, "G3 after 601s should broadcast (window expired)"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Gate sequence — Kp G1 → G3 → G3-in-window → G5
# ─────────────────────────────────────────────────────────────────────────────

class TestGateSequence:
    """Kp crossing G1→G3→G3-within-window→G5 gate sequence."""

    @pytest.fixture(autouse=True)
    def _setup(self, mem_db):
        self.db = mem_db

    def test_kp_gate_sequence(self):
        """Four-step sequence verifying floor, window, and scale-escalation.

        Step 0: G1 (Kp=5) → below G3 floor → suppress
        Step 1: G3 (Kp=7) → first sighting → broadcast
        Step 2: G3 again, within 600s → geomag window → suppress
        Step 3: G5 (Kp=9), within 600s → NEW scale_code "G5" → broadcast
        """
        from meshai.notifications.gating.swpc import decide

        t0 = _AT

        # Step 0: G1 — below floor
        c_g1 = {"event_id": "seq_g1", "driver": "kp", "scalar": 5.0,
                 "scale_code": "G1", "message": "", "issued_at": None}
        gate0 = decide(c_g1, source="swpc", now=t0)
        assert not gate0.broadcast, "G1 must be suppressed (below G3 floor)"
        assert "floor" in gate0.reason.lower() or "below" in gate0.reason.lower()

        # Step 1: G3 — first sighting
        c_g3 = {"event_id": "seq_g3_first", "driver": "kp", "scalar": 7.0,
                 "scale_code": "G3", "message": "", "issued_at": None}
        gate1 = decide(c_g3, source="swpc", now=t0 + 10)
        assert gate1.broadcast, "G3 first sighting must broadcast"
        assert gate1.data_patch.get("_severity_override") == "priority"
        assert gate1.data_patch.get("_cooldown_suffix") == "G3"

        # Commit: arm window
        gate1.commit(t0 + 11)

        # Step 2: G3 from different sub-adapter, within 600s → suppressed by window
        c_g3b = {"event_id": "seq_g3_second", "driver": "kp", "scalar": 7.0,
                  "scale_code": "G3", "message": "", "issued_at": None}
        gate2 = decide(c_g3b, source="swpc", now=t0 + 200)
        assert not gate2.broadcast, "G3 within 600s window must be suppressed"

        # Step 3: G5 escalation — different scale_code, window doesn't apply
        c_g5 = {"event_id": "seq_g5", "driver": "kp", "scalar": 9.0,
                 "scale_code": "G5", "message": "", "issued_at": None}
        gate3 = decide(c_g5, source="swpc", now=t0 + 300)
        assert gate3.broadcast, "G5 must broadcast (different scale_code from G3)"
        assert gate3.data_patch.get("_severity_override") == "immediate", (
            f"G5 must be 'immediate'; got {gate3.data_patch.get('_severity_override')!r}"
        )

    def test_commit_deferred_window_stamp(self):
        """Window stamp happens on commit, not on decision."""
        from meshai.notifications.gating.swpc import decide, _geomag_window

        t0 = _AT
        c = {"event_id": "deferred_stamp", "driver": "kp", "scalar": 7.0,
             "scale_code": "G3", "message": "", "issued_at": None}

        # Before commit, window should not be stamped
        pre_stamp = _geomag_window.get("G3")
        gate = decide(c, source="swpc", now=t0)
        assert gate.broadcast
        post_decide_stamp = _geomag_window.get("G3")
        assert post_decide_stamp == pre_stamp, (
            "Window must NOT be stamped at decision time — deferred to commit"
        )

        # After commit, window is stamped
        gate.commit(t0 + 5.0)
        assert _geomag_window.get("G3") == t0 + 5.0, (
            "Window must be stamped with committed_at on commit"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 4. Flare R-scale floor — R1/R2 suppressed, R3+ passes
# ─────────────────────────────────────────────────────────────────────────────

class TestFlareRScaleFloor:
    """R-scale floor gate: R1/R2 suppressed, R3/R4/R5 passes."""

    @pytest.fixture(autouse=True)
    def _setup(self, mem_db):
        self.db = mem_db

    def _flare_canonical(self, scale_code: str, event_id: str,
                         scalar: str = "X1.0") -> dict:
        return {
            "event_id": event_id,
            "driver": "flare",
            "scalar": scalar,
            "scale_code": scale_code,
            "message": "",
            "issued_at": None,
        }

    def test_r1_suppressed(self):
        from meshai.notifications.gating.swpc import decide
        gate = decide(self._flare_canonical("R1", "r1_test"), source="swpc", now=_AT)
        assert not gate.broadcast, "R1 must be suppressed (below R3 floor)"

    def test_r2_suppressed(self):
        from meshai.notifications.gating.swpc import decide
        gate = decide(self._flare_canonical("R2", "r2_test"), source="swpc", now=_AT)
        assert not gate.broadcast, "R2 must be suppressed (below R3 floor)"

    def test_r3_broadcasts(self):
        from meshai.notifications.gating.swpc import decide
        gate = decide(self._flare_canonical("R3", "r3_test"), source="swpc", now=_AT)
        assert gate.broadcast, "R3 must broadcast (meets floor)"
        assert gate.data_patch.get("_severity_override") == "priority"

    def test_r4_broadcasts_immediate(self):
        from meshai.notifications.gating.swpc import decide
        gate = decide(self._flare_canonical("R4", "r4_test", scalar="X10.0"),
                      source="swpc", now=_AT)
        assert gate.broadcast, "R4 must broadcast"
        assert gate.data_patch.get("_severity_override") == "immediate"

    def test_r5_broadcasts_immediate(self):
        from meshai.notifications.gating.swpc import decide
        gate = decide(self._flare_canonical("R5", "r5_test", scalar="X20.0"),
                      source="swpc", now=_AT)
        assert gate.broadcast, "R5 must broadcast"
        assert gate.data_patch.get("_severity_override") == "immediate"


# ─────────────────────────────────────────────────────────────────────────────
# 5. Schema conformance — to_event() emits required canonical fields
# ─────────────────────────────────────────────────────────────────────────────

class TestSchemaConformance:
    """env/swpc.py to_event() emits canonical data schema fields."""

    CANONICAL_KEYS = frozenset({
        "event_id", "driver", "scalar", "scale_code", "message", "issued_at",
    })

    def _make_swpc_evt(self, scale: str, level: int) -> dict:
        """Build the internal evt dict that _update_events() produces."""
        scale_letter = scale.upper()
        event_id = f"swpc_{scale.lower()}{level}"
        severity = "priority" if level >= 3 else "routine"
        return {
            "source": "swpc",
            "event_id": event_id,
            "event_type": f"{scale_letter}{level} {scale_letter} Storm",
            "scale": scale_letter,
            "level": level,
            "severity": severity,
            "headline": f"{scale_letter}{level} in progress",
            "expires": 9_999_999_999.0,
            "areas": [],
            "fetched_at": _AT,
        }

    def test_g3_canonical_keys_present(self):
        """G3 event has all canonical data keys."""
        from unittest.mock import MagicMock
        from meshai.env.swpc import SWPCAdapter

        cfg = MagicMock()
        adapter = SWPCAdapter(cfg)
        evt = self._make_swpc_evt("g", 3)
        event = adapter.to_event(evt)

        assert event is not None, "to_event() must return Event for G3"
        assert event.data is not None, "event.data must not be None"

        missing = self.CANONICAL_KEYS - set(event.data.keys())
        assert not missing, (
            f"G3 event.data missing canonical keys: {missing}\n"
            f"Got keys: {sorted(event.data.keys())}"
        )

    def test_g3_canonical_values(self):
        """G3 event.data has correct driver/scale_code values."""
        from unittest.mock import MagicMock
        from meshai.env.swpc import SWPCAdapter

        cfg = MagicMock()
        adapter = SWPCAdapter(cfg)
        evt = self._make_swpc_evt("g", 3)
        event = adapter.to_event(evt)

        assert event.data["driver"] == "kp", (
            f"G-scale driver must be 'kp'; got {event.data['driver']!r}"
        )
        assert event.data["scale_code"] == "G3"
        assert event.data["scalar"] is None  # not available from noaa-scales.json
        assert event.data["event_id"] == "swpc_g3"

    def test_r3_canonical_values(self):
        """R3 event.data has correct driver/scale_code values."""
        from unittest.mock import MagicMock
        from meshai.env.swpc import SWPCAdapter

        cfg = MagicMock()
        adapter = SWPCAdapter(cfg)
        evt = self._make_swpc_evt("r", 3)
        event = adapter.to_event(evt)

        assert event is not None
        assert event.data["driver"] == "flare", (
            f"R-scale driver must be 'flare'; got {event.data['driver']!r}"
        )
        assert event.data["scale_code"] == "R3"

    def test_s1_canonical_driver_none(self):
        """S-scale (solar radiation storm) has driver=None (not in new arch)."""
        from unittest.mock import MagicMock
        from meshai.env.swpc import SWPCAdapter

        cfg = MagicMock()
        adapter = SWPCAdapter(cfg)
        evt = self._make_swpc_evt("s", 1)
        event = adapter.to_event(evt)

        assert event is not None
        # S-scale gets driver=None since it's not in the new arch
        assert event.data["driver"] is None

    def test_canonical_data_doesnt_crash_formatter(self):
        """G3 from native to_event() can be fed to the formatter without crashing."""
        from unittest.mock import MagicMock
        from meshai.env.swpc import SWPCAdapter
        from meshai.notifications.formatters.swpc import format as sfmt

        cfg = MagicMock()
        adapter = SWPCAdapter(cfg)
        evt = self._make_swpc_evt("g", 3)
        event = adapter.to_event(evt)

        assert event is not None
        with pinned_time(_AT):
            result = sfmt(event, now=_AT, budget=140)

        assert result is not None
        assert "G3" in result
        assert len(result) <= 140, f"Budget exceeded: {len(result)} > 140"


# ─────────────────────────────────────────────────────────────────────────────
# 6. Proton NOT registered — solar_radiation_storm absent from registries
# ─────────────────────────────────────────────────────────────────────────────

class TestProtonNotRegistered:
    """solar_radiation_storm must not appear in either registry."""

    def test_solar_radiation_storm_not_in_formatters(self):
        from meshai.notifications.formatters import FORMATTERS
        assert "solar_radiation_storm" not in FORMATTERS, (
            "solar_radiation_storm must NOT be in FORMATTERS "
            "(proton events stay on legacy path)"
        )

    def test_solar_radiation_storm_not_in_deciders(self):
        from meshai.notifications.gating import DECIDERS
        assert "solar_radiation_storm" not in DECIDERS, (
            "solar_radiation_storm must NOT be in DECIDERS "
            "(proton events stay on legacy path)"
        )

    def test_geomagnetic_storm_in_formatters(self):
        from meshai.notifications.formatters import FORMATTERS
        assert "geomagnetic_storm" in FORMATTERS, (
            "geomagnetic_storm must be in FORMATTERS"
        )

    def test_rf_propagation_alert_in_formatters(self):
        from meshai.notifications.formatters import FORMATTERS
        assert "rf_propagation_alert" in FORMATTERS, (
            "rf_propagation_alert must be in FORMATTERS"
        )

    def test_geomagnetic_storm_in_deciders(self):
        from meshai.notifications.gating import DECIDERS
        assert "geomagnetic_storm" in DECIDERS, (
            "geomagnetic_storm must be in DECIDERS"
        )

    def test_rf_propagation_alert_in_deciders(self):
        from meshai.notifications.gating import DECIDERS
        assert "rf_propagation_alert" in DECIDERS, (
            "rf_propagation_alert must be in DECIDERS"
        )
