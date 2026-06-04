"""Tests for the work_zone mesh renderer."""

from datetime import datetime, timedelta

import pytest

from meshai.notifications.renderers.work_zone import format_work_zone_mesh


def _bytelen(s: str) -> int:
    return len(s.encode("utf-8"))


# ---------- canonical / fully-populated case ------------------------------


def test_all_fields_present_produces_canonical_format():
    n = {
        "source": "state_511_atis", "road": "SH-3", "direction": "both",
        "mile_start": 60, "mile_end": None, "description": "...",
        "sub_type": "construction work", "impact": "partial",
        "ends_at": datetime(2026, 6, 2, 17, 0),
        "town": "Plummer", "distance_mi": 8, "bearing": "N",
    }
    out = format_work_zone_mesh(n, now=datetime(2026, 6, 1, 14, 0))
    assert out.startswith("🚧 SH-3 @ mile 60")
    assert "8 mi N of Plummer" in out
    assert "both directions" in out
    assert "construction work" in out
    assert _bytelen(out) <= 80


# ---------- segment-drop progression --------------------------------------


def test_no_mile_drops_at_mile_segment():
    n = {"source": "state_511_atis", "road": "W Prairie Ave", "direction": "both",
         "mile_start": None, "mile_end": None, "sub_type": "paving",
         "impact": "partial", "town": "Coeur d'Alene", "distance_mi": 5, "bearing": "E",
         "ends_at": None, "description": ""}
    out = format_work_zone_mesh(n)
    assert "@ mile" not in out
    assert "W Prairie Ave" in out
    assert "5 mi E of Coeur d'Alene" in out


def test_no_town_drops_distance_segment():
    n = {"source": "state_511_atis", "road": "SH-55", "direction": "both",
         "mile_start": 17, "mile_end": 18, "sub_type": "paving",
         "impact": "partial", "town": None, "distance_mi": None, "bearing": None,
         "ends_at": None, "description": ""}
    out = format_work_zone_mesh(n)
    assert " mi " not in out
    assert " of " not in out
    assert "@ mile 17–18" in out


def test_no_ends_drops_ends_suffix():
    n = {"source": "state_511_atis", "road": "I-86", "direction": "both",
         "mile_start": 58, "mile_end": 59, "sub_type": "bridge maintenance",
         "impact": "partial", "town": "Pocatello", "distance_mi": 15, "bearing": "W",
         "ends_at": None, "description": ""}
    out = format_work_zone_mesh(n)
    assert ", ends" not in out


def test_unknown_direction_drops_direction_phrase():
    n = {"source": "state_511_atis", "road": "SH-36", "direction": "unknown",
         "mile_start": 17, "mile_end": 18, "sub_type": "paving",
         "impact": "partial", "town": None, "distance_mi": None, "bearing": None,
         "ends_at": None, "description": ""}
    out = format_work_zone_mesh(n)
    assert "unknown" not in out.lower().split(":")[-1]   # no 'unknown' in tail


def test_full_closure_promoted():
    n = {"source": "state_511_atis", "road": "I-15", "direction": "southbound",
         "mile_start": None, "mile_end": None, "sub_type": "road construction",
         "impact": "full_closure", "town": None, "distance_mi": None, "bearing": None,
         "ends_at": None, "description": ""}
    out = format_work_zone_mesh(n)
    assert "all lanes closed" in out


# ---------- byte budget ---------------------------------------------------


def test_byte_length_under_80_for_canonical():
    n = {"source": "state_511_atis", "road": "SH-3", "direction": "both",
         "mile_start": 60, "mile_end": None, "sub_type": "construction work",
         "impact": "partial", "town": "Plummer", "distance_mi": 8, "bearing": "N",
         "ends_at": datetime(2026, 6, 2, 17, 0), "description": ""}
    out = format_work_zone_mesh(n, now=datetime(2026, 6, 1, 14, 0))
    assert _bytelen(out) <= 80


def test_byte_length_under_80_with_long_road_name():
    n = {"source": "state_511_atis",
         "road": "SCIENCE CENTER DR / E ANDERSON ST / N THIRD WAY",
         "direction": "both",
         "mile_start": 100, "mile_end": 200, "sub_type": "construction work",
         "impact": "partial", "town": "Idaho Falls", "distance_mi": 12, "bearing": "SE",
         "ends_at": datetime(2026, 9, 16, 1, 0), "description": ""}
    out = format_work_zone_mesh(n, now=datetime(2026, 6, 1, 14, 0))
    assert _bytelen(out) <= 80, f"over budget: {len(out.encode('utf-8'))} = {out!r}"


