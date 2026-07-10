"""Persistence tests for the dedicated native WZDx ingest path.

The daily summary (wzdx_summary.fire_once) and DM detail
(env_reporter.build_work_zones_detail) both read the CURRENT active work-zone
set straight from traffic_events:

    SELECT ... FROM traffic_events
    WHERE source='wzdx' AND (end_at IS NULL OR end_at >= now)

Before the fix, native wzdx rode the generic ``_delta_emit`` path, which
silent-seeds every zone on the cold-start first poll and ``return``s BEFORE the
incident decider's ``INSERT INTO traffic_events`` runs -- so the current
coalesced set never persisted and the summary counted ~0. ``store._ingest_wzdx``
now UPSERTS the current coalesced set into traffic_events every poll
(persist-only, never emit/decide/broadcast) and reconciles removals, so the
table always equals the current active set the summary reads.

These tests drive the REAL EnvironmentalStore + EventBus with a fake WZDx
adapter whose per-poll coalesced set we control, then assert directly against
traffic_events AND against the bus (nothing must ever be dispatched).
"""
from __future__ import annotations

from meshai.env.store import EnvironmentalStore
from meshai.config import EnvironmentalConfig
from meshai.notifications.pipeline.bus import EventBus
from meshai.notifications.events import make_event
from meshai.persistence import get_db


class _FakeWZDx:
    """Native-WZDx stand-in whose coalesced current set the test controls.

    Its stored-event dicts carry the SAME shape env/wzdx.py emits: a coalesced
    ``external_id`` (``wzdx_<road>|<lat>|<lon>|<sub_type>``), ``lat``/``lon``,
    ``start_at``/``end_at`` epochs, and a ``normalized`` dict (the
    _parse_wzdx_federal output: road/direction/mile_start/mile_end/sub_type/
    impact). ``get_events()`` returns exactly the current coalesced set --
    which is what ``_ingest_wzdx`` upserts + reconciles against.
    """

    def __init__(self):
        self._batch: list[dict] = []
        self.emitted = False  # set True if to_event() is ever called

    def set_zones(self, zones: list[dict]) -> None:
        """zones: list of {ext, road, lat, lon, sub_type, impact, end_at,
        start_at?, direction?} -> stored-event dicts like env/wzdx builds."""
        batch = []
        for z in zones:
            ext = z["ext"]
            n = {
                "road": z.get("road"),
                "direction": z.get("direction"),
                "mile_start": z.get("mile_start"),
                "mile_end": z.get("mile_end"),
                "sub_type": z.get("sub_type"),
                "impact": z.get("impact"),
            }
            batch.append({
                "source": "wzdx",
                "event_id": ext,
                "external_id": ext,
                "lat": z.get("lat"),
                "lon": z.get("lon"),
                "start_at": z.get("start_at"),
                "end_at": z.get("end_at"),
                "severity": "priority" if z.get("impact") == "full_closure" else "routine",
                "normalized": n,
                "fetched_at": 0,
            })
        self._batch = batch

    def set_raw(self, batch: list[dict]) -> None:
        """Set the raw stored-event batch verbatim (edge cases)."""
        self._batch = batch

    def tick(self) -> bool:
        # Real wzdx.tick() returns True even on a 0-event (registry-only) tick.
        return True

    def get_events(self) -> list:
        return list(self._batch)

    def to_event(self, raw_evt: dict):
        # If _ingest_wzdx ever emitted, it would call this. It must NOT.
        self.emitted = True
        return make_event(source="wzdx", category="work_zone",
                          severity="routine", title=raw_evt.get("external_id"),
                          summary=raw_evt.get("external_id"),
                          group_key=raw_evt.get("external_id"))


def _build_store(adapter):
    """Construct a store (runs the durable pre-seed against the fresh migrated
    DB the conftest points MESHAI_DB_PATH at), then inject the fake adapter.
    Captures everything that reaches the EventBus so we can assert silence."""
    bus = EventBus()
    captured: list = []
    bus.subscribe(lambda e: captured.append(e))
    store = EnvironmentalStore(EnvironmentalConfig(), event_bus=bus)
    store._adapters["wzdx"] = adapter
    return store, captured


