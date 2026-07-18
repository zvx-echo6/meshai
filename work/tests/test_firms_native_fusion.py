"""Native FIRMS fire-fusion tests — the source-agnostic ingest entrypoint.

Covers the wiring that lets the native ``env/firms.py`` adapter feed
locally-fetched NASA FIRMS pixels into the SAME attribution/fusion pipeline the
Central handler uses, via ``central.firms_handler.ingest_hotspot_pixel``:

1. ``ingest_hotspot_pixel`` drives growth / spotting / halt from canonical
   pixels (and, F3, curated ``unattributed_hotspot_cluster`` "possible new
   fire" broadcasts) and returns ``(wire, data)`` — and NEVER a raw per-pixel
   hotspot / new_ignition broadcast.
2. ``env/firms.py`` tick() (CSV fetch monkeypatched) feeds those pixels through
   the shared engine, emits the fusion Events, and still returns None for raw
   hotspots.
3. A guard that the Central ``handle_firms`` path is unchanged by the
   extraction (storage side-effects + growth wire/stamps identical).

Mirrors the driving style of test_firms_refactor.py / test_fire_tracker_phase*.
"""
from __future__ import annotations

import math
import time
import uuid
from datetime import datetime, timezone

import pytest

_MI_PER_DEG_LAT = 69.0
# F3: unattributed_hotspot_cluster is a CURATED fusion output (a "possible new
# fire"), not a raw per-pixel broadcast. Raw hotspots are still never emitted.
_FUSION_CATS = {"wildfire_growth", "wildfire_spotting", "wildfire_halted",
                "unattributed_hotspot_cluster"}
_RAW_CATS = {"wildfire_hotspot", "new_ignition"}


# ── isolation (real-DB, mirrors test_firms_refactor) ─────────────────────────

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


@pytest.fixture(autouse=True)
def _no_cutover(monkeypatch):
    """Default deploy state: nothing cut over -> legacy stamps, direct emit."""
    monkeypatch.delenv("MESHAI_CUTOVER_CATEGORIES", raising=False)
    from meshai.notifications.cutover import _clear_cache
    _clear_cache()
    yield
    _clear_cache()


# ── helpers ──────────────────────────────────────────────────────────────────

def _seed_fire(*, irwin_id, lat, lon, name="Stub Fire", **cols):
    from meshai.persistence import get_db
    conn = get_db()
    base = {"irwin_id": irwin_id, "incident_name": name, "lat": lat, "lon": lon,
            "last_event_at": int(time.time())}
    base.update(cols)
    keys = ",".join(base)
    ph = ",".join("?" * len(base))
    conn.execute(f"INSERT INTO fires({keys}) VALUES ({ph})", tuple(base.values()))


def _acq_epoch(acq_date: str, acq_time: str) -> int:
    return int(datetime.strptime(f"{acq_date} {acq_time.zfill(4)}",
                                 "%Y-%m-%d %H%M")
               .replace(tzinfo=timezone.utc).timestamp())


def _pixel(*, lat, lon, acq_date="2026-06-06", acq_time="1200",
           frp=20.0, confidence="high", brightness=320.0, satellite="N20"):
    """Build a canonical FIRMS pixel dict."""
    return {
        "lat": lat, "lon": lon, "frp": frp, "confidence": confidence,
        "brightness": brightness, "satellite": satellite,
        "acq_epoch": _acq_epoch(acq_date, acq_time),
    }


def _offset_mi(lat, lon, north_mi, east_mi):
    dlat = north_mi / _MI_PER_DEG_LAT
    dlon = east_mi / (_MI_PER_DEG_LAT * math.cos(math.radians(lat)))
    return lat + dlat, lon + dlon


def _feed(pixel, *, now):
    from meshai.env.fire_fusion import ingest_hotspot_pixel
    return ingest_hotspot_pixel(pixel, now=now)


def _assert_no_raw(broadcasts):
    """Every returned broadcast must be a fusion category; never a raw one."""
    for wire, data in broadcasts:
        cat = data.get("category")
        assert cat in _FUSION_CATS, f"unexpected non-fusion broadcast: {cat!r}"
        assert cat not in _RAW_CATS


# ═════════════════════════════════════════════════════════════════════════════
# 1. ingest_hotspot_pixel — growth / spotting / halt
# ═════════════════════════════════════════════════════════════════════════════

