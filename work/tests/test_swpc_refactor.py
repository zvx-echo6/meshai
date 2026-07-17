"""Phase-1 SWPC refactor tests.

Seven test groups:

1. Parity — for a kindex-style fixture and a flare fixture, the new formatter
   produces output equivalent to old _render() (noting tier-b severity fix).
   Also covers proton (S-scale) parity against the old proton _render() branch.

2. Cross-source identity — same Kp from swpc_kindex and swpc_alerts shares
   the 600s geomag dedup window (committed broadcast suppresses the second).

3. Gate sequence — Kp crossing G1 → G3 → G3-within-600s-window → G5.
   Verifies in-window suppression and G5 passes (different scale_code).

4. Flare R-scale floor — R1/R2 suppressed, R3+ passes.

5. Schema conformance — to_event() emits all required canonical fields.

6. Proton registered — solar_radiation_storm IS present in both FORMATTERS
   and DECIDERS registries (native S-scale support), with its own S1+ floor
   (lower than G3+/R3+) recovered from central/swpc_handler.py's original
   proton threshold.
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
    """formatters/swpc.format() renders equivalent output to swpc_handler._render().

    PROVENANCE (pinned goldens): the `old_wire = "..."` literals below were
    captured by calling `meshai.central.swpc_handler._render()` directly
    (the pre-excision oracle) with the exact arguments each test previously
    passed through the now-removed `_render_old()` helper, then verified
    byte-identical against this branch's native formatter output before
    being pinned. They are a verified spec of Central's original wire
    format, NOT a snapshot of current native-formatter behavior — do not
    "fix" them to match a future formatter change; a mismatch means the
    formatter regressed, not the golden. swpc_handler.py is scheduled for
    deletion by PR #144 (chore/excise-dead-central-path); pinning these as
    literals removes the `from meshai.central.swpc_handler import _render`
    import so this file no longer breaks when that PR lands.
    """

    def test_kindex_g3_parity(self, mem_db):
        """Kp=7 (G3) kindex envelope → new formatter ≈ old _render.

        Tier-b note: the only intentional delta is _severity_override (now
        "priority" instead of missing/routine), which does NOT affect the
        wire text — parity is exact for the text body.
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

        old_wire = (
            "🧲 New: G3 Geomagnetic Storm — Kp7\n"
            "HF degraded, aurora possible\n"
            "SWPC · 2026-07-04 05:00"
        )

        with pinned_time(_AT):
            new_wire = sfmt(_make_fake_event(canonical), now=_AT, budget=140)

        # Content must match: same line 1 and line 2.
        assert "G3" in new_wire, f"scale_code missing from wire: {new_wire!r}"
        assert "Kp7" in new_wire, f"scalar 'Kp7' missing from wire: {new_wire!r}"
        assert "Geomagnetic Storm" in new_wire

        # Old wire content also present
        assert "G3" in old_wire
        assert "Kp7" in old_wire
        assert new_wire == old_wire, (
            f"Parity failure for G3/Kp7:\n  old: {old_wire!r}\n  new: {new_wire!r}"
        )

    def test_flare_x1_r3_parity(self, mem_db):
        """X1.0 flare (R3) alert → new formatter ≈ old _render.

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

        old_wire = (
            "☀️ New: X1.0 Solar Flare — R3\n"
            "HF radio fading, GPS may glitch\n"
            "SWPC · 2026-06-03 11:59"
        )

        with pinned_time(_AT):
            new_wire = sfmt(_make_fake_event(canonical), now=_AT, budget=140)

        assert "R3" in new_wire
        assert "X1.0" in new_wire
        assert "Solar Flare" in new_wire
        assert new_wire == old_wire, (
            f"Parity failure for X1.0/R3:\n  old: {old_wire!r}\n  new: {new_wire!r}"
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

        old_wire = (
            "🧲 New: G5 Geomagnetic Storm — Kp9\n"
            "Widespread power disruptions possible\n"
            "SWPC · 2026-07-04 08:00"
        )

        with pinned_time(_AT):
            new_wire = sfmt(_make_fake_event(canonical), now=_AT, budget=140)

        assert "G5" in new_wire
        assert "Kp9" in new_wire
        assert new_wire == old_wire, (
            f"G5 parity failure:\n  old: {old_wire!r}\n  new: {new_wire!r}"
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

    def test_proton_s1_parity(self, mem_db):
        """S1 proton event (10 pfu) -> new formatter matches old _render()
        proton branch.

        central/swpc_handler.py._render's "proton" branch (event_kind="proton")
        is the specification recovered here: it is DERIVED from the old code,
        not captured from the new formatter's own output. scalar="10 pfu"
        mirrors the scalar_str central computed for a 10 pfu / S1 reading
        (swpc_handler.py L256).
        """
        from meshai.notifications.formatters.swpc import format as sfmt

        canonical = {
            "event_id": "proton_s1_parity",
            "driver": None,          # native env/swpc.py always sets driver=None for S
            "scalar": "10 pfu",
            "scale_code": "S1",
            "message": "Polar HF radio affected",
            "issued_at": "2026-07-04T06:00:00Z",
        }

        old_wire = (
            "☢️ New: S1 Radiation Storm — 10 pfu\n"
            "Polar HF radio affected\n"
            "SWPC · 2026-07-04 06:00"
        )

        with pinned_time(_AT):
            new_wire = sfmt(_make_fake_event(canonical), now=_AT, budget=140)

        assert "S1" in new_wire
        assert "10 pfu" in new_wire
        assert "Radiation Storm" in new_wire
        assert "☢️" in new_wire
        assert new_wire == old_wire, (
            f"Parity failure for S1/10 pfu:\n  old: {old_wire!r}\n  new: {new_wire!r}"
        )

    def test_proton_s5_parity(self, mem_db):
        """S5 proton event (200000 pfu) -> new formatter matches old _render().

        Derived from central/swpc_handler.py._render, same as test_proton_s1_parity.
        """
        from meshai.notifications.formatters.swpc import format as sfmt

        canonical = {
            "event_id": "proton_s5_parity",
            "driver": None,
            "scalar": "200000 pfu",
            "scale_code": "S5",
            "message": "Widespread satellite/HF disruption",
            "issued_at": "2026-07-04T09:00:00Z",
        }

        old_wire = (
            "☢️ New: S5 Radiation Storm — 200000 pfu\n"
            "Widespread satellite/HF disruption\n"
            "SWPC · 2026-07-04 09:00"
        )

        with pinned_time(_AT):
            new_wire = sfmt(_make_fake_event(canonical), now=_AT, budget=140)

        assert "S5" in new_wire
        assert new_wire == old_wire, (
            f"Parity failure for S5:\n  old: {old_wire!r}\n  new: {new_wire!r}"
        )

    def test_proton_null_scalar_renders_without_dash_tail(self, mem_db):
        """Native path: S-scale scalar=None (no pfu reading available from
        noaa-scales.json) -> renders without '— N pfu' tail.

        This is the real shape env/swpc.py produces today (see
        TestSchemaConformance.test_s1_canonical_driver_none) — unlike the
        S1/S5 parity tests above, which use a hypothetical scalar to prove
        wire-format parity with the old Central proton renderer.
        """
        from meshai.notifications.formatters.swpc import format as sfmt

        canonical = {
            "event_id": "native_s1",
            "driver": None,
            "scalar": None,
            "scale_code": "S1",
            "message": "",
            "issued_at": None,
        }

        with pinned_time(_AT):
            wire = sfmt(_make_fake_event(canonical), now=_AT, budget=140)

        assert "S1" in wire
        assert "Radiation Storm" in wire
        assert "pfu" not in wire, f"Unexpected pfu in wire when scalar=None: {wire!r}"
        assert "Polar HF radio affected" in wire  # default line2 fallback


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

    # NOTE: test_m5_flare_suppressed_via_handler and
    # test_x1_flare_broadcasts_via_handler previously lived here, exercising
    # Central's flare_class ("M5.5"/"X1.0") -> R-scale string-parsing
    # (swpc_handler.py::_flare_r_scale) via the legacy handle_swpc() entry
    # point. That mapping is Central-only dead logic: the native path
    # (env/swpc.py) never parses flare_class text — it reads the R-scale
    # level directly from noaa-scales.json, same as G/S. There is no native
    # equivalent to convert these to. Removed (rather than converted) because
    # the behavior they actually cared about — R2 suppressed / R3 broadcasts
    # at the floor — is already covered natively by test_r2_suppressed and
    # test_r3_broadcasts above, and the R3/X1.0 wire-format text is covered
    # by TestFormatterParity.test_flare_x1_r3_parity. Deleting them removes
    # their `from meshai.central.swpc_handler import handle_swpc` imports,
    # which would otherwise ModuleNotFoundError once PR #144
    # (chore/excise-dead-central-path) deletes that module.


# ─────────────────────────────────────────────────────────────────────────────
# 4b. Proton S-scale floor — S1+ passes (lower floor than G3+/R3+)
# ─────────────────────────────────────────────────────────────────────────────

class TestProtonSScaleFloor:
    """S-scale (proton) floor gate: S1+ broadcasts — unlike G/R's G3+/R3+ floor.

    Threshold provenance: central/swpc_handler.py's module docstring states
    the original proton broadcast rule verbatim: "Solar proton event >= 10 pfu
    @ >= 10 MeV (S1 minor radiation storm or higher)". That IS the S1 NOAA
    threshold, so Central's floor was S1+ (level >= 1) — a deliberately lower
    bar than the G3+/R3+ floor used for geomag/flare. Native S-scale support
    recovers that floor exactly (gating/swpc.py._floor_for_scale), it does not
    invent a new one.
    """

    @pytest.fixture(autouse=True)
    def _setup(self, mem_db):
        self.db = mem_db

    def _proton_canonical(self, scale_code: str, event_id: str,
                          scalar: str | None = None) -> dict:
        return {
            "event_id": event_id,
            "driver": None,       # native env/swpc.py always sets driver=None for S
            "scalar": scalar,
            "scale_code": scale_code,
            "message": "",
            "issued_at": None,
        }

    def test_s0_suppressed(self):
        """Defensive: a sub-S1 scale_code (not produced by env/swpc.py today,
        which never emits level<1) is still suppressed by the floor check."""
        from meshai.notifications.gating.swpc import decide
        gate = decide(self._proton_canonical("S0", "s0_test"),
                      source="swpc", now=_AT)
        assert not gate.broadcast, "S0 must be suppressed (below S1 floor)"

    def test_s1_broadcasts(self):
        """S1 broadcasts — unlike R1/G1, which are suppressed (see
        TestFlareRScaleFloor.test_r1_suppressed)."""
        from meshai.notifications.gating.swpc import decide
        gate = decide(self._proton_canonical("S1", "s1_test"),
                      source="swpc", now=_AT)
        assert gate.broadcast, "S1 must broadcast (meets S1+ floor)"
        assert gate.data_patch.get("_severity_override") == "routine"

    def test_s2_broadcasts(self):
        from meshai.notifications.gating.swpc import decide
        gate = decide(self._proton_canonical("S2", "s2_test"),
                      source="swpc", now=_AT)
        assert gate.broadcast, "S2 must broadcast"
        assert gate.data_patch.get("_severity_override") == "routine"

    def test_s3_broadcasts_priority(self):
        from meshai.notifications.gating.swpc import decide
        gate = decide(self._proton_canonical("S3", "s3_test"),
                      source="swpc", now=_AT)
        assert gate.broadcast, "S3 must broadcast"
        assert gate.data_patch.get("_severity_override") == "priority"

    def test_s4_broadcasts_immediate(self):
        from meshai.notifications.gating.swpc import decide
        gate = decide(self._proton_canonical("S4", "s4_test"),
                      source="swpc", now=_AT)
        assert gate.broadcast, "S4 must broadcast"
        assert gate.data_patch.get("_severity_override") == "immediate"

    def test_s5_broadcasts_immediate(self):
        from meshai.notifications.gating.swpc import decide
        gate = decide(self._proton_canonical("S5", "s5_test"),
                      source="swpc", now=_AT)
        assert gate.broadcast, "S5 must broadcast"
        assert gate.data_patch.get("_severity_override") == "immediate"

    def test_s1_not_suppressed_by_geomag_window(self):
        """Proton events (driver=None) must not be affected by the kp-only
        600s geomag cross-adapter dedup window."""
        from meshai.notifications.gating.swpc import decide, _geomag_window
        _geomag_window["S1"] = _AT  # would only matter if S-scale shared the window
        gate = decide(self._proton_canonical("S1", "s1_window_test"),
                      source="swpc", now=_AT + 1)
        assert gate.broadcast, "proton events must not consult the geomag window"

    def test_s1_first_sighting_then_suppressed(self):
        """Point-in-time semantics apply to proton same as geomag/flare: a
        second decide() for the same event_id after a committed broadcast is
        suppressed (no re-broadcast on revision)."""
        from meshai.notifications.gating.swpc import decide

        gate1 = decide(self._proton_canonical("S1", "s1_repeat"),
                       source="swpc", now=_AT)
        assert gate1.broadcast
        gate1.commit(_AT)

        gate2 = decide(self._proton_canonical("S1", "s1_repeat"),
                       source="swpc", now=_AT + 100)
        assert not gate2.broadcast, "already-broadcast proton event must suppress"


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

    def test_s1_canonical_data_doesnt_crash_formatter(self):
        """S1 from native to_event() (driver=None, scalar=None) can be fed to
        the formatter without crashing — the real shape env/swpc.py produces."""
        from unittest.mock import MagicMock
        from meshai.env.swpc import SWPCAdapter
        from meshai.notifications.formatters.swpc import format as sfmt

        cfg = MagicMock()
        adapter = SWPCAdapter(cfg)
        evt = self._make_swpc_evt("s", 1)
        event = adapter.to_event(evt)

        assert event is not None
        with pinned_time(_AT):
            result = sfmt(event, now=_AT, budget=140)

        assert result is not None
        assert "S1" in result
        assert len(result) <= 140, f"Budget exceeded: {len(result)} > 140"


# ─────────────────────────────────────────────────────────────────────────────
# 6. Proton registered — solar_radiation_storm present in both registries
# ─────────────────────────────────────────────────────────────────────────────

class TestProtonRegistered:
    """solar_radiation_storm (S-scale/proton) IS registered in both registries.

    Native S-scale support closes the silent-drop gap: env/swpc.py already
    emitted solar_radiation_storm events, but neither registry carried a
    decider/formatter for it, so they were dropped before dispatch (see
    formatters/swpc.py and gating/swpc.py for the proton branches added to
    close this gap).
    """

    def test_solar_radiation_storm_in_formatters(self):
        from meshai.notifications.formatters import FORMATTERS
        assert "solar_radiation_storm" in FORMATTERS, (
            "solar_radiation_storm must be in FORMATTERS (native S-scale support)"
        )

    def test_solar_radiation_storm_in_deciders(self):
        from meshai.notifications.gating import DECIDERS
        assert "solar_radiation_storm" in DECIDERS, (
            "solar_radiation_storm must be in DECIDERS (native S-scale support)"
        )

    def test_get_decider_resolves_solar_radiation_storm(self):
        from meshai.notifications.gating import get_decider
        assert get_decider("solar_radiation_storm") is not None

    def test_get_formatter_resolves_solar_radiation_storm(self):
        from meshai.notifications.formatters import get_formatter
        assert get_formatter("solar_radiation_storm") is not None

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

    # NOTE: test_proton_stays_on_legacy_path previously lived here, exercising
    # Central's swpc_protons raw p10mev-pfu-flux -> S-scale mapping (S1 at
    # 15 pfu) via the legacy handle_swpc() entry point, guarded with
    # pytest.importorskip("meshai.central.swpc_handler") plus an unguarded
    # `from meshai.central.swpc_handler import handle_swpc` inside the body.
    # That raw-pfu-flux mapping is Central-only dead logic: the native path
    # (env/swpc.py) never parses p10mev — it reads the S-scale level directly
    # from noaa-scales.json, same as R/G. There is no native equivalent to
    # convert this to, and it duplicates coverage that already exists
    # natively: the S1+ floor/broadcast behavior is covered by
    # TestProtonSScaleFloor.test_s1_broadcasts, and the "S1"/"☢️" wire-format
    # text is covered by TestFormatterParity.test_proton_s1_parity. Removed
    # so this file no longer imports meshai.central.swpc_handler, which would
    # otherwise ModuleNotFoundError once PR #144
    # (chore/excise-dead-central-path) deletes that module.
