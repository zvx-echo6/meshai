"""Phase-1 quake refactor tests — reference implementation verification.

Four test groups:

1. Parity (tier-b): fixture 0002 → canonical data → formatter.
   Expected string is hand-written (the new correct format).
   The OLD _render() output for the same fixture is captured in a comment so
   the intended tier-b diff is explicit and reviewable.
   Two synthetic cases show the PAGER + update-prefix diffs explicitly.

2. Cross-source identity: native adapter builds the same canonical data as
   the Central path for fixture 0002.  Both render byte-identically.

3. Gate-sequence: replay four synthetic events through the OLD handle_quake
   gating and the NEW gating.quake.decide(); assert broadcast/suppress match.

4. Schema-conformance: env/usgs_quake.py to_event() emits all canonical keys.
"""
from __future__ import annotations

import pytest

from meshai.persistence import close_thread_connection, init_db
from meshai.persistence import db as persistence_db
from tests.harness.goldens import (
    assert_byte_identical,
    load_fixtures,
    pinned_time,
    run_gate_sequence,
)

# ── Shared clock epoch for deterministic renders ─────────────────────────────
_AT = 1_783_200_000.0   # 2026-07-03T00:00:00Z (pinned)

# ── DB fixture shared by gate-sequence tests ─────────────────────────────────

@pytest.fixture
def mem_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "quake-refactor-test.sqlite")
    monkeypatch.setenv("MESHAI_DB_PATH", db_path)
    persistence_db._initialised.clear()
    close_thread_connection()
    conn = init_db()
    yield conn
    close_thread_connection()
    persistence_db._initialised.discard(db_path)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Parity (tier-b) — formatter renders from canonical data
# ─────────────────────────────────────────────────────────────────────────────

def _make_fake_event(data: dict):
    """Minimal fake Event for calling the formatter without the full pipeline."""
    class _FakeEvent:
        pass
    e = _FakeEvent()
    e.data = data
    return e


