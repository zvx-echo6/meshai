"""Phase-3c FIRMS fusion refactor tests.

Verifies the source-agnostic formatter+decider migration for the THREE FIRMS
fusion broadcast categories — wildfire_growth / wildfire_spotting /
wildfire_halted — mirroring test_fire_refactor.py (WFIGS) and
test_hydro_refactor.py:

1. Registration: the three categories resolve to the right formatter/decider;
   wildfire_growth reuses the FIRE formatter but keeps its OWN firms decider;
   still-deferred native FIRMS categories do NOT resolve.

2. Gate-sequence + deferred-latch (the tier-b validation): a `now`-timeline
   driven through the NEW gating.firms.decide() reproduces the OLD handle_firms
   broadcast/suppress + stamp behavior, AND the latch is now DEFERRED — a
   decision does NOT burn the latch until commit() fires (simulating delivery),
   after which the next decision suppresses.

3. Golden byte-identity: the cutover formatter path reproduces the legacy inline
   wire byte-for-byte for growth (via fire.py), spotting, and halt (via firms.py).

4. Not-cutover parity: with no category cut over, handle_firms keeps the legacy
   eager-latch + stamps VERBATIM (byte-identical live behavior).

5. The unattributed_hotspot_cluster path is curated: below cluster_min_pixels
   it stays silent (returns None) -- F3 enabled the real broadcast path.
"""
from __future__ import annotations

import math
import uuid

import pytest

from meshai.central.budget import budget_for
from meshai.notifications.formatters.fire import format as fire_format
from meshai.notifications.formatters.firms import format as firms_format
from meshai.notifications.gating.firms import decide as firms_decide
from tests.harness.goldens import assert_byte_identical

_MI_PER_DEG_LAT = 69.0


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / f"meshai-{uuid.uuid4().hex}.sqlite")
    monkeypatch.setenv("MESHAI_DB_PATH", db_path)
    from meshai.persistence import db as pdb
    pdb.close_thread_connection()
    pdb._initialised.discard(db_path)
    from meshai.persistence import init_db
    init_db(db_path)
    try:
        from meshai.adapter_config import adapter_config as _ac
        _ac.invalidate()
    except Exception:
        pass
    yield db_path
    pdb.close_thread_connection()
    pdb._initialised.discard(db_path)


@pytest.fixture
def _no_cutover(monkeypatch):
    """Default deploy state: nothing cut over → handler runs legacy verbatim."""
    monkeypatch.delenv("MESHAI_CUTOVER_CATEGORIES", raising=False)
    from meshai.notifications.cutover import _clear_cache
    _clear_cache()
    yield
    _clear_cache()


def _cutover(monkeypatch, *cats):
    monkeypatch.setenv("MESHAI_CUTOVER_CATEGORIES", ",".join(cats))
    from meshai.notifications.cutover import _clear_cache
    _clear_cache()


class _FakeEvent:
    def __init__(self, data, category=None):
        self.data = data
        self.category = category


# ── shared FIRMS driving helpers (mirror test_fire_tracker_phase2/3) ──────────

def _seed_fire(*, irwin_id, lat, lon, name="Stub Fire", **cols):
    import time
    from meshai.persistence import get_db
    conn = get_db()
    base = {"irwin_id": irwin_id, "incident_name": name, "lat": lat, "lon": lon,
            "last_event_at": int(time.time())}
    base.update(cols)
    keys = ",".join(base)
    ph = ",".join("?" * len(base))
    conn.execute(f"INSERT INTO fires({keys}) VALUES ({ph})", tuple(base.values()))


def _envelope(*, lat, lon, acq_date="2026-06-06", acq_time="1200",
              frp=20.0, satellite="N20"):
    return {
        "data": {
            "adapter": "firms",
            "category": "wildfire_hotspot",
            "severity": "routine",
            "data": {
                "latitude": lat, "longitude": lon, "frp": frp,
                "bright_ti4": 320.0, "satellite": satellite,
                "instrument": "VIIRS", "confidence": "high",
                "acq_date": acq_date, "acq_time": acq_time,
                "daynight": "D", "version": "2.0NRT",
            },
        }
    }


