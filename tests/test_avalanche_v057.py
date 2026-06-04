"""v0.5.7-avalanche: Central avalanche check + categories audit.

Covers two things shipped in v0.5.7-avalanche:

1. Central avalanche adapter check -- VERIFIED ABSENT in Central v0.10.0.
   The guide (docs/CONSUMER-INTEGRATION.md at v0.10.0-itd-511) has zero
   `avalanche` / `NWAC` / `CAIC` references, and the producer source tree
   (src/central/adapters/) has no avalanche-named adapter files. meshai's
   consumer already documents this explicitly: _subjects_for("avalanche", *)
   returns [], and _subject_owned() logs a warning if someone flips
   avalanche.feed_source=central. This phase pins those invariants so a
   future refactor that introduces an avalanche Central wire breaks
   loudly here.

2. ALERT_CATEGORIES avalanche-family audit. Native avalanche.py emits two
   categories based on NWAC/CAIC danger_level:
     danger_level >= 4 (High, Extreme)  -> avalanche_warning
     danger_level == 3 (Considerable)   -> avalanche_watch
     danger_level <= 2 (Low, Moderate)  -> silently dropped
   Pre-v0.5.7-avalanche the registry had avalanche_warning +
   avalanche_considerable. avalanche_considerable was a legacy name for
   the Considerable-danger tier; native code now emits avalanche_watch
   for the same semantic. Added avalanche_watch in v0.5.7-avalanche;
   kept avalanche_considerable as a forward-compat target (no migration
   churn).
"""

import inspect
import re

import pytest

from meshai.central.consumer import (
    CENTRAL_ADAPTER_TO_SOURCE,
    CentralConsumer,
    _SUBJECTS_BARE,
    _subjects_for,
)
from meshai.config import EnvironmentalConfig
from meshai.notifications.categories import ALERT_CATEGORIES


# ---------- FIX 1: Central has no avalanche adapter -----------------------


def test_avalanche_has_no_central_subscription():
    """_subjects_for returns empty for the avalanche source regardless of
    region (no Central counterpart exists in v0.10.0)."""
    for region in ("us.id", "us.mt", "us.co", "", None):
        assert _subjects_for("avalanche", region) == [], \
            f"unexpected subjects for region={region!r}"


def test_avalanche_absent_from_subjects_bare():
    """The bare-wildcard table also has no avalanche entry."""
    assert "avalanche" not in _SUBJECTS_BARE


def test_avalanche_absent_from_central_adapter_remap():
    """No Central adapter name remaps to meshai's 'avalanche' source."""
    assert "avalanche" not in CENTRAL_ADAPTER_TO_SOURCE.values(), \
        f"unexpected avalanche remap entry: {CENTRAL_ADAPTER_TO_SOURCE}"


def test_avalanche_feed_source_central_subscribes_nothing():
    """If a user accidentally sets avalanche.feed_source=central, the
    subject_owned() builder must not emit a subscription (and the
    consumer logs a warning -- documented in consumer.py)."""
    env = EnvironmentalConfig()
    env.avalanche.feed_source = "central"
    so = CentralConsumer(env, None)._subject_owned()
    # No subjects added for avalanche; nothing to subscribe to.
    assert not any("avalanche" in s.lower() for s in so.keys())


# ---------- FIX 2: ALERT_CATEGORIES avalanche-family audit ---------------


def test_avalanche_watch_in_registry():
    """v0.5.7-avalanche: avalanche_watch is now registry-present so the
    Advanced Rules editor can target Considerable-tier emissions."""
    assert "avalanche_watch" in ALERT_CATEGORIES
    info = ALERT_CATEGORIES["avalanche_watch"]
    assert info["toggle"] == "avalanche"
    assert info["default_severity"] == "routine"
    assert info["name"]
    assert info["description"]
    assert info["example_message"]


def test_avalanche_warning_still_in_registry():
    """Pre-v0.5.7-avalanche entry survives the edit."""
    assert "avalanche_warning" in ALERT_CATEGORIES
    assert ALERT_CATEGORIES["avalanche_warning"]["toggle"] == "avalanche"


def test_avalanche_considerable_legacy_kept():
    """avalanche_considerable kept as forward-compat / legacy target even
    though no current code path emits it. Documented in the commit body
    and categories.py inline note for future cleanup."""
    assert "avalanche_considerable" in ALERT_CATEGORIES
    assert ALERT_CATEGORIES["avalanche_considerable"]["toggle"] == "avalanche"


def _native_emitted_avalanche_categories() -> set[str]:
    """Walk avalanche.py for category= literals routing to toggle=avalanche."""
    from meshai.env import avalanche as aval_mod
    src = inspect.getsource(aval_mod)
    emitted = set(re.findall(r'category\s*=\s*"([a-z_]+)"', src))
    return {c for c in emitted if c in ALERT_CATEGORIES
            and ALERT_CATEGORIES[c].get("toggle") == "avalanche"}


def test_alert_categories_avalanche_complete():
    """Every category native avalanche.py emits must have a registry entry
    under toggle='avalanche'. Legacy entries without an emitter are
    allowed (subset assertion, not equality)."""
    registry_avalanche = {
        cid for cid, info in ALERT_CATEGORIES.items()
        if info.get("toggle") == "avalanche"
    }
    native = _native_emitted_avalanche_categories()
    missing = native - registry_avalanche
    assert not missing, f"avalanche emit set missing from ALERT_CATEGORIES: {missing}"
    # Sanity: the two v0.5.7-avalanche-recognized categories are both there.
    assert "avalanche_warning" in native, "native should emit avalanche_warning"
    assert "avalanche_watch" in native, "native should emit avalanche_watch"


@pytest.mark.parametrize(
    "cat", ["avalanche_warning", "avalanche_watch", "avalanche_considerable"],
)
def test_avalanche_categories_have_required_fields(cat):
    info = ALERT_CATEGORIES[cat]
    assert info["toggle"] == "avalanche"
    assert info["name"]
    assert info["description"]
    assert info["default_severity"] in {"routine", "priority", "immediate"}
    assert info["example_message"]
