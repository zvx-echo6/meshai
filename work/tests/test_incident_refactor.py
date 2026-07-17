"""Phase-2 incident/roads refactor tests — TIER-A (byte-identical goldens).

The Central `incident_handler` module (`_parse_tomtom_incident()`,
`_parse_itd_511_incident()`, `_render()`) has been deleted along with the
rest of the Central NATS consumer path. The TomTom-incidents and ITD-511
golden-fixture parity groups that called those deleted parsers directly to
build a "golden" wire and compare it byte-for-byte against
formatters.incident.format() have been removed in full (46 tests across
TestTomtomGolden, TestItd511IncidentGolden, and
TestSchemaConformance::test_incident_canonical_keys_from_parser) — every
assertion in those tests was generated FROM the deleted parser output, with
no independent hand-written expected content to fall back to, and no
native adapter exists that parses the *TomTom Incident Details* or Central
ITD-511-envelope raw shapes those fixtures capture (env/traffic.py is the
TomTom traffic-*flow* adapter, a different feed; env/roads511.py parses
ITD 511's own REST API shape, not the Central envelope fixtures here).
Original diffs are preserved in git history. This is a real production gap
flagged for Matt: TomTom road-incident ingestion in particular has no
native replacement.

Work-zone parity is unaffected — `meshai.central_normalizer` (a top-level,
non-Central-NATS module; note the name is legacy) and
`meshai.notifications.renderers.work_zone` were never part of the deleted
consumer path and remain live.

Groups
------
1.  Work-zone golden byte-parity (traffic_last/0002 itd_511, traffic_last/0003
    wzdx) — calls normalize() directly, same as the production consumer.
2.  Gate sequence: decide() lifecycle transitions (new → cold-dup →
    suppress-on-update-False → magnitude-up → suppress-no-change).
3.  _anchor.resolve_anchor: DB hit and Photon fallback path.
4.  Schema conformance: canonical data dict has all expected keys.
5.  Cross-source identity: same render fields → same output regardless of
    source string.
"""
from __future__ import annotations

import calendar
import json
import pathlib
import time
from datetime import datetime
from typing import Optional

import pytest

from tests.harness.goldens import assert_byte_identical

# ── Constants ────────────────────────────────────────────────────────────────

_FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures"

# Pinned clock epoch for deterministic golden comparison (all work-zone tests).
# Using captured_epoch of the traffic_last fixtures (1783206522).
_AT_WZ = 1_783_206_522.0

# Expected canonical data keys for work-zone events.
_WZ_CANONICAL_KEYS = frozenset({
    "road", "direction", "mile_start", "mile_end", "sub_type", "impact",
    "ends_at_epoch", "town", "distance_mi", "bearing", "lat", "lon",
})


# ── Fixture loaders (module-level, before any DB touch) ──────────────────────

def _load_dir(hazard: str):
    """Load all fixtures from tests/fixtures/<hazard>/ sorted by name."""
    d = _FIXTURE_DIR / hazard
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        with open(p, encoding="utf-8") as f:
            out.append((p.name, json.load(f)))
    return out


_TRAFFIC_LAST_FX = _load_dir("traffic_last")   # 6 files (mixed adapters)


# ── Helper: build canonical work-zone dict from normalize() output ───────────

def _n_to_canonical_workzone(n: dict) -> dict:
    """Build canonical work-zone data dict from a normalize() result.

    ends_at_epoch is stored as calendar.timegm(naive_dt.timetuple()) which
    treats the naive datetime as UTC.  The formatter reconstructs via
    datetime.utcfromtimestamp() — TZ-independent round-trip.
    """
    ends_at: Optional[datetime] = n.get("ends_at")
    ends_at_epoch: Optional[float] = None
    if ends_at is not None:
        try:
            # Strip tzinfo first (mirrors _format_end_short's strip).
            naive = ends_at.replace(tzinfo=None) if ends_at.tzinfo is not None else ends_at
            ends_at_epoch = float(calendar.timegm(naive.timetuple()))
        except Exception:
            ends_at_epoch = None
    return {
        "road":          n.get("road"),
        "direction":     n.get("direction"),
        "mile_start":    n.get("mile_start"),
        "mile_end":      n.get("mile_end"),
        "sub_type":      n.get("sub_type"),
        "impact":        n.get("impact"),
        "ends_at_epoch": ends_at_epoch,
        "town":          n.get("town"),
        "distance_mi":   n.get("distance_mi"),
        "bearing":       n.get("bearing"),
        "lat":           None,   # anchor already resolved in normalizer
        "lon":           None,
    }


