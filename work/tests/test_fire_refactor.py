"""Phase-3b WFIGS wildfire refactor tests.

Verifies the source-agnostic formatter+decider migration for the wildfire
hazard, mirroring test_hydro_refactor.py / test_quake_refactor.py:

1. Golden byte-identical: formatters.fire.format() reproduces the legacy
   wfigs_handler._render() wire exactly for a New incident, an Update-with-
   growth ((+delta) size line), a movement-line case, an anchor-line case, and
   the wildfire_closed all-clear.

2. Gate-sequence parity: an explicit `now`-timeline of WFIGS events driven
   through the NEW gating.fire.decide() matches the OLD handle_wfigs
   broadcast/suppress behavior AND the data-dict stamps
   (category / _severity_override / _dedup_suffix / _cooldown_suffix).

3. Registration: the three explicit categories the wfigs_handler emits
   (wildfire_declared / wildfire_incident / wildfire_closed) resolve to the
   fire formatter + decider, and FIRMS categories do NOT.
"""
from __future__ import annotations

import pytest

from meshai import central_normalizer as cn
from meshai.central.budget import budget_for
from meshai.central.wfigs_handler import (
    _build_canonical,
    _render as _wfigs_render,
    handle_wfigs,
)
from meshai.notifications.formatters.fire import format as fire_format
from meshai.notifications.gating.fire import decide as fire_decide
from meshai.persistence import close_thread_connection, init_db
from meshai.persistence import db as persistence_db
from tests.harness.goldens import assert_byte_identical
from tests.test_wfigs_handler import (
    _IRWIN_A,
    _make_active_envelope,
    _make_tombstone,
)

_AT = 1_800_000_000.0  # pinned epoch (unused by fire render; kept for parity)


@pytest.fixture
def mem_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "fire-refactor-test.sqlite")
    monkeypatch.setenv("MESHAI_DB_PATH", db_path)
    persistence_db._initialised.clear()
    close_thread_connection()
    conn = init_db()
    try:
        from meshai.adapter_config import adapter_config as _ac
        _ac.invalidate()
    except Exception:
        pass
    try:
        from meshai.central import wfigs_handler as _wh
        _wh._last_cleanup = 0
    except Exception:
        pass
    yield conn
    close_thread_connection()
    persistence_db._initialised.discard(db_path)


class _FakeEvent:
    def __init__(self, data, category=None):
        self.data = data
        self.category = category


# ─────────────────────────────────────────────────────────────────────────────
# 1. Golden byte-identical — formatter reproduces _render()/all-clear exactly
# ─────────────────────────────────────────────────────────────────────────────

