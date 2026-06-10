"""v0.5.7-traffic: NATS pattern fix + itd_511 sub-adapter routing + categories audit.

Covers four things shipped in v0.5.7-traffic:

1. NATS pattern syntax — `>` is legal only at the tail. Pre-v0.5.7-traffic
   we shipped `central.traffic.>.<state>` (mid-subject `>`), invalid per
   NATS rules. Now: `central.traffic.*.<state>` (Convention B, bare state)
   for traffic; roads511 dual-subscribes both Convention B and
   `central.traffic.*.us.<state>` (Convention A, itd_511 form).
2. roads511 dual subscription — owns both shared bare-state and us.<state>
   subjects so itd_511 events route to the roads511 source in meshai.
3. CENTRAL_ADAPTER_TO_SOURCE['itd_511'] == 'roads511'.
4. ALERT_CATEGORIES roads-family parity — every category we can emit
   (native + central path post-map_category) has a registry entry.
"""

import inspect

import pytest

from meshai.central.consumer import (
    CENTRAL_ADAPTER_TO_SOURCE,
    _SUBJECTS_BARE,
    _subjects_for,
    map_category,
)
from meshai.notifications.categories import ALERT_CATEGORIES


# ---------- NATS pattern validation (Convention A / B) ---------------------


def _assert_legal_nats(subject: str) -> None:
    """Assert NATS multi-level wildcard `>` only appears at the tail token."""
    tokens = subject.split(".")
    if ">" in tokens:
        assert tokens[-1] == ">", f"`>` not at tail in {subject!r}"
        assert tokens.count(">") == 1, f"multiple `>` in {subject!r}"
    for tok in tokens:
        # `*` and `>` are wildcards; everything else must be a non-empty
        # token without further wildcard characters mixed in.
        assert tok, f"empty token in {subject!r}"
        if tok not in {"*", ">"}:
            assert "*" not in tok and ">" not in tok, f"mixed wildcard in token {tok!r}"


def test_subjects_for_traffic_uses_convention_b():
    """traffic adapter -> bare-state Convention B; no `>` anywhere."""
    subs = _subjects_for("traffic", "us.id")
    assert subs == ["central.traffic.*.id"]
    for s in subs:
        _assert_legal_nats(s)
        assert ">" not in s, f"`>` in {s!r}"


def test_subjects_for_roads511_dual_subscribes():
    """roads511 owns bare-state (shared with traffic) AND us.<state> (itd_511)."""
    subs = _subjects_for("roads511", "us.id")
    assert subs == ["central.traffic.*.id", "central.traffic.*.us.id"]
    for s in subs:
        _assert_legal_nats(s)
        assert ">" not in s, f"`>` in {s!r}"


def test_traffic_and_roads511_share_convention_b_subject():
    """The bare-state subject is shared so sub-adapter routing kicks in."""
    traffic_subs = set(_subjects_for("traffic", "us.id"))
    roads511_subs = set(_subjects_for("roads511", "us.id"))
    shared = traffic_subs & roads511_subs
    assert shared == {"central.traffic.*.id"}


def test_no_invalid_mid_subject_wildcards_in_traffic_family():
    """Sanity sweep, scoped to this phase: traffic + roads511 region-aware
    subjects are NATS-legal (no `>` mid-subject). Other adapters (firms,
    usgs, usgs_quake, fires, nws) carry the v0.5.4 mid-`>` patterns and
    are intentionally OUT OF SCOPE for v0.5.7-traffic -- they'll be fixed
    per-family later in the v0.5.7 campaign."""
    for adapter in ("traffic", "roads511"):
        for s in _subjects_for(adapter, "us.id"):
            _assert_legal_nats(s)
            assert ">" not in s, f"`>` still present in {adapter} subject {s!r}"


def test_bare_form_unchanged_when_region_empty():
    """Empty region returns _SUBJECTS_BARE for backward compat."""
    assert _subjects_for("traffic", "") == ["central.traffic.>"]
    assert _subjects_for("roads511", None) == ["central.traffic.>"]


# ---------- itd_511 -> roads511 remap --------------------------------------


def test_itd_511_remaps_to_roads511():
    assert CENTRAL_ADAPTER_TO_SOURCE.get("itd_511") == "roads511"


def test_state_511_atis_still_remaps_to_roads511():
    """v0.5.3 mapping must survive the v0.5.7-traffic edit."""
    assert CENTRAL_ADAPTER_TO_SOURCE.get("state_511_atis") == "roads511"


# ---------- map_category preserves event_type distinctions -----------------


@pytest.mark.parametrize("central_cat,expected", [
    ("work_zone.wzdx", "work_zone"),
    ("work_zone", "work_zone"),
    ("incident.tomtom_incidents", "road_incident"),
    ("incident", "road_incident"),
    ("closure.itd_511", "road_closure"),
    ("closure", "road_closure"),
    # The catchall still flattens unknown traffic.* shapes.
    ("traffic.unknown_thing", "traffic_congestion"),
])
def test_map_category_traffic_event_types(central_cat, expected):
    assert map_category(central_cat) == expected


# ---------- ALERT_CATEGORIES roads-family parity ---------------------------


def _native_emitted_roads_categories() -> set[str]:
    """Walk traffic.py and roads511.py for category= literals."""
    import re
    from meshai.env import traffic as traffic_mod
    from meshai.env import roads511 as roads511_mod
    emitted: set[str] = set()
    for mod in (traffic_mod, roads511_mod):
        src = inspect.getsource(mod)
        emitted |= set(re.findall(r'category="([a-z_]+)"', src))
    return emitted


def _central_path_roads_categories() -> set[str]:
    """Categories the central path can deliver into the roads family.

    Drives off map_category() so the test breaks if the routing changes.
    """
    central_inputs = [
        "work_zone.wzdx",
        "incident.tomtom_incidents",
        "closure.itd_511",
        "closure",
        "incident",
        "traffic.flow_slow",
    ]
    return {map_category(c) for c in central_inputs}


def test_alert_categories_roads_complete():
    """Every category emitted by native traffic/roads511 OR delivered via
    the central path (post-map_category) must have an ALERT_CATEGORIES
    entry with toggle='roads'. No orphans.
    """
    registry_roads = {
        cid for cid, info in ALERT_CATEGORIES.items()
        if info.get("toggle") == "roads"
    }
    emitted = _native_emitted_roads_categories() | _central_path_roads_categories()
    missing = emitted - registry_roads
    orphans = registry_roads - emitted
    assert not missing, f"emit set has roads categories missing from ALERT_CATEGORIES: {missing}"
    assert not orphans, f"ALERT_CATEGORIES has orphan roads entries: {orphans}"


@pytest.mark.parametrize(
    "cat",
    ["road_closure", "traffic_congestion", "work_zone", "road_incident"],
)
def test_roads_categories_have_required_fields(cat):
    info = ALERT_CATEGORIES[cat]
    assert info["toggle"] == "roads"
    assert info["name"]
    assert info["description"]
    assert info["default_severity"] in {"routine", "priority", "immediate"}
    assert info["example_message"]
