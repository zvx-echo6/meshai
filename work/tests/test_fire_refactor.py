"""Phase-3b WFIGS wildfire refactor tests.

Verifies the source-agnostic formatter+decider migration for the wildfire
hazard, mirroring test_hydro_refactor.py / test_quake_refactor.py:

1. Golden byte-identical: formatters.fire.format() reproduces the legacy
   wfigs_handler._render() wire exactly for a New incident, an Update-with-
   growth ((+delta) size line), a movement-line case, an anchor-line case, and
   the wildfire_closed all-clear.

2. Gate-sequence: an explicit `now`-timeline of WFIGS events driven through
   `gating.fire.decide()` (the LIVE decider -- see
   tests/test_fire_native_growth.py for its full end-to-end native-adapter
   coverage) exercises the New/cooldown/Update/closed lifecycle and the
   data-dict stamps (category / _severity_override / _dedup_suffix /
   _cooldown_suffix) `decide()` hands the dispatcher.

3. Registration: the three explicit categories the WFIGS decider/formatter
   pair emits (wildfire_declared / wildfire_incident / wildfire_closed)
   resolve to the fire formatter + decider, and FIRMS categories do NOT.

chore/ripout-2dii: `handle_wfigs` (the dead Central NATS-envelope entrypoint
this file used purely as a byte-identity driver) has been REMOVED from
`meshai.env.fire_render` -- zero live production callers. `fire_format` IS
live (reached for wildfire_declared/wildfire_incident via the native WFIGS
adapter `env/fires.py` -> `env/store.py::_emit_event`, forced onto
`gating.fire.decide` + this formatter via `cutover.NATIVE_ALWAYS_DECIDE`
independent of any env var -- see `notifications/renderers/composer.py:343-347`
and `tests/test_fire_native_growth.py`), so this file's fire_format coverage
stays green; only the handle_wfigs-as-oracle plumbing was replaced with
direct `_wfigs_render` calls (also live -- see env/fire_fusion.py's FIRMS
growth path) and direct `fire_decide()` driving.
"""
from __future__ import annotations

import pytest