def test_emoji_counts_as_4_bytes():
    # 🚧 is U+1F6A7 → 4 bytes in UTF-8.
    assert _bytelen("🚧") == 4


def test_extreme_road_name_truncated():
    # Force the renderer into the truncate-road last-resort branch.
    long_road = "VERY-LONG-ROAD-NAME-" * 10
    n = {"source": "state_511_atis", "road": long_road, "direction": None,
         "mile_start": None, "mile_end": None, "sub_type": None,
         "impact": "partial", "town": None, "distance_mi": None, "bearing": None,
         "ends_at": None, "description": ""}
    out = format_work_zone_mesh(n)
    assert _bytelen(out) <= 80
    assert out.startswith("🚧 ")
    assert "…" in out or _bytelen(long_road) <= 80   # truncated with ellipsis


# ---------- ends_at relative-time formatting ------------------------------


def test_ends_today_format():
    now = datetime(2026, 6, 1, 9, 0)
    ends = datetime(2026, 6, 1, 18, 0)
    n = {"source": "state_511_atis", "road": "X", "direction": None,
         "mile_start": None, "mile_end": None, "sub_type": None, "impact": "partial",
         "town": None, "distance_mi": None, "bearing": None,
         "ends_at": ends, "description": ""}
    out = format_work_zone_mesh(n, now=now)
    assert "today" in out and "6pm" in out


def test_ends_within_week_uses_weekday():
    now = datetime(2026, 6, 1, 9, 0)   # Monday
    ends = datetime(2026, 6, 5, 16, 30)
    n = {"source": "state_511_atis", "road": "X", "direction": None,
         "mile_start": None, "mile_end": None, "sub_type": None, "impact": "partial",
         "town": None, "distance_mi": None, "bearing": None,
         "ends_at": ends, "description": ""}
    out = format_work_zone_mesh(n, now=now)
    assert "Fri" in out
    assert "4:30pm" in out


def test_ends_past_drops_segment():
    now = datetime(2026, 6, 10, 9, 0)
    ends = datetime(2026, 5, 5, 17, 0)  # already past
    n = {"source": "state_511_atis", "road": "X", "direction": None,
         "mile_start": None, "mile_end": None, "sub_type": None, "impact": "partial",
         "town": None, "distance_mi": None, "bearing": None,
         "ends_at": ends, "description": ""}
    out = format_work_zone_mesh(n, now=now)
    assert ", ends" not in out


# ---------- v0.5.8 distance < 1 mi → "near X" -----------------------------


def test_distance_zero_drops_to_near_only():
    n = {"source": "state_511_atis", "road": "SH-55", "direction": "both",
         "mile_start": None, "mile_end": None, "sub_type": "emergency repairs",
         "impact": "partial", "town": "McCall", "distance_mi": 0, "bearing": "S",
         "ends_at": None, "description": ""}
    out = format_work_zone_mesh(n)
    assert "near McCall" in out
    assert "0 mi" not in out
    assert "S of McCall" not in out


def test_distance_one_keeps_bearing_segment():
    n = {"source": "state_511_atis", "road": "SH-41", "direction": "southbound",
         "mile_start": None, "mile_end": None, "sub_type": "utility work",
         "impact": "partial", "town": "Rathdrum", "distance_mi": 1, "bearing": "S",
         "ends_at": None, "description": ""}
    out = format_work_zone_mesh(n)
    assert "1 mi S of Rathdrum" in out


# ---------- v0.5.8 no-road fallback (leads with town) ---------------------


def test_no_road_leads_with_town_distance():
    n = {"source": "state_511_atis", "road": None, "direction": "southbound",
         "mile_start": None, "mile_end": None, "sub_type": "ramp work",
         "impact": "partial", "town": "Stanley", "distance_mi": 3, "bearing": "NE",
         "ends_at": None, "description": ""}
    out = format_work_zone_mesh(n)
    # Head is the distance/town form, not a placeholder.
    assert out.startswith("🚧 3 mi NE of Stanley")
    assert "Road event" not in out


def test_no_road_no_town_falls_back_to_placeholder():
    n = {"source": "state_511_atis", "road": None, "direction": "both",
         "mile_start": None, "mile_end": None, "sub_type": None,
         "impact": "partial", "town": None, "distance_mi": None, "bearing": None,
         "ends_at": None, "description": ""}
    out = format_work_zone_mesh(n)
    # Placeholder is acceptable when we have literally nothing.
    assert out.startswith("🚧 ")
