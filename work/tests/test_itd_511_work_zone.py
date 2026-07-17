"""Tests for the v0.5.9 GAMMA itd_511 work_zone parser in central_normalizer.

Covers the cutover path: itd_511 supplies all Idaho work_zone broadcasts now
(state_511_atis ID is skipped). The parser must produce the same flat dict
shape as _parse_state_511_atis so the work-zone renderer (now
formatters.incident._render_work_zone(), via _n_to_canonical_workzone())
works unchanged.
"""

from datetime import datetime, timezone

import pytest

from meshai import central_normalizer as cn


# ---------- envelope builder ---------------------------------------------


def _itd_work_zone_env(*, roadway="SH-55", direction="North",
                          event_sub_type="roadConstruction",
                          is_full_closure=False,
                          external_id="ITD:469:42",
                          lat=44.103, lon=-116.110,
                          geocoder_city="McCall",
                          start_epoch=1780600000,
                          planned_end_epoch=1781200000,
                          description="Road construction on SH-55 Northbound from MM (140) to MM (145). 6/4/2026 7:00 AM to 6/11/2026 5:00 PM."):
    return {
        "id": external_id,
        "subject": "central.traffic.work_zone.us.id",
        "data": {
            "id": external_id, "adapter": "itd_511",
            "category": "work_zone.itd_511", "severity": 1,
            "geo": {"centroid": [lon, lat], "primary_region": "US-ID"},
            "data": {
                "event_type_short": "work_zone",
                "event_sub_type": event_sub_type,
                "roadway_name": roadway, "direction": direction,
                "description": description,
                "lanes_affected": "All lanes affected",
                "is_full_closure": is_full_closure,
                "itd_severity": "None",
                "comment": "",
                "cause": "roadwork",
                "organization": "ERS",
                "recurrence_text": "",
                "recurrence_schedules": [],
                "restrictions": {},
                "encoded_polyline": "",
                "id_internal": 42, "source_id": "999",
                "reported_epoch": start_epoch,
                "last_updated_epoch": start_epoch,
                "start_epoch": start_epoch,
                "planned_end_epoch": planned_end_epoch,
                "latitude": lat, "longitude": lon,
                "_enriched": {"geocoder": {
                    "name": None, "city": geocoder_city,
                    "county": "Valley", "state": "ID",
                    "country": "United States",
                    "landclass": None, "elevation_m": 1530.0,
                }},
            },
        },
    }


@pytest.fixture
def no_photon(monkeypatch):
    monkeypatch.setattr(cn, "_photon_reverse_places", lambda lat, lon: [])
    if hasattr(cn, "_H3_NEAREST_CACHE"):
        cn._H3_NEAREST_CACHE.clear()


# ---------- test (a): all fields populate -----------------------------------


def test_itd_511_work_zone_parses_with_all_fields(no_photon):
    env = _itd_work_zone_env()
    n = cn.normalize(env)
    assert n is not None
    assert n["source"] == "itd_511"
    assert n["road"] == "SH-55"
    # _norm_direction returns 'northbound' (matches state_511 convention)
    assert n["direction"] == "northbound"
    assert n["mile_start"] == 140
    assert n["mile_end"] == 145
    assert n["sub_type"] is not None
    # is_full_closure=False -> impact 'partial' matches state_511 convention
    assert n["impact"] == "partial"
    assert n["town"] == "McCall"
    # ends_at is a datetime
    assert isinstance(n["ends_at"], datetime)


def test_itd_511_work_zone_full_closure_impact(no_photon):
    env = _itd_work_zone_env(is_full_closure=True)
    n = cn.normalize(env)
    assert n["impact"] == "full_closure"


def test_itd_511_work_zone_end_date_formatting(no_photon):
    """planned_end_epoch should serialize to a datetime that the renderer
    can format consistently with state_511."""
    env = _itd_work_zone_env(planned_end_epoch=1781200000)
    n = cn.normalize(env)
    assert isinstance(n["ends_at"], datetime)
    assert n["ends_at"].tzinfo is not None  # UTC-aware


def test_itd_511_work_zone_no_end_date(no_photon):
    """planned_end_epoch == None or 0 -> ends_at is None."""
    env = _itd_work_zone_env(planned_end_epoch=None)
    n = cn.normalize(env)
    assert n["ends_at"] is None


def test_itd_511_incident_does_not_go_through_work_zone_parser(no_photon):
    """The work_zone parser should NOT be invoked for category=incident.itd_511
    -- those still route through the incident_handler. normalize() returns
    None (defer) for non-work_zone itd_511 categories."""
    env = _itd_work_zone_env()
    env["data"]["category"] = "incident.itd_511"
    n = cn.normalize(env)
    # Either None (defer) or not a work_zone dict (no road/direction/etc).
    if n is not None:
        assert "_kind" in n  # marker, not a parsed work_zone dict
