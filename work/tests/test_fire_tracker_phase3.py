"""v0.7-fire-tracker-3 tests."""
from __future__ import annotations

import json
import math
import time
import uuid

import pytest


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / f"meshai-{uuid.uuid4().hex}.sqlite")
    monkeypatch.setenv("MESHAI_DB_PATH", db_path)
    from meshai.persistence import db as pdb
    pdb.close_thread_connection()
    pdb._initialised.discard(db_path)
    from meshai.persistence import init_db
    init_db(db_path)
    yield db_path
    pdb.close_thread_connection()
    pdb._initialised.discard(db_path)


def _seed_fire(*, irwin_id, lat, lon, name="Stub Fire"):
    from meshai.persistence import get_db
    get_db().execute(
        "INSERT INTO fires(irwin_id, incident_name, lat, lon, last_event_at) "
        "VALUES (?,?,?,?,?)",
        (irwin_id, name, lat, lon, int(time.time())),
    )


def _pixel(*, lat, lon, acq_date="2026-06-06", acq_time="1200",
           frp=20.0, satellite="N20"):
    """Build a canonical FIRMS pixel dict for the LIVE ingest_hotspot_pixel
    entrypoint (mirrors tests/test_firms_native_fusion.py's `_pixel`)."""
    import datetime as _dt
    acq_epoch = int(_dt.datetime.strptime(
        f"{acq_date} {str(acq_time).zfill(4)}", "%Y-%m-%d %H%M"
    ).replace(tzinfo=_dt.timezone.utc).timestamp())
    return {"lat": lat, "lon": lon, "frp": frp, "confidence": "high",
            "brightness": 320.0, "satellite": satellite, "acq_epoch": acq_epoch}


def _ingest(pixel, *, now):
    """Feed one pixel through the LIVE native entrypoint (chore/ripout-2dii:
    the dead Central handle_firms wrapper this file used to drive through is
    gone) and return the (wire, data) of the first broadcast it produced (or
    (None, {}))."""
    from meshai.env.fire_fusion import ingest_hotspot_pixel
    broadcasts = ingest_hotspot_pixel(pixel, now=now)
    if broadcasts:
        return broadcasts[0]
    return None, {}


# Useful constant: 1 mi in latitude degrees.
_MI_PER_DEG_LAT = 69.0


def _offset_mi(lat, lon, *, north_mi=0.0, east_mi=0.0):
    """Return (lat, lon) offset by north_mi north and east_mi east."""
    dlat = north_mi / _MI_PER_DEG_LAT
    cos_lat = math.cos(math.radians(lat))
    dlon = east_mi / (_MI_PER_DEG_LAT * max(0.01, cos_lat))
    return lat + dlat, lon + dlon


# ---------------------------------------------------------------------------
# (a) Pass-close stamps perimeter_geojson.
# ---------------------------------------------------------------------------


def test_pass_close_stamps_perimeter_geojson():
    """Pass A: 6 pixels in a hex around the seeded center. First pixel
    of pass B triggers boundary close -> perimeter_geojson written for
    pass A as a closed GeoJSON Polygon."""
    from meshai.persistence import get_db

    center_lat, center_lon = 42.500, -114.500
    _seed_fire(irwin_id="ID-SPOT-001",
                 lat=center_lat, lon=center_lon, name="Hex Fire")

    # Pass A: 6 hex vertices ~0.05 mi from center; bucket 12:00 N20.
    for i in range(6):
        angle = i * math.pi / 3
        la = center_lat + 0.001 * math.sin(angle)
        lo = center_lon + 0.001 * math.cos(angle)
        _ingest(_pixel(lat=la, lon=lo, acq_time=f"12{i * 2:02d}"),
                now=1780747200 + i)

    # First pass B pixel 1 mi N. Within the 5 mi spread radius -> the
    # pixel attributes to the seeded fire -> boundary detected ->
    # _close_prev_perimeter runs. 1 mi puts us below the 1.5 mi
    # spotting threshold so this also avoids spotting noise in the
    # test (we just want perimeter_geojson to materialize).
    far_lat, far_lon = _offset_mi(center_lat, center_lon, north_mi=1.0)
    _ingest(_pixel(lat=far_lat, lon=far_lon, acq_time="1800"), now=1780768800)

    row = get_db().execute(
        "SELECT perimeter_geojson FROM fire_passes WHERE irwin_id=? "
        "ORDER BY pass_ended_at ASC LIMIT 1",
        ("ID-SPOT-001",),
    ).fetchone()
    assert row["perimeter_geojson"] is not None
    poly = json.loads(row["perimeter_geojson"])
    assert poly["type"] == "Polygon"
    ring = poly["coordinates"][0]
    # Ring is closed: first == last.
    assert ring[0] == ring[-1]
    # Convex hull of a hex has 6 vertices; the closed ring has 7 entries.
    assert len(ring) == 7


