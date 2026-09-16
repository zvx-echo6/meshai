"""Tests for env/watchduty.py's pure matcher (match_fires) and the
candidate-fires DB query (WatchDutyAdapter._candidate_fires).

No network calls -- match_fires is a pure function; the candidate query
hits the per-test isolated SQLite DB (tests/conftest.py autouse fixture).
"""
from __future__ import annotations

import time

import pytest

from meshai.config import WatchDutyConfig
from meshai.env.watchduty import WatchDutyAdapter, match_fires
from meshai.persistence import get_db

# Anchor point + a WD event ~2.80 km north (well inside the 3.0 km default
# radius) and ~3.50 km north (just outside it).
_LAT0, _LON0 = 44.0, -114.0
_LAT_2_8KM = 44.0252   # ~2.80 km from (_LAT0, _LON0)
_LAT_3_5KM = 44.0315   # ~3.50 km from (_LAT0, _LON0)

_RADIUS_KM = 3.0


def _wd_event(event_id, *, lat=_LAT0, lng=_LON0, is_active=True,
              is_prescribed=False, name="Test Fire WD"):
    return {
        "id": event_id,
        "name": name,
        "is_active": is_active,
        "lat": lat,
        "lng": lng,
        "data": {"is_prescribed": is_prescribed},
    }


def _candidate(irwin_id, *, lat=_LAT0, lon=_LON0):
    return {"irwin_id": irwin_id, "lat": lat, "lon": lon}


# ---------- match_fires: radius ---------------------------------------------


def test_match_within_radius_matches():
    wd = [_wd_event("wd1", lat=_LAT_2_8KM, lng=_LON0)]
    cands = [_candidate("irwin1")]
    pairs = match_fires(wd, cands, _RADIUS_KM)
    assert pairs == [("irwin1", wd[0])]


def test_match_just_outside_radius_does_not_match():
    wd = [_wd_event("wd1", lat=_LAT_3_5KM, lng=_LON0)]
    cands = [_candidate("irwin1")]
    pairs = match_fires(wd, cands, _RADIUS_KM)
    assert pairs == []


# ---------- match_fires: one-to-one -----------------------------------------


def test_match_one_to_one_nearer_fire_wins():
    wd = [_wd_event("wd1", lat=_LAT0, lng=_LON0)]
    near = _candidate("irwin_near", lat=_LAT0 + 0.001, lon=_LON0)   # very close
    far = _candidate("irwin_far", lat=_LAT_2_8KM, lon=_LON0)        # ~2.8 km away
    pairs = match_fires(wd, [far, near], _RADIUS_KM)
    assert pairs == [("irwin_near", wd[0])]


def test_match_claimed_wd_id_never_reassigned():
    wd = [_wd_event("wd1", lat=_LAT0, lng=_LON0)]
    cands = [_candidate("irwin1")]
    pairs = match_fires(wd, cands, _RADIUS_KM, claimed_ids={"wd1"})
    assert pairs == []


def test_match_claimed_wd_id_excludes_all_nearby_candidates():
    # Two candidates near the same already-claimed WD event: neither matches.
    wd = [_wd_event("wd1", lat=_LAT0, lng=_LON0)]
    cands = [_candidate("irwin1"), _candidate("irwin2", lat=_LAT0 + 0.0001, lon=_LON0)]
    pairs = match_fires(wd, cands, _RADIUS_KM, claimed_ids={"wd1"})
    assert pairs == []


# ---------- match_fires: eligibility ----------------------------------------


def test_match_prescribed_never_matches():
    wd = [_wd_event("wd1", is_prescribed=True)]
    cands = [_candidate("irwin1")]
    assert match_fires(wd, cands, _RADIUS_KM) == []


def test_match_inactive_never_matches():
    wd = [_wd_event("wd1", is_active=False)]
    cands = [_candidate("irwin1")]
    assert match_fires(wd, cands, _RADIUS_KM) == []


def test_match_string_lat_lng_parsed():
    wd = [_wd_event("wd1", lat=str(_LAT0), lng=str(_LON0))]
    cands = [_candidate("irwin1")]
    pairs = match_fires(wd, cands, _RADIUS_KM)
    assert pairs == [("irwin1", wd[0])]


def test_match_unparseable_lat_lng_never_matches():
    wd = [_wd_event("wd1", lat="not-a-number", lng=_LON0)]
    cands = [_candidate("irwin1")]
    assert match_fires(wd, cands, _RADIUS_KM) == []


# ---------- candidate fires DB query ----------------------------------------


