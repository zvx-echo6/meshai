"""Tests for v0.5.10 SWPC space-weather handler."""
import pytest

from meshai.central.swpc_handler import handle_swpc
from meshai.persistence import close_thread_connection, init_db
from meshai.persistence import db as persistence_db


@pytest.fixture
def mem_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "swpc-test.sqlite")
    monkeypatch.setenv("MESHAI_DB_PATH", db_path)
    persistence_db._initialised.clear()
    close_thread_connection()
    conn = init_db()
    # Clear module-level geomag dedup caches between tests.
    # Phase-1: _geomag_recent moved to gating.swpc._geomag_window.
    from meshai.central import swpc_handler as _swpc_mod
    if hasattr(_swpc_mod, '_geomag_recent'):
        _swpc_mod._geomag_recent.clear()
    from meshai.notifications.gating import swpc as _swpc_gate
    _swpc_gate._geomag_window.clear()
    yield conn
    close_thread_connection()
    persistence_db._initialised.discard(db_path)


def _kindex_env(*, kp=3.0, event_id="kp_2026_06_05_15Z"):
    return {
        "id": event_id, "subject": "central.space.kindex",
        "data": {
            "id": event_id, "adapter": "swpc_kindex",
            "category": "space.kindex", "severity": 0,
            "geo": {},
            "data": {"id": event_id, "kp_index": kp,
                       "time": "2026-06-05T15:00:00Z"},
        },
    }


def _protons_env(*, flux=1.0, event_id="p_2026_06_05_15Z"):
    return {
        "id": event_id, "subject": "central.space.proton_flux",
        "data": {
            "id": event_id, "adapter": "swpc_protons",
            "category": "space.proton_flux", "severity": 0,
            "geo": {},
            "data": {"id": event_id, "p10mev": flux,
                       "time": "2026-06-05T15:00:00Z"},
        },
    }


def _alert_env(*, flare_class=None, kp=None, pfu=None,
                 event_id="alert_001", product_id="ALTPRO"):
    d = {"id": event_id, "product_id": product_id,
         "time": "2026-06-05T15:00:00Z"}
    if flare_class: d["flare_class"] = flare_class
    if kp: d["kp_index"] = kp
    if pfu: d["p10mev"] = pfu
    return {
        "id": event_id, "subject": "central.space.alert.xrayflare",
        "data": {
            "id": event_id, "adapter": "swpc_alerts",
            "category": "space.alert", "severity": 1,
            "geo": {}, "data": d,
        },
    }


def _commit(data, t):
    cb = data.get("_on_broadcast_committed")
    if cb is not None:
        cb(float(t))


# ---- geomagnetic storm ----


def test_kp_below_7_skipped(mem_db):
    env = _kindex_env(kp=4.0, event_id="kp_low")
    wire = handle_swpc(env, env["subject"], data={}, now=1_000_000)
    assert wire is None
    # Row persisted for trending, not broadcast.
    row = mem_db.execute(
        "SELECT last_broadcast_at FROM swpc_events WHERE event_id='kp_low'"
    ).fetchone()
    assert row is not None
    assert row["last_broadcast_at"] is None


def test_kp7_g3_broadcasts(mem_db):
    env = _kindex_env(kp=7.0, event_id="kp_g3")
    wire = handle_swpc(env, env["subject"], data={}, now=1_000_000)
    assert wire is not None
    assert wire.startswith("🧲")
    assert "G3" in wire
    assert "Kp7" in wire
    assert "Geomagnetic Storm" in wire


def test_kp9_g5_broadcasts_with_extreme_label(mem_db):
    env = _kindex_env(kp=9.0, event_id="kp_g5")
    wire = handle_swpc(env, env["subject"], data={}, now=1_000_000)
    assert wire is not None
    assert "G5" in wire
    assert "Kp9" in wire


# ---- solar flares ----


def test_m_class_flare_skipped(mem_db):
    env = _alert_env(flare_class="M5.5", event_id="m55_flare")
    wire = handle_swpc(env, env["subject"], data={}, now=1_000_000)
    assert wire is None