# ---------------------------------------------------------------------------
# (b) Spotting fires for pixels outside perimeter beyond threshold.
# ---------------------------------------------------------------------------


def _seed_pass_a_hex_then_close(*, irwin_id, center_lat, center_lon,
                                  start_now=1780747200):
    """Helper: seed a fire + 6 hex-vertex pass A pixels. Caller follows up
    with a pass B pixel to trigger boundary close + perimeter write."""
    _seed_fire(irwin_id=irwin_id, lat=center_lat, lon=center_lon,
                 name=irwin_id)
    for i in range(6):
        angle = i * math.pi / 3
        # Hex ~0.5 mi radius (well inside the 5 mi spread radius).
        la = center_lat + (0.5 / _MI_PER_DEG_LAT) * math.sin(angle)
        cos_lat = math.cos(math.radians(center_lat))
        lo = center_lon + (0.5 / (_MI_PER_DEG_LAT * cos_lat)) * math.cos(angle)
        _ingest(_pixel(lat=la, lon=lo, acq_time=f"12{i * 2:02d}"),
                now=start_now + i)


def test_pixel_2mi_ne_of_perimeter_emits_spotting():
    """Pass B pixel 2 mi NE of pass A's perimeter centroid fires
    wildfire_spotting with the correct distance + direction."""
    from meshai.persistence import get_db

    center_lat, center_lon = 43.000, -115.000
    _seed_pass_a_hex_then_close(irwin_id="ID-SPOT-002",
                                 center_lat=center_lat,
                                 center_lon=center_lon)

    # First pass B pixel 2 mi NE.
    sp_lat, sp_lon = _offset_mi(
        center_lat, center_lon,
        north_mi=2.0 / math.sqrt(2), east_mi=2.0 / math.sqrt(2),
    )
    wire, data = _ingest(_pixel(lat=sp_lat, lon=sp_lon, acq_time="1800"),
                         now=1780768800)
    assert wire is not None
    assert wire.startswith("🔥 Possible spotting ")
    assert "NE of ID-SPOT-002 perimeter" in wire
    # Distance should be ~2 mi from perimeter -- vertex closest is ~1.5
    # mi from center (2.0 - 0.5 hex radius).
    # Allow a generous window: 1.0..2.5 mi.
    import re
    m = re.search(r"spotting (\d+\.\d+) mi", wire)
    assert m, f"distance not found in wire: {wire!r}"
    dist = float(m.group(1))
    assert 1.0 <= dist <= 2.5, f"distance {dist} out of expected band"
    # The data dict is tagged (category/severity) for the dispatcher.
    # This is verified here so future regressions don\'t silently break
    # routing; verification REPORTS only quote the wire (per the
    # feedback-no-event-metadata-in-reports memory rule).
    assert data["category"] == "wildfire_spotting"
    assert data["_severity_override"] == "immediate"

    # Latch stamped.
    fire = get_db().execute(
        "SELECT last_spotting_broadcast_at FROM fires WHERE irwin_id=?",
        ("ID-SPOT-002",),
    ).fetchone()
    assert fire["last_spotting_broadcast_at"] == 1780768800.0


# ---------------------------------------------------------------------------
# (c) Pixel inside perimeter does not fire spotting.
# ---------------------------------------------------------------------------


def test_pixel_inside_perimeter_no_spotting():
    center_lat, center_lon = 43.500, -114.500
    _seed_pass_a_hex_then_close(irwin_id="ID-SPOT-003",
                                 center_lat=center_lat,
                                 center_lon=center_lon)

    # Close the perimeter with one boundary-only pixel ~10 mi away.
    far_lat, far_lon = _offset_mi(center_lat, center_lon, north_mi=10.0)
    _ingest(_pixel(lat=far_lat, lon=far_lon, acq_time="1800"), now=1780768800)

    # Now ingest a pixel 0.1 mi NE of center -- well inside the 0.5 mi
    # radius hex.
    inside_lat, inside_lon = _offset_mi(
        center_lat, center_lon, north_mi=0.05, east_mi=0.05,
    )
    wire, data = _ingest(_pixel(lat=inside_lat, lon=inside_lon,
                                acq_time="1810"), now=1780768900)
    assert wire is None or "spotting" not in (wire or "")
    assert data.get("category") != "wildfire_spotting"


# ---------------------------------------------------------------------------
# (d) Cooldown semantics: second spotting within 1h is suppressed.
# ---------------------------------------------------------------------------


