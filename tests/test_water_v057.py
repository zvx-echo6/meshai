"""v0.5.7-water: USGS NWIS hydro NATS pattern + water/hydro categories audit.

Covers two things shipped in v0.5.7-water:

1. USGS NWIS hydro subject pattern -- per Central v0.10.0-itd-511 nwis.py
   producer subject_for() body, the actual published subject is
   `central.hydro.<param>.<agency>.<site>.<region>` where <region> is
   `us.<state>` (7 tokens) or `unknown` (6 tokens). The pre-v0.5.7-water
   `central.hydro.>.us.id` was invalid NATS (`>` mid-subject) -- replaced
   with three single-token `*` wildcards in the param/agency/site slots
   plus the bare region tail.

   Note on guide vs code: the Central guide §nwis text shows only the
   4-token category-shape stem `central.hydro.<parameter_code>.<agency>.
   <bare_site_no>` without the regional suffix. That doc text is stale
   w.r.t. the producer code. The producer code is the ground truth (it's
   what NATS actually delivers); we follow the code.

2. ALERT_CATEGORIES water/hydro audit -- pre-v0.5.7-water the registry had
   `stream_flood_warning` and `stream_high_water` (both toggle=seismic from
   the v0.5.2 geohazards migration). The central path's
   `("hydro.", "stream_flow")` _CATEGORY_MAP entry produced a category
   `stream_flow` that had no registry entry -- the rule editor couldn't
   target it. Added `stream_flow` (toggle=seismic) so central-delivered
   raw gauge readings are UI-selectable. The native usgs.py threshold-
   classified categories are unchanged.
"""

import inspect
import re

import pytest

from meshai.central.consumer import (
    _SUBJECTS_BARE,
    _subjects_for,
    map_category,
    map_severity,
)
from meshai.notifications.categories import ALERT_CATEGORIES


def _assert_legal_nats(subject: str) -> None:
    tokens = subject.split(".")
    if ">" in tokens:
        assert tokens[-1] == ">", f"`>` not at tail in {subject!r}"
        assert tokens.count(">") == 1, f"multiple `>` in {subject!r}"
    for tok in tokens:
        assert tok, f"empty token in {subject!r}"
        if tok not in {"*", ">"}:
            assert "*" not in tok and ">" not in tok, f"mixed wildcard in token {tok!r}"


# ---------- FIX 1: USGS NWIS hydro subject pattern ------------------------


def test_usgs_subjects_are_nats_legal():
    """No `>` mid-subject; all wildcards are single-token `*`."""
    subs = _subjects_for("usgs", "us.id")
    assert subs == [
        "central.hydro.*.*.*.us.id",
        "central.hydro.*.*.*.unknown",
    ]
    for s in subs:
        _assert_legal_nats(s)
        # Per-state filter has 7 tokens; .unknown has 6.
        assert ">" not in s, f"`>` should not appear in fixed-token form: {s!r}"


def test_usgs_subjects_match_producer_published_shape():
    """Sanity: the subscription patterns match what nwis.py actually
    publishes. Producer publishes:
        central.hydro.<param>.<agency>.<site>.<region>
    where <region> is us.<state> (2 tokens) or unknown (1 token).
    """
    sub_state, sub_unknown = _subjects_for("usgs", "us.id")
    # Per-state form: matches a 7-token published subject.
    sample_published_state = "central.hydro.00060.usgs.06898000.us.id"
    sample_published_unknown = "central.hydro.00060.usgs.06898000.unknown"
    # Token-count check (NATS `*` matches exactly one token).
    assert len(sub_state.split(".")) == len(sample_published_state.split("."))
    assert len(sub_unknown.split(".")) == len(sample_published_unknown.split("."))
    # Per-state must end with the requested region, .unknown with literal.
    assert sub_state.endswith(".us.id")
    assert sub_unknown.endswith(".unknown")


def test_usgs_bare_form_unchanged():
    """Empty region falls back to the bare wildcard (backward compat)."""
    assert _subjects_for("usgs", "") == ["central.hydro.>"]
    assert _subjects_for("usgs", None) == ["central.hydro.>"]