class TestFormatterGolden:
    """formatters.fire.format() == wfigs_handler._render() for the same inputs."""

    def _budget(self) -> int:
        return budget_for("wfigs")

    def _incident_data(self, **over) -> dict:
        d = {
            "incident_name": "Cache Peak Fire",
            "acres": 1847.0,
            "contained_pct": 23,
            "fire_cause": "Lightning",
            "declared_at_epoch": 1_781_204_400,
            "unique_fire_id": "2026-IDSCF-000987",
            "geocoder_city": "Burley",  # short-circuits anchor (no DB/Photon)
            "lat": 42.197,
            "lon": -113.710,
            "county": "Cassia",
            "state": "ID",
            "landclass": None,
        }
        d.update(over)
        return d

    def test_new_incident(self, mem_db):
        n = self._incident_data()
        old = _wfigs_render(n, prefix="New")
        new = fire_format(_FakeEvent({**n, "is_update": False}),
                          now=_AT, budget=self._budget())
        assert_byte_identical(new, old)
        assert new.startswith("🔥 Cache Peak Fire — New")
        assert "1,847 ac" in new
        assert "containment 23%" in new

    def test_update_with_growth_delta(self, mem_db):
        n = self._incident_data(acres=3000.0, contained_pct=35)
        old = _wfigs_render(n, prefix="Update",
                            last_bcast_acres=1847.0, last_bcast_contained=23)
        new = fire_format(
            _FakeEvent({**n, "is_update": True,
                        "last_bcast_acres": 1847.0, "last_bcast_contained": 23}),
            now=_AT, budget=self._budget())
        assert_byte_identical(new, old)
        assert new.startswith("🔥 Cache Peak Fire — Update")
        assert "3,000 ac (+1,153)" in new  # delta line
        assert "containment 35%" in new

    def test_movement_line(self, mem_db):
        # movement is FIRMS-injected; the formatter must read it from event.data
        # exactly as _render(movement=...) does today.
        mv = {"direction": "NE", "speed_mph": 1.2}
        n = self._incident_data()
        old = _wfigs_render(n, prefix="New", movement=mv)
        new = fire_format(_FakeEvent({**n, "is_update": False, "movement": mv}),
                          now=_AT, budget=self._budget())
        assert_byte_identical(new, old)
        assert "Moving NE 1.2 mi/h" in new

    def test_anchor_line(self, mem_db):
        # No geocoder_city → line 3 is resolved via the shared resolve_anchor +
        # the legacy fallback tiers. Both paths hit the same seeded town_anchors
        # table, so the wire must stay byte-identical.
        n = self._incident_data(geocoder_city=None)
        old = _wfigs_render(n, prefix="New")
        new = fire_format(_FakeEvent({**n, "is_update": False}),
                          now=_AT, budget=self._budget())
        assert_byte_identical(new, old)
        # line 3 is NOT a movement line and NOT a raw city name
        assert "Moving" not in new

    def test_wildfire_closed_all_clear(self, mem_db):
        # Drive the real handler to produce the legacy all-clear wire, then
        # assert the formatter reproduces it byte-for-byte from event.data.
        env = _make_active_envelope(geocoder_city="Burley")
        n0 = cn.normalize(env)
        data0 = {}
        handle_wfigs(n0, env, env["subject"], data=data0, now=1_000_000)
        data0["_on_broadcast_committed"](float(1_000_000))  # arm last_broadcast_*

        tomb = _make_tombstone()
        data_t = {}
        old_wire = handle_wfigs(cn.normalize(tomb), tomb, tomb["subject"],
                                data=data_t, now=2_000_000)
        assert old_wire is not None
        assert old_wire.startswith("✅ Cache Peak Fire — contained & closed")
        assert data_t["category"] == "wildfire_closed"

        # Reconstruct the canonical fields the decider's data_patch supplies to
        # the formatter for the closed wire, and render.
        closed_data = {
            "category": "wildfire_closed",
            "incident_name": "Cache Peak Fire",
            "acres": 1847.0,
            "contained_pct": 23,
            "lat": env["data"]["data"]["latitude"],
            "lon": env["data"]["data"]["longitude"],
            "county": "Cassia",
            "state": "ID",
        }
        new_wire = fire_format(_FakeEvent(closed_data, category="wildfire_closed"),
                               now=_AT, budget=budget_for("wfigs"))
        assert_byte_identical(new_wire, old_wire)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Gate-sequence parity — new decide() vs old handle_wfigs across a lifecycle
# ─────────────────────────────────────────────────────────────────────────────

