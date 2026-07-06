"""Received-delta gate tests for the native EnvironmentalStore path.

The operator's required model: a native adapter broadcasts an item ONLY when
it was newly RECEIVED from the API this poll — never by scanning an accumulated
backlog. The store enforces this with an in-memory, per-adapter seen-set:

  * the FIRST data-bearing poll for an adapter records every current item key
    as "seen" and broadcasts NOTHING (that batch is pre-existing backlog);
  * every later poll broadcasts only items whose key is not already seen;
  * a fresh store (process restart) has empty sets, so its next poll is again a
    "first poll" that re-seeds silently — backlog is never re-broadcast.

These tests drive the real EnvironmentalStore + EventBus with a fake adapter
whose per-poll batch we control, and assert exactly which events reach the bus.
"""
from __future__ import annotations

from meshai.env.store import EnvironmentalStore, _key_ext
from meshai.config import EnvironmentalConfig
from meshai.notifications.pipeline.bus import EventBus
from meshai.notifications.events import make_event


class _FakeAdapter:
    """Native-adapter stand-in: returns a controllable batch of raw events.

    Raw events carry the (source, event_id) shape every native adapter emits,
    which is exactly what the store's seen-set keys on. Each poll's batch is
    set by the test via `set_batch`.
    """

    def __init__(self, source: str = "roads511"):
        self._source = source
        self._batch: list[dict] = []

    def set_batch(self, ids: list[str]) -> None:
        self._batch = [
            {"source": self._source, "event_id": eid, "fetched_at": 0}
            for eid in ids
        ]

    def tick(self) -> bool:
        # Data is "fetched" every poll; the store decides what is new.
        return True

    def get_events(self) -> list:
        return list(self._batch)

    def to_event(self, raw_evt: dict):
        eid = raw_evt["event_id"]
        return make_event(
            source=raw_evt["source"],
            category="test_delta",   # no decider registered -> emits directly
            severity="routine",
            title=eid,
            summary=eid,
            group_key=eid,
        )


def _make_store(adapter_name: str = "roads511"):
    """Build a store with NO real adapters, then inject one fake adapter."""
    bus = EventBus()
    captured: list = []
    bus.subscribe(lambda e: captured.append(e))

    # All feeds default disabled -> zero native adapters register.
    store = EnvironmentalStore(EnvironmentalConfig(), event_bus=bus)
    adapter = _FakeAdapter(source="511")
    store._adapters[adapter_name] = adapter
    return store, adapter, captured


def _emitted_ids(captured) -> list[str]:
    return [e.title for e in captured]


def test_first_poll_seeds_and_broadcasts_nothing():
    store, adapter, captured = _make_store()
    adapter.set_batch(["A", "B", "C"])

    store.refresh()  # poll 1 — the backlog

    assert captured == [], "first poll must broadcast NOTHING (backlog seed)"


def test_second_poll_emits_only_newly_received():
    store, adapter, captured = _make_store()

    adapter.set_batch(["A", "B", "C"])
    store.refresh()                       # poll 1: seed
    assert _emitted_ids(captured) == []

    adapter.set_batch(["A", "B", "C", "D"])
    store.refresh()                       # poll 2: only D is new
    assert _emitted_ids(captured) == ["D"]


def test_unchanged_poll_emits_nothing():
    store, adapter, captured = _make_store()

    adapter.set_batch(["A", "B", "C"])
    store.refresh()                       # poll 1: seed
    adapter.set_batch(["A", "B", "C", "D"])
    store.refresh()                       # poll 2: D
    adapter.set_batch(["A", "B", "C", "D"])
    store.refresh()                       # poll 3: nothing new

    assert _emitted_ids(captured) == ["D"], "poll 3 has no new items"


def test_restart_reseeds_and_never_rebroadcasts_backlog():
    # Process 1 sees A,B,C,D and broadcasts D.
    store1, adapter1, cap1 = _make_store()
    adapter1.set_batch(["A", "B", "C"])
    store1.refresh()
    adapter1.set_batch(["A", "B", "C", "D"])
    store1.refresh()
    assert _emitted_ids(cap1) == ["D"]

    # RESTART: a fresh store has an empty seen-set. The SAME backlog [A,B,C,D]
    # arriving on its first poll must be re-seeded silently, not re-broadcast.
    store2, adapter2, cap2 = _make_store()
    adapter2.set_batch(["A", "B", "C", "D"])
    store2.refresh()
    assert cap2 == [], "restart must NEVER re-broadcast the existing backlog"

    # And a genuinely new item after the restart still broadcasts once.
    adapter2.set_batch(["A", "B", "C", "D", "E"])
    store2.refresh()
    assert _emitted_ids(cap2) == ["E"]


