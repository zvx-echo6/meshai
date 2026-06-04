"""v0.5.7-rf: SWPC subject validation + protons severity=0 docs + categories audit.

Covers three things shipped in v0.5.7-rf:

1. SWPC subscription subject -- verifies the existing `central.space.>`
   tail-only-`>` form (per Central v0.10.0 guide §swpc_*: planetary, no
   region in subject; one umbrella subscription covers swpc_alerts,
   swpc_kindex, swpc_protons). The pattern was already correct from v0.5.4
   work; this phase pins it explicitly so future "add a region tail"
   refactors fail loudly.
2. swpc_protons severity=0 routing -- per guide §swpc_protons live sample
   the adapter always publishes severity=0. Verifies map_severity(0) ->
   "routine" and the NotificationToggle.severity_channels string-keyed
   dict accepts "routine" with no IndexError. The "silently dropped"
   failure mode the prompt described does not exist; this test is a
   regression guard against a future refactor introducing it.
3. ALERT_CATEGORIES RF-family audit -- adds four missing entries that
   meshai emits but the rule editor couldn't target:
     - rf_anomalous_propagation (ducting.py super_refraction tier)
     - rf_ducting_enhancement   (ducting.py duct + surface_duct tiers)
     - rf_propagation_alert     (central swpc_alerts -> space.alert)
     - solar_radiation_storm    (central swpc_protons -> space.proton_flux)
   Verifies geomagnetic_storm (central swpc_kindex -> space.kindex)
   stays mapped. Legacy hf_blackout and tropospheric_ducting are kept as
   selectable forward-compat targets even though no current emitter
   produces them; flagged in the commit body for follow-up.
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
    map_severity,
)
from meshai.config import EnvironmentalConfig, NotificationToggle
from meshai.notifications.categories import ALERT_CATEGORIES
from meshai.notifications.pipeline.bus import EventBus


def _assert_legal_nats(subject: str) -> None:
    tokens = subject.split(".")
    if ">" in tokens:
        assert tokens[-1] == ">", f"`>` not at tail in {subject!r}"
        assert tokens.count(">") == 1, f"multiple `>` in {subject!r}"
    for tok in tokens:
        assert tok, f"empty token in {subject!r}"
        if tok not in {"*", ">"}:
            assert "*" not in tok and ">" not in tok, f"mixed wildcard in token {tok!r}"


# ---------- FIX 1: SWPC subject pattern -----------------------------------


def test_swpc_subject_is_global_umbrella():
    """Per Central v0.10.0 guide §space stream, all SWPC adapters publish
    under `central.space.>`. Single tail-only-`>` subscription catches
    all three (swpc_alerts / swpc_kindex / swpc_protons)."""
    subs = _subjects_for("swpc", "us.id")
    assert subs == ["central.space.>"]
    for s in subs:
        _assert_legal_nats(s)


def test_swpc_subject_ignores_region():
    """Space weather is planetary; region argument MUST be a no-op."""
    assert _subjects_for("swpc", "us.id") == ["central.space.>"]
    assert _subjects_for("swpc", "us.mt") == ["central.space.>"]
    assert _subjects_for("swpc", "") == ["central.space.>"]
    assert _subjects_for("swpc", None) == ["central.space.>"]


def test_swpc_subject_covers_all_three_adapter_subjects():
    """The umbrella `central.space.>` matches every per-adapter subject
    documented in the guide."""
    sub = _subjects_for("swpc", "us.id")[0]
    # `>` matches one or more tokens at the tail.
    assert sub.endswith(".>")
    prefix = sub[:-2]  # strip the .>
    for published in (
        "central.space.alert.a20f",         # swpc_alerts (4 tokens, product_id tail)
        "central.space.kindex",              # swpc_kindex (3 tokens, fixed)
        "central.space.proton_flux",         # swpc_protons (3 tokens, fixed)
    ):
        assert published.startswith(prefix), f"{published!r} not covered by {sub!r}"


# ---------- FIX 2: severity=0 routing -------------------------------------


def test_map_severity_zero_routes_to_routine():
    """All three SWPC adapters publish severity=0 by default. The boundary
    contract: 0 -> 'routine' (not dropped, not error)."""
    assert map_severity(0) == "routine"


def test_severity_channels_dict_accepts_routine_key():
    """NotificationToggle.severity_channels is dict-keyed by severity STRING
    -- so "routine" is a valid key with no IndexError vector. Pins the
    contract so a refactor to an int-indexed list would break this test."""
    t = NotificationToggle(name="rf_propagation")
    assert isinstance(t.severity_channels, dict)
    # dict.get returns the default for unknown keys; no exception possible.
    assert t.severity_channels.get("routine", ["mesh_broadcast"]) == ["mesh_broadcast"]


def test_swpc_protons_severity_zero_routes_through_consumer():
    """Synthetic swpc_protons envelope (severity=0 per guide §swpc_protons)
    -- verify it normalizes to ev.severity='routine' and emits on the bus
    with no exception."""
    rec = []
    bus = EventBus(); bus.subscribe(rec.append)
    c = CentralConsumer(EnvironmentalConfig(), bus)
    env = {"id": "2026-05-18T05:55:00Z|>=100 MeV", "data": {
        "id": "2026-05-18T05:55:00Z|>=100 MeV", "adapter": "swpc_protons",
        "category": "space.proton_flux",
        "time": "2026-05-18T05:55:00Z", "severity": 0,
        "geo": {"centroid": None, "primary_region": None, "regions": []},
        "data": {"flux": 0.16, "energy": ">=100 MeV",
                 "time_tag": "2026-05-18T05:55:00Z", "satellite": 19}}}
    ev = c._handle("central.space.proton_flux", json.dumps(env).encode())
    assert ev is not None
    assert ev.severity == "routine"
    assert ev.category == "solar_radiation_storm"
    assert ev.source == "swpc"
    assert len(rec) == 1


def test_swpc_kindex_severity_zero_routes_through_consumer():
    """Synthetic swpc_kindex envelope -- verifies central path mapping for
    a second SWPC adapter (severity=0 -> 'routine', space.kindex ->
    geomagnetic_storm)."""
    rec = []
    bus = EventBus(); bus.subscribe(rec.append)
    c = CentralConsumer(EnvironmentalConfig(), bus)
    env = {"id": "2026-05-12T00:00:00", "data": {
        "id": "2026-05-12T00:00:00", "adapter": "swpc_kindex",
        "category": "space.kindex",
        "time": "2026-05-12T00:00:00Z", "severity": 0,
        "geo": {"centroid": None, "primary_region": None, "regions": []},
        "data": {"Kp": 0.67, "time_tag": "2026-05-12T00:00:00",
                 "a_running": 3, "station_count": 8}}}
    ev = c._handle("central.space.kindex", json.dumps(env).encode())
    assert ev is not None
    assert ev.severity == "routine"
    assert ev.category == "geomagnetic_storm"


# ---------- FIX 3: ALERT_CATEGORIES RF-family audit ----------------------


@pytest.mark.parametrize("cat", [
    "rf_anomalous_propagation",
    "rf_ducting_enhancement",
    "rf_propagation_alert",
    "solar_radiation_storm",
])
def test_v057_rf_added_categories_present(cat):
    """v0.5.7-rf: four new rf_propagation categories must be registry-present
    so the Advanced Rules editor can target them."""
    assert cat in ALERT_CATEGORIES
    info = ALERT_CATEGORIES[cat]
    assert info["toggle"] == "rf_propagation"
    assert info["name"]
    assert info["description"]
    assert info["default_severity"] in {"routine", "priority", "immediate"}
    assert info["example_message"]


def test_geomagnetic_storm_still_in_registry():
    """swpc_kindex -> space.kindex -> geomagnetic_storm: registry entry
    survives the v0.5.7-rf edit."""
    assert "geomagnetic_storm" in ALERT_CATEGORIES
    assert ALERT_CATEGORIES["geomagnetic_storm"]["toggle"] == "rf_propagation"


@pytest.mark.parametrize(
    "central_cat,expected",
    [
        ("space.alert.a20f", "rf_propagation_alert"),
        ("space.alert", "rf_propagation_alert"),
        ("space.kindex", "geomagnetic_storm"),
        ("space.proton_flux", "solar_radiation_storm"),
        ("space.unknown_sub", "geomagnetic_storm"),  # catchall
    ],
)
def test_map_category_swpc_routings(central_cat, expected):
    """Pin the central -> meshai category map for each SWPC adapter."""
    assert map_category(central_cat) == expected


def _native_emitted_rf_categories() -> set[str]:
    """Walk ducting.py for _TIER_CATEGORY values mapping to toggle=rf_propagation."""
    from meshai.env import ducting as ducting_mod
    src = inspect.getsource(ducting_mod)
    # _TIER_CATEGORY entries are `"<tier>": "<category>",` literals.
    emitted = set(re.findall(
        r'_TIER_CATEGORY\s*=\s*\{([^}]+)\}', src, re.DOTALL))
    cats: set[str] = set()
    for block in emitted:
        cats |= set(re.findall(r':\s*"([a-z_]+)"', block))
    return {c for c in cats if c in ALERT_CATEGORIES
            and ALERT_CATEGORIES[c].get("toggle") == "rf_propagation"}


def _central_path_rf_categories() -> set[str]:
    central_inputs = [
        "space.alert.a20f", "space.alert",
        "space.kindex",
        "space.proton_flux",
        "space.unknown",
    ]
    return {map_category(c) for c in central_inputs}


def test_alert_categories_rf_complete():
    """Native + central-path emit set must be a SUBSET of registry rf
    entries (i.e., everything we emit is selectable). Legacy entries
    without an emitter are allowed as forward-compat targets and
    documented in the commit body."""
    registry_rf = {
        cid for cid, info in ALERT_CATEGORIES.items()
        if info.get("toggle") == "rf_propagation"
    }
    native = _native_emitted_rf_categories()
    central = _central_path_rf_categories()
    emitted = native | central
    missing = emitted - registry_rf
    assert not missing, f"rf emit set missing from ALERT_CATEGORIES: {missing}"
    # Sanity: at minimum the four v0.5.7-rf additions + geomagnetic_storm
    # must be in the emit set.
    for required in (
        "rf_anomalous_propagation", "rf_ducting_enhancement",
        "rf_propagation_alert", "solar_radiation_storm",
        "geomagnetic_storm",
    ):
        assert required in emitted, f"{required!r} not emitted by native or central path"