def _insert_fire(conn, irwin_id, *, lat=_LAT0, lon=_LON0,
                  last_broadcast_at=None, tombstoned_at=None,
                  watchduty_event_id=None):
    now = int(time.time())
    conn.execute(
        "INSERT INTO fires(irwin_id, incident_name, current_acres, "
        "current_contained_pct, lat, lon, county, state, declared_at, "
        "last_event_at, last_broadcast_at, tombstoned_at, watchduty_event_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (irwin_id, f"Fire {irwin_id}", 100.0, 0, lat, lon, "Boise", "ID",
         now, now, last_broadcast_at, tombstoned_at, watchduty_event_id),
    )


@pytest.fixture
def adapter():
    return WatchDutyAdapter(WatchDutyConfig(enabled=True))


def test_candidate_excludes_stale_last_broadcast_even_when_close(adapter):
    """The real 'Stinson Creek' near-miss: geometrically within radius
    (2.8 km) but last_broadcast_at is outside the recency window ->
    excluded from candidates entirely."""
    conn = get_db()
    now = time.time()
    stale = now - 1_000_000  # older than the default 604800s recency window
    _insert_fire(conn, "irwin_stinson", lat=_LAT_2_8KM, lon=_LON0,
                 last_broadcast_at=stale)
    cands = adapter._candidate_fires(conn, recency_window_seconds=604800)
    assert [c["irwin_id"] for c in cands] == []


def test_candidate_includes_recent_broadcast_same_geometry(adapter):
    conn = get_db()
    now = time.time()
    recent = now - 3600
    _insert_fire(conn, "irwin_stinson2", lat=_LAT_2_8KM, lon=_LON0,
                 last_broadcast_at=recent)
    cands = adapter._candidate_fires(conn, recency_window_seconds=604800)
    ids = [c["irwin_id"] for c in cands]
    assert "irwin_stinson2" in ids
    # And it actually matches a WD event at the 3 km radius via match_fires.
    wd = [_wd_event("wd_stinson")]
    pairs = match_fires(wd, cands, _RADIUS_KM)
    assert ("irwin_stinson2", wd[0]) in pairs


def test_candidate_excludes_tombstoned(adapter):
    conn = get_db()
    now = time.time()
    _insert_fire(conn, "irwin_closed", last_broadcast_at=now - 10,
                 tombstoned_at=now - 5)
    cands = adapter._candidate_fires(conn, recency_window_seconds=604800)
    assert [c["irwin_id"] for c in cands] == []


def test_candidate_excludes_already_matched(adapter):
    conn = get_db()
    now = time.time()
    _insert_fire(conn, "irwin_matched", last_broadcast_at=now - 10,
                 watchduty_event_id="wd_already")
    cands = adapter._candidate_fires(conn, recency_window_seconds=604800)
    assert [c["irwin_id"] for c in cands] == []


def test_candidate_excludes_never_broadcast(adapter):
    conn = get_db()
    _insert_fire(conn, "irwin_never", last_broadcast_at=None)
    cands = adapter._candidate_fires(conn, recency_window_seconds=604800)
    assert [c["irwin_id"] for c in cands] == []


def test_candidate_irwin_id_mode_ignores_null_last_broadcast_at(adapter):
    """A brand-new fire is INSERTed with last_broadcast_at=NULL (it's only
    set later by the decider's deferred commit, after the broadcast is
    delivered). The immediate post-emit lookup (irwin_id given) must still
    treat it as a candidate; batch mode (irwin_id=None) must not."""
    conn = get_db()
    _insert_fire(conn, "irwin_brand_new", last_broadcast_at=None)

    single = adapter._candidate_fires(
        conn, recency_window_seconds=604800, irwin_id="irwin_brand_new")
    assert [c["irwin_id"] for c in single] == ["irwin_brand_new"]

    batch = adapter._candidate_fires(conn, recency_window_seconds=604800)
    assert [c["irwin_id"] for c in batch] == []


def test_match_and_store_matches_brand_new_fire_with_null_last_broadcast(adapter, monkeypatch):
    """End-to-end: match_and_store(irwin_id=...) actually matches a fire
    whose last_broadcast_at is still NULL (the immediate post-emit path)."""
    conn = get_db()
    _insert_fire(conn, "irwin_brand_new2", last_broadcast_at=None)
    wd_events = [_wd_event("wd_brand_new")]
    monkeypatch.setattr(adapter, "fetch_geo_events", lambda: wd_events)

    new_count = adapter.match_and_store(irwin_id="irwin_brand_new2")
    assert new_count == 1
    row = conn.execute(
        "SELECT watchduty_event_id FROM fires WHERE irwin_id=?",
        ("irwin_brand_new2",),
    ).fetchone()
    assert row["watchduty_event_id"] == "wd_brand_new"