def _wzdx_rows():
    conn = get_db()
    return conn.execute(
        "SELECT external_id, source, road, sub_type, impact, lat, lon, "
        "end_at, first_seen_at, last_seen_at, last_broadcast_at "
        "FROM traffic_events WHERE source='wzdx' "
        "ORDER BY external_id"
    ).fetchall()


def _summary_visible_count(now: int):
    """Count exactly what wzdx_summary.fire_once / build_work_zones_detail see:
    source='wzdx' AND not-expired."""
    conn = get_db()
    return conn.execute(
        "SELECT COUNT(*) FROM traffic_events "
        "WHERE source='wzdx' AND (end_at IS NULL OR end_at >= ?)",
        (now,),
    ).fetchone()[0]


ZONES3 = [
    {"ext": "wzdx_US-20|43.600|-116.200|construction work",
     "road": "US-20", "lat": 43.6, "lon": -116.2,
     "sub_type": "construction work", "impact": "partial",
     "end_at": None},
    {"ext": "wzdx_I-84|43.500|-116.400|construction work",
     "road": "I-84", "lat": 43.5, "lon": -116.4,
     "sub_type": "construction work", "impact": "full_closure",
     "end_at": 9_000_000_000},
    {"ext": "wzdx_ID-55|44.000|-116.000|maintenance",
     "road": "ID-55", "lat": 44.0, "lon": -116.0,
     "sub_type": "maintenance", "impact": "partial",
     "end_at": 9_000_000_000},
]


def test_first_poll_persists_current_set_and_broadcasts_nothing():
    # THE GAP FIX: the cold-start first poll must PERSIST every current
    # coalesced zone into traffic_events (source='wzdx') and emit NOTHING.
    adapter = _FakeWZDx()
    store, captured = _build_store(adapter)
    adapter.set_zones(ZONES3)

    store.refresh()  # first (cold-start) poll

    rows = _wzdx_rows()
    exts = {r["external_id"] for r in rows}
    assert exts == {z["ext"] for z in ZONES3}, (
        "first poll must persist ALL current coalesced zones (the gap fix)")
    # No broadcast / no emit occurred.
    assert captured == [], "persist-only: NOTHING may reach the pipeline bus"
    assert adapter.emitted is False, "to_event()/emit must never be called"
    # Every row is source='wzdx' with last_broadcast_at NULL (never armed).
    for r in rows:
        assert r["source"] == "wzdx"
        assert r["last_broadcast_at"] is None
    # The summary/DM query now sees the real count, not ~0.
    assert _summary_visible_count(now=0) == 3


def test_columns_match_summary_and_dm_queries():
    # The persisted columns the summary (road/lat/lon/sub_type/impact/end_at)
    # and DM (road/direction/sub_type/impact/lat/lon/end_at) read must be
    # populated from the coalesced zone, not left NULL.
    adapter = _FakeWZDx()
    store, _ = _build_store(adapter)
    adapter.set_zones([ZONES3[1]])  # the full_closure I-84 zone
    store.refresh()

    r = _wzdx_rows()[0]
    assert r["road"] == "I-84"
    assert r["sub_type"] == "construction work"
    assert r["impact"] == "full_closure"
    assert abs(r["lat"] - 43.5) < 1e-9
    assert abs(r["lon"] - (-116.4)) < 1e-9
    assert r["end_at"] == 9_000_000_000


def test_subsequent_poll_reconciles_removed_zone():
    # A zone that DROPS OUT of the feed on a later poll must be REMOVED so the
    # table equals the CURRENT active set (else stale rows inflate the count).
    adapter = _FakeWZDx()
    store, captured = _build_store(adapter)
    adapter.set_zones(ZONES3)
    store.refresh()
    assert len(_wzdx_rows()) == 3

    # Next poll: US-20 dropped out; I-84 + ID-55 remain.
    adapter.set_zones([ZONES3[1], ZONES3[2]])
    store.refresh()

    exts = {r["external_id"] for r in _wzdx_rows()}
    assert exts == {ZONES3[1]["ext"], ZONES3[2]["ext"]}, (
        "the dropped zone must be reconciled out of traffic_events")
    assert captured == [], "reconcile is persist-only; still no broadcast"