def test_second_spotting_within_cooldown_suppressed():
    center_lat, center_lon = 44.000, -116.000
    _seed_pass_a_hex_then_close(irwin_id="ID-SPOT-004",
                                 center_lat=center_lat,
                                 center_lon=center_lon)

    # First spotting pixel 2 mi N.
    sp1_lat, sp1_lon = _offset_mi(center_lat, center_lon, north_mi=2.0)
    wire1, _data1 = _ingest(_pixel(lat=sp1_lat, lon=sp1_lon, acq_time="1800"),
                            now=1780768800)
    assert wire1 is not None and "spotting" in wire1

    # Second spotting candidate 30 min later, 2 mi SE (different
    # direction, still beyond perimeter). Within 1h cooldown -> suppressed.
    sp2_lat, sp2_lon = _offset_mi(
        center_lat, center_lon,
        north_mi=-2.0 / math.sqrt(2), east_mi=2.0 / math.sqrt(2),
    )
    wire2, _data2 = _ingest(_pixel(lat=sp2_lat, lon=sp2_lon, acq_time="1830"),
                            now=1780768800 + 1800)
    assert wire2 is None, f"second spotting in cooldown should suppress: {wire2}"


# ---------------------------------------------------------------------------
# (e) Past-cooldown spotting fires again.
# ---------------------------------------------------------------------------


def test_spotting_refires_after_cooldown():
    center_lat, center_lon = 44.500, -116.500
    _seed_pass_a_hex_then_close(irwin_id="ID-SPOT-005",
                                 center_lat=center_lat,
                                 center_lon=center_lon)

    sp1_lat, sp1_lon = _offset_mi(center_lat, center_lon, north_mi=2.0)
    wire1, _data1 = _ingest(_pixel(lat=sp1_lat, lon=sp1_lon, acq_time="1800"),
                            now=1780768800)
    assert wire1 is not None

    # 70 minutes later -> past the 1h cooldown -> next spotting fires.
    sp2_lat, sp2_lon = _offset_mi(center_lat, center_lon, north_mi=-2.0)
    wire2, _data2 = _ingest(_pixel(lat=sp2_lat, lon=sp2_lon, acq_time="1910"),
                            now=1780768800 + 70 * 60)
    assert wire2 is not None and "spotting" in wire2


# ---------------------------------------------------------------------------
# (f) Helper sanity.
# ---------------------------------------------------------------------------


def test_convex_hull_basic():
    from meshai.env.fire_fusion import _convex_hull
    pts = [(0, 0), (1, 0), (1, 1), (0, 1), (0.5, 0.5)]
    hull = _convex_hull(pts)
    assert (0.5, 0.5) not in hull
    assert (0, 0) in hull and (1, 1) in hull


def test_point_in_polygon_basic():
    from meshai.env.fire_fusion import _point_in_polygon
    square = [(0, 0), (0, 10), (10, 10), (10, 0)]  # (lat, lon)
    assert _point_in_polygon((5, 5), square) is True
    assert _point_in_polygon((15, 5), square) is False
    assert _point_in_polygon((-1, 5), square) is False


def test_geojson_round_trip_via_hull():
    """Hull -> GeoJSON -> parse -> ring shape sane."""
    from meshai.env.fire_fusion import _convex_hull, _hull_to_geojson
    hull = _convex_hull([(0, 0), (1, 0), (0, 1), (1, 1)])
    raw = _hull_to_geojson(hull)
    parsed = json.loads(raw)
    assert parsed["type"] == "Polygon"
    ring = parsed["coordinates"][0]
    assert ring[0] == ring[-1]
    # Vertices stored as [lon, lat] per GeoJSON RFC 7946.
    for entry in ring:
        assert isinstance(entry, list) and len(entry) == 2


# ---------------------------------------------------------------------------
# (g) Adapter_config + category registration.
# ---------------------------------------------------------------------------


def test_adapter_config_seeds_spotting_keys():
    from meshai.persistence import get_db
    rows = {
        (r["adapter"], r["key"]): r["default_json"]
        for r in get_db().execute(
            "SELECT adapter, key, default_json FROM adapter_config "
            "WHERE (adapter, key) IN ( "
            " ('fires','spotting_distance_threshold_mi'), "
            " ('fires','spotting_cooldown_seconds') )"
        )
    }
    assert rows[("fires", "spotting_distance_threshold_mi")] == "1.5"
    assert rows[("fires", "spotting_cooldown_seconds")] == "3600"


def test_wildfire_spotting_category_registered():
    from meshai.notifications.categories import ALERT_CATEGORIES
    e = ALERT_CATEGORIES["wildfire_spotting"]
    assert e["default_severity"] == "immediate"
    assert e["toggle"] == "fire"