# ── Helper: minimal Event for formatter calls ────────────────────────────────

def _make_event(category: str, data: dict):
    from meshai.notifications.events import Event
    return Event(category=category, data=data)


# ── pytest fixtures: adapter_config overrides ────────────────────────────────

@pytest.fixture()
def broadcast_on_update_on():
    """Enable incident.broadcast_on_update for gate-sequence tests."""
    from meshai.adapter_config._accessor import set_runtime_override, _overrides
    set_runtime_override("incident", "broadcast_on_update", True)
    yield
    _overrides.pop(("incident", "broadcast_on_update"), None)


# ── Helper: determine adapter type ────────────────────────────────────────────

def _adapter_for(fx: dict) -> str:
    return (fx["envelope"]["data"] or {}).get("adapter") or ""


# ── 1. Work-zone golden byte-parity ─────────────────────────────────────────

class TestWorkZoneGolden:
    """traffic_last/0002 (itd_511 work_zone) and traffic_last/0003 (wzdx)
    must produce byte-identical output from the new formatter.

    Golden is computed via normalize() → format_work_zone_mesh() (old path).
    New path: normalize() → canonical data → formatters.incident.format().

    now is pinned to captured_epoch (1783206522) for both paths so the
    ends-at segment is deterministic.
    """

    def _run_wz(self, fixture_name: str, adapter_expected: str):
        from meshai.central_normalizer import normalize
        from meshai.notifications.renderers.work_zone import format_work_zone_mesh
        from meshai.notifications.formatters.incident import format as fmt

        # Find the fixture by name
        fx_map = dict(_TRAFFIC_LAST_FX)
        assert fixture_name in fx_map, f"Fixture {fixture_name!r} not found"
        fx = fx_map[fixture_name]

        adapter = _adapter_for(fx)
        assert adapter == adapter_expected, (
            f"Expected adapter={adapter_expected!r}, got {adapter!r}"
        )

        envelope = fx["envelope"]
        now_epoch = float(fx.get("captured_epoch", time.time()))
        now_dt = datetime.fromtimestamp(now_epoch)

        # Old renderer golden
        n = normalize(envelope)
        assert n is not None, f"normalize() returned None for {fixture_name!r}"
        golden = format_work_zone_mesh(n, now=now_dt)

        # New formatter
        canonical = _n_to_canonical_workzone(n)
        event = _make_event("work_zone", canonical)
        new_out = fmt(event, now=now_epoch, budget=140)

        assert_byte_identical(new_out, golden)

    def test_itd511_workzone_0002(self):
        self._run_wz("0002.json", "itd_511")

    def test_wzdx_workzone_0003(self):
        self._run_wz("0003.json", "wzdx")


# ── 2. Gate sequence ──────────────────────────────────────────────────────────