def test_stable_key_prevents_reemit_when_batch_reorders():
    # The same real-world items in a different order are NOT "newly received".
    store, adapter, captured = _make_store()
    adapter.set_batch(["A", "B", "C"])
    store.refresh()                       # seed
    adapter.set_batch(["C", "A", "B"])    # reordered, same items
    store.refresh()
    assert captured == [], "reordering the same items emits nothing"


def test_disabled_for_days_then_backlog_is_not_broadcast():
    # Simulate an adapter that was off for days: its first poll after coming
    # back returns a large accumulated backlog. None of it may broadcast.
    store, adapter, captured = _make_store()
    backlog = [f"evt{i}" for i in range(200)]
    adapter.set_batch(backlog)
    store.refresh()                       # first poll after re-enable
    assert captured == [], "a days-old backlog is seeded silently, never sent"

    # Only a truly new arrival afterward is announced.
    adapter.set_batch(backlog + ["fresh"])
    store.refresh()
    assert _emitted_ids(captured) == ["fresh"]


# ═══════════════════════════════════════════════════════════════════════════
# Durable persistent pre-seed (the robust anti-leak fix)
#
# meshai already has a durable record of everything it has ever received: the
# persistent hazard tables. At startup the store loads their identifying keys
# into self._seen so nothing ever received can re-broadcast, immune to how the
# fetch is staged and immune to restarts. These tests use the REAL persistent
# DB (conftest points MESHAI_DB_PATH at a fresh migrated tmp file per test).
# ═══════════════════════════════════════════════════════════════════════════
from meshai.persistence import get_db


class _FakeWZDx:
    """Native-WZDx stand-in. Its raw events carry a stable ``external_id``
    (like the real adapter), so the seen-key is ``wzdx\x1eext:<id>`` — exactly
    what the durable pre-seed loads from traffic_events(source='wzdx')."""

    def __init__(self):
        self._batch: list[dict] = []

    def set_batch(self, ext_ids: list[str]) -> None:
        self._batch = [
            {"source": "wzdx", "event_id": f"wzdx_{x}",
             "external_id": x, "fetched_at": 0}
            for x in ext_ids
        ]

    def tick(self) -> bool:
        # Real wzdx.tick() returns True even on a 0-event (registry-only) tick.
        return True

    def get_events(self) -> list:
        return list(self._batch)

    def to_event(self, raw_evt: dict):
        eid = raw_evt["external_id"]
        return make_event(source="wzdx", category="test_delta",
                          severity="routine", title=eid, summary=eid,
                          group_key=eid)


class _FakeQuake:
    """Native usgs_quake stand-in. Raw events have NO external_id, so the
    seen-key falls to ``usgs_quake\x1eeid:<event_id>`` — exactly what the
    durable pre-seed loads from quake_events.event_id."""

    def __init__(self):
        self._batch: list[dict] = []

    def set_batch(self, event_ids: list[str]) -> None:
        self._batch = [
            {"source": "usgs_quake", "event_id": e, "fetched_at": 0}
            for e in event_ids
        ]

    def tick(self) -> bool:
        return True

    def get_events(self) -> list:
        return list(self._batch)

    def to_event(self, raw_evt: dict):
        eid = raw_evt["event_id"]
        return make_event(source="usgs_quake", category="test_delta",
                          severity="routine", title=eid, summary=eid,
                          group_key=eid)


def _insert_traffic(external_ids: list[str], source: str = "wzdx") -> None:
    conn = get_db()
    for x in external_ids:
        conn.execute(
            "INSERT OR IGNORE INTO traffic_events"
            "(source, external_id, first_seen_at, last_seen_at) "
            "VALUES (?,?,?,?)",
            (source, x, 0, 0),
        )


def _insert_quake(event_ids: list[str]) -> None:
    conn = get_db()
    for e in event_ids:
        conn.execute(
            "INSERT OR IGNORE INTO quake_events(event_id, first_seen_at) "
            "VALUES (?,?)",
            (e, 0),
        )


def _build_store(adapter_name: str, adapter):
    """Construct a store (runs the durable pre-seed against the current DB),
    then inject a fake adapter. Insert persistent rows BEFORE calling this."""
    bus = EventBus()
    captured: list = []
    bus.subscribe(lambda e: captured.append(e))
    store = EnvironmentalStore(EnvironmentalConfig(), event_bus=bus)
    store._adapters[adapter_name] = adapter
    return store, captured