class TestIngestGrowth:
    def test_two_pass_growth_broadcasts(self):
        center_lat, center_lon = 42.0, -114.0
        _seed_fire(irwin_id="ID-NG", lat=center_lat, lon=center_lon,
                   name="Pine Gulch")
        # Pass A: 5 pixels around the anchor (acq 12:00-12:04 = one pass bucket).
        for i in range(5):
            out = _feed(_pixel(lat=center_lat + 0.0001 * i,
                               lon=center_lon + 0.0001 * (i - 2),
                               acq_time=f"12{i:02d}", frp=20.0 + i),
                        now=1780747200 + i)
            _assert_no_raw(out)
            assert out == [], "pass-A pixels must not broadcast (no boundary yet)"
        # Pass B: one pixel 1 mi north (18:00 = different pass bucket) -> growth.
        pass_b_lat = center_lat + (1.0 / _MI_PER_DEG_LAT)
        out = _feed(_pixel(lat=pass_b_lat, lon=center_lon, acq_time="1800",
                           frp=22.0), now=1780768800)
        _assert_no_raw(out)
        assert len(out) == 1
        wire, data = out[0]
        assert data["category"] == "wildfire_growth"
        assert data["_severity_override"] == "immediate"
        assert wire.startswith("🔥 Pine Gulch")
        assert "Moving N" in wire


class TestIngestSpotting:
    def _seed_hex_pass_a(self, irwin_id, center_lat, center_lon,
                         start_now=1780747200):
        _seed_fire(irwin_id=irwin_id, lat=center_lat, lon=center_lon,
                   name=irwin_id)
        for i in range(6):
            angle = i * math.pi / 3
            la = center_lat + (0.5 / _MI_PER_DEG_LAT) * math.sin(angle)
            cos_lat = math.cos(math.radians(center_lat))
            lo = center_lon + (0.5 / (_MI_PER_DEG_LAT * cos_lat)) * math.cos(angle)
            out = _feed(_pixel(lat=la, lon=lo, acq_time=f"12{i * 2:02d}"),
                        now=start_now + i)
            _assert_no_raw(out)

    def test_spotting_broadcasts(self):
        center_lat, center_lon = 43.0, -115.0
        self._seed_hex_pass_a("ID-NS", center_lat, center_lon)
        # Pass B pixel ~2 mi NE of the perimeter -> spotting.
        sp_lat, sp_lon = _offset_mi(center_lat, center_lon,
                                    north_mi=2.0 / math.sqrt(2),
                                    east_mi=2.0 / math.sqrt(2))
        out = _feed(_pixel(lat=sp_lat, lon=sp_lon, acq_time="1800"),
                    now=1780768800)
        _assert_no_raw(out)
        assert len(out) == 1
        wire, data = out[0]
        assert data["category"] == "wildfire_spotting"
        assert data["_severity_override"] == "immediate"
        assert wire.startswith("🔥 Possible spotting ")
        # Eager latch stamped during ingest (not-cutover legacy path).
        from meshai.persistence import get_db
        latch = get_db().execute(
            "SELECT last_spotting_broadcast_at FROM fires WHERE irwin_id=?",
            ("ID-NS",)).fetchone()[0]
        assert latch == 1780768800.0


class TestIngestHalt:
    def test_halt_broadcasts_for_idle_fire(self):
        now = 1780768800
        idle_at = now - 14 * 3600
        from meshai.persistence import get_db
        get_db().execute(
            "INSERT INTO fires(irwin_id, incident_name, lat, lon, "
            "last_event_at, last_pass_id, last_pass_at) VALUES (?,?,?,?,?,?,?)",
            ("ID-NH", "Cold Fire", 42.5, -114.5, int(idle_at),
             "N20-329627", float(idle_at)),
        )
        # A fresh pixel FAR from the idle fire -> no attribution, cluster dead,
        # halt detector fires for the idle fire.
        out = _feed(_pixel(lat=45.0, lon=-118.0, acq_time="1800"), now=now)
        _assert_no_raw(out)
        assert len(out) == 1
        wire, data = out[0]
        assert data["category"] == "wildfire_halted"
        assert data["_severity_override"] == "routine"
        assert wire == "🔥 Cold Fire no growth in 14h"
        latch = get_db().execute(
            "SELECT halt_broadcast_at FROM fires WHERE irwin_id=?",
            ("ID-NH",)).fetchone()[0]
        assert latch == float(now)