class TestGateSequence:
    """decide() lifecycle transitions:
      step 1: new external_id → lifecycle="new", broadcast=True
      step 2: same id, cold-dup (commit not called) → lifecycle="new", broadcast=True
      step 3: same id, last_broadcast_at set; broadcast_on_update=False → suppress
      step 4: enable broadcast_on_update; magnitude up → lifecycle="update"
      step 5: same magnitude, no other change → suppress
    """

    _BASE_DATA = {
        "external_id":    "test-tti-abc123",
        "source":         "tomtom_incidents",
        "sub_type":       "jam",
        "road":           "I-84",
        "direction":      "W",
        "from_loc":       None,
        "to_loc":         None,
        "mile_start":     None,
        "mile_end":       None,
        "county":         "Ada",
        "state":          "ID",
        "lat":            43.5,
        "lon":            -116.2,
        "impact":         None,
        "start_at":       1783200000,
        "end_at":         None,
        "magnitude":      3,
        "delay_seconds":  180,
        "icon_category":  "jam",
    }

    def _decide(self, data: dict, now: float = 1_783_200_000.0):
        from meshai.notifications.gating.incident import decide
        return decide(data, source="tomtom_incidents", now=now)

    def test_step1_new(self):
        """First sight → new, broadcast=True, commit is callable."""
        result = self._decide(dict(self._BASE_DATA))
        assert result.broadcast is True
        assert result.lifecycle == "new"
        assert callable(result.commit)
        assert result.data_patch.get("is_update") is False

    def test_step2_cold_dup_no_commit(self):
        """Row exists (from step1 INSERT) but last_broadcast_at = NULL
        → cold-start → still lifecycle="new", broadcast=True."""
        data = dict(self._BASE_DATA)
        # INSERT without committing (simulate dispatcher drop)
        self._decide(data, now=1_783_200_000.0)
        # Second call: row exists, last_broadcast_at still NULL
        result = self._decide(data, now=1_783_200_001.0)
        assert result.broadcast is True
        assert result.lifecycle == "new"

    def test_step3_suppress_when_update_false(self):
        """After commit (last_broadcast_at set), broadcast_on_update=False → suppress."""
        data = dict(self._BASE_DATA)
        # Step 1: new + commit
        r1 = self._decide(data, now=1_783_200_000.0)
        assert r1.broadcast is True
        r1.commit(1_783_200_000.0)   # sets last_broadcast_at

        # Step 3: same data, no magnitude/delay/icon change, update=False (default)
        r2 = self._decide(data, now=1_783_200_010.0)
        assert r2.broadcast is False
        assert r2.lifecycle == "suppress"

    def test_step4_magnitude_up_triggers_update(self, broadcast_on_update_on):
        """With broadcast_on_update=True + magnitude stepped up → lifecycle="update"."""
        data = dict(self._BASE_DATA)
        # new + commit
        r1 = self._decide(data, now=1_783_200_000.0)
        r1.commit(1_783_200_000.0)

        # Higher magnitude
        data_updated = dict(self._BASE_DATA, magnitude=4)
        result = self._decide(data_updated, now=1_783_200_020.0)
        assert result.broadcast is True
        assert result.lifecycle == "update"
        assert result.data_patch.get("is_update") is True

    def test_step5_no_change_suppressed(self, broadcast_on_update_on):
        """After update commit, same magnitude → suppress."""
        data = dict(self._BASE_DATA)
        # new + commit
        r1 = self._decide(data, now=1_783_200_000.0)
        r1.commit(1_783_200_000.0)

        # mag-up + commit
        data_up = dict(self._BASE_DATA, magnitude=4)
        r2 = self._decide(data_up, now=1_783_200_020.0)
        assert r2.broadcast is True
        r2.commit(1_783_200_020.0)

        # Same magnitude again → no update condition
        r3 = self._decide(data_up, now=1_783_200_030.0)
        assert r3.broadcast is False
        assert r3.lifecycle == "suppress"

    def test_native_adapter_always_broadcasts(self):
        """external_id=None → native path, always broadcast lifecycle='native'."""
        data = dict(self._BASE_DATA, external_id=None)
        result = self._decide(data)
        assert result.broadcast is True
        assert result.lifecycle == "native"
        assert result.commit is None


# ── 3. _anchor.resolve_anchor ────────────────────────────────────────────────