def test_x1_flare_r3_broadcasts(mem_db):
    env = _alert_env(flare_class="X1.2", event_id="x1_flare")
    wire = handle_swpc(env, env["subject"], data={}, now=1_000_000)
    assert wire is not None
    assert wire.startswith("☀️")
    assert "R3" in wire
    assert "X1.2" in wire


def test_x10_flare_r4_broadcasts(mem_db):
    env = _alert_env(flare_class="X10", event_id="x10_flare")
    wire = handle_swpc(env, env["subject"], data={}, now=1_000_000)
    assert wire is not None
    assert "R4" in wire or "R5" in wire


def test_flare_class_in_product_id(mem_db):
    """Some swpc_alerts encode the class in product_id rather than flare_class."""
    env = _alert_env(event_id="prod_id_flare", product_id="X2.1 FLARE EVENT")
    wire = handle_swpc(env, env["subject"], data={}, now=1_000_000)
    assert wire is not None
    assert "R3" in wire


# ---- proton events ----


def test_proton_below_threshold_skipped(mem_db):
    env = _protons_env(flux=0.5, event_id="p_low")
    wire = handle_swpc(env, env["subject"], data={}, now=1_000_000)
    assert wire is None
    row = mem_db.execute(
        "SELECT last_broadcast_at FROM swpc_events WHERE event_id='p_low'"
    ).fetchone()
    assert row is not None
    assert row["last_broadcast_at"] is None


def test_proton_s1_threshold_broadcasts(mem_db):
    env = _protons_env(flux=15, event_id="p_s1")
    wire = handle_swpc(env, env["subject"], data={}, now=1_000_000)
    assert wire is not None
    assert wire.startswith("☢️")
    assert "S1" in wire


def test_proton_s2_broadcasts(mem_db):
    env = _protons_env(flux=200, event_id="p_s2")
    wire = handle_swpc(env, env["subject"], data={}, now=1_000_000)
    assert wire is not None
    assert "S2" in wire


# ---- wire format ----


def test_wire_has_scale_code_and_scalar_tail(mem_db):
    env = _kindex_env(kp=7.0, event_id="fmt1")
    wire = handle_swpc(env, env["subject"], data={}, now=1_000_000)
    # Wire format: "🧲 New: G3 Geomagnetic Storm — Kp7\nHF degraded, ..."
    assert "G3" in wire
    assert "Kp7" in wire
    assert "\n" in wire


# ---- per-event dedup ----


def test_per_event_dedup_no_reissue(mem_db):
    env = _kindex_env(kp=7.0, event_id="dedup_kp")
    data1 = {}
    handle_swpc(env, env["subject"], data=data1, now=1_000_000)
    _commit(data1, 1_000_001)
    # Re-publish with same id and same Kp -- should not re-broadcast.
    wire2 = handle_swpc(env, env["subject"], data={}, now=1_000_300)
    assert wire2 is None


# ---- commit callback ----


def test_commit_callback_updates_last_broadcast(mem_db):
    env = _kindex_env(kp=7.0, event_id="cb_swpc")
    data = {}
    handle_swpc(env, env["subject"], data=data, now=1_000_000)
    pre = mem_db.execute(
        "SELECT last_broadcast_at FROM swpc_events WHERE event_id='cb_swpc'"
    ).fetchone()
    assert pre["last_broadcast_at"] is None
    _commit(data, 1_000_001)
    post = mem_db.execute(
        "SELECT last_broadcast_at FROM swpc_events WHERE event_id='cb_swpc'"
    ).fetchone()
    assert post["last_broadcast_at"] == 1_000_001


# ---- routine readings persist but never broadcast ----


def test_routine_kp_reading_persists_no_broadcast(mem_db):
    """Sub-G3 Kp must still be saved for trending queries."""
    env = _kindex_env(kp=4.5, event_id="routine_kp")
    wire = handle_swpc(env, env["subject"], data={}, now=1_000_000)
    assert wire is None
    row = mem_db.execute(
        "SELECT event_type, payload_json FROM swpc_events "
        "WHERE event_id='routine_kp'").fetchone()
    assert row is not None
    assert row["event_type"] == "swpc_kindex"
    assert "kp_index" in row["payload_json"]