def test_usgs_per_state_filter_does_not_match_wrong_state():
    """Sanity: a Montana-region subscription wouldn't match an Idaho subject.
    (Just verifies the substitution flows through cleanly per region.)"""
    mt_subs = _subjects_for("usgs", "us.mt")
    assert mt_subs == [
        "central.hydro.*.*.*.us.mt",
        "central.hydro.*.*.*.unknown",
    ]


# ---------- FIX 2: ALERT_CATEGORIES water/hydro audit ---------------------


def test_stream_flow_in_registry():
    """v0.5.7-water: central path's `hydro.* -> stream_flow` mapping now has
    a corresponding ALERT_CATEGORIES entry under toggle='seismic'."""
    assert "stream_flow" in ALERT_CATEGORIES
    assert ALERT_CATEGORIES["stream_flow"]["toggle"] == "seismic"
    assert ALERT_CATEGORIES["stream_flow"]["default_severity"] == "routine"


def test_existing_hydro_entries_unchanged():
    """v0.5.2 USGS-water -> toggle='seismic' migration must survive."""
    for cat in ("stream_flood_warning", "stream_high_water"):
        assert cat in ALERT_CATEGORIES
        assert ALERT_CATEGORIES[cat]["toggle"] == "seismic"


def _native_emitted_water_categories() -> set[str]:
    """Walk usgs.py for category= literals routing to toggle=seismic."""
    from meshai.env import usgs as usgs_mod
    src = inspect.getsource(usgs_mod)
    emitted = set(re.findall(r'category\s*=\s*"([a-z_]+)"', src))
    return {c for c in emitted if c in ALERT_CATEGORIES
            and ALERT_CATEGORIES[c].get("toggle") == "seismic"}


def _central_path_water_categories() -> set[str]:
    """Map a representative set of central hydro category strings through
    map_category() to see what meshai categories we'd emit downstream.
    Per the guide §nwis, every NWIS event has category
    `hydro.<pcode>.<agency>.<site>`."""
    central_inputs = [
        "hydro.00060.usgs.06898000",   # discharge
        "hydro.00065.usgs.06898000",   # gage height
        "hydro.00010.usgs.06898000",   # water temperature
        "hydro.00060.mo005.0000123",   # cooperator agency
    ]
    return {map_category(c) for c in central_inputs}


def test_alert_categories_water_complete():
    """Native + central-path water emit must equal registry's water-side
    subset of toggle='seismic'. (The quake-side earthquake_event added in
    v0.5.7-seismic is also under toggle='seismic' but emitted by a
    different adapter — exclude it from this water-only audit.)"""
    registry_water = {
        cid for cid, info in ALERT_CATEGORIES.items()
        if info.get("toggle") == "seismic"
        and (cid.startswith("stream_") or cid == "stream_flow")
    }
    native = _native_emitted_water_categories()
    central = _central_path_water_categories()
    emitted = native | central
    missing = emitted - registry_water
    orphans = registry_water - emitted
    assert not missing, f"water emit set missing from ALERT_CATEGORIES: {missing}"
    assert not orphans, f"ALERT_CATEGORIES has orphan water entries: {orphans}"


def test_native_threshold_categories_still_emitted():
    """Spot-check that usgs.py still has the two threshold-classified
    categories (regression guard against accidental removal)."""
    native = _native_emitted_water_categories()
    assert "stream_flood_warning" in native
    assert "stream_high_water" in native


def test_central_hydro_pcode_strings_all_map_to_stream_flow():
    """Every realistic central hydro category collapses to stream_flow
    via the catchall `("hydro.", "stream_flow")` _CATEGORY_MAP entry."""
    for pcode in ("00060", "00065", "00010", "00045", "00095"):
        assert map_category(f"hydro.{pcode}.usgs.12345678") == "stream_flow"


@pytest.mark.parametrize(
    "cat", ["stream_flow", "stream_flood_warning", "stream_high_water"],
)
def test_water_categories_have_required_fields(cat):
    info = ALERT_CATEGORIES[cat]
    assert info["toggle"] == "seismic"
    assert info["name"]
    assert info["description"]
    assert info["default_severity"] in {"routine", "priority", "immediate"}
    assert info["example_message"]


# ---------- Severity sanity for central NWIS events -----------------------


def test_central_nwis_severity_zero_routes_to_routine():
    """Central NWIS publishes severity=0 (no threshold classification).
    Confirm that becomes 'routine' in meshai's three-level scale."""
    assert map_severity(0) == "routine"