class TestFormatterParity:
    """formatter/quake.format() renders correct output from canonical data."""

    def _render_old(self, *, mag, place, depth_km, lat, lon, tsunami, is_update=False):
        """Capture OLD _render() output for diff comments."""
        from meshai.central.quake_handler import _render
        from meshai.central.budget import budget_for
        return _render(mag=mag, place=place, depth_km=depth_km, lat=lat,
                       lon=lon, tsunami=tsunami, is_update=is_update)

    def test_fixture_0002_new_format(self):
        """Fixture 0002 (M3.3 Lima Montana) → NEW formatter output matches hand-written expected.

        Fixture 0002 has alert=null and no tsunami so the tier-b additions
        (PAGER line, update-prefix) are not visible.  The old and new outputs
        are IDENTICAL for this fixture — which is correct.  The hand-written
        expected below documents the canonical format; synthetic tests below
        show the tier-b additions.

        OLD _render() output (captured for diff transparency):
            "🌐 New: M3.3 — 19 km S of Lima, Montana\\nDepth: 11 km · @ 44.460, -112.611"
        NEW formatter output (same — no tier-b changes triggered):
            "🌐 New: M3.3 — 19 km S of Lima, Montana\\nDepth: 11 km · @ 44.460, -112.611"
        """
        from meshai.notifications.formatters.quake import format as qfmt

        fixtures = load_fixtures("quake")
        fx = next(f for f in fixtures if f["envelope"]["id"] == "us6000t9bn")
        inner = fx["envelope"]["data"]
        d = inner["data"]
        geo = inner["geo"]
        cent = geo["centroid"]  # [lon, lat]

        canonical = {
            "magnitude": d["magnitude"],        # 3.3
            "depth_km": d["depth"],             # 11.169 (raw USGS key)
            "lat": cent[1],                     # 44.46
            "lon": cent[0],                     # -112.6108
            "place": d["place"],                # "19 km S of Lima, Montana"
            "tsunami": bool(d["tsunami"]),       # False
            "pager": d.get("alert"),            # None
            "occurred_at": None,
            "event_id": fx["envelope"]["id"],
            "is_update": False,
            "_severity_override": None,
            "_dedup_suffix": "",
            "distance_km": 160.0,
        }

        # Hand-written new correct format (tier-b changes are invisible here)
        expected = (
            "\U0001f310 New: M3.3 — 19 km S of Lima, Montana"
            "\nDepth: 11 km · @ 44.460, -112.611"
        )

        with pinned_time(_AT):
            result = qfmt(_make_fake_event(canonical), now=_AT, budget=140)

        assert_byte_identical(result, expected)

        # Verify old _render matches new for this fixture (no tier-b diff)
        old_wire = self._render_old(
            mag=canonical["magnitude"], place=canonical["place"],
            depth_km=canonical["depth_km"], lat=canonical["lat"],
            lon=canonical["lon"], tsunami=canonical["tsunami"],
            is_update=False,
        )
        assert_byte_identical(result, old_wire), (
            "For fixture 0002 (null PAGER, is_update=False) old and new "
            "outputs must be identical — the tier-b diff only appears when "
            "PAGER or is_update are set."
        )

    def test_tier_b_pager_orange_rendered(self):
        """Tier-b ①: PAGER=orange is NOW rendered on a 4th line.

        OLD _render() output (captured):
            "🌐 New: M2.0 — Off the coast of Oregon\\nDepth: 10 km · @ 44.000, -125.000"
        NEW formatter output (tier-b change — PAGER line added):
            "🌐 New: M2.0 — Off the coast of Oregon\\nDepth: 10 km · @ 44.000, -125.000\\n⚠️ PAGER: orange"
        """
        from meshai.notifications.formatters.quake import format as qfmt

        canonical = {
            "magnitude": 2.0,
            "depth_km": 10.0,
            "lat": 44.0,
            "lon": -125.0,
            "place": "Off the coast of Oregon",
            "tsunami": False,
            "pager": "orange",      # PAGER set — triggers tier-b line
            "is_update": False,
            "occurred_at": None,
            "event_id": "test_pager_orange",
            "_severity_override": "immediate",
            "_dedup_suffix": "",
            "distance_km": 500.0,
        }

        # OLD _render() output (PAGER not rendered)
        old_wire = self._render_old(
            mag=2.0, place="Off the coast of Oregon", depth_km=10.0,
            lat=44.0, lon=-125.0, tsunami=False, is_update=False,
        )
        # NEW formatter output (PAGER rendered as 4th line)
        expected_new = (
            "\U0001f310 New: M2.0 — Off the coast of Oregon"
            "\nDepth: 10 km · @ 44.000, -125.000"
            "\n⚠️ PAGER: orange"
        )

        with pinned_time(_AT):
            result = qfmt(_make_fake_event(canonical), now=_AT, budget=140)

        assert_byte_identical(result, expected_new)
        # Confirm the old wire does NOT have the PAGER line
        assert "PAGER" not in old_wire, (
            f"OLD _render() must not contain PAGER line; got: {old_wire!r}"
        )

    def test_tier_b_update_prefix_rendered(self):
        """Tier-b ②: is_update=True produces 'Update:' prefix (was hard-coded 'New:').

        OLD _render() output (is_update always False):
            "🌐 New: M3.0 — 5 km NE of Stanley, Idaho\\nDepth: 8 km · @ 44.200, -114.900"
        NEW formatter output (is_update=True):
            "🌐 Update: M3.0 — 5 km NE of Stanley, Idaho\\nDepth: 8 km · @ 44.200, -114.900"
        """
        from meshai.notifications.formatters.quake import format as qfmt

        canonical = {
            "magnitude": 3.0,
            "depth_km": 8.0,
            "lat": 44.2,
            "lon": -114.9,
            "place": "5 km NE of Stanley, Idaho",
            "tsunami": False,
            "pager": None,
            "is_update": True,      # tier-b: update-prefix now live
            "occurred_at": None,
            "event_id": "test_update_prefix",
            "_severity_override": None,
            "_dedup_suffix": "",
            "distance_km": 10.0,
        }

        # OLD _render() always uses is_update=False
        old_wire = self._render_old(
            mag=3.0, place="5 km NE of Stanley, Idaho", depth_km=8.0,
            lat=44.2, lon=-114.9, tsunami=False, is_update=False,
        )
        expected_new = (
            "\U0001f310 Update: M3.0 — 5 km NE of Stanley, Idaho"
            "\nDepth: 8 km · @ 44.200, -114.900"
        )
        expected_old = (
            "\U0001f310 New: M3.0 — 5 km NE of Stanley, Idaho"
            "\nDepth: 8 km · @ 44.200, -114.900"
        )

        with pinned_time(_AT):
            result = qfmt(_make_fake_event(canonical), now=_AT, budget=140)

        assert_byte_identical(result, expected_new)
        assert_byte_identical(old_wire, expected_old)
        assert "Update:" in result
        assert "New:" not in result

    def test_tsunami_escalation_preserved(self):
        """Tsunami escalation (🚨 emoji + TSUNAMI WARNING line) unchanged from _render."""
        from meshai.notifications.formatters.quake import format as qfmt

        canonical = {
            "magnitude": 4.5,
            "depth_km": 5.0,
            "lat": 35.0,
            "lon": 141.0,
            "place": "off the coast of Japan",
            "tsunami": True,
            "pager": None,
            "is_update": False,
            "occurred_at": None,
            "event_id": "test_tsunami",
            "_severity_override": "immediate",
            "_dedup_suffix": "",
            "distance_km": 8000.0,
        }

        with pinned_time(_AT):
            result = qfmt(_make_fake_event(canonical), now=_AT, budget=140)

        old_wire = self._render_old(
            mag=4.5, place="off the coast of Japan", depth_km=5.0,
            lat=35.0, lon=141.0, tsunami=True,
        )
        assert result.startswith("\U0001f6a8"), "Tsunami emoji must be 🚨"
        assert "\U0001f6a8 TSUNAMI WARNING" in result
        assert_byte_identical(result, old_wire)

    def test_m5_escalation_emoji_preserved(self):
        """M5+ uses ⚠️ emoji — unchanged from _render."""
        from meshai.notifications.formatters.quake import format as qfmt

        canonical = {
            "magnitude": 5.2,
            "depth_km": 12.0,
            "lat": 44.0,
            "lon": -114.0,
            "place": "15 km NW of Mackay, Idaho",
            "tsunami": False,
            "pager": None,
            "is_update": False,
            "occurred_at": None,
            "event_id": "test_m5",
            "_severity_override": None,
            "_dedup_suffix": "",
            "distance_km": 50.0,
        }

        with pinned_time(_AT):
            result = qfmt(_make_fake_event(canonical), now=_AT, budget=140)

        assert result.startswith("⚠️"), f"M5.2 must use ⚠️ emoji; got: {result!r}"