def test_persistent_preseed_known_suppressed_new_emitted():
    # N durable rows in traffic_events(source=wzdx). A fresh store must treat
    # them as already-received: a poll of those same N broadcasts NOTHING; a
    # poll adding one external_id NOT in the table broadcasts only that one.
    known = [f"z{i}" for i in range(5)]
    _insert_traffic(known)

    adapter = _FakeWZDx()
    store, captured = _build_store("wzdx", adapter)

    # wzdx was pre-seeded from the durable table AND marked seeded (baseline).
    assert "wzdx" in store._seeded
    assert len(store._seen["wzdx"]) == 5

    adapter.set_batch(known)
    store.refresh()
    assert captured == [], "all 5 are durably-known → zero broadcast"

    adapter.set_batch(known + ["z_new"])
    store.refresh()
    assert _emitted_ids(captured) == ["z_new"], "only the not-in-table id broadcasts"


def test_persistent_preseed_cross_tick_staging_no_leak():
    # The scenario the DURABLE seed uniquely fixes: a source is marked seeded
    # on a partial tick, then a LATER tick brings a backlog item that was never
    # seen in-process. Without the durable record it would leak.
    _insert_traffic(["A", "B"])          # both already RECEIVED (durable)
    adapter = _FakeWZDx()
    store, captured = _build_store("wzdx", adapter)

    adapter.set_batch(["A"])
    store.refresh()                       # tick 1: only A present
    adapter.set_batch(["A", "B"])
    store.refresh()                       # tick 2: B appears (backlog)
    assert captured == [], "B is durably-known — must NOT leak on a later tick"

    # CONTROL: identical staging but NO durable rows → B leaks (proves the
    # durable seed is what prevents it; in-memory alone cannot).
    ctrl = _FakeWZDx()
    bus = EventBus(); cap2: list = []
    bus.subscribe(lambda e: cap2.append(e))
    # A separate source name so its 0-row durable seed doesn't mark it seeded.
    ctrl._batch = []
    store2 = EnvironmentalStore(EnvironmentalConfig(), event_bus=bus)
    store2._adapters["wzdx_ctrl"] = ctrl
    ctrl.set_batch(["A"])
    # Re-point ctrl events to a fresh source with no durable rows.
    for e in ctrl._batch:
        e["source"] = "wzdx_ctrl"
    store2.refresh()
    ctrl.set_batch(["A", "B"])
    for e in ctrl._batch:
        e["source"] = "wzdx_ctrl"
    store2.refresh()
    assert [e.title for e in cap2] == ["B"], "without a durable record, B leaks"


def test_incremental_empty_first_tick_then_only_new_broadcasts():
    # Task's incremental case: tick 1 yields [] (e.g. wzdx registry-only tick),
    # tick 2 yields [A,B,C] where A,B are durably-known and C is new.
    # The empty tick must not mark-and-leak; the durable seed catches A,B; only
    # C — genuinely never received — broadcasts.
    _insert_traffic(["A", "B"])
    adapter = _FakeWZDx()
    store, captured = _build_store("wzdx", adapter)

    adapter.set_batch([])                 # empty first tick
    store.refresh()
    assert captured == [], "empty tick emits nothing"

    adapter.set_batch(["A", "B", "C"])    # backlog A,B + new C
    store.refresh()
    assert _emitted_ids(captured) == ["C"], "only the never-received C broadcasts"


def test_restart_against_same_persistent_db_never_rebroadcasts():
    # A full received backlog is durable. Process 1 broadcasts nothing for it.
    # After a RESTART (fresh store, same DB) the backlog still never broadcasts.
    backlog = ["A", "B", "C", "D"]
    _insert_traffic(backlog)

    a1 = _FakeWZDx()
    store1, cap1 = _build_store("wzdx", a1)
    a1.set_batch(backlog)
    store1.refresh()
    assert cap1 == [], "process 1: durable backlog is silent"

    # RESTART: brand-new store, same persistent DB → pre-seed reloads.
    a2 = _FakeWZDx()
    store2, cap2 = _build_store("wzdx", a2)
    a2.set_batch(backlog)
    store2.refresh()
    assert cap2 == [], "restart must NEVER re-broadcast the durable backlog"

    a2.set_batch(backlog + ["E"])
    store2.refresh()
    assert _emitted_ids(cap2) == ["E"], "a genuinely-new item still broadcasts once"


