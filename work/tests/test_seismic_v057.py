"""v0.5.7-seismic: USGS quake NATS pattern + severity clamp + categories audit.

Covers three things shipped in v0.5.7-seismic:

1. USGS quake subject pattern -- per Central v0.10.0 guide §usgs_quake the
   pattern is `central.quake.event.<tier>` (4 tokens, NO region). Pre-v0.5.7
   we shipped `central.quake.event.>.us.id` which is invalid NATS (`>`
   mid-subject) AND wouldn't have matched anything Central publishes.
2. Severity clamp -- documents/regression-tests the existing `map_severity`
   behavior. The v0.5.7-seismic prompt described a "severity=5 great-quake
   IndexError / drop" bug; investigation confirmed that bug does NOT exist:
     - map_severity already clamps any int >= 3 to "immediate"
       (so severity=5, 99, etc. all map safely).
     - NotificationToggle.severity_channels is dict-keyed by severity STRING
       ({"routine","priority","immediate"}), not int -- IndexError is
       structurally impossible from this boundary.
     - Per the guide §5b severity vocabulary is documented as 0-4 only;
       severity=5 is not in Central's contract. The clamp is defensive
       padding against contract drift.
   These tests pin the clamp so a future regression doesn't introduce the
   bug Matt was guarding against.
3. ALERT_CATEGORIES seismic-family audit -- earthquake_event was MISSING
   from the registry. Native usgs_quake.py emits it and the central path
   maps every quake.event.<tier> to it via map_category, but the
   Advanced Rules editor couldn't select it (it fell through to
   get_category's mesh_health default). Added in v0.5.7-seismic. The
   hydro entries (stream_flood_warning / stream_high_water under
   toggle='seismic' from v0.5.2) are out of scope; this audit only adds
   the quake side and verifies hydro toggles are unchanged.
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
from meshai.config import EnvironmentalConfig
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


# ---------- FIX 1: USGS quake subject pattern -----------------------------


def test_usgs_quake_subject_uses_tail_only_wildcard():
    """Per Central v0.10.0 guide §usgs_quake: `central.quake.event.<tier>`,
    4 tokens, no region. Tail-only `>` is the legal wildcard form."""
    subs = _subjects_for("usgs_quake", "us.id")
    assert subs == ["central.quake.event.>"]
    for s in subs:
        _assert_legal_nats(s)


def test_usgs_quake_subject_has_no_mid_subject_wildcard():
    """Belt-and-braces NATS-syntax check."""
    for s in _subjects_for("usgs_quake", "us.id"):
        tokens = s.split(".")
        for tok in tokens[:-1]:
            assert tok != ">", f"`>` mid-subject in {s!r}"


def test_usgs_quake_bare_form_unchanged():
    """Empty region falls back to the broader bare wildcard for backward compat."""
    assert _subjects_for("usgs_quake", "") == ["central.quake.>"]


# ---------- FIX 2: severity clamp regression guard ------------------------


@pytest.mark.parametrize("sev,expected", [
    (0, "routine"),
    (1, "routine"),
    (2, "priority"),
    (3, "immediate"),
    (4, "immediate"),
    # v0.5.7-seismic regression guard: hypothetical "great quake" severity=5
    # (not in the Central v0.10.0 contract, but defensible if it ever appears)
    # MUST clamp to "immediate", not raise / not drop.
    (5, "immediate"),
    (10, "immediate"),
    (99, "immediate"),
    # Edge cases that previously degraded to "routine".
    (None, "routine"),
    ("nonsense", "routine"),
    (-1, "routine"),
])
def test_map_severity_handles_full_range(sev, expected):
    assert map_severity(sev) == expected


def test_severity_5_quake_routes_through_consumer_without_crashing():
    """Inject a synthetic Central quake envelope with severity=5 (out-of-
    contract great-quake hypothetical) and verify it normalizes cleanly
    into an Event with severity='immediate' -- no IndexError, no drop."""
    rec = []
    bus = EventBus(); bus.subscribe(rec.append)
    c = CentralConsumer(EnvironmentalConfig(), bus)
    env = {"id": "us8000mc12", "data": {
        "id": "us8000mc12", "adapter": "usgs_quake",
        "category": "quake.event.great",
        "time": "2026-05-19T02:50:39+00:00",
        "severity": 5,    # the out-of-contract value
        "geo": {"centroid": [-148.93, 61.32], "primary_region": "US-AK", "regions": ["US-AK"]},
        "data": {"title": "M 8.2 - 23 km ESE of Anchorage, AK",
                 "magnitude": 8.2, "depth": 32.0, "magType": "mw",
                 "alert": "red", "tsunami": 1, "type": "earthquake"}}}
    ev = c._handle("central.quake.event.great", json.dumps(env).encode())
    assert ev is not None
    assert ev.severity == "immediate"
    assert ev.category == "earthquake_event"
    assert ev.source == "usgs_quake"
    assert len(rec) == 1


def test_severity_channels_is_string_keyed_no_int_indexerror_risk():
    """The shape that would make severity=5 dangerous is an int-indexed
    list; ours is a dict keyed by severity STRING. This pins that contract
    so a refactor can't quietly introduce the IndexError vector."""
    from meshai.config import NotificationToggle
    t = NotificationToggle(name="seismic")
    assert isinstance(t.severity_channels, dict)
    # dict.get with an unknown key returns None / default, never raises.
    assert t.severity_channels.get("any_string", []) == []