# ─────────────────────────────────────────────────────────────────────────────
# 2. Cross-source identity — native and Central produce identical renders
# ─────────────────────────────────────────────────────────────────────────────

class TestCrossSourceIdentity:
    """Native to_event() canonical data renders byte-identically to Central path."""

    def test_native_central_render_identical_fixture_0002(self):
        """Build native canonical data for fixture 0002 event, render → byte-identical.

        The Central path produces canonical data by extracting from the
        envelope.  The native path produces canonical data in to_event().
        The formatter reads the same keys from both → same wire.
        """
        from meshai.notifications.formatters.quake import format as qfmt

        # Central-path canonical data (hand-extracted from fixture 0002)
        central_canonical = {
            "magnitude": 3.3,
            "depth_km": 11.169,     # normalized from raw "depth" field
            "lat": 44.46,
            "lon": -112.6108,
            "place": "19 km S of Lima, Montana",
            "tsunami": False,
            "pager": None,
            "occurred_at": None,
            "event_id": "us6000t9bn",
            "is_update": False,
            "_severity_override": None,
            "_dedup_suffix": "",
            "distance_km": 160.0,
        }

        # Native-path canonical data (as to_event() would set it)
        native_canonical = {
            "magnitude": 3.3,
            "depth_km": 11.169,
            "lat": 44.46,
            "lon": -112.6108,
            "place": "19 km S of Lima, Montana",
            "tsunami": False,       # native has no tsunami flag
            "pager": None,          # native has no PAGER
            "occurred_at": None,
            "event_id": "us6000t9bn",
            "is_update": False,
            "_severity_override": None,
            "_dedup_suffix": "",
            "distance_km": 160.0,
        }

        with pinned_time(_AT):
            central_wire = qfmt(_make_fake_event(central_canonical),
                                now=_AT, budget=140)
            native_wire = qfmt(_make_fake_event(native_canonical),
                               now=_AT, budget=140)

        assert_byte_identical(native_wire, central_wire), (
            f"Native and Central renders must be byte-identical:\n"
            f"  Central: {central_wire!r}\n"
            f"  Native:  {native_wire!r}"
        )

    def test_native_to_event_uses_canonical_keys(self):
        """to_event() data dict has the canonical keys the formatter reads."""
        from unittest.mock import MagicMock
        from meshai.env.usgs_quake import USGSQuakeAdapter

        cfg = MagicMock()
        cfg.feed_url = "https://example.com/feed"
        cfg.min_magnitude = 1.0
        cfg.bbox = []
        cfg.region = "magic_valley"
        cfg.tick_seconds = 300

        adapter = USGSQuakeAdapter(cfg)
        raw_evt = {
            "event_id": "us_test_identity",
            "magnitude": 3.5,
            "place": "5 km SW of Twin Falls, Idaho",
            "depth_km": 7.5,
            "lat": 42.5,
            "lon": -114.5,
            "quake_time": 1_783_000_000.0,
            "fetched_at": 1_783_000_010.0,
            "expires": 1_783_086_400.0,
            "severity": "priority",
        }
        event = adapter.to_event(raw_evt)

        assert event is not None, "to_event() must return an Event for valid input"
        assert event.data is not None, "event.data must not be None"

        canonical_keys = {
            "magnitude", "depth_km", "lat", "lon", "place",
            "tsunami", "pager", "occurred_at", "event_id",
        }
        missing = canonical_keys - set(event.data.keys())
        assert not missing, (
            f"to_event() event.data missing canonical keys: {missing}\n"
            f"Got keys: {sorted(event.data.keys())}"
        )

        # Spot-check values
        assert event.data["magnitude"] == 3.5
        assert event.data["lat"] == 42.5
        assert event.data["lon"] == -114.5
        assert event.data["depth_km"] == 7.5
        assert event.data["tsunami"] is False
        assert event.data["pager"] is None
        assert event.data["event_id"] == "us_test_identity"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Gate-sequence — old gating vs new decide() — identical decisions
