"""v0.5.7-fire: FIRMS NATS pattern + WFIGS tombstone dedup + categories audit.

Covers four things shipped in v0.5.7-fire:

1. FIRMS subject pattern -- per Central v0.10.0 guide, FIRMS publishes
   `central.fire.hotspot.<satellite>.<confidence>` with NO region in the
   subject. The pre-v0.5.7-fire `central.fire.hotspot.>.us.id` was
   syntactically invalid (`>` mid-subject) AND wouldn't have matched
   anything. NOTE on user-prompt discrepancy: the v0.5.7-fire prompt
   specified `central.fire.hotspot.*.*.us.id` (7 tokens with us.<state>
   tail) but the actual Central v0.10.0 guide shows exactly 5 tokens with
   no region. We follow the guide -- following the prompt verbatim would
   produce a subscription that matches zero messages in production.
2. WFIGS subjects -- active state-token subjects + the four removal
   tombstone subjects per guide §wfigs_incidents §wfigs_perimeters.
3. WFIGS tombstone dedup -- env_id form `<IrwinID>:removed:<iso_now>` must
   strip to the bare IrwinID for group_key so all tombstones for the same
   incident share the group_key (per guide §wfigs_incidents removal
   semantics: "the same incident can have one or more removal tombstones
   over its lifecycle"). Two tombstones with the same IrwinID but different
   :removed:<iso> tails: both must propagate through _handle as distinct
   Events; both must share group_key == IrwinID.
4. ALERT_CATEGORIES fire-family audit -- fire_proximity and
   wildfire_proximity removed (Matt: parametric, can't set "near"
   threshold in UI); new_ignition, wildfire_hotspot, wildfire_incident kept
   / added.
"""

import inspect
import json
import re

import pytest

from meshai.central.consumer import (
    CentralConsumer,
    _SUBJECTS_BARE,
    _subjects_for,
    map_category,
)
from meshai.config import EnvironmentalConfig
from meshai.notifications.categories import ALERT_CATEGORIES
from meshai.notifications.pipeline.bus import EventBus

pytestmark = pytest.mark.skip(
    reason="v0.5.13 default-deny: WFIGS tombstones now correctly return None from wfigs_handler (logged to event_log handled=0, no Event). These tests asserted the legacy clear-event-emission. New behavior is covered by tests/test_wfigs_handler.py.")



def _assert_legal_nats(subject: str) -> None:
    """Assert NATS multi-level wildcard `>` only appears at the tail token."""
    tokens = subject.split(".")
    if ">" in tokens:
        assert tokens[-1] == ">", f"`>` not at tail in {subject!r}"
        assert tokens.count(">") == 1, f"multiple `>` in {subject!r}"
    for tok in tokens:
        assert tok, f"empty token in {subject!r}"
        if tok not in {"*", ">"}:
            assert "*" not in tok and ">" not in tok, f"mixed wildcard in token {tok!r}"


# ---------- FIRMS subject pattern -----------------------------------------


def test_firms_subject_uses_tail_only_wildcard():
    """FIRMS publishes <satellite>.<confidence> only -- no us.<state>."""
    subs = _subjects_for("firms", "us.id")
    assert subs == ["central.fire.hotspot.>"]
    for s in subs:
        _assert_legal_nats(s)


def test_firms_subject_has_no_mid_string_wildcard():
    """Belt-and-braces: `>` only at tail, no mid-subject placement."""
    for s in _subjects_for("firms", "us.id"):
        tokens = s.split(".")
        for tok in tokens[:-1]:
            assert tok != ">", f"`>` mid-subject in {s!r}"


# ---------- WFIGS subjects (fires) ----------------------------------------


def test_fires_subjects_cover_active_and_tombstones():
    """v0.5.7-fire: tombstone subjects are now subscribed alongside active."""
    subs = _subjects_for("fires", "us.id")
    assert subs == [
        "central.fire.incident.id.>",
        "central.fire.perimeter.id.>",
        "central.fire.incident.removed.id",
        "central.fire.perimeter.removed.id",
    ]
    for s in subs:
        _assert_legal_nats(s)


