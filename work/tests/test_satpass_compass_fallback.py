"""Tests for compass direction fallback on satpass_predict envelopes.

Proves the fix for empty compass on satpass_predict broadcasts:
 (a) satpass_predict-shape envelope (raw azimuth degrees, NO _compass fields)
     produces non-empty compass directions in the wire message.
 (b) n2yo-shape envelope with precomputed _compass strings is unchanged.
 (c) Envelope with neither raw azimuths nor _compass strings produces
     empty compass, no crash.

Uses verbatim field shapes from the live AO-27 predict envelope.
"""
from __future__ import annotations

import copy
import json

import pytest


# ── Live AO-27 satpass_predict envelope (no _compass fields) ─────────

AO27_PREDICT_ENVELOPE = {
    "id": "filer:36122:2026-06-13T06:12:00+00:00",
    "source": "central.echo6.co",
    "type": "central.pass.satpass_predict.v1",
    "time": "2026-06-13T06:12:00+00:00",
    "datacontenttype": "application/json",
    "centralschemaversion": "1.0",
    "centralcategory": "pass.satpass_predict",
    "centralseverity": 1,
    "specversion": "1.0",
    "data": {
        "id": "filer:36122:2026-06-13T06:12:00+00:00",
        "adapter": "satpass_predict",
        "category": "pass.satpass_predict",
        "time": "2026-06-13T06:12:00Z",
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
            "norad_id": 36122,
            "satellite_name": "EYESAT A (AO-27)",
            "aos_time": "2026-06-13T06:12:00+00:00",
            "peak_time": "2026-06-13T06:18:00+00:00",
            "los_time": "2026-06-13T06:24:00+00:00",
            "max_elevation_deg": 62.3,
            "azimuth_at_aos": 163.2,
            "azimuth_at_peak": 245.0,
            "azimuth_at_los": 348.7,
            "duration_s": 720,
        },
    },
}


# ── n2yo envelope with precomputed _compass strings ──────────────────

N2YO_ENVELOPE = {
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
    """Set satpass.enabled=true with permissive filters."""
    from meshai.persistence import get_db
    from meshai.adapter_config import invalidate_cache
    conn = get_db()
    conn.execute(
        "UPDATE adapter_config SET value_json='true' "
        "WHERE adapter='satpass' AND key='enabled'"
    )
    conn.execute(
        "UPDATE adapter_config SET value_json='5' "
        "WHERE adapter='satpass' AND key='min_elevation'"
    )
    conn.execute(
        "UPDATE adapter_config SET value_json='false' "
        "WHERE adapter='satpass' AND key='dry_run'"
    )
    if norad_ids is None:
        norad_ids = [36122, 28654]
    conn.execute(
        "UPDATE adapter_config SET value_json=? "
        "WHERE adapter='satpass' AND key='norad_ids'",
        (json.dumps(norad_ids),)
    )
    invalidate_cache()


def _clear_handler_flags():
    from meshai.central.satpass_handler import handle_satpass
    for attr in ("_disabled_logged", "_no_norad_ids_logged"):
        if hasattr(handle_satpass, attr):
            delattr(handle_satpass, attr)


def _ingest_and_consolidate(env, subject, *, now, data=None):
    """Drive the two-call async satpass contract (ingest → consolidate).

    handle_satpass() ingests the pass and returns None; the consumer then
    runs consolidate_satpass_pending(), which yields the (wire, data) to
    broadcast (or None). Returns the consolidation result so a test can
    assert on the real broadcast wire.
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


# ── (a) satpass_predict envelope: raw azimuths → non-empty compass ───

def test_satpass_predict_compass_from_raw_azimuths():
    """satpass_predict envelope with only raw azimuth degrees produces
    non-empty compass directions like SSE→N in wire output."""
    from meshai.central.satpass_handler import handle_satpass

    _enable_satpass()
    _clear_handler_flags()

    now = 1781330520  # well before the AO-27 pass window
    result = _ingest_and_consolidate(
        AO27_PREDICT_ENVELOPE,
        "central.sat.pass.us.id.filer",
        now=now,
    )
    assert result is not None, "handler returned None for satpass_predict envelope"
    wire, _ = result

    # Single-line wire: extract the compass segment between "max NN° " and " (".
    assert "\n" not in wire, f"Expected single line: {wire!r}"
    compass = wire.split("° ", 1)[1].split(" (", 1)[0]
    parts = compass.split("\u2192")
    assert len(parts) == 3, f"Expected aos->peak->los sweep, got {parts!r}"
    # 163.2 -> S, 245.0 -> SW (peak), 348.7 -> N  (8-point compass)
    assert parts[0] == "S", f"Expected S (from 163.2): {parts!r}"
    assert parts[1] == "SW", f"Expected SW (from peak 245.0): {parts!r}"
    assert parts[2] == "N", f"Expected N (from 348.7): {parts!r}"


# ── (b) n2yo envelope: precomputed _compass strings used as-is ───────

def test_n2yo_precomputed_compass_unchanged():
    """n2yo envelope with _compass string fields uses those strings,
    not raw azimuth conversion."""
    from meshai.central.satpass_handler import handle_satpass

    _enable_satpass()
    _clear_handler_flags()

    now = 1781065800  # before NOAA-18 pass window
    result = _ingest_and_consolidate(
        N2YO_ENVELOPE,
        "central.sat.pass.us.id.filer",
        now=now,
    )
    assert result is not None, "handler returned None for n2yo envelope"
    wire, _ = result

    # Single-line wire: must use the precomputed strings verbatim: SE→ENE→N.
    assert "\n" not in wire, f"Expected single line: {wire!r}"
    compass = wire.split("° ", 1)[1].split(" (", 1)[0]
    parts = compass.split("\u2192")
    assert len(parts) == 3, f"Expected aos->peak->los sweep, got {parts!r}"
    assert parts[0] == "SE", f"Expected precomputed aos SE: {parts!r}"
    assert parts[1] == "ENE", f"Expected precomputed peak ENE: {parts!r}"
    assert parts[2] == "N", f"Expected precomputed los N: {parts!r}"


# ── (c) envelope with neither → empty compass, no crash ──────────────

def test_no_compass_no_azimuth_no_crash():
    """Envelope with no _compass fields AND no raw azimuth fields
    produces empty compass directions without crashing."""
    from meshai.central.satpass_handler import handle_satpass

    _enable_satpass()
    _clear_handler_flags()

    env = copy.deepcopy(AO27_PREDICT_ENVELOPE)
    d = env["data"]["data"]
    # Remove all azimuth fields
    for key in ("azimuth_at_aos", "azimuth_at_los", "azimuth_at_peak",
                "azimuth_at_aos_compass", "azimuth_at_los_compass",
                "azimuth_at_peak_compass"):
        d.pop(key, None)

    now = 1781330520
    result = _ingest_and_consolidate(
        env,
        "central.sat.pass.us.id.filer",
        now=now,
    )
    assert result is not None, "handler crashed or returned None — should produce wire with empty compass"
    wire, _ = result

    # Single clean line; with no azimuth data the compass segment is simply
    # omitted (no arrow, no stray double space) and the wire still renders.
    assert "\n" not in wire, f"Expected single line: {wire!r}"
    assert wire.startswith("\U0001F6F0")
    assert "max" in wire
    assert "min)" in wire
    assert "\u2192" not in wire     # empty compass -> no sweep arrows
    assert "\u00b0  (" not in wire  # no stray double space where compass would be
    # No crash = test passes