_SUBJECT = "central.fire.hotspot.N20.high.us.id"


def _drive_two_pass_growth(irwin_id, center_lat, center_lon):
    """Seed a fire + pass A (5 px) + first pass-B pixel 1 mi N. Returns the
    (wire, data) from the boundary pixel that fires wildfire_growth."""
    from meshai.central.firms_handler import handle_firms
    _seed_fire(irwin_id=irwin_id, lat=center_lat, lon=center_lon, name="Pine Gulch")
    for i in range(5):
        env = _envelope(lat=center_lat + 0.0001 * i,
                        lon=center_lon + 0.0001 * (i - 2),
                        acq_date="2026-06-06", acq_time=f"12{i:02d}",
                        frp=20.0 + i)
        handle_firms(env, subject=_SUBJECT, data={}, now=1780747200 + i)
    pass_b_lat = center_lat + (1.0 / _MI_PER_DEG_LAT)
    env_b = _envelope(lat=pass_b_lat, lon=center_lon, acq_time="1800", frp=22.0)
    data = {}
    wire = handle_firms(env_b, subject=_SUBJECT, data=data, now=1780768800)
    return wire, data


def _seed_pass_a_hex_then_close(irwin_id, center_lat, center_lon,
                                start_now=1780747200):
    from meshai.central.firms_handler import handle_firms
    _seed_fire(irwin_id=irwin_id, lat=center_lat, lon=center_lon, name=irwin_id)
    for i in range(6):
        angle = i * math.pi / 3
        la = center_lat + (0.5 / _MI_PER_DEG_LAT) * math.sin(angle)
        cos_lat = math.cos(math.radians(center_lat))
        lo = center_lon + (0.5 / (_MI_PER_DEG_LAT * cos_lat)) * math.cos(angle)
        env = _envelope(lat=la, lon=lo, acq_time=f"12{i * 2:02d}")
        handle_firms(env, subject=_SUBJECT, data={}, now=start_now + i)


def _offset_mi(lat, lon, north_mi, east_mi):
    dlat = north_mi / _MI_PER_DEG_LAT
    dlon = east_mi / (_MI_PER_DEG_LAT * math.cos(math.radians(lat)))
    return lat + dlat, lon + dlon


def _drive_spotting(irwin_id, center_lat, center_lon, now=1780768800):
    """Seed hex pass A + closed perimeter, then a pass-B pixel 2 mi NE that
    fires wildfire_spotting. Returns (wire, data)."""
    from meshai.central.firms_handler import handle_firms
    _seed_pass_a_hex_then_close(irwin_id, center_lat, center_lon)
    sp_lat, sp_lon = _offset_mi(center_lat, center_lon,
                                north_mi=2.0 / math.sqrt(2),
                                east_mi=2.0 / math.sqrt(2))
    env_b = _envelope(lat=sp_lat, lon=sp_lon, acq_time="1800")
    data = {}
    wire = handle_firms(env_b, subject=_SUBJECT, data=data, now=now)
    return wire, data