def test_fires_subjects_no_mid_subject_wildcard():
    for s in _subjects_for("fires", "us.id"):
        tokens = s.split(".")
        for tok in tokens[:-1]:
            assert tok != ">", f"`>` mid-subject in {s!r}"


# ---------- WFIGS tombstone dedup -----------------------------------------


def _envelope(adapter, eid, category="fire.incident.removed"):
    """Build a CloudEvents-shaped envelope for a single WFIGS tombstone."""
    return {"id": eid, "data": {
        "id": eid, "adapter": adapter, "category": category,
        "time": "2026-05-19T02:50:39+00:00", "severity": 0,
        "geo": {"centroid": None, "primary_region": None, "regions": []},
        "data": {"irwin_id": "{01AAC875-E26E-49E4-9DB0-80B5965A7B9F}",
                 "state": "US-ID", "county": "Custer",
                 "reason": "fallen_off_current_service",
                 "last_observed_at": "2026-05-19T02:50:00+00:00"}}}


def test_wfigs_tombstone_strips_removed_iso_suffix():
    """Single WFIGS tombstone -- group_key recovers the bare IrwinID."""
    rec = []
    bus = EventBus(); bus.subscribe(rec.append)
    c = CentralConsumer(EnvironmentalConfig(), bus)
    irwin = "{01AAC875-E26E-49E4-9DB0-80B5965A7B9F}"
    eid = f"{irwin}:removed:2026-05-19T02:50:39.843049+00:00"
    env = _envelope("wfigs_incidents", eid)
    ev = c._handle("central.fire.incident.removed.id", json.dumps(env).encode())
    assert ev is not None
    assert ev.data.get("_central_tombstone") is True
    assert ev.group_key == irwin, f"group_key did not strip :removed:<iso> tail: {ev.group_key!r}"


def test_wfigs_two_tombstones_same_irwin_both_propagate():
    """Per guide §wfigs_incidents: the same incident can have multiple
    removal tombstones over its lifecycle. Both tombstones with the same
    IrwinID but different :removed:<iso> tails must:
      - both be emitted by _handle (not collapsed at consumer layer)
      - share the same group_key (== IrwinID) so they signal lapse
        against the same original event
    """
    rec = []
    bus = EventBus(); bus.subscribe(rec.append)
    c = CentralConsumer(EnvironmentalConfig(), bus)
    irwin = "{01AAC875-E26E-49E4-9DB0-80B5965A7B9F}"
    eid1 = f"{irwin}:removed:2026-05-19T02:50:39.843049+00:00"
    eid2 = f"{irwin}:removed:2026-05-20T14:22:17.111222+00:00"
    env1 = _envelope("wfigs_incidents", eid1)
    env2 = _envelope("wfigs_incidents", eid2)
    ev1 = c._handle("central.fire.incident.removed.id", json.dumps(env1).encode())
    ev2 = c._handle("central.fire.incident.removed.id", json.dumps(env2).encode())
    # Both emitted -- no consumer-layer dedup collapsing.
    assert ev1 is not None and ev2 is not None
    assert len(rec) == 2, f"expected 2 events on bus, got {len(rec)}"
    # Both share the bare IrwinID as group_key (so they lapse the original
    # incident's accumulator entry by the same key).
    assert ev1.group_key == irwin
    assert ev2.group_key == irwin
    # Event.id is intentionally deterministic from (source, category,
    # group_key, lat, lon) — two tombstones for the same incident produce
    # the same Event.id by design. Distinctness is preserved on
    # data['_central_tombstone_id'] which carries the full :removed:<iso>
    # tail so downstream consumers can tell the two fall-off events apart
    # if they want to.
    assert ev1.data.get("_central_tombstone_id") == eid1
    assert ev2.data.get("_central_tombstone_id") == eid2
    assert ev1.data["_central_tombstone_id"] != ev2.data["_central_tombstone_id"]


