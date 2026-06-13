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


# ── (a) satpass_predict envelope: raw azimuths → non-empty compass ───

def test_satpass_predict_compass_from_raw_azimuths():
    """satpass_predict envelope with only raw azimuth degrees produces
    non-empty compass directions like SSE→N in wire output."""
    from meshai.central.satpass_handler import handle_satpass

    _enable_satpass()
    _clear_handler_flags()

    now = 1781330520  # well before the AO-27 pass window
    wire = handle_satpass(
        AO27_PREDICT_ENVELOPE,
        "central.sat.pass.us.id.filer",
        data={},
        now=now,
    )
    assert wire is not None, "handler returned None for satpass_predict envelope"

    lines = wire.split("\n")
    assert len(lines) == 2, f"Expected 2 lines, got {len(lines)}: {wire!r}"

    # Line 1 must contain non-empty compass directions
    # 163.2° → S (8-point compass), 348.7° → N
    assert "S" in lines[0].split(",")[-1], f"Expected S (from 163.2°) in compass portion: {lines[0]!r}"
    assert "\u2192" in lines[0], f"Expected → arrow in line 1: {lines[0]!r}"

    # Extract the compass portion: after bucket comma, before newline
    # Format: 🛰️ {name} {bucket}, {aos_compass}→{los_compass}
    arrow_idx = lines[0].index("\u2192")
    los_part = lines[0][arrow_idx + 1:]
    assert los_part != "", f"los_compass is empty in line 1: {lines[0]!r}"
    # 348.7° → N in 8-point compass
    assert los_part == "N", f"Expected N (from 348.7°) after arrow: {los_part!r}"


# ── (b) n2yo envelope: precomputed _compass strings used as-is ───────

def test_n2yo_precomputed_compass_unchanged():
    """n2yo envelope with _compass string fields uses those strings,
    not raw azimuth conversion."""
    from meshai.central.satpass_handler import handle_satpass

    _enable_satpass()
    _clear_handler_flags()

    now = 1781065800  # before NOAA-18 pass window
    wire = handle_satpass(
        N2YO_ENVELOPE,
        "central.sat.pass.us.id.filer",
        data={},
        now=now,
    )
    assert wire is not None, "handler returned None for n2yo envelope"

    lines = wire.split("\n")
    # Must use the precomputed strings: SE→N
    assert "SE" in lines[0], f"Expected SE from precomputed _compass: {lines[0]!r}"
    # The los_compass from precomputed is "N"
    arrow_idx = lines[0].index("\u2192")
    los_part = lines[0][arrow_idx + 1:]
    assert los_part == "N", f"Expected precomputed los_compass N, got {los_part!r}"


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
    wire = handle_satpass(
        env,
        "central.sat.pass.us.id.filer",
        data={},
        now=now,
    )
    assert wire is not None, "handler crashed or returned None — should produce wire with empty compass"

    lines = wire.split("\n")
    assert len(lines) == 2
    # Arrow should still be present with empty directions: "→" or similar
    assert "\u2192" in lines[0], f"Expected → in line 1 even with empty compass: {lines[0]!r}"
    # No crash = test passes