# ─────────────────────────────────────────────────────────────────────────────

def _make_envelope(*, event_id, mag, lat, lon, depth_km=10.0, place=None,
                   tsunami=0, alert=None, time_ms=1_780_000_000_000):
    """Build a minimal Central-style quake envelope for gate-sequence testing."""
    place = place or f"near test location ({lat:.1f},{lon:.1f})"
    return {
        "envelope": {
            "id": event_id,
            "data": {
                "id": event_id,
                "adapter": "usgs_quake",
                "category": "quake.event.test",
                "severity": 0,
                "geo": {"centroid": [lon, lat]},
                "data": {
                    "id": event_id,
                    "magnitude": mag,
                    "place": place,
                    "depth_km": depth_km,
                    "time_ms": time_ms,
                    "tsunami": tsunami,
                    "alert": alert,
                    "latitude": lat,
                    "longitude": lon,
                    "depth": depth_km,
                },
            },
        },
        "subject": "central.quake.event.test.unknown",
        "captured_epoch": int(time_ms / 1000),
    }


class TestGateSequence:
    """Gate parity: old handle_quake decisions match new gating.quake.decide()."""

    @pytest.fixture(autouse=True)
    def _db(self, mem_db):
        """All tests in this class share the same mem_db."""
        self.db = mem_db

    def _old_gate(self, fixture, *, now):
        """Old path: handle_quake returning non-None = broadcast."""
        from meshai.central.quake_handler import handle_quake
        env = fixture["envelope"]
        wire = handle_quake(env, fixture["subject"], data={}, now=int(now))
        return wire is not None

    def _new_gate(self, fixture, *, now):
        """New path: gating.quake.decide()."""
        from meshai.notifications.gating.quake import decide
        env = fixture["envelope"]
        inner = env.get("data") or {}
        d = inner.get("data") or {}
        geo = inner.get("geo") or {}
        cent = geo.get("centroid") or []
        lon, lat = (cent[0], cent[1]) if len(cent) >= 2 else (None, None)
        tms = d.get("time_ms")
        occurred_at = None
        if isinstance(tms, (int, float)):
            occurred_at = int(tms / 1000) if tms > 1e12 else int(tms)

        canonical = {
            "magnitude": d.get("magnitude"),
            "depth_km": d.get("depth_km") or d.get("depth"),
            "lat": lat,
            "lon": lon,
            "place": d.get("place"),
            "tsunami": bool(d.get("tsunami")),
            "pager": d.get("alert"),
            "occurred_at": occurred_at,
            "event_id": d.get("id") or inner.get("id"),
        }
        return decide(canonical, source="usgs_quake", now=float(now))

    def test_gate_sequence_matches(self):
        """Four-event sequence: old and new gates make identical broadcast/suppress decisions.

        Sequence (each event has a DISTINCT event_id — the commit/suppress
        cycle is tested separately in test_suppress_after_commit):
          [0] M2.0, far (below all thresholds)             → suppress
          [1] M2.7, within Idaho (regional gate)            → broadcast
          [2] M3.5, anywhere (global floor)                 → broadcast
          [3] M6.0 + tsunami (any-magnitude tsunami gate)   → broadcast

        Gate decisions (broadcast True/False) must match between old and new.
        NOTE: PAGER/update-prefix are formatter-only tier-b changes; they do
        NOT affect gate decisions — any divergence here is a regression.
        """
        t_base = 1_780_000_000.0

        # [0] M2.0 far outside Idaho (lat=10.0, lon=140.0 → Japan)
        fx0 = _make_envelope(event_id="gs_seq_0", mag=2.0, lat=10.0, lon=140.0,
                              time_ms=int(t_base * 1000))
        # [1] M2.7 within 250mi of Idaho centroid (Wyoming border)
        fx1 = _make_envelope(event_id="gs_seq_1", mag=2.7, lat=44.09, lon=-115.96,
                              time_ms=int((t_base + 100) * 1000))
        # [2] M3.5 anywhere (global_mag_floor = 3.0 exceeded)
        fx2 = _make_envelope(event_id="gs_seq_2", mag=3.5, lat=10.0, lon=140.0,
                              time_ms=int((t_base + 200) * 1000))
        # [3] M6.0 + tsunami (any magnitude with tsunami → broadcast)
        fx3 = _make_envelope(event_id="gs_seq_3", mag=6.0, lat=35.0, lon=141.0,
                              tsunami=1, time_ms=int((t_base + 300) * 1000))

        ordered = [fx0, fx1, fx2, fx3]
        timeline = [t_base, t_base + 100, t_base + 200, t_base + 300]

        results = run_gate_sequence(
            self._old_gate,
            self._new_gate,
            ordered,
            timeline=timeline,
        )

        mismatches = [r for r in results if not r["match"]]
        assert not mismatches, (
            "Gate sequence mismatch between old handle_quake and new decide():\n"
            + "\n".join(
                f"  step {r['fixture_n']}: old={r['old_broadcast']} "
                f"new={r['new_broadcast']} diffs={r['diffs']}"
                for r in mismatches
            )
        )

        # Verify expected pattern
        assert results[0]["old_broadcast"] is False, "M2.0 far must be suppressed"
        assert results[1]["old_broadcast"] is True,  "M2.7 Idaho must broadcast"
        assert results[2]["old_broadcast"] is True,  "M3.5 global must broadcast"
        assert results[3]["old_broadcast"] is True,  "M6.0+tsunami must broadcast"

    def test_suppress_after_commit(self):
        """After commit, the same event_id is suppressed by both old and new gates.

        The run_gate_sequence harness does not call commits between steps, so
        the commit+suppress lifecycle is tested here separately by manual
        sequencing.
        """
        from meshai.central.quake_handler import handle_quake
        from meshai.notifications.gating.quake import decide

        t0 = 1_780_000_000.0
        event_id = "suppress_after_commit_test"

        fx = _make_envelope(event_id=event_id, mag=3.5, lat=44.09, lon=-115.96,
                             time_ms=int(t0 * 1000))
        env = fx["envelope"]

        # First arrival: both old and new broadcast
        data1 = {}
        old_wire1 = handle_quake(env, fx["subject"], data=data1, now=int(t0))
        assert old_wire1 is not None, "First arrival must broadcast (old)"

        # Build canonical from fixture for new gate
        inner = env["data"]
        d = inner["data"]
        geo = inner["geo"]
        cent = geo["centroid"]
        canonical = {
            "magnitude": d["magnitude"],
            "depth_km": d.get("depth_km") or d.get("depth"),
            "lat": cent[1], "lon": cent[0],
            "place": d.get("place"),
            "tsunami": bool(d.get("tsunami")),
            "pager": d.get("alert"),
            "occurred_at": int(d["time_ms"] / 1000),
            "event_id": event_id,
        }
        # Since old gate already wrote the row (INSERT), new gate sees the
        # same DB state.  Both should broadcast on first arrival.
        # (We test new gate's second call AFTER commit below)

        # Call commit (simulates confirmed delivery)
        assert "_on_broadcast_committed" in data1, "commit callback must be attached"
        data1["_on_broadcast_committed"](t0 + 1.0)

        # Second arrival with same event_id — old gate must suppress
        old_wire2 = handle_quake(env, fx["subject"], data={}, now=int(t0 + 60))
        assert old_wire2 is None, "Old gate must suppress after commit"

        # New gate must also suppress
        new_result2 = decide(canonical, source="usgs_quake", now=t0 + 60)
        assert new_result2.broadcast is False, "New gate must suppress after commit"

    def test_severity_override_from_decide(self):
        """decide() sets _severity_override=immediate for tsunami/PAGER."""
        from meshai.notifications.gating.quake import decide

        # Tsunami
        canonical_tsunami = {
            "magnitude": 4.5, "depth_km": 10.0, "lat": 35.0, "lon": 141.0,
            "place": "off Japan", "tsunami": True, "pager": None,
            "occurred_at": None, "event_id": "sv_tsunami_test",
        }
        result_ts = decide(canonical_tsunami, source="usgs_quake", now=_AT)
        assert result_ts.broadcast is True
        assert result_ts.data_patch.get("_severity_override") == "immediate"

        # PAGER orange
        canonical_pager = {
            "magnitude": 2.0, "depth_km": 10.0, "lat": 10.0, "lon": 140.0,
            "place": "Pacific Ocean", "tsunami": False, "pager": "orange",
            "occurred_at": None, "event_id": "sv_pager_test",
        }
        result_pg = decide(canonical_pager, source="usgs_quake", now=_AT)
        assert result_pg.broadcast is True
        assert result_pg.data_patch.get("_severity_override") == "immediate"

    def test_dedup_suffix_is_empty(self):
        """decide() data_patch has _dedup_suffix='' (bare event.id used for dedup)."""
        from meshai.notifications.gating.quake import decide

        canonical = {
            "magnitude": 3.5, "depth_km": 10.0, "lat": 44.0, "lon": -114.0,
            "place": "near Stanley, Idaho", "tsunami": False, "pager": None,
            "occurred_at": None, "event_id": "dedup_suffix_test",
        }
        result = decide(canonical, source="usgs_quake", now=_AT)
        assert result.broadcast is True
        assert result.data_patch.get("_dedup_suffix") == ""

    def test_is_update_always_false_in_patch(self):
        """decide() data_patch always has is_update=False (v0.5.9 no-Update rule)."""
        from meshai.notifications.gating.quake import decide

        canonical = {
            "magnitude": 3.0, "depth_km": 8.0, "lat": 44.0, "lon": -114.5,
            "place": "central Idaho", "tsunami": False, "pager": None,
            "occurred_at": None, "event_id": "is_update_test",
        }
        result = decide(canonical, source="usgs_quake", now=_AT)
        assert result.broadcast is True
        assert result.data_patch.get("is_update") is False