class TestGateSequenceParity:
    """A full fire lifecycle agrees between decide() and handle_wfigs()."""

    def _decide(self, env, now):
        n = cn.normalize(env)
        canonical = _build_canonical(n, n["_kind"])
        return fire_decide(canonical, source="wfigs", now=float(now))

    def _step(self, env, now, *, expect_broadcast, expect_lifecycle):
        """Assert decide() and handle_wfigs() agree at one timeline step.

        decide() (called first) only READS state, so it sees the same pre-write
        row the handler's internal decide() sees.  On broadcast the handler's
        legacy (not-cutover) stamps must equal decide()'s data_patch, and we arm
        last_broadcast_* via the commit callback (simulating the dispatcher).
        """
        n = cn.normalize(env)
        gate = self._decide(env, now)
        assert gate.broadcast is expect_broadcast, (
            f"decide broadcast {gate.broadcast} != {expect_broadcast} "
            f"@ {now} ({expect_lifecycle})")
        assert gate.lifecycle == expect_lifecycle, (
            f"decide lifecycle {gate.lifecycle} != {expect_lifecycle} @ {now}")

        data = {}
        wire = handle_wfigs(n, env, env["subject"], data=data, now=now)
        assert (wire is not None) is expect_broadcast

        if expect_broadcast:
            # Handler's stamped keys (legacy path) match decide()'s data_patch.
            for k in ("_severity_override", "_dedup_suffix", "_cooldown_suffix"):
                assert data.get(k) == gate.data_patch.get(k), (
                    f"stamp {k}: handler={data.get(k)!r} "
                    f"decide={gate.data_patch.get(k)!r}")
            if expect_lifecycle == "new":
                assert data.get("category") == "wildfire_declared"
                assert gate.data_patch.get("category") == "wildfire_declared"
            elif expect_lifecycle == "update":
                # Update keeps the envelope-derived wildfire_incident: no override.
                assert "category" not in data
                assert "category" not in gate.data_patch
            elif expect_lifecycle == "closed":
                assert data.get("category") == "wildfire_closed"
                assert gate.data_patch.get("category") == "wildfire_closed"
            assert data.get("_severity_override") == "priority"
            # Arm last_broadcast_* for the next cooldown check.
            data["_on_broadcast_committed"](float(now))
        return wire

    def test_full_lifecycle(self, mem_db):
        irwin = _IRWIN_A
        base = 1_800_000_000
        disc_ms = (base - 3600) * 1000  # discovered 1h before first sight (fresh)

        def _active(acres, pct, subject_n="a"):
            return _make_active_envelope(
                irwin_id=irwin, geocoder_city="Burley",
                daily_acres=acres, pct_contained=pct,
                fire_discovery_dt_ms=disc_ms)

        # [0] first sight → New broadcast
        self._step(_active(250.0, 0), base,
                   expect_broadcast=True, expect_lifecycle="new")
        # [1] small growth 1h later (inside 8h cooldown) → suppress
        self._step(_active(300.0, 0), base + 3600,
                   expect_broadcast=False, expect_lifecycle="cooldown")
        # [2] growth after cooldown (8h) → Update
        self._step(_active(500.0, 0), base + 28800,
                   expect_broadcast=True, expect_lifecycle="update")
        # [3] containment change after another cooldown → Update
        self._step(_active(500.0, 40), base + 28800 * 2,
                   expect_broadcast=True, expect_lifecycle="update")
        # [4] tombstone → all-clear (fire was broadcast earlier)
        tomb = _make_tombstone(irwin_id=irwin)
        self._step(tomb, base + 100000,
                   expect_broadcast=True, expect_lifecycle="closed")

    def test_dedup_suffix_tracks_state(self, mem_db):
        """_dedup_suffix carries the acres|contained that justified the
        broadcast (so unchanged re-deliveries dedup but genuine updates pass)."""
        base = 1_800_000_000
        disc_ms = (base - 3600) * 1000
        env_new = _make_active_envelope(
            irwin_id=_IRWIN_A, geocoder_city="Burley",
            daily_acres=250.0, pct_contained=0, fire_discovery_dt_ms=disc_ms)
        gate = self._decide(env_new, base)
        n = cn.normalize(env_new)
        assert gate.data_patch["_dedup_suffix"] == f"{n['acres']}|{n['contained_pct']}"
        assert gate.data_patch["_cooldown_suffix"] == _IRWIN_A

    def test_never_broadcast_tombstone_suppressed(self, mem_db):
        """A tombstone for a fire that never reached the mesh is silent."""
        tomb = _make_tombstone(irwin_id=_IRWIN_A)
        gate = self._decide(tomb, 1_000_000)
        assert gate.broadcast is False
        assert gate.lifecycle == "suppress"
        # Handler agrees: returns None.
        out = handle_wfigs(cn.normalize(tomb), tomb, tomb["subject"],
                           data={}, now=1_000_000)
        assert out is None

    def test_perimeter_never_broadcasts(self, mem_db):
        from tests.test_wfigs_handler import _make_perimeter
        per = _make_perimeter(irwin_id=_IRWIN_A)
        gate = self._decide(per, 1_000_000)
        assert gate.broadcast is False
        assert gate.lifecycle == "suppress"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Registration — the three explicit categories resolve; FIRMS does not
# ─────────────────────────────────────────────────────────────────────────────

class TestRegistration:
    @pytest.mark.parametrize(
        "cat", ["wildfire_declared", "wildfire_incident", "wildfire_closed"])
    def test_formatter_registered(self, cat):
        from meshai.notifications.formatters import get_formatter
        assert get_formatter(cat) is fire_format

    @pytest.mark.parametrize(
        "cat", ["wildfire_declared", "wildfire_incident", "wildfire_closed"])
    def test_decider_registered(self, cat):
        from meshai.notifications.gating import get_decider
        assert get_decider(cat) is fire_decide

    @pytest.mark.parametrize(
        "cat", ["wildfire_hotspot", "new_ignition",
                "unattributed_hotspot_cluster", "wildfire_growth"])
    def test_firms_categories_not_captured(self, cat):
        # FIRMS is deferred; its categories must NOT resolve to the fire
        # formatter/decider via the "fire" toggle family fallback.
        from meshai.notifications.formatters import get_formatter
        from meshai.notifications.gating import get_decider
        assert get_formatter(cat) is not fire_format
        assert get_decider(cat) is not fire_decide
