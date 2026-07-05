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

from meshai.env.store import EnvironmentalStore
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