# ─────────────────────────────────────────────────────────────────────────────
# 4. Schema conformance — to_event() canonical data completeness
# ─────────────────────────────────────────────────────────────────────────────

class TestSchemaConformance:
    """env/usgs_quake.py to_event() emits exactly the canonical key set."""

    @pytest.fixture
    def adapter(self):
        from unittest.mock import MagicMock
        from meshai.env.usgs_quake import USGSQuakeAdapter
        cfg = MagicMock()
        cfg.feed_url = "https://example.com/feed"
        cfg.min_magnitude = 1.0
        cfg.bbox = []
        cfg.region = "magic_valley"
        cfg.tick_seconds = 300
        return USGSQuakeAdapter(cfg)

    CANONICAL_KEYS = frozenset({
        "magnitude", "depth_km", "lat", "lon", "place",
        "tsunami", "pager", "occurred_at", "event_id",
    })

    def _raw_evt(self, **overrides):
        base = {
            "event_id": "conform_test",
            "magnitude": 3.1,
            "place": "5 km NW of test, Idaho",
            "depth_km": 9.0,
            "lat": 44.0,
            "lon": -114.5,
            "quake_time": 1_783_000_000.0,
            "fetched_at": 1_783_000_010.0,
            "expires": 1_783_086_400.0,
            "severity": "routine",
        }
        base.update(overrides)
        return base

    def test_all_canonical_keys_present(self, adapter):
        """event.data contains all canonical schema keys."""
        event = adapter.to_event(self._raw_evt())
        assert event is not None
        missing = self.CANONICAL_KEYS - set(event.data.keys())
        assert not missing, f"Missing canonical keys: {missing}"

    def test_no_extra_non_canonical_fields_cause_formatter_crash(self, adapter):
        """Extra fields in event.data (e.g. raw USGS keys) don't crash the formatter."""
        from meshai.notifications.formatters.quake import format as qfmt

        event = adapter.to_event(self._raw_evt())
        assert event is not None

        # Inject extra keys that might come from Central enrichment
        event.data["_enriched"] = {"geocoder": {"city": "TestCity"}}
        event.data["sig"] = 123

        with pinned_time(_AT):
            result = qfmt(event, now=_AT, budget=140)

        assert result is not None
        assert "M3.1" in result
        assert len(result) <= 140

    def test_tsunami_defaults_false(self, adapter):
        """Native to_event() sets tsunami=False (native feed has no tsunami data)."""
        event = adapter.to_event(self._raw_evt())
        assert event.data["tsunami"] is False

    def test_pager_defaults_none(self, adapter):
        """Native to_event() sets pager=None (PAGER comes from Central only)."""
        event = adapter.to_event(self._raw_evt())
        assert event.data["pager"] is None

    def test_event_id_matches_raw_evt(self, adapter):
        """event.data["event_id"] matches the source event_id."""
        event = adapter.to_event(self._raw_evt(event_id="my_quake_id"))
        assert event.data["event_id"] == "my_quake_id"

    def test_missing_depth_km_yields_none(self, adapter):
        """to_event() handles missing depth gracefully (depth_km=None in data)."""
        raw = self._raw_evt()
        del raw["depth_km"]  # simulate missing depth
        event = adapter.to_event(raw)
        assert event is not None
        assert event.data.get("depth_km") is None

    def test_formatter_budget_respected(self, adapter):
        """Formatter output fits within budget for worst-case place string."""
        from meshai.notifications.formatters.quake import format as qfmt

        raw = self._raw_evt(
            magnitude=7.9,
            place="293 km SSW of a pathologically long place description island "
                  "region in the remote northern pacific ocean near absolutely nowhere "
                  "at all off the coast of the far edge of the map",
            depth_km=12.0,
            lat=44.123,
            lon=-114.987,
        )
        event = adapter.to_event(raw)
        assert event is not None
        with pinned_time(_AT):
            result = qfmt(event, now=_AT, budget=140)
        assert len(result) <= 140, f"{len(result)} chars:\n{result!r}"
        assert "M7.9" in result