def test_persistent_preseed_quake_by_event_id():
    # Durable seed for the event_id-keyed path (no external_id): quake_events.
    _insert_quake(["us1000aaaa", "us1000bbbb"])
    adapter = _FakeQuake()
    store, captured = _build_store("usgs_quake", adapter)

    assert "usgs_quake" in store._seeded
    assert len(store._seen["usgs_quake"]) == 2

    adapter.set_batch(["us1000aaaa", "us1000bbbb"])
    store.refresh()
    assert captured == [], "both quakes already received → zero broadcast"

    adapter.set_batch(["us1000aaaa", "us1000bbbb", "us1000cccc"])
    store.refresh()
    assert _emitted_ids(captured) == ["us1000cccc"], "only the new quake broadcasts"


def test_no_durable_rows_falls_back_to_silent_first_poll():
    # Fresh DB (0 durable rows): the source must NOT be pre-marked seeded, so
    # the first real batch is silently seeded (never leaked) — the fresh-start
    # safety property.
    adapter = _FakeWZDx()
    store, captured = _build_store("wzdx", adapter)
    assert "wzdx" not in store._seeded, "0 durable rows → not pre-marked seeded"

    adapter.set_batch(["A", "B"])
    store.refresh()
    assert captured == [], "first non-empty poll on a fresh DB is silent"

    adapter.set_batch(["A", "B", "C"])
    store.refresh()
    assert _emitted_ids(captured) == ["C"]


class _FakeRoads511:
    """Native roads511 stand-in. Raw events carry a stable external_id
    '511_{id}' EQUAL to their event_id (like the patched adapter), so the
    seen-key is '511\x1eext:511_{id}' — exactly what the durable pre-seed loads
    from traffic_events(source='511')."""

    def __init__(self):
        self._batch: list[dict] = []

    def set_batch(self, ext_ids: list[str]) -> None:
        self._batch = [
            {"source": "511", "event_id": x, "external_id": x, "fetched_at": 0}
            for x in ext_ids
        ]

    def tick(self) -> bool:
        return True

    def get_events(self) -> list:
        return list(self._batch)

    def to_event(self, raw_evt: dict):
        eid = raw_evt["external_id"]
        return make_event(source="511", category="test_delta",
                          severity="routine", title=eid, summary=eid,
                          group_key=eid)


def test_roads511_seen_key_matches_persistent_preseed_key():
    # CONSISTENCY PROOF: the SAME roads511 item must produce a byte-identical
    # self._seen key on both sides — the live emit path (_seen_key over the real
    # adapter's raw event) and the durable pre-seed (_key_ext built from the
    # persisted traffic_events row). If these drift the pre-seed silently does
    # nothing.
    from meshai.env.roads511 import Roads511Adapter

    # Build a raw event exactly as the adapter would from an ITD item.
    # _parse_event does not touch self, so __new__ (no network/init) is safe.
    adapter = Roads511Adapter.__new__(Roads511Adapter)
    raw = adapter._parse_event(
        {"id": "11165", "Latitude": 43.6, "Longitude": -116.2,
         "Description": "US-20 closed"},
        now=0.0,
    )
    assert raw["source"] == "511"
    assert raw["external_id"] == "511_11165"
    assert raw["event_id"] == "511_11165"

    # Live emit key (no bus needed for _seen_key).
    store = EnvironmentalStore(EnvironmentalConfig(), event_bus=None)
    live_key = store._seen_key(raw)

    # The incident decider persists traffic_events.external_id == raw external_id.
    persisted_external_id = raw["external_id"]
    preseed_key = _key_ext("511", persisted_external_id)

    assert live_key == preseed_key == "511\x1eext:511_11165", (
        f"live={live_key!r} preseed={preseed_key!r} must be identical"
    )


def test_persistent_preseed_roads511_by_external_id():
    # N durable rows in traffic_events(source='511'). A fresh store must treat
    # them as already-received: polling those same N broadcasts NOTHING; adding
    # one external_id NOT in the table broadcasts only that one.
    known = [f"511_{i}" for i in range(4)]
    _insert_traffic(known, source="511")

    adapter = _FakeRoads511()
    store, captured = _build_store("roads511", adapter)

    # roads511's SOURCE is '511'; pre-seed keys/marks by source, not adapter name.
    assert "511" in store._seeded
    assert len(store._seen["511"]) == 4

    adapter.set_batch(known)
    store.refresh()
    assert captured == [], "all 4 durably-known 511 rows → zero broadcast"

    adapter.set_batch(known + ["511_99"])
    store.refresh()
    assert _emitted_ids(captured) == ["511_99"], "only the not-in-table id broadcasts"
