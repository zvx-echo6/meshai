"""v0.5.7-tracking: Central tracking adapter check + categories audit.

The tracking family is a PLACEHOLDER for Phase 7 (per
meshai/notifications/categories.py:19 header comment: "tracking - ADS-B,
AIS, satellite passes (Phase 7)"). As of v0.5.7-tracking it has:

  - "tracking" in VALID_TOGGLES (reserved toggle name)
  - dashboard-frontend/src/pages/Environment.tsx FAMILIES list entry with
    label="Tracking", icon=Satellite, adapters=[]  (empty placeholder)
  - ZERO native adapter files in meshai/env/
  - ZERO ALERT_CATEGORIES entries with toggle="tracking"
  - ZERO Central wires (no central.tracking.* / central.aprs.* / etc.;
    no entries in _SUBJECTS_BARE; no entries in CENTRAL_ADAPTER_TO_SOURCE)

This file pins all of those invariants as regression guards. The intent
is that a future Phase 7 commit that flips any of these (e.g. adds an
APRS adapter, wires a Central tracking subject) will fail these tests
and FORCE the implementer to come back and complete the family-audit
shape (registry entries with required fields, composer emoji/labels,
test file refresh) the same way every other family in v0.5.7 was done.

Central v0.10.0 cross-check
---------------------------
The Central v0.10.0 guide (docs/CONSUMER-INTEGRATION.md at v0.10.0-itd-511)
documents 22 per-adapter sections covering wx / fire / quake / space /
disaster / traffic / hydro -- NONE are tracking-related. The producer
source tree src/central/adapters/ contains 24 adapter files; NONE are
named for any tracking concept (aprs / adsb / opensky / satellite /
position). Subject prefixes used: central.{disaster,fire,fires,hydro,
meta,models,quake,space,traffic,traffic_cameras,traffic_flow,wx}.> --
no central.tracking.* / central.aprs.*.

Same shape as v0.5.7-avalanche: no Central counterpart, native-only
(currently native-empty).
"""

import os
from pathlib import Path

import pytest

from meshai.central.consumer import (
    CENTRAL_ADAPTER_TO_SOURCE,
    CentralConsumer,
    _SUBJECTS_BARE,
    _subjects_for,
)
from meshai.config import EnvironmentalConfig
from meshai.notifications.categories import ALERT_CATEGORIES, VALID_TOGGLES


# ---------- FIX 1: Central has no tracking adapter -----------------------


def test_central_has_no_tracking_subject_prefix():
    """No Central stream/subject namespace uses a tracking-style prefix."""
    for adapter, subs in _SUBJECTS_BARE.items():
        for s in subs:
            assert "tracking" not in s.lower(), \
                f"unexpected tracking subject for adapter {adapter}: {s!r}"
            for needle in ("aprs", "adsb", "opensky", "ads_b"):
                assert needle not in s.lower(), \
                    f"unexpected {needle!r} subject for adapter {adapter}: {s!r}"


def test_central_adapter_remap_has_no_tracking_entries():
    """No Central adapter name remaps to a tracking source on either side."""
    for src_name, mesh_name in CENTRAL_ADAPTER_TO_SOURCE.items():
        for needle in ("tracking", "aprs", "adsb", "opensky"):
            assert needle not in src_name.lower(), \
                f"unexpected Central adapter name: {src_name}"
            assert needle not in mesh_name.lower(), \
                f"unexpected meshai source name: {mesh_name}"


def test_tracking_source_is_unknown_to_subjects_for():
    """Asking the consumer for tracking-source subjects returns empty for
    every region -- the source isn't in the table at all."""
    for region in ("us.id", "us.mt", "", None):
        assert _subjects_for("tracking", region) == [], \
            f"_subjects_for('tracking', {region!r}) should be []"
        assert _subjects_for("aprs", region) == []
        assert _subjects_for("adsb", region) == []


# ---------- meshai-side placeholder invariants ---------------------------


def test_tracking_toggle_is_reserved_in_valid_toggles():
    """The toggle name 'tracking' is reserved (placeholder for Phase 7)
    even though no categories use it yet."""
    assert "tracking" in VALID_TOGGLES


def test_alert_categories_has_zero_tracking_entries():
    """v0.5.7-tracking placeholder check: registry has no toggle='tracking'
    entries. If Phase 7 lands, this test should be updated alongside the
    new entries -- not silently deleted."""
    tracking_entries = [
        cid for cid, info in ALERT_CATEGORIES.items()
        if info.get("toggle") == "tracking"
    ]
    assert tracking_entries == [], \
        f"unexpected tracking-family entries: {tracking_entries}"


def test_no_native_tracking_adapter_files():
    """meshai/env/ has no tracking-related adapter files. Phase 7 will
    add at least one (likely aprs.py / adsb.py / opensky.py); when it
    does, this test should be updated to point at the new adapter."""
    env_dir = Path("meshai/env")
    if not env_dir.is_dir():
        pytest.skip("meshai/env not present in this working tree")
    files = {p.name for p in env_dir.iterdir() if p.suffix == ".py"}
    for needle in ("aprs", "adsb", "ads_b", "opensky", "tracking", "satellite"):
        for fname in files:
            assert needle not in fname.lower(), \
                f"unexpected env adapter file {fname!r} hints at a tracking adapter"


# ---------- frontend placeholder invariant -------------------------------


def test_environment_tsx_tracking_family_has_empty_adapter_list():
    """Environment.tsx FAMILIES entry for 'tracking' must currently have
    adapters=[]. Phase 7 will populate this list; that change should be
    paired with new ALERT_CATEGORIES entries + adapter files + a test
    refresh here."""
    tsx = Path("dashboard-frontend/src/pages/Environment.tsx")
    if not tsx.is_file():
        pytest.skip("Environment.tsx not present in this working tree")
    text = tsx.read_text()
    # Look for the FAMILIES line for tracking; the adapter list must be empty.
    assert "key: 'tracking'" in text or 'key: "tracking"' in text, \
        "Environment.tsx FAMILIES is missing the tracking placeholder entry"
    # Pattern: `key: 'tracking', label: 'Tracking', icon: Satellite, adapters: []`
    # Accept any quote style + minor formatting tolerance.
    import re
    m = re.search(
        r"""key:\s*['"]tracking['"]\s*,\s*"""
        r"""label:\s*['"]Tracking['"]\s*,\s*"""
        r"""icon:\s*\w+\s*,\s*"""
        r"""adapters:\s*\[\s*\]""",
        text, re.DOTALL,
    )
    assert m, (
        "Environment.tsx tracking-family adapter list is no longer empty. "
        "If you're landing Phase 7, update this test together with the "
        "new ALERT_CATEGORIES entries + adapter files + composer glyphs."
    )


# ---------- safety: no orphan routing into tracking ----------------------


def test_no_prefix_fallback_routes_to_tracking():
    """The _TOGGLE_PREFIX_FALLBACK chain in categories.py has no rule that
    silently routes unknown categories to toggle='tracking'. Adding such
    a rule without paired registry entries would create orphan tracking
    events -- the v0.5.7 audit purity rule (every emitted = selectable)
    forbids this."""
    from meshai.notifications.categories import _TOGGLE_PREFIX_FALLBACK
    for prefix, toggle in _TOGGLE_PREFIX_FALLBACK:
        assert toggle != "tracking", \
            f"prefix-fallback {prefix!r} -> tracking without registry entries"