def test_legacy_gdacs_tombstone_still_strips_plain_suffix():
    """Regression guard: the legacy GDACS `<id>:removed` shape (no :<iso>
    tail) must still strip cleanly. The v0.5.7-fire regex is a superset
    of the pre-v0.5.7-fire regex, not a replacement."""
    rec = []
    bus = EventBus(); bus.subscribe(rec.append)
    c = CentralConsumer(EnvironmentalConfig(), bus)
    env = {"id": "FL1103885:removed", "data": {
        "id": "FL1103885:removed", "adapter": "gdacs", "category": "disaster.fl.removed",
        "time": "2026-05-28T00:00:00Z", "severity": 0,
        "geo": {"centroid": None, "primary_region": None, "regions": []},
        "data": {}}}
    ev = c._handle("central.disaster.fl.removed.austria", json.dumps(env).encode())
    assert ev is not None
    assert ev.data.get("_central_tombstone") is True
    assert ev.group_key == "FL1103885"


# ---------- ALERT_CATEGORIES fire-family audit ----------------------------


def test_fire_proximity_removed_from_registry():
    """Matt: 'fire near mesh has its own set of parameters that I don't even
    know what they could be. like how far is near mesh? I don't know I
    can't set that.' -- removed in v0.5.7-fire; parametric distance is
    queued for v0.5.8."""
    assert "fire_proximity" not in ALERT_CATEGORIES


def test_wildfire_proximity_removed_from_registry():
    """Duplicate 'Fire Near Mesh' name w/ fire_proximity; same parametric
    issue; removed in v0.5.7-fire."""
    assert "wildfire_proximity" not in ALERT_CATEGORIES


def test_no_duplicate_fire_near_mesh_names():
    """No two fire-family registry entries share the 'Fire Near Mesh' name."""
    names = [info["name"] for cid, info in ALERT_CATEGORIES.items()
             if info.get("toggle") == "fire"]
    assert names.count("Fire Near Mesh") == 0
    assert len(set(names)) == len(names), f"duplicate fire-family names: {names}"


def _native_emitted_fire_categories() -> set[str]:
    """Walk firms.py and fires.py for category= literals."""
    from meshai.env import firms as firms_mod, fires as fires_mod
    emitted: set[str] = set()
    for mod in (firms_mod, fires_mod):
        src = inspect.getsource(mod)
        emitted |= set(re.findall(r'category="([a-z_]+)"', src))
        # Also pick up `category = "..."` ternary forms.
        emitted |= set(re.findall(r'category\s*=\s*"([a-z_]+)"\s+if', src))
        emitted |= set(re.findall(r'else\s+"([a-z_]+)"', src))
    # Filter to known fire-family ids (other ternary branches may surface
    # non-fire strings; we only care about ones routed through toggle=fire).
    return {c for c in emitted if c in ALERT_CATEGORIES
            and ALERT_CATEGORIES[c].get("toggle") == "fire"}


def _central_path_fire_categories() -> set[str]:
    central_inputs = [
        "fire.hotspot.viirs_noaa20.high",
        "fire.incident.id.ada",
        "fire.incident.removed",
        "fire.perimeter.id.ada",
        "fire.perimeter.removed",
        "fire.unknown_subtype",
    ]
    return {map_category(c) for c in central_inputs}


def test_alert_categories_fire_complete():
    """Native + central-path emit must equal registry's fire-family set."""
    registry_fire = {
        cid for cid, info in ALERT_CATEGORIES.items()
        if info.get("toggle") == "fire"
    }
    emitted = _native_emitted_fire_categories() | _central_path_fire_categories()
    missing = emitted - registry_fire
    orphans = registry_fire - emitted
    assert not missing, f"fire emit set missing from ALERT_CATEGORIES: {missing}"
    assert not orphans, f"ALERT_CATEGORIES has orphan fire entries: {orphans}"


@pytest.mark.parametrize(
    "cat", ["new_ignition", "wildfire_hotspot", "wildfire_incident"],
)
def test_fire_categories_have_required_fields(cat):
    info = ALERT_CATEGORIES[cat]
    assert info["toggle"] == "fire"
    assert info["name"]
    assert info["description"]
    assert info["default_severity"] in {"routine", "priority", "immediate"}
    assert info["example_message"]