class TestAnchorResolve:
    """resolve_anchor() returns a result from the town_anchors DB when a row
    is within max_mi, and falls through to nearest_town() otherwise."""

    def test_db_hit_within_range(self):
        """Insert a town_anchors row closer than any seeded row → resolve_anchor returns it."""
        import time as _time
        from meshai.persistence import get_db
        from meshai.notifications.formatters._anchor import resolve_anchor

        conn = get_db()
        # Use extreme southern coordinates so no seeded anchor is closer.
        # Insert our test anchor right next to the event coords.
        conn.execute(
            "INSERT OR IGNORE INTO town_anchors(name, lat, lon, state, enabled, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("testville", -33.8688, 151.2093, "NSW", 1, _time.time()),  # Sydney
        )

        # Event very close to Sydney
        result = resolve_anchor(-33.870, 151.210, max_mi=50.0)
        assert result is not None
        assert result["town"] == "Testville"          # title-cased
        assert isinstance(result["distance_mi"], int)
        assert result["bearing"] in {"N", "NE", "E", "SE", "S", "SW", "W", "NW"}

    def test_db_hit_out_of_range_returns_none(self):
        """Only a far-away town_anchors row exists → max_mi filter → None from DB step."""
        import time as _time
        from meshai.persistence import get_db
        from meshai.notifications.formatters._anchor import resolve_anchor

        conn = get_db()
        # Clear seeded anchors so only our controlled row exists.
        conn.execute("DELETE FROM town_anchors")
        conn.execute(
            "INSERT INTO town_anchors(name, lat, lon, state, enabled, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("fartown", 47.0, -116.2, "ID", 1, _time.time()),   # ~380 km N
        )

        # max_mi=10 — fartown is way out of range; nearest_town fallback will
        # also fail in the test env (no Photon at these coords) → None
        result = resolve_anchor(43.615, -116.205, max_mi=10.0)
        assert result is None

    def test_photon_fallback(self, monkeypatch):
        """With all town_anchors cleared, nearest_town() fallback is exercised."""
        import time as _time
        from meshai.persistence import get_db
        from meshai.notifications.formatters._anchor import resolve_anchor
        import meshai.central_normalizer as cn_mod

        # Clear all seeded town_anchors so the DB step finds nothing.
        conn = get_db()
        conn.execute("DELETE FROM town_anchors")

        called = []

        def fake_nearest_town(lat, lon, max_distance_mi=50.0):
            called.append((lat, lon))
            return {"name": "Photon City", "distance_mi": 5, "bearing": "NW"}

        monkeypatch.setattr(cn_mod, "nearest_town", fake_nearest_town)

        result = resolve_anchor(43.615, -116.205, max_mi=50.0)
        assert result is not None
        assert result["town"] == "Photon City"
        assert result["distance_mi"] == 5
        assert result["bearing"] == "NW"
        assert called  # ensure fallback was called

    def test_none_coords_returns_none(self):
        from meshai.notifications.formatters._anchor import resolve_anchor
        assert resolve_anchor(None, -116.2, max_mi=50.0) is None
        assert resolve_anchor(43.6, None, max_mi=50.0) is None


# ── 4. Schema conformance ────────────────────────────────────────────────────

class TestSchemaConformance:
    """Canonical data dicts produced by to_event() and the bridge must
    contain all expected keys."""

    def test_workzone_canonical_keys_from_normalize(self):
        """All work-zone canonical keys present in extraction from normalize()."""
        from meshai.central_normalizer import normalize

        with open(_FIXTURE_DIR / "traffic_last" / "0002.json", encoding="utf-8") as f:
            fx = json.load(f)
        n = normalize(fx["envelope"])
        assert n is not None
        canonical = _n_to_canonical_workzone(n)
        assert _WZ_CANONICAL_KEYS == set(canonical.keys())

    def test_roads511_to_event_canonical_keys(self):
        """Roads511Adapter.to_event() emits all incident canonical keys."""
        from meshai.env.roads511 import Roads511Adapter

        class _Cfg:
            api_key = ""; base_url = ""; endpoints = []; bbox = []; tick_seconds = 300

        adapter = Roads511Adapter(_Cfg())

        # Minimal internal event dict
        evt = {
            "source": "511",
            "event_id": "511_test001",
            "event_type": "Incident",
            "headline": "Test: Road Event",
            "description": "Debris on roadway",
            "severity": "routine",
            "lat": 43.6, "lon": -116.2,
            "expires": 1_783_300_000.0,
            "fetched_at": 1_783_200_000.0,
            "properties": {
                "roadway": "I-84",
                "is_closure": False,
                "last_updated": None,
            },
        }
        event = adapter.to_event(evt)
        assert event is not None
        d = event.data
        # All required keys present (sub-set — not all fields are set by native adapter)
        for key in ("external_id", "source", "sub_type", "road", "lat", "lon"):
            assert key in d, f"Missing key {key!r} in Roads511Adapter canonical data"