class TestIngestNeverRaw:
    def test_lone_pixel_no_fire_yields_nothing(self):
        # Stored, attributed to nothing, below the cluster threshold, no idle
        # fire -> returns []. A raw hotspot is NEVER emitted on any path.
        out = _feed(_pixel(lat=44.4, lon=-116.2, acq_time="1300"),
                    now=1780750000)
        assert out == []

    def test_dense_unattributed_cluster_broadcasts_curated(self):
        # F3: several unattributed pixels close together form a CURATED cluster
        # ("possible new fire"). Non-seed path (no cold-start), so it broadcasts:
        # a wire fires on the 3rd pixel (min_pixels=3), the first 3 are stamped
        # so they can't re-fire, and the next 3 form a fresh cluster -> a 2nd
        # wire on the 6th pixel. NEVER a raw per-pixel broadcast.
        base_lat, base_lon = 44.0, -116.0
        produced = []
        for i in range(6):
            out = _feed(_pixel(lat=base_lat + 0.001 * i, lon=base_lon,
                               acq_time=f"13{i:02d}"), now=1780750000 + i)
            _assert_no_raw(out)
            produced.extend(out)
        assert len(produced) == 2, f"expected 2 curated cluster wires: {produced}"
        for wire, data in produced:
            assert wire.startswith("🔥 Possible new fire:")
            assert data["category"] == "unattributed_hotspot_cluster"
            assert data["_severity_override"] == "priority"


# ═════════════════════════════════════════════════════════════════════════════
# 2. env/firms.py tick() — shared engine + raw-hotspot suppression
# ═════════════════════════════════════════════════════════════════════════════

def _adapter(source="VIIRS_NOAA20_NRT"):
    from unittest.mock import MagicMock
    from meshai.env.firms import FIRMSAdapter
    cfg = MagicMock()
    cfg.map_key = "test-key"
    cfg.source = source
    cfg.bbox = [-117, 42, -114, 44]
    cfg.day_range = 1
    cfg.tick_seconds = 0
    cfg.confidence_min = "nominal"
    cfg.proximity_km = 10.0
    a = FIRMSAdapter(cfg, region_anchors=[], fires_adapter=None)
    a._last_tick = 0.0
    return a


def _csv(rows):
    header = "latitude,longitude,bright_ti4,confidence,frp,acq_date,acq_time"
    lines = [header]
    for r in rows:
        lines.append("{lat},{lon},{b},{c},{frp},{d},{t}".format(
            lat=r["lat"], lon=r["lon"], b=r.get("b", 320.0),
            c=r.get("c", "high"), frp=r.get("frp", 20.0),
            d=r.get("d", "2026-06-06"), t=r.get("t", "1200")))
    return "\n".join(lines)


class _FakeResp:
    def __init__(self, text):
        self._text = text
    def read(self):
        return self._text.encode("utf-8")
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def _patch_fetch(monkeypatch, csv_text):
    monkeypatch.setattr("meshai.env.firms.urlopen",
                        lambda *a, **k: _FakeResp(csv_text))