def _seed_stale_fire(irwin_id, *, now_epoch, idle_hours=14, name="Cold Fire"):
    from meshai.persistence import get_db
    idle_at = now_epoch - (idle_hours * 3600)
    get_db().execute(
        "INSERT INTO fires(irwin_id, incident_name, lat, lon, last_event_at, "
        "last_pass_id, last_pass_at) VALUES (?,?,?,?,?,?,?)",
        (irwin_id, name, 42.5, -114.5, int(idle_at), "N20-329627",
         float(idle_at)),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. Registration
# ─────────────────────────────────────────────────────────────────────────────

class TestRegistration:
    def test_growth_formatter_is_fire(self):
        from meshai.notifications.formatters import get_formatter
        assert get_formatter("wildfire_growth") is fire_format

    @pytest.mark.parametrize("cat", ["wildfire_spotting", "wildfire_halted"])
    def test_spotting_halt_formatter_is_firms(self, cat):
        from meshai.notifications.formatters import get_formatter
        assert get_formatter(cat) is firms_format

    @pytest.mark.parametrize(
        "cat", ["wildfire_growth", "wildfire_spotting", "wildfire_halted"])
    def test_decider_is_firms(self, cat):
        from meshai.notifications.gating import get_decider
        assert get_decider(cat) is firms_decide

    @pytest.mark.parametrize(
        "cat", ["wildfire_hotspot", "new_ignition",
                "unattributed_hotspot_cluster"])
    def test_deferred_firms_natives_not_registered(self, cat):
        from meshai.notifications.formatters import get_formatter
        from meshai.notifications.gating import get_decider
        assert get_formatter(cat) is None
        assert get_decider(cat) is None


# ─────────────────────────────────────────────────────────────────────────────
# 2. Gate-sequence + deferred-latch (tier-b)
# ─────────────────────────────────────────────────────────────────────────────

class TestGrowthDecideSequence:
    def _in(self, **over):
        d = {"_kind": "firms_growth", "irwin_id": "ID-G", "boundary": True,
             "drift_mi": 1.0, "drift_direction": "N", "drift_mi_per_hour": 0.16}
        d.update(over)
        return d

    def test_boundary_drift_broadcasts_with_stamps(self):
        gr = firms_decide(self._in(), source="firms", now=1000.0)
        assert gr.broadcast is True
        assert gr.data_patch["category"] == "wildfire_growth"
        assert gr.data_patch["_severity_override"] == "immediate"
        assert gr.data_patch["_cooldown_suffix"] == "ID-G"
        # Growth has NO latch to defer.
        assert gr.commit is None

    def test_no_boundary_suppresses(self):
        gr = firms_decide(self._in(boundary=False), source="firms", now=1000.0)
        assert gr.broadcast is False

    def test_sub_threshold_drift_suppresses(self):
        # Default growth_drift_threshold_mi is 0.5; 0.3 mi is below.
        gr = firms_decide(self._in(drift_mi=0.3), source="firms", now=1000.0)
        assert gr.broadcast is False


class TestSpottingDecideSequence:
    def _in(self, **over):
        d = {"_kind": "firms_spotting", "irwin_id": "ID-S", "dist_mi": 2.0,
             "direction": "NE", "incident_name": "Pine Gulch"}
        d.update(over)
        return d

    def test_first_broadcasts_with_severity_override(self):
        # issue #121: must be _severity_override -- consumer.py only ever
        # honors that key, a plain "severity" key is a silent no-op.
        _seed_fire(irwin_id="ID-S", lat=43.0, lon=-115.0)
        gr = firms_decide(self._in(), source="firms", now=1_000_000.0)
        assert gr.broadcast is True
        assert gr.data_patch["category"] == "wildfire_spotting"
        assert gr.data_patch["_severity_override"] == "immediate"
        assert "severity" not in gr.data_patch
        assert gr.commit is not None

    def test_latch_not_burned_without_commit(self):
        """Tier-b: a decision that is never delivered must NOT burn the latch."""
        _seed_fire(irwin_id="ID-S", lat=43.0, lon=-115.0)
        t0 = 1_000_000.0
        gr1 = firms_decide(self._in(), source="firms", now=t0)
        assert gr1.broadcast is True
        # No commit() → 10s later a fresh candidate STILL broadcasts.
        gr2 = firms_decide(self._in(), source="firms", now=t0 + 10)
        assert gr2.broadcast is True, "latch burned without delivery (tier-b broken)"

    def test_commit_then_cooldown_suppresses_then_reopens(self):
        _seed_fire(irwin_id="ID-S", lat=43.0, lon=-115.0)
        t0 = 1_000_000.0
        gr1 = firms_decide(self._in(), source="firms", now=t0)
        gr1.commit(t0)  # simulate delivery → stamps last_spotting_broadcast_at=t0
        # 30 min later (< 1h cooldown) → suppressed.
        gr2 = firms_decide(self._in(), source="firms", now=t0 + 1800)
        assert gr2.broadcast is False
        # 2h later (> cooldown) → reopens.
        gr3 = firms_decide(self._in(), source="firms", now=t0 + 7200)
        assert gr3.broadcast is True


class TestHaltDecideSequence:
    def test_first_broadcasts_with_hours_and_severity(self):
        # issue #121: must be _severity_override -- consumer.py only ever
        # honors that key, a plain "severity" key is a silent no-op.
        now = 1780768800.0
        _seed_stale_fire("ID-H", now_epoch=now, idle_hours=14)
        gr = firms_decide({"_kind": "firms_halt"}, source="firms", now=now)
        assert gr.broadcast is True
        assert gr.data_patch["category"] == "wildfire_halted"
        assert gr.data_patch["_severity_override"] == "routine"
        assert "severity" not in gr.data_patch
        assert gr.data_patch["hours"] == 14
        assert gr.data_patch["incident_name"] == "Cold Fire"
        assert gr.data_patch["irwin_id"] == "ID-H"
        assert gr.commit is not None

    def test_latch_not_burned_without_commit(self):
        now = 1780768800.0
        _seed_stale_fire("ID-H", now_epoch=now, idle_hours=14)
        gr1 = firms_decide({"_kind": "firms_halt"}, source="firms", now=now)
        assert gr1.broadcast is True
        gr2 = firms_decide({"_kind": "firms_halt"}, source="firms", now=now)
        assert gr2.broadcast is True, "latch burned without delivery (tier-b broken)"

    def test_commit_suppresses_then_reeligible_after_new_pass(self):
        from meshai.persistence import get_db
        now = 1780768800.0
        _seed_stale_fire("ID-H", now_epoch=now, idle_hours=14)
        gr1 = firms_decide({"_kind": "firms_halt"}, source="firms", now=now)
        gr1.commit(now)  # stamps halt_broadcast_at=now
        # Same fire no longer eligible (halt_broadcast_at >= last_pass_at).
        gr2 = firms_decide({"_kind": "firms_halt"}, source="firms", now=now)
        assert gr2.broadcast is False
        # Fire reactivates: a new pass advances last_pass_at past the latch.
        new_pass_at = now + 100
        get_db().execute("UPDATE fires SET last_pass_at=? WHERE irwin_id=?",
                         (float(new_pass_at), "ID-H"))
        # Evaluate later, when the fire is idle again → re-eligible.
        later = new_pass_at + 14 * 3600
        gr3 = firms_decide({"_kind": "firms_halt"}, source="firms", now=later)
        assert gr3.broadcast is True


# ─────────────────────────────────────────────────────────────────────────────
# 3. Golden byte-identity — cutover formatter reproduces the legacy inline wire
# ─────────────────────────────────────────────────────────────────────────────

class TestFormatterGolden:
    def test_growth_wire_reuses_fire_formatter(self, monkeypatch):
        # Drive the real growth broadcast under cutover: handle_firms returns the
        # inline _render() wire AND populates data with the fire formatter hints.
        _cutover(monkeypatch, "wildfire_growth")
        try:
            wire, data = _drive_two_pass_growth("ID-GG", 42.0, -114.0)
            assert wire is not None and wire.startswith("🔥 Pine Gulch")
            rendered = fire_format(_FakeEvent(data, category="wildfire_growth"),
                                   now=0.0, budget=budget_for("wfigs"))
            assert_byte_identical(rendered, wire)
        finally:
            from meshai.notifications.cutover import _clear_cache
            _clear_cache()

    def test_spotting_wire_golden(self, monkeypatch):
        _cutover(monkeypatch, "wildfire_spotting")
        try:
            wire, data = _drive_spotting("ID-SS", 43.0, -115.0)
            assert wire is not None and wire.startswith("🔥 Possible spotting ")
            rendered = firms_format(_FakeEvent(data, category="wildfire_spotting"),
                                    now=0.0, budget=budget_for("firms"))
            assert_byte_identical(rendered, wire)
        finally:
            from meshai.notifications.cutover import _clear_cache
            _clear_cache()

    def test_halt_wire_golden(self, monkeypatch):
        from meshai.central.firms_handler import _maybe_emit_halt
        from meshai.persistence import get_db
        _cutover(monkeypatch, "wildfire_halted")
        try:
            now = 1780768800
            _seed_stale_fire("ID-HH", now_epoch=now, idle_hours=14)
            data = {}
            wire = _maybe_emit_halt(get_db(), data=data, now=now)
            assert wire is not None and "no growth in 14h" in wire
            rendered = firms_format(_FakeEvent(data, category="wildfire_halted"),
                                    now=0.0, budget=budget_for("firms"))
            assert_byte_identical(rendered, wire)
        finally:
            from meshai.notifications.cutover import _clear_cache
            _clear_cache()

    def test_spotting_formatter_exact_format(self):
        # Pin the exact legacy f-string shape independent of the driver.
        wire = firms_format(
            _FakeEvent({"dist_mi": 2.34, "direction": "SW",
                        "incident_name": "Cache Peak"},
                       category="wildfire_spotting"),
            now=0.0, budget=140)
        assert wire == "🔥 Possible spotting 2.3 mi SW of Cache Peak perimeter"

    def test_halt_formatter_exact_format(self):
        wire = firms_format(
            _FakeEvent({"incident_name": "Cache Peak", "hours": 9},
                       category="wildfire_halted"),
            now=0.0, budget=140)
        assert wire == "🔥 Cache Peak no growth in 9h"


# ─────────────────────────────────────────────────────────────────────────────
# 4. Not-cutover parity — legacy eager-latch + stamps preserved VERBATIM
# ─────────────────────────────────────────────────────────────────────────────

class TestNotCutoverLegacyVerbatim:
    def test_growth_stamps_and_no_latch(self, _no_cutover):
        wire, data = _drive_two_pass_growth("ID-GN", 42.0, -114.0)
        assert wire is not None and wire.startswith("🔥 Pine Gulch")
        assert "Moving N" in wire
        assert data["category"] == "wildfire_growth"
        assert data["_severity_override"] == "immediate"
        assert data["_cooldown_suffix"] == "ID-GN"
        # Legacy growth never attached a deferred commit.
        assert "_on_broadcast_committed" not in data

    def test_spotting_eager_latch_stamped(self, _no_cutover):
        from meshai.persistence import get_db
        wire, data = _drive_spotting("ID-SN", 43.0, -115.0, now=1780768800)
        assert wire is not None and "spotting" in wire
        assert data["category"] == "wildfire_spotting"
        assert data["_severity_override"] == "immediate"
        # Legacy path stamps the latch EAGERLY with the handler `now`.
        latch = get_db().execute(
            "SELECT last_spotting_broadcast_at FROM fires WHERE irwin_id=?",
            ("ID-SN",)).fetchone()[0]
        assert latch == 1780768800.0
        assert "_on_broadcast_committed" not in data

    def test_halt_eager_latch_stamped(self, _no_cutover):
        from meshai.central.firms_handler import _maybe_emit_halt
        from meshai.persistence import get_db
        now = 1780768800
        _seed_stale_fire("ID-HN", now_epoch=now, idle_hours=14)
        data = {}
        wire = _maybe_emit_halt(get_db(), data=data, now=now)
        assert wire == "🔥 Cold Fire no growth in 14h"
        assert data["category"] == "wildfire_halted"
        assert data["_severity_override"] == "routine"
        latch = get_db().execute(
            "SELECT halt_broadcast_at FROM fires WHERE irwin_id=?",
            ("ID-HN",)).fetchone()[0]
        assert latch == float(now)
        assert "_on_broadcast_committed" not in data


# ─────────────────────────────────────────────────────────────────────────────
# 5. Cluster path: below-threshold is silent (F3 enabled the path; a lone
#    unattributed pixel with no nearby unstamped neighbors never clusters)
# ─────────────────────────────────────────────────────────────────────────────

class TestClusterBelowThreshold:
    def test_maybe_emit_cluster_below_threshold_returns_none(self):
        from meshai.central.firms_handler import _maybe_emit_cluster
        from meshai.persistence import get_db
        data = {}
        # No pixels in firms_pixels -> the cluster query finds < min_pixels
        # members, so no wire and no data tagging (curated: needs a real cluster).
        out = _maybe_emit_cluster(
            get_db(), lat=43.0, lon=-115.0, acq_epoch=1780747200,
            frp=20.0, data=data, now=1780747200, this_pixel_id=1)
        assert out is None
        assert data == {}

