"""Tests for satpass_handler wire field name reads.

Uses the verbatim live NOAA-18 envelope captured from Central NATS
(central.sat.pass.us.id.filer, 2026-06-10). Proves:
 1. Handler extracts correct norad_id, satellite_name, observer_name,
    max_elevation_deg, aos_time, los_time from the actual wire format.
 2. satpass_events row is inserted with correct values.
 3. Envelope missing norad_id is rejected (returns None).
 4. Wire message format includes the correct extracted values.
 5. Category mapping: pass.n2yo_visualpasses -> sat_pass.
"""
from __future__ import annotations

import json
import time

import pytest


# ── Verbatim live NOAA-18 envelope from Central NATS ────────────────

NOAA18_ENVELOPE = {
    "id": "filer:28654:2026-06-10T04:34:40+00:00",
    "source": "central.echo6.co",
    "type": "central.pass.n2yo_visualpasses.v1",
    "time": "2026-06-10T04:41:35+00:00",
    "datacontenttype": "application/json",
    "centralschemaversion": "1.0",
    "centralcategory": "pass.n2yo_visualpasses",
    "centralseverity": 1,
    "specversion": "1.0",
    "data": {
        "id": "filer:28654:2026-06-10T04:34:40+00:00",
        "adapter": "n2yo_visualpasses",
        "category": "pass.n2yo_visualpasses",
        "time": "2026-06-10T04:41:35Z",
        "expires": None,
        "severity": 1,
        "geo": {
            "centroid": [-114.6, 42.57],
            "bbox": None,
            "regions": ["US-ID"],
            "primary_region": "US-ID",
            "geometry": None,
        },
        "data": {
            "observer_name": "Filer",
            "observer_slug": "filer",
            "observer_state": "ID",
            "norad_id": 28654,
            "satellite_name": "NOAA 18",
            "aos_time": "2026-06-10T04:34:40+00:00",
            "peak_time": "2026-06-10T04:41:35+00:00",
            "los_time": "2026-06-10T04:48:30+00:00",
            "max_elevation_deg": 22.69,
            "magnitude": 6.7,
            "azimuth_at_aos": 125.6,
            "azimuth_at_aos_compass": "SE",
            "azimuth_at_peak": 63.0,
            "azimuth_at_peak_compass": "ENE",
            "azimuth_at_los": 359.5,
            "azimuth_at_los_compass": "N",
            "duration_s": 630,
        },
    },
}


def _enable_satpass(norad_ids=None):
    """Set satpass.enabled=true in the test DB."""
    from meshai.persistence import get_db
    from meshai.adapter_config import invalidate_cache
    conn = get_db()
    conn.execute(
        "UPDATE adapter_config SET value_json='true' "
        "WHERE adapter='satpass' AND key='enabled'"
    )
    # Set min_elevation low enough to accept this 22.69 deg pass
    conn.execute(
        "UPDATE adapter_config SET value_json='5' "
        "WHERE adapter='satpass' AND key='min_elevation'"
    )
    # Disable dry_run for tests that expect wire output
    conn.execute(
        "UPDATE adapter_config SET value_json='false' "
        "WHERE adapter='satpass' AND key='dry_run'"
    )
    # Set norad_ids (must be non-empty for opt-in)
    if norad_ids is None:
        norad_ids = [28654]
    conn.execute(
        "UPDATE adapter_config SET value_json=? "
        "WHERE adapter='satpass' AND key='norad_ids'",
        (json.dumps(norad_ids),)
    )
    invalidate_cache()


def _ingest_and_consolidate(env, subject, *, now, data=None):
    """Drive the two-call async satpass contract (ingest -> consolidate).

    handle_satpass() ingests the pass and returns None; the consumer then
    runs consolidate_satpass_pending(), which returns the (wire, data) to
    broadcast (or None). Returns the consolidation result.
    """
    from meshai.central.satpass_handler import (
        handle_satpass, consolidate_satpass_pending,
        drain_pending_consolidation_ids)
    drain_pending_consolidation_ids()
    assert handle_satpass(
        env, subject, data=data if data is not None else {}, now=now) is None
    for cid in drain_pending_consolidation_ids():
        res = consolidate_satpass_pending(cid)
        if res is not None:
            return res
    return None


# ── Handler produces correct satpass_events row ─────────────────────

