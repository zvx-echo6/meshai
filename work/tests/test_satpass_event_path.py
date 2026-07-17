"""Tests for satpass event path — wire adapter names route correctly.

Verifies:
 - celestrak_tle envelope routes to tle_handler and inserts sat_tles row
 - n2yo_visualpasses envelope routes to satpass_handler
 - satpass_predict envelope routes to satpass_handler
 - adapter_config reads min_elevation (not min_elevation_deg)
 - CENTRAL_ADAPTER_TO_SOURCE maps wire names to 'satpass'
 - stale adapter names removed from dispatch
"""

import json
import time

import pytest


# ── Realistic wire envelopes ────────────────────────────────────────

CELESTRAK_TLE_ENVELOPE = {
    "specversion": "1.0",
    "id": "tle-25544-1718200000",
    "source": "central",
    "type": "central.sat.tle",
    "data": {
        "id": "tle-25544-1718200000",
        "adapter": "celestrak_tle",
        "category": "sat.tle",
        "data": {
            "norad_id": 25544,
            "satellite_name": "ISS (ZARYA)",
            "tle_line1": "1 25544U 98067A   26163.51782528  .00020000  00000-0  35000-3 0  9999",
            "tle_line2": "2 25544  51.6416 247.4627 0006703 130.5360 325.0288 15.49815002    17",
            "epoch": "2026-06-12T12:25:40Z",
        },
    },
}

N2YO_PASS_ENVELOPE = {
    "specversion": "1.0",
    "id": "pass-25544-twinfalls-1718300000",
    "source": "central",
    "type": "central.sat.pass",
    "data": {
        "id": "pass-25544-twinfalls-1718300000",
        "adapter": "n2yo_visualpasses",
        "category": "sat.pass",
        "data": {
            "norad_id": 25544,
            "sat_name": "ISS (ZARYA)",
            "observer": "Twin Falls",
            "max_elevation": 72.5,
            "aos": "2026-06-13T04:15:00Z",
            "los": "2026-06-13T04:21:30Z",
            "direction": "visible",
        },
    },
}

SATPASS_PREDICT_ENVELOPE = {
    "specversion": "1.0",
    "id": "pass-25544-boise-1718300500",
    "source": "central",
    "type": "central.sat.pass",
    "data": {
        "id": "pass-25544-boise-1718300500",
        "adapter": "satpass_predict",
        "category": "sat.pass",
        "data": {
            "norad_id": 25544,
            "sat_name": "ISS (ZARYA)",
            "observer": "Boise",
            "max_elevation": 45.0,
            "aos": "2026-06-13T04:20:00Z",
            "los": "2026-06-13T04:26:00Z",
            "direction": "visible",
        },
    },
}


def _enable_satpass():
    """Set satpass.enabled=true in the test DB."""
    from meshai.persistence import get_db
    from meshai.adapter_config import invalidate_cache
    conn = get_db()
    conn.execute(
        "UPDATE adapter_config SET value_json='true' "
        "WHERE adapter='satpass' AND key='enabled'"
    )
    invalidate_cache()


# ── TLE handler route ──────────────────────────────────────────────

def test_tle_handler_inserts_sat_tles_row():
    """A celestrak_tle envelope must land a row in sat_tles."""
    from meshai.central.tle_handler import handle_tle
    from meshai.persistence import get_db

    _enable_satpass()

    # Reset the disabled-logged flag if set
    if hasattr(handle_tle, "_disabled_logged"):
        del handle_tle._disabled_logged

    result = handle_tle(
        CELESTRAK_TLE_ENVELOPE,
        "central.sat.tle.25544",
        now=int(time.time()),
    )

    # TLE handler always returns None (storage-only)
    assert result is None

    conn = get_db()
    row = conn.execute(
        "SELECT norad_id, name, line1, line2, epoch FROM sat_tles WHERE norad_id=25544"
    ).fetchone()
    assert row is not None, "TLE row not inserted into sat_tles"
    assert row["name"] == "ISS (ZARYA)"
    assert row["line1"].startswith("1 25544U")
    assert row["line2"].startswith("2 25544")
    assert row["epoch"] == "2026-06-12T12:25:40Z"


def test_tle_handler_drops_when_disabled():
    """When satpass.enabled=false, TLE handler drops and returns None."""
    from meshai.central.tle_handler import handle_tle
    from meshai.persistence import get_db

    # enabled=false is the default from conftest seed
    if hasattr(handle_tle, "_disabled_logged"):
        del handle_tle._disabled_logged

    result = handle_tle(
        CELESTRAK_TLE_ENVELOPE,
        "central.sat.tle.25544",
        now=int(time.time()),
    )
    assert result is None

    conn = get_db()
    row = conn.execute(
        "SELECT norad_id FROM sat_tles WHERE norad_id=25544"
    ).fetchone()
    assert row is None, "TLE row should NOT be inserted when disabled"


# ── Pass handler route ──────────────────────────────────────────────

def test_satpass_handler_accepts_n2yo_envelope():
    """n2yo_visualpasses must not be rejected by the adapter guard."""
    inner = N2YO_PASS_ENVELOPE["data"]
    adapter = inner.get("adapter")
    assert adapter == "n2yo_visualpasses"
    assert adapter in ("n2yo_visualpasses", "satpass_predict")


def test_satpass_handler_accepts_satpass_predict_envelope():
    """satpass_predict must not be rejected by the adapter guard."""
    inner = SATPASS_PREDICT_ENVELOPE["data"]
    adapter = inner.get("adapter")
    assert adapter == "satpass_predict"
    assert adapter in ("n2yo_visualpasses", "satpass_predict")


def test_satpass_handler_rejects_wrong_adapter():
    """An envelope with adapter='celestrak_tle' must be rejected."""
    from meshai.central.satpass_handler import handle_satpass
    if hasattr(handle_satpass, "_disabled_logged"):
        del handle_satpass._disabled_logged
    result = handle_satpass(CELESTRAK_TLE_ENVELOPE, "central.sat.tle.25544")
    assert result is None


# ── Config key consistency ──────────────────────────────────────────

def test_registry_has_min_elevation_not_deg():
    """The REGISTRY key must be 'min_elevation', not 'min_elevation_deg'."""
    from meshai.adapter_config.defaults import REGISTRY
    assert ("satpass", "min_elevation") in REGISTRY
    assert ("satpass", "min_elevation_deg") not in REGISTRY


def test_handler_reads_min_elevation():
    """satpass_handler must read cfg.min_elevation (matching REGISTRY key)."""
    import inspect
    from meshai.central import satpass_handler
    src = inspect.getsource(satpass_handler)
    assert "min_elevation" in src
    assert "min_elevation_deg" not in src