def test_empty_or_failed_fetch_does_not_wipe_existing_rows():
    # An empty/failed fetch (get_events() == []) must NOT delete existing rows
    # -- a transient upstream outage must never zero out the summary's data.
    adapter = _FakeWZDx()
    store, _ = _build_store(adapter)
    adapter.set_zones(ZONES3)
    store.refresh()
    assert len(_wzdx_rows()) == 3

    adapter.set_raw([])          # empty/failed poll
    store.refresh()
    assert len(_wzdx_rows()) == 3, (
        "an empty fetch must NEVER wipe the existing active set")


def test_upsert_preserves_first_seen_at_and_refreshes_end_at():
    # A zone seen across polls keeps its first_seen_at but refreshes
    # last_seen_at + end_at (so the summary's not-expired filter tracks the
    # feed's latest end date).
    adapter = _FakeWZDx()
    store, _ = _build_store(adapter)

    z = dict(ZONES3[0]); z["end_at"] = 1000
    adapter.set_zones([z])
    store.refresh()
    r1 = _wzdx_rows()[0]
    first_seen = r1["first_seen_at"]
    assert r1["end_at"] == 1000

    # Same zone reappears with a LATER end_at.
    z2 = dict(ZONES3[0]); z2["end_at"] = 5000
    adapter.set_zones([z2])
    store.refresh()
    r2 = _wzdx_rows()[0]
    assert r2["first_seen_at"] == first_seen, "first_seen_at must be preserved"
    assert r2["end_at"] == 5000, "end_at must refresh from the feed"


def test_expiry_end_at_preserved_for_not_expired_filter():
    # end_at must be persisted verbatim so the summary's not-expired filter
    # (end_at IS NULL OR end_at >= now) correctly includes/excludes zones.
    now = 1_000_000
    adapter = _FakeWZDx()
    store, _ = _build_store(adapter)
    zones = [
        {"ext": "wzdx_open|1.0|1.0|x", "road": "OPEN", "lat": 1.0, "lon": 1.0,
         "sub_type": "x", "impact": "partial", "end_at": None},          # open-ended
        {"ext": "wzdx_future|2.0|2.0|x", "road": "FUT", "lat": 2.0, "lon": 2.0,
         "sub_type": "x", "impact": "partial", "end_at": now + 10_000},  # not expired
        {"ext": "wzdx_past|3.0|3.0|x", "road": "PAST", "lat": 3.0, "lon": 3.0,
         "sub_type": "x", "impact": "partial", "end_at": now - 10_000},  # expired
    ]
    adapter.set_zones(zones)
    store.refresh()

    # All 3 persisted (ingest does not itself drop expired rows) ...
    assert len(_wzdx_rows()) == 3
    # ... but the summary's not-expired filter counts only the open + future.
    assert _summary_visible_count(now=now) == 2


def test_id_less_zone_is_skipped_not_fatal():
    # A stored event with no external_id cannot be keyed; it is skipped (never
    # persisted, never raises), and does not participate in reconciliation.
    adapter = _FakeWZDx()
    store, _ = _build_store(adapter)
    good = ZONES3[0]
    adapter.set_zones([good])
    store.refresh()
    assert len(_wzdx_rows()) == 1

    # Poll with the good zone plus an id-less junk event.
    adapter.set_zones([good])
    junk = {"source": "wzdx", "event_id": None, "external_id": None,
            "lat": 5.0, "lon": 5.0, "normalized": {}, "fetched_at": 0}
    adapter._batch.append(junk)
    store.refresh()

    rows = _wzdx_rows()
    assert {r["external_id"] for r in rows} == {good["ext"]}, (
        "id-less zone skipped; good zone still persisted, reconcile intact")


def test_bulk_current_set_persists_all_like_the_real_127():
    # Scale check mirroring the real ~127-zone active set: every coalesced
    # zone in a large current set persists and is visible to the summary.
    adapter = _FakeWZDx()
    store, captured = _build_store(adapter)
    zones = [
        {"ext": f"wzdx_R{i}|{40.0 + i * 0.001:.3f}|-116.000|maintenance",
         "road": f"R{i}", "lat": 40.0 + i * 0.001, "lon": -116.0,
         "sub_type": "maintenance", "impact": "partial", "end_at": None}
        for i in range(127)
    ]
    adapter.set_zones(zones)
    store.refresh()

    assert len(_wzdx_rows()) == 127
    assert _summary_visible_count(now=0) == 127, (
        "the full active set is counted, not ~0")
    assert captured == [], "bulk persist still broadcasts nothing"