# ---------- FIX 3: seismic-family categories audit ------------------------


def test_earthquake_event_in_registry():
    """v0.5.7-seismic: registry now has earthquake_event so the Advanced
    Rules editor can target it. Pre-v0.5.7-seismic it was missing entirely
    and fell through to the mesh_health default via get_category()."""
    assert "earthquake_event" in ALERT_CATEGORIES
    assert ALERT_CATEGORIES["earthquake_event"]["toggle"] == "seismic"


def test_hydro_entries_still_seismic_toggle():
    """The v0.5.2 USGS-water migration to toggle='seismic' (geohazards
    family in the GUI) must survive the v0.5.7-seismic edit. Out of scope
    for THIS phase to modify; in scope to verify-unchanged."""
    assert ALERT_CATEGORIES["stream_flood_warning"]["toggle"] == "seismic"
    assert ALERT_CATEGORIES["stream_high_water"]["toggle"] == "seismic"


def _native_emitted_quake_categories() -> set[str]:
    """Walk usgs_quake.py for category= literals routing to toggle=seismic."""
    from meshai.env import usgs_quake as quake_mod
    src = inspect.getsource(quake_mod)
    emitted = set(re.findall(r'category="([a-z_]+)"', src))
    return {c for c in emitted if c in ALERT_CATEGORIES
            and ALERT_CATEGORIES[c].get("toggle") == "seismic"}


def _central_path_quake_categories() -> set[str]:
    central_inputs = [
        "quake.event.minor", "quake.event.light", "quake.event.moderate",
        "quake.event.strong", "quake.event.major", "quake.event.great",
    ]
    return {map_category(c) for c in central_inputs}


def test_alert_categories_quake_complete():
    """Every quake-side category that meshai emits (native or central path)
    must have an ALERT_CATEGORIES entry under toggle='seismic'. Hydro
    entries are out of scope for this audit but kept as a control."""
    native = _native_emitted_quake_categories()
    central = _central_path_quake_categories()
    emitted = native | central
    # All six tiers should fold to earthquake_event via the central path.
    assert emitted == {"earthquake_event"}, f"unexpected quake emit set: {emitted}"
    assert "earthquake_event" in ALERT_CATEGORIES


def test_seismic_family_required_fields():
    info = ALERT_CATEGORIES["earthquake_event"]
    assert info["toggle"] == "seismic"
    assert info["name"]
    assert info["description"]
    assert info["default_severity"] in {"routine", "priority", "immediate"}
    assert info["example_message"]
