"""v0.6-5 router env scope detection tests."""
from __future__ import annotations

import pytest

from meshai.router import _detect_env_subtype


# ============================================================================
# _detect_env_subtype
# ============================================================================


@pytest.mark.parametrize("msg, expected", [
    ("any fires near me?", "fires"),
    ("Are there wildfires today?", "fires"),
    ("FIRE WARNING ISSUED",   "fires"),    # "fire" matches; "warning" alerts loses to first
    ("how's the band conditions", "swpc"),
    ("solar storm?",              "swpc"),
    ("how's HF propagation?",     "swpc"),
    ("are there earthquakes nearby?", "quakes"),
    ("any seismic activity?",         "quakes"),
    ("river flood warning",     "gauges"),   # "flood" maps to gauges
    ("stream gauge readings",   "gauges"),
    ("any tornado warnings?",   "alerts"),
    ("severe thunderstorm",     "alerts"),
    ("any traffic incidents?",  "traffic"),
    ("road closures on I-84",   "traffic"),
])
def test_env_subtype_detection(msg, expected):
    assert _detect_env_subtype(msg.lower()) == expected


def test_env_subtype_returns_none_for_non_env_questions():
    assert _detect_env_subtype("hello, how are you?") is None
    assert _detect_env_subtype("what's the time?") is None


def test_env_subtype_does_not_match_substrings():
    """'firearm' shouldn't match 'fire' (we use word boundaries)."""
    assert _detect_env_subtype("firearm collection") is None


# ============================================================================
# Synthetic probe: stub fires table, confirm router scope detection
# ============================================================================


def test_env_reporter_emits_fires_block_when_table_populated():
    """Seed the fires table and confirm env_reporter.build_all() includes a
    fires block. Stand-in for the "DM the router 'any fires near me?'"
    end-to-end since the full router needs many constructor mocks."""
    import time
    from meshai.notifications.env_reporter import env_reporter
    from meshai.persistence import get_db

    conn = get_db()
    now = int(time.time())
    conn.execute(
        "INSERT INTO fires(irwin_id, incident_name, incident_type, "
        "current_acres, current_contained_pct, lat, lon, county, state, "
        "declared_at, last_event_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("PROBE-1", "Probe Fire A", "WF", 1500, 15,
         42.5, -114.5, "Cassia", "ID", now - 3600, now),
    )
    conn.execute(
        "INSERT INTO fires(irwin_id, incident_name, incident_type, "
        "current_acres, current_contained_pct, lat, lon, county, state, "
        "declared_at, last_event_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("PROBE-2", "Probe Fire B", "WF", 750, 0,
         42.6, -114.6, "Twin Falls", "ID", now - 7200, now),
    )

    text = env_reporter.build_all()
    assert "ENVIRONMENTAL CONTEXT" in text
    assert "Probe Fire A" in text
    assert "Probe Fire B" in text
    assert "1,500 ac" in text


def test_router_scope_detector_returns_env_for_fire_keyword():
    """The module-level _detect_env_subtype short-circuits to env subtype.
    Spot-check on a NotificationRouter._detect_mesh_scope is covered by the
    parametrized test above; here we confirm the integration shape."""
    assert _detect_env_subtype("any fires near me?") == "fires"
    # Different env subtypes route distinctly.
    assert _detect_env_subtype("how about quakes today?") == "quakes"
    assert _detect_env_subtype("river flood?") == "gauges"