class TestAdapterTickFusion:
    def _growth_rows(self, center_lat, center_lon):
        # Anchor acq times to NOW (native path uses now=time.time()): pass A
        # ~6h ago, pass B ~now. Both are well inside the 12h halt gate, so the
        # growing fire never looks idle -- only the growth broadcast fires.
        # (6h apart => distinct pass buckets => a real pass boundary.)
        now_dt = datetime.now(timezone.utc)
        pass_a = now_dt - __import__("datetime").timedelta(hours=6)
        rows = []
        for i in range(5):
            t = pass_a + __import__("datetime").timedelta(minutes=i)
            rows.append({"lat": center_lat + 0.0001 * i,
                         "lon": center_lon + 0.0001 * (i - 2),
                         "d": t.strftime("%Y-%m-%d"), "t": t.strftime("%H%M"),
                         "frp": 20.0 + i})
        rows.append({"lat": center_lat + 1.0 / _MI_PER_DEG_LAT,
                     "lon": center_lon,
                     "d": now_dt.strftime("%Y-%m-%d"),
                     "t": now_dt.strftime("%H%M"), "frp": 22.0})
        return rows

    def test_tick_emits_growth_and_suppresses_raw(self, monkeypatch):
        center_lat, center_lon = 42.0, -114.0
        _seed_fire(irwin_id="ID-TG", lat=center_lat, lon=center_lon,
                   name="Pine Gulch")
        _patch_fetch(monkeypatch, _csv(self._growth_rows(center_lat, center_lon)))

        a = _adapter()
        # Steady-state growth routing (not cold start): mark the adapter past its
        # first-fetch silent-seed so this tick's growth broadcasts. Cold-start
        # fusion suppression is covered in test_firms_cluster_f3.
        a._firms_seeded = True
        assert a.tick() is True

        evts = a.get_events()
        raw = [e for e in evts if e.get("source") == "firms"]
        fusion = [e for e in evts if e.get("source") == "firms_fusion"]
        # Raw pixels present (LLM context) but every one renders None.
        assert raw, "raw hotspots should still be cached for LLM context"
        for r in raw:
            assert a.to_event(r) is None
        # Exactly one fusion broadcast, and it renders a real growth Event.
        assert len(fusion) == 1
        ev = a.to_event(fusion[0])
        assert ev is not None
        assert ev.category == "wildfire_growth"
        assert ev.summary.startswith("🔥 Pine Gulch")
        assert ev.data.get("_meshai_precomposed") is True

    def test_tick_emits_through_store_emit_event(self, monkeypatch):
        """End-to-end: the fusion Event reaches the pipeline bus via
        store._emit_event; raw hotspots never do."""
        from unittest.mock import MagicMock
        from meshai.env.store import EnvironmentalStore

        center_lat, center_lon = 42.0, -114.0
        _seed_fire(irwin_id="ID-TS", lat=center_lat, lon=center_lon,
                   name="Pine Gulch")
        _patch_fetch(monkeypatch, _csv(self._growth_rows(center_lat, center_lon)))

        a = _adapter()
        bus = MagicMock()
        store = EnvironmentalStore.__new__(EnvironmentalStore)
        store._events = {}
        store._event_bus = bus
        # Received-delta gate state. This test exercises STEADY-STATE fusion
        # routing (fusion reaches the bus, raw hotspots never do), so mark firms
        # as already past its first-poll seed — otherwise the store's
        # received-delta gate would (correctly) seed this first batch silently.
        # First-poll seed-silence is covered by test_store_received_delta.
        # The gate keys on the event's ``source`` (not the adapter name), and
        # this adapter emits BOTH "firms" (raw hotspots) and "firms_fusion"
        # (consolidated growth) — mark both seeded, exactly as a non-empty
        # ingest would (store._ingest marks every source it touched).
        store._seen = {}
        store._seeded = {"firms", "firms_fusion"}
        # Adapter-level cold-start seed is also a first-fetch silence gate; this
        # steady-state test wants the growth wire, so mark the adapter seeded too
        # (cold-start fusion suppression is covered in test_firms_cluster_f3).
        a._firms_seeded = True
        assert a.tick() is True
        store._ingest("firms", a)

        emitted = [c.args[0] for c in bus.emit.call_args_list]
        cats = [e.category for e in emitted]
        assert cats == ["wildfire_growth"], cats
        assert emitted[0].summary.startswith("🔥 Pine Gulch")

    def test_tick_with_no_fire_emits_nothing(self, monkeypatch):
        # Pixels with no seeded fire: attribution misses, cluster dead ->
        # zero fusion, and raw hotspots still suppressed.
        _patch_fetch(monkeypatch, _csv([
            {"lat": 43.5, "lon": -115.5, "t": "1300"},
            {"lat": 43.6, "lon": -115.4, "t": "1305"},
        ]))
        a = _adapter()
        a.tick()
        fusion = [e for e in a.get_events() if e.get("source") == "firms_fusion"]
        assert fusion == []

# chore/ripout-2dii: section 3 ("Central-path guard — extraction did not
# change handle_firms") REMOVED. `handle_firms` (the dead Central
# NATS-envelope entrypoint it drove) has been deleted from
# `meshai.env.fire_fusion` -- zero live production callers, and this section
# existed purely to prove that entrypoint was unchanged by the extraction.
# The shared core it guarded (_ingest_pixel_core) is exercised directly by
# every test above via `ingest_hotspot_pixel` (the live native entrypoint).