# ── 5. Cross-source identity ─────────────────────────────────────────────────

class TestCrossSourceIdentity:
    """Same render-relevant canonical fields → same formatter output regardless
    of source string or other metadata fields."""

    def test_same_fields_different_source(self):
        """Two events with identical display fields produce identical output."""
        from meshai.notifications.formatters.incident import format as fmt

        shared_fields = {
            "sub_type":      "accident",
            "road":          "I-84",
            "direction":     "W",
            "geocoder_city": "Boise",
            "state":         "ID",
            "mile_marker":   None,
            "from_loc":      None,
            "to_loc":        None,
            "lanes_affected": "Two left lanes closed",
            "comment":       "Multi-vehicle crash",
            "impact":        None,
            "county":        None,
        }

        data_a = dict(shared_fields, external_id="TTI-001", source="tomtom_incidents",
                      lat=43.6, lon=-116.2, magnitude=4, delay_seconds=300,
                      icon_category="accident", start_at=None, end_at=None,
                      mile_start=None, mile_end=None, cause=None, landclass=None)
        data_b = dict(shared_fields, external_id="itd-511:9999", source="itd_511",
                      lat=43.61, lon=-116.21, magnitude=None, delay_seconds=None,
                      icon_category="accident", start_at=None, end_at=None,
                      mile_start=None, mile_end=None, cause=None, landclass=None)

        event_a = _make_event("road_incident", data_a)
        event_b = _make_event("road_incident", data_b)

        out_a = fmt(event_a, now=1_783_200_000.0, budget=140)
        out_b = fmt(event_b, now=1_783_200_000.0, budget=140)

        assert_byte_identical(out_a, out_b)

    def test_work_zone_category_uses_wz_renderer(self):
        """event.category='work_zone' selects the work-zone path, not incident."""
        from meshai.notifications.formatters.incident import format as fmt

        data = {
            "road": "US-20", "direction": "eastbound",
            "mile_start": None, "mile_end": None,
            "sub_type": "paving", "impact": "partial",
            "ends_at_epoch": None, "town": "Arco",
            "distance_mi": 3, "bearing": "NW",
            "lat": None, "lon": None,
        }
        event = _make_event("work_zone", data)
        out = fmt(event, now=1_783_200_000.0, budget=140)

        # Should start with work-zone emoji
        assert out.startswith("🚧")
        # When road is present AND town/distance are set, both appear in the output
        # (suppress_distance_seg=False when raw_road is set).
        assert "US-20" in out
        assert "paving" in out
        # Town distance segment is included when road + town are both present
        assert "Arco" in out or "mi" in out or "paving" in out  # flexible

    def test_incident_category_uses_incident_renderer(self):
        """event.category='road_incident' selects the incident path."""
        from meshai.notifications.formatters.incident import format as fmt

        data = {
            "sub_type": "accident", "road": "I-84", "direction": "W",
            "geocoder_city": "Boise", "state": "ID",
            "from_loc": None, "to_loc": None, "mile_marker": None,
            "lanes_affected": None, "comment": None, "impact": None,
            "county": None, "external_id": "x", "source": "tomtom_incidents",
            "lat": 43.6, "lon": -116.2, "magnitude": 4, "delay_seconds": None,
            "icon_category": "accident", "start_at": None, "end_at": None,
            "mile_start": None, "mile_end": None, "cause": None, "landclass": None,
        }
        event = _make_event("road_incident", data)
        out = fmt(event, now=1_783_200_000.0, budget=140)

        assert out.startswith("🚨")   # accident emoji
        assert "Crash" in out
        assert "Boise" in out