def test_noaa18_envelope_produces_satpass_event():
    """Verbatim NOAA-18 envelope inserts row with correct field values."""
    from meshai.central.satpass_handler import handle_satpass
    from meshai.persistence import get_db

    _enable_satpass()
    if hasattr(handle_satpass, "_disabled_logged"):
        del handle_satpass._disabled_logged
    if hasattr(handle_satpass, "_no_norad_ids_logged"):
        del handle_satpass._no_norad_ids_logged

    now = 1781065800  # before NOAA18 envelope los_time
    result = _ingest_and_consolidate(
        NOAA18_ENVELOPE,
        "central.sat.pass.us.id.filer",
        now=now,
    )
    assert result is not None, "handler returned None -- field extraction failed"
    wire, _ = result

    conn = get_db()
    rows = conn.execute(
        "SELECT norad_id, sat_name, observer, max_elevation, aos_at, los_at "
        "FROM satpass_events WHERE norad_id=28654"
    ).fetchall()
    assert len(rows) >= 1, "no satpass_events row for norad_id=28654"
    row = rows[0]

    assert row["norad_id"] == 28654
    assert row["sat_name"] == "NOAA 18"
    assert row["observer"] == "Filer"
    assert abs(row["max_elevation"] - 22.69) < 0.01
    # aos_time = 2026-06-10T04:34:40+00:00 -> epoch
    assert row["aos_at"] is not None
    assert row["los_at"] is not None
    # los must be after aos
    assert row["los_at"] > row["aos_at"]


def test_noaa18_wire_message_format():
    """Wire message includes satellite name, direction in new 2-line format."""
    from meshai.central.satpass_handler import handle_satpass

    _enable_satpass()
    if hasattr(handle_satpass, "_disabled_logged"):
        del handle_satpass._disabled_logged
    if hasattr(handle_satpass, "_no_norad_ids_logged"):
        del handle_satpass._no_norad_ids_logged

    result = _ingest_and_consolidate(
        NOAA18_ENVELOPE,
        "central.sat.pass.us.id.filer",
        now=1781065800,  # before NOAA18 envelope los_time
    )
    assert result is not None
    wire, _ = result

    # Single clean line: name, numeric elevation, aos->peak->los compass sweep.
    assert "\n" not in wire, f"Expected single line: {wire!r}"
    assert "NOAA 18" in wire        # no mapping -> cleaned catalog name
    assert "max 23°" in wire   # 22.69 rounds to 23, not "low pass"
    assert "low pass" not in wire
    assert "min window" not in wire
    assert "SE→ENE→N" in wire  # aos -> peak -> los


def test_missing_norad_id_rejected():
    """Envelope with norad_id removed returns None."""
    from meshai.central.satpass_handler import handle_satpass

    _enable_satpass()
    if hasattr(handle_satpass, "_disabled_logged"):
        del handle_satpass._disabled_logged
    if hasattr(handle_satpass, "_no_norad_ids_logged"):
        del handle_satpass._no_norad_ids_logged

    # Deep copy and remove norad_id
    import copy
    env = copy.deepcopy(NOAA18_ENVELOPE)
    del env["data"]["data"]["norad_id"]

    wire = handle_satpass(
        env,
        "central.sat.pass.us.id.filer",
        data={},
        now=1781065800,  # before NOAA18 envelope los_time
    )
    assert wire is None, "handler should reject envelope without norad_id"


def test_missing_max_elevation_deg_rejected():
    """Envelope with max_elevation_deg removed returns None."""
    from meshai.central.satpass_handler import handle_satpass

    _enable_satpass()
    if hasattr(handle_satpass, "_disabled_logged"):
        del handle_satpass._disabled_logged
    if hasattr(handle_satpass, "_no_norad_ids_logged"):
        del handle_satpass._no_norad_ids_logged

    import copy
    env = copy.deepcopy(NOAA18_ENVELOPE)
    del env["data"]["data"]["max_elevation_deg"]

    wire = handle_satpass(
        env,
        "central.sat.pass.us.id.filer",
        data={},
        now=1781065800,  # before NOAA18 envelope los_time
    )
    assert wire is None, "handler should reject envelope without max_elevation_deg"


def test_observer_fallback_to_slug():
    """When observer_name is absent, falls back to observer_slug."""
    from meshai.central.satpass_handler import handle_satpass

    _enable_satpass()
    if hasattr(handle_satpass, "_disabled_logged"):
        del handle_satpass._disabled_logged
    if hasattr(handle_satpass, "_no_norad_ids_logged"):
        del handle_satpass._no_norad_ids_logged

    import copy
    env = copy.deepcopy(NOAA18_ENVELOPE)
    del env["data"]["data"]["observer_name"]
    # observer_slug = "filer" still present

    result = _ingest_and_consolidate(
        env,
        "central.sat.pass.us.id.filer",
        now=1781065800,  # before NOAA18 envelope los_time
    )
    assert result is not None
    # Observer name stored in DB, not in broadcast wire format
    from meshai.persistence import get_db
    conn = get_db()
    row = conn.execute(
        "SELECT observer FROM satpass_events WHERE norad_id=28654"
    ).fetchone()
    assert row is not None
    assert row["observer"] == "filer"
