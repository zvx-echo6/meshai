"""Step 3 fire age-gate tests (Step 5 verification, age-gate part).

Targets `meshai.central.wfigs_handler._fire_too_old_to_announce` and the
handler's "New"-path suppression behaviour.

The gate exists because MeshAI broadcast a 6-week-old, already-closed fire
(OTR 11, declared_at=2026-05-06, ~45d old) to the live mesh as "New". The
gate keys on the fire's OWN declared_at age, not event recency.

Knob: ("wfigs","max_declare_age_seconds"), default 1209600 (14d), 0 = off.
The helper re-reads the knob each call (cache-backed, GUI-invalidated) and
FAILS OPEN (announces) when disabled or declared_at is None.

The autouse conftest fixture seeds adapter_config from the defaults
registry, so max_declare_age_seconds starts at its 14d default. Tests that
need a different value UPDATE the row + invalidate_cache().
"""
from __future__ import annotations

import time

import pytest

from meshai.central.wfigs_handler import _fire_too_old_to_announce


_14D = 14 * 86400
_45D = 45 * 86400
_5D = 5 * 86400


def _set_knob(seconds: int):
    """Override max_declare_age_seconds in adapter_config + drop the cache so
    the helper re-reads it on its next call."""
    from meshai.persistence import get_db
    from meshai.adapter_config import invalidate_cache

    get_db().execute(
        "UPDATE adapter_config SET value_json=? "
        "WHERE adapter='wfigs' AND key='max_declare_age_seconds'",
        (str(int(seconds)),),
    )
    invalidate_cache()


# ---------------------------------------------------------------------------
# Helper-level: the five required cases.
# ---------------------------------------------------------------------------


def test_declared_at_none_fails_open():
    """declared_at_epoch=None -> announce (fail-open). Default 14d knob."""
    now = int(time.time())
    assert _fire_too_old_to_announce(None, now) is False


def test_old_fire_suppressed_default_knob():
    """~45 days old + default 14d knob -> suppress (models OTR 11)."""
    now = int(time.time())
    declared = now - _45D
    assert _fire_too_old_to_announce(declared, now) is True


def test_recent_fire_announces():
    """~5 days old -> announce (well under the 14d default)."""
    now = int(time.time())
    declared = now - _5D
    assert _fire_too_old_to_announce(declared, now) is False


def test_knob_zero_disables_gate():
    """knob=0 -> gate disabled, even an ancient fire announces (fail-open)."""
    _set_knob(0)
    now = int(time.time())
    declared = now - _45D
    assert _fire_too_old_to_announce(declared, now) is False


def test_boundary_exactly_14d_is_suppressed():
    """Boundary: a fire declared exactly the knob age ago -> suppressed (>=)."""
    now = int(time.time())
    declared = now - _14D  # exactly 14 days
    assert _fire_too_old_to_announce(declared, now) is True


def test_boundary_one_second_under_14d_announces():
    """Just under the boundary (14d - 1s) -> announce (strict >= cutoff)."""
    now = int(time.time())
    declared = now - _14D + 1
    assert _fire_too_old_to_announce(declared, now) is False


def test_custom_knob_respected():
    """A custom (non-default) knob value is honoured by the helper."""
    _set_knob(_5D)  # 5-day gate
    now = int(time.time())
    assert _fire_too_old_to_announce(now - 6 * 86400, now) is True   # older than 5d
    assert _fire_too_old_to_announce(now - 4 * 86400, now) is False  # younger than 5d


# ---------------------------------------------------------------------------
# Handler-path: New is suppressed, Update still emits.
#
# These exercise the real Case (i)/(ii)/(iii) paths in handle_wfigs against
# the isolated tmp DB seeded by conftest.
# ---------------------------------------------------------------------------


def _normalized(*, irwin_id, declared_at_epoch, acres=250.0, contained=0):
    return {
        "_kind": "wfigs_incident",
        "irwin_id": irwin_id,
        "incident_name": "Old Town Road",
        "incident_type": "WF",
        "acres": acres,
        "contained_pct": contained,
        "lat": 42.93, "lon": -114.45,
        "county": "Twin Falls", "state": "ID",
        "declared_at_epoch": declared_at_epoch,
    }


def _envelope():
    return {
        "data": {"adapter": "wfigs", "category": "wildfire_incident",
                  "severity": "priority"}
    }


def test_handler_new_path_suppresses_old_fire():
    """Case (i): a brand-new fire whose declared_at is ~45d old is INSERTed
    but the 'New' broadcast is suppressed (wire is None), and the New-path
    category tag is NOT applied."""
    from meshai.central.wfigs_handler import handle_wfigs
    from meshai.persistence import get_db

    now = int(time.time())
    declared = now - _45D  # OTR 11 style
    data = {}
    wire = handle_wfigs(_normalized(irwin_id="ID-OTR-11", declared_at_epoch=declared),
                        _envelope(),
                        subject="central.fire.incident.id",
                        data=data, now=now)
    assert wire is None, f"old fire should be silenced, got wire={wire!r}"
    assert data.get("category") != "wildfire_declared"
    # The row is still INSERTed (so genuine future Updates work).
    row = get_db().execute(
        "SELECT irwin_id, last_broadcast_at FROM fires WHERE irwin_id=?",
        ("ID-OTR-11",)).fetchone()
    assert row is not None
    assert row["last_broadcast_at"] is None


def test_handler_new_path_announces_recent_fire():
    """Case (i): a recent fire (~5d) DOES broadcast 'New' and tags
    wildfire_declared."""
    from meshai.central.wfigs_handler import handle_wfigs

    now = int(time.time())
    declared = now - _5D
    data = {}
    wire = handle_wfigs(_normalized(irwin_id="ID-RECENT-1", declared_at_epoch=declared),
                        _envelope(),
                        subject="central.fire.incident.id",
                        data=data, now=now)
    assert wire is not None and "New" in wire
    assert data.get("category") == "wildfire_declared"


def test_handler_update_path_still_emits_for_old_fire():
    """An already-broadcast OLD fire that grows acreage still emits an
    'Update' (Case (iii) is NOT gated -- genuine old-but-active fires keep
    getting containment/acreage updates)."""
    from meshai.central.wfigs_handler import handle_wfigs
    from meshai.persistence import get_db

    now = int(time.time())
    declared = now - _45D
    # Pre-existing row that has already been broadcast.
    get_db().execute(
        "INSERT INTO fires(irwin_id, incident_name, current_acres, "
        "current_contained_pct, lat, lon, declared_at, last_event_at, "
        "last_broadcast_at, last_broadcast_acres, last_broadcast_contained) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("ID-OLD-ACTIVE", "Old Town Road", 250.0, 0, 42.93, -114.45,
         declared, now - 30000, now - 30000, 250.0, 0),
    )
    data = {}
    wire = handle_wfigs(
        _normalized(irwin_id="ID-OLD-ACTIVE", declared_at_epoch=declared,
                    acres=900.0, contained=20),
        _envelope(), subject="central.fire.incident.id",
        data=data, now=now)
    assert wire is not None and "Update" in wire, \
        f"old-but-active fire Update must still emit, got {wire!r}"
    assert data.get("category") != "wildfire_declared"