from meshai.notifications.formatters._budget import budget_for
from meshai.env.fire_render import (
    _build_canonical,
    _render as _wfigs_render,
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
    _normalize_wfigs,
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
        from meshai.env import fire_render as _wh
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


def _write_fire_state(conn, *, irwin_id, name, acres, contained_pct,
                       lat, lon, county, state, declared_at_epoch=None,
                       now):
    """Unconditional current_* state write, mirroring the LIVE native path's
    own upsert (env/store.py::_ingest_fires INSERT/UPDATE current_acres /
    current_contained_pct) -- the same shape handle_wfigs used to do inline
    before it was deleted (chore/ripout-2dii)."""
    row = conn.execute(
        "SELECT irwin_id FROM fires WHERE irwin_id=?", (irwin_id,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO fires(irwin_id, incident_name, current_acres, "
            "current_contained_pct, lat, lon, county, state, declared_at, "
            "last_event_at, last_broadcast_at, last_broadcast_acres, "
            "last_broadcast_contained) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (irwin_id, name, acres, contained_pct, lat, lon, county, state,
             declared_at_epoch, now, None, None, None),
        )
    else:
        conn.execute(
            "UPDATE fires SET current_acres=?, current_contained_pct=?, "
            "lat=COALESCE(?, lat), lon=COALESCE(?, lon), last_event_at=? "
            "WHERE irwin_id=?",
            (acres, contained_pct, lat, lon, now, irwin_id),
        )


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
        # Drive the LIVE tombstone decision (gating.fire.decide) directly --
        # handle_wfigs (the dead Central entrypoint that used to wrap this) is
        # gone (chore/ripout-2dii). State is written the same unconditional
        # shape the live native path uses (_write_fire_state).
        irwin_id = "IRWIN-CLOSED-1"
        _write_fire_state(
            mem_db, irwin_id=irwin_id, name="Cache Peak Fire",
            acres=1847.0, contained_pct=23, lat=42.197, lon=-113.710,
            county="Cassia", state="ID", now=1_000_000)
        gate0 = fire_decide(
            {"_kind": "wfigs_incident", "irwin_id": irwin_id,
             "incident_name": "Cache Peak Fire", "acres": 1847.0,
             "contained_pct": 23, "declared_at_epoch": None,
             "lat": 42.197, "lon": -113.710, "county": "Cassia", "state": "ID"},
            source="wfigs", now=1_000_000.0)
        assert gate0.broadcast is True
        gate0.commit(1_000_000.0)  # arm last_broadcast_* (fire "reached mesh")

        gate_t = fire_decide(
            {"_kind": "wfigs_tombstone", "irwin_id": irwin_id},
            source="wfigs", now=2_000_000.0)
        assert gate_t.broadcast is True
        assert gate_t.data_patch["category"] == "wildfire_closed"
        assert gate_t.data_patch["incident_name"] == "Cache Peak Fire"

        new_wire = fire_format(
            _FakeEvent(gate_t.data_patch, category="wildfire_closed"),
            now=_AT, budget=budget_for("wfigs"))
        assert new_wire.startswith("✅ Cache Peak Fire — contained & closed")
        # Golden literal (captured from the live fire_format all-clear branch;
        # this is the SAME format string handle_wfigs used to build inline
        # before its removal -- see notifications/formatters/fire.py::_render_allclear).
        assert new_wire == "✅ Cache Peak Fire — contained & closed\n1,847 ac | 23% contained | 24 mi S of Burley"


# ─────────────────────────────────────────────────────────────────────────────
# 2. Gate-sequence — decide() drives New/cooldown/Update/closed
# ─────────────────────────────────────────────────────────────────────────────

class TestGateSequenceParity:
    """decide() correctly negotiates a full fire lifecycle end to end.

    New/Update/cooldown/suppress behavior through the REAL native entrypoint
    (env/fires.py -> env/store.py -> compose_mesh_message) is covered more
    faithfully by tests/test_fire_native_growth.py; this class focuses on the
    parts that file doesn't reach -- the tombstone/closed lifecycle step (no
    live producer currently emits `_kind=wfigs_tombstone`, but the decider +
    formatter branch is live/shipped code and deserves regression coverage)
    -- plus a defense-in-depth pass over the whole sequence via decide()
    directly (no handle_wfigs; that entrypoint is gone).
    """

    def _decide(self, env, now):
        n = _normalize_wfigs(env)
        canonical = _build_canonical(n, n["_kind"])
        return fire_decide(canonical, source="wfigs", now=float(now))

    def test_full_lifecycle(self, mem_db):
        irwin = _IRWIN_A
        base = 1_800_000_000
        disc_ms = (base - 3600) * 1000  # discovered 1h before first sight (fresh)

        def _active(acres, pct, subject_n="a"):
            return _make_active_envelope(
                irwin_id=irwin, geocoder_city="Burley",
                daily_acres=acres, pct_contained=pct,
                fire_discovery_dt_ms=disc_ms)

        def _step(env, now, *, expect_broadcast, expect_lifecycle):
            n = _normalize_wfigs(env)
            gate = self._decide(env, now)
            assert gate.broadcast is expect_broadcast, (
                f"decide broadcast {gate.broadcast} != {expect_broadcast} "
                f"@ {now} ({expect_lifecycle})")
            assert gate.lifecycle == expect_lifecycle, (
                f"decide lifecycle {gate.lifecycle} != {expect_lifecycle} @ {now}")
            # Unconditional state write, mirroring the live native path.
            _write_fire_state(
                mem_db, irwin_id=irwin, name=n.get("incident_name"),
                acres=n.get("acres"), contained_pct=n.get("contained_pct"),
                lat=n.get("lat"), lon=n.get("lon"), county=n.get("county"),
                state=n.get("state"), declared_at_epoch=n.get("declared_at_epoch"),
                now=now)
            if expect_broadcast:
                if expect_lifecycle == "new":
                    assert gate.data_patch.get("category") == "wildfire_declared"
                elif expect_lifecycle == "update":
                    assert "category" not in gate.data_patch
                assert gate.data_patch.get("_severity_override") == "priority"
                gate.commit(float(now))

        # [0] first sight → New broadcast
        _step(_active(250.0, 0), base,
              expect_broadcast=True, expect_lifecycle="new")
        # [1] small growth 1h later (inside 8h cooldown) → suppress
        _step(_active(300.0, 0), base + 3600,
              expect_broadcast=False, expect_lifecycle="cooldown")
        # [2] growth after cooldown (8h) → Update
        _step(_active(500.0, 0), base + 28800,
              expect_broadcast=True, expect_lifecycle="update")
        # [3] containment change after another cooldown → Update
        _step(_active(500.0, 40), base + 28800 * 2,
              expect_broadcast=True, expect_lifecycle="update")
        # [4] tombstone → all-clear (fire was broadcast earlier)
        gate_t = fire_decide({"_kind": "wfigs_tombstone", "irwin_id": irwin},
                             source="wfigs", now=float(base + 100000))
        assert gate_t.broadcast is True
        assert gate_t.lifecycle == "closed"
        assert gate_t.data_patch["category"] == "wildfire_closed"

    def test_dedup_suffix_tracks_state(self, mem_db):
        """_dedup_suffix carries the acres|contained that justified the
        broadcast (so unchanged re-deliveries dedup but genuine updates pass)."""
        base = 1_800_000_000
        disc_ms = (base - 3600) * 1000
        env_new = _make_active_envelope(
            irwin_id=_IRWIN_A, geocoder_city="Burley",
            daily_acres=250.0, pct_contained=0, fire_discovery_dt_ms=disc_ms)
        gate = self._decide(env_new, base)
        n = _normalize_wfigs(env_new)
        assert gate.data_patch["_dedup_suffix"] == f"{n['acres']}|{n['contained_pct']}"
        assert gate.data_patch["_cooldown_suffix"] == _IRWIN_A

    def test_never_broadcast_tombstone_suppressed(self, mem_db):
        """A tombstone for a fire that never reached the mesh is silent."""
        tomb = _make_tombstone(irwin_id=_IRWIN_A)
        gate = self._decide(tomb, 1_000_000)
        assert gate.broadcast is False
        assert gate.lifecycle == "suppress"

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
                "unattributed_hotspot_cluster"])
    def test_firms_categories_not_captured(self, cat):
        # These native FIRMS categories remain deferred; they must NOT resolve
        # to the fire formatter/decider via the "fire" toggle family fallback.
        from meshai.notifications.formatters import get_formatter
        from meshai.notifications.gating import get_decider
        assert get_formatter(cat) is not fire_format
        assert get_decider(cat) is not fire_decide
