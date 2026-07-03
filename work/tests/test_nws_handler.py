"""Tests for v0.5.10 NWS handler."""
import pytest

from meshai.central.nws_handler import handle_nws, _emoji_for_event, _render
from meshai.persistence import close_thread_connection, init_db
from meshai.persistence import db as persistence_db


@pytest.fixture
def mem_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "nws-test.sqlite")
    monkeypatch.setenv("MESHAI_DB_PATH", db_path)
    persistence_db._initialised.clear()
    close_thread_connection()
    conn = init_db()
    yield conn
    close_thread_connection()
    persistence_db._initialised.discard(db_path)


def _nws_env(*, cap_id="urn:oid:test.001",
              event="Severe Thunderstorm Warning",
              severity_str="Severe",
              area_desc="Twin Falls County",
              county="Twin Falls", state="ID",
              expires="2026-06-05T03:00:00Z",
              msg_type=None,
              lat=42.500, lon=-114.460,
              geocoder_city=None,
              category="wx.alert.severe_thunderstorm_warning"):
    return {
        "id": cap_id, "subject": "central.wx.alert.us.id",
        "data": {
            "id": cap_id, "adapter": "nws", "category": category,
            "severity": 2,
            "geo": {"centroid": [lon, lat], "primary_region": "US-ID"},
            "data": {
                "id": cap_id, "@type": "wx:Alert",
                "event": event, "severity": severity_str,
                "areaDesc": area_desc, "msgType": msg_type or "Alert",
                "headline": f"{event} for {area_desc}",
                "description": "Storm details.",
                "expires": expires,
                "_enriched": {"geocoder": {"city": geocoder_city,
                                              "county": county, "state": state}},
            },
        },
    }


def _commit(data, t):
    data["_on_broadcast_committed"](float(t))


# ---- severity gate ----


def test_severe_thunderstorm_warning_broadcasts(mem_db):
    env = _nws_env(severity_str="Severe", event="Severe Thunderstorm Warning")
    data = {}
    wire = handle_nws(env, env["subject"], data=data, now=1_000_000)
    assert wire is not None
    assert wire.startswith("🌩️")
    assert "Severe Thunderstorm Warning" in wire


def test_extreme_emergency_broadcasts(mem_db):
    env = _nws_env(severity_str="Extreme", event="Tornado Warning",
                    category="wx.alert.tornado_warning")
    data = {}
    wire = handle_nws(env, env["subject"], data=data, now=1_000_000)
    assert wire is not None
    assert wire.startswith("🌪️")


def test_special_weather_statement_passes_through(mem_db):
    # GATE A removed: Minor/SWS is no longer dropped on CAP severity alone.
    env = _nws_env(severity_str="Minor", event="Special Weather Statement",
                    category="wx.alert.special_weather_statement")
    data = {}
    wire = handle_nws(env, env["subject"], data=data, now=1_000_000)
    assert wire is not None, "SWS should now pass through (GATE A removed)"
    assert "Special Weather Statement" in wire
    # Row inserted in nws_alerts (not a warning category → no override).
    n_rows = mem_db.execute("SELECT COUNT(*) AS n FROM nws_alerts").fetchone()["n"]
    assert n_rows == 1
    # _severity_override should NOT be set for a non-warning category.
    assert data.get("_severity_override") is None


def test_watch_severity_moderate_passes_through(mem_db):
    # GATE A removed: Moderate watches now pass through; dispatcher threshold governs.
    env = _nws_env(severity_str="Moderate", event="Severe Thunderstorm Watch",
                    category="wx.alert.severe_thunderstorm_watch")
    data = {}
    wire = handle_nws(env, env["subject"], data=data, now=1_000_000)
    assert wire is not None, "Moderate watch should now pass through (GATE A removed)"
    assert "Severe Thunderstorm Watch" in wire
    # Watches end in _watch, not _warning — no severity override.
    assert data.get("_severity_override") is None


# ---- emoji map ----


@pytest.mark.parametrize("event_type, expected_emoji", [
    ("Severe Thunderstorm Warning", "🌩️"),
    ("Tornado Warning",             "🌪️"),
    ("Flash Flood Warning",         "🌊"),
    ("Flood Warning",               "🌊"),
    ("Winter Storm Warning",        "❄️"),
    ("Blizzard Warning",            "❄️"),
    ("Excessive Heat Warning",      "🌡️"),
    ("High Wind Warning",           "🌬️"),
    ("Red Flag Warning",            "🔥"),
    ("Fire Weather Watch",          "🔥"),
    ("Air Quality Alert",           "😷"),
    ("Freeze Warning",              "🥶"),
    ("Coastal Flood Warning",       "🌊"),
    ("(some other warning)",        "⚠️"),
])
def test_emoji_map(event_type, expected_emoji):
    assert _emoji_for_event(event_type) == expected_emoji


# ---- tombstone ----


def test_cancel_msgType_tombstone_skipped(mem_db):
    env = _nws_env(severity_str="Severe", event="Severe Thunderstorm Warning",
                    msg_type="Cancel")
    data = {}
    wire = handle_nws(env, env["subject"], data=data, now=1_000_000)
    assert wire is None
    n_log = mem_db.execute(
        "SELECT COUNT(*) AS n FROM event_log WHERE source='nws' AND handled=0"
    ).fetchone()["n"]
    assert n_log == 1


def test_expire_msgType_tombstone_skipped(mem_db):
    env = _nws_env(severity_str="Severe", event="Tornado Warning",
                    msg_type="Expire")
    wire = handle_nws(env, env["subject"], data={}, now=1_000_000)
    assert wire is None


# ---- per-CAP-id dedup ----


def test_per_cap_id_dedup_no_reissue(mem_db):
    env = _nws_env(severity_str="Severe")
    data1 = {}
    wire1 = handle_nws(env, env["subject"], data=data1, now=1_000_000)
    assert wire1 is not None
    _commit(data1, 1_000_001)

    # Same CAP id republishes (e.g. headline update). Should NOT re-broadcast.
    data2 = {}
    wire2 = handle_nws(env, env["subject"], data=data2, now=1_000_300)
    assert wire2 is None


# ---- area_desc fallback ----


def test_area_desc_used_when_geocoder_city_missing(mem_db):
    env = _nws_env(severity_str="Severe", area_desc="Twin Falls County",
                    geocoder_city=None)
    wire = handle_nws(env, env["subject"], data={}, now=1_000_000)
    assert "Twin Falls" in wire


def test_geocoder_city_preferred_over_area_desc(mem_db):
    env = _nws_env(severity_str="Severe", area_desc="Twin Falls County",
                    geocoder_city="Twin Falls")
    wire = handle_nws(env, env["subject"], data={}, now=1_000_000)
    assert "Twin Falls" in wire   # either source serves the same anchor


# ---- commit callback ----


def test_commit_callback_updates_last_broadcast(mem_db):
    env = _nws_env(severity_str="Severe")
    data = {}
    handle_nws(env, env["subject"], data=data, now=1_000_000)
    fr_pre = mem_db.execute(
        "SELECT last_broadcast_at FROM nws_alerts").fetchone()
    assert fr_pre["last_broadcast_at"] is None
    _commit(data, 1_000_001)
    fr_post = mem_db.execute(
        "SELECT last_broadcast_at FROM nws_alerts").fetchone()
    assert fr_post["last_broadcast_at"] == 1_000_001
    # event_log row flipped to handled=1.
    el = mem_db.execute(
        "SELECT handled FROM event_log WHERE source='nws' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert el["handled"] == 1


def test_wire_includes_event_and_headline(mem_db):
    env = _nws_env(severity_str="Severe", lat=42.500, lon=-114.460)
    wire = handle_nws(env, env["subject"], data={}, now=1_000_000)
    assert "Severe Thunderstorm Warning" in wire
    assert "Twin Falls County" in wire

# ---- warning → immediate promotion (Step 2) ----


def test_warning_category_sets_severity_override_immediate(mem_db):
    """A *_warning category sets data[_severity_override]='immediate'."""
    env = _nws_env(severity_str="Severe", event="Severe Thunderstorm Warning",
                    category="wx.alert.severe_thunderstorm_warning")
    data = {}
    wire = handle_nws(env, env["subject"], data=data, now=1_000_000)
    assert wire is not None
    assert data.get("_severity_override") == "immediate"


def test_tornado_warning_dotted_category_sets_severity_override(mem_db):
    """A category ending in .warning also sets _severity_override='immediate'."""
    env = _nws_env(severity_str="Extreme", event="Tornado Warning",
                    category="wx.alert.tornado_warning")
    # Override the data.data.severity to use dotted-style category check
    env["data"]["category"] = "wx.alert.tornado.warning"
    env["data"]["data"]["severity"] = "Extreme"
    data = {}
    wire = handle_nws(env, env["subject"], data=data, now=2_000_000)
    assert wire is not None
    assert data.get("_severity_override") == "immediate"


def test_non_warning_category_no_severity_override(mem_db):
    """A non-warning category (watch, advisory, statement) leaves no override."""
    env = _nws_env(severity_str="Severe", event="Severe Thunderstorm Watch",
                    category="wx.alert.severe_thunderstorm_watch")
    data = {}
    wire = handle_nws(env, env["subject"], data=data, now=3_000_000)
    assert wire is not None
    assert "_severity_override" not in data


# ---- packet-budget enforcement ----


def test_svr_long_locations_path_sampled(mem_db):
    """SVR with a long town list: render must fit in 200 chars, and the town
    list must be represented as a PATH SAMPLE (first -> middle -> last) rather
    than a tail-drop. The old bug dropped the final town ('Shoshone')."""
    # Long list; first town "Buhl", last town "and Shoshone" (exercises the
    # leading-"and " strip on the tail element).
    long_locations = (
        "Buhl, Eden, Hazelton, Murtaugh, Richfield, Dietrich, "
        "Gooding, Hagerman, Wendell, and Shoshone"
    )
    description = (
        "HAZARD...Damaging winds to 60 mph and quarter-size hail.\n\n"
        f"Locations impacted include...{long_locations}"
    )
    d = {
        "eventCode": {"SAME": ["SVR"]},
        "certainty": "Observed",
        "parameters": {
            "maxWindGust": ["60 MPH"],
            "maxHailSize": ["1.00"],
            # 254 DEG 35 KT -> "Moving W 40 mph"
            "eventMotionDescription": ["2200000T254DEG...35KT 42.5,-114.5"],
        },
        "description": description,
    }
    rendered = _render(
        event_type="Severe Thunderstorm Warning",
        area_desc="Twin Falls County",
        geocoder_city=None,
        county="Twin Falls",
        state="ID",
        expires_epoch=1_751_400_000,
        lat=42.5,
        lon=-114.46,
        now=1_751_400_000,
        d=d,
    )

    # (a) fits in one mesh packet (budget is now the 140-char LoRa max)
    assert len(rendered) <= 140, (
        f"rendered is {len(rendered)} chars (expected <= 140):\n{rendered!r}"
    )

    # (b) all data-point categories present, hazard wording TIGHTENED
    assert "Severe Thunderstorm Warning" in rendered, "event type missing"
    assert "Until" in rendered, "expiry time segment missing"
    assert "Twin Falls County" in rendered, "area missing"
    assert "60mph winds" in rendered, "wind hazard not tightened to '60mph winds'"
    assert '1" hail' in rendered, "hail hazard not rendered as numeric inches"
    assert "radar" in rendered, "certainty not collapsed to 'radar'"
    assert "Moving" in rendered, "motion segment missing"

    # (c) path-sampling applied (arrow) with the soonest-impact town retained.
    # At the 140 budget the farthest-along town may be trimmed by the final
    # backstop; the hard cap wins over endpoint preservation.
    assert "→" in rendered, "no arrow -> not path-sampled"
    assert "Buhl" in rendered, "first (soonest-impact) town missing"


def test_svr_short_locations_shown_in_full(mem_db):
    """Short town list that fits in one packet: show the FULL comma-joined
    list, and never emit the path-sample arrow."""
    short_locations = "Buhl, Eden, and Hazelton"
    description = (
        "HAZARD...Damaging winds to 60 mph and quarter-size hail.\n\n"
        f"Locations impacted include...{short_locations}"
    )
    d = {
        "eventCode": {"SAME": ["SVR"]},
        "certainty": "Observed",
        "parameters": {
            "maxWindGust": ["60 MPH"],
            "maxHailSize": ["1.00"],
            "eventMotionDescription": ["2200000T254DEG...35KT 42.5,-114.5"],
        },
        "description": description,
    }
    rendered = _render(
        event_type="Severe Thunderstorm Warning",
        area_desc="Twin Falls County",
        geocoder_city=None,
        county="Twin Falls",
        state="ID",
        expires_epoch=1_751_400_000,
        lat=42.5,
        lon=-114.46,
        now=1_751_400_000,
        d=d,
    )

    assert len(rendered) <= 140
    assert "→" not in rendered, "short list should not be path-sampled"
    assert "Buhl, Eden, Hazelton" in rendered, "full comma-joined list expected"
    # Hazard wording is tightened even on the short-list path.
    assert "60mph winds" in rendered
    assert '1" hail' in rendered
    assert "radar" in rendered


def test_svr_worst_case_fits_140(mem_db):
    """Pathologically long SVR payload: the final wire MUST fit 140 chars while
    still carrying event name, area, time, tightened hazard, and >=1 town."""
    long_locations = (
        "Buhl, Eden, Hazelton, Murtaugh, Richfield, Dietrich, Gooding, "
        "Hagerman, Wendell, Jerome, Kimberly, Hansen, Filer, and Shoshone"
    )
    description = (
        "HAZARD...Damaging winds to 70 mph and golf ball size hail.\n\n"
        f"Locations impacted include...{long_locations}"
    )
    d = {
        "eventCode": {"SAME": ["SVR"]},
        "certainty": "Observed",
        "parameters": {
            "maxWindGust": ["70 MPH"],
            "maxHailSize": ["1.75"],
            "eventMotionDescription": ["2200000T254DEG...35KT 42.5,-114.5"],
        },
        "description": description,
    }
    rendered = _render(
        event_type="Severe Thunderstorm Warning",
        area_desc="Twin Falls County",
        geocoder_city=None, county="Twin Falls", state="ID",
        expires_epoch=1_751_400_000, lat=42.5, lon=-114.46,
        now=1_751_400_000, d=d,
    )
    assert len(rendered) <= 140, f"{len(rendered)} chars:\n{rendered!r}"
    assert "Severe Thunderstorm Warning" in rendered   # event name
    assert "Twin Falls County" in rendered              # area
    assert "Until" in rendered                          # time
    assert "70mph winds" in rendered                    # tightened hazard (wind)
    assert '1.75" hail' in rendered                     # golf ball -> 1.75"
    assert "Buhl" in rendered                           # >=1 town present


# ---- no dangling "— …" across ALL product types ----


def _assert_no_dangling_separator(rendered: str):
    """The L4 motion/locations line must never end in a stray separator:
    no trailing '—…', '— …', or a bare '—'. Either a real location list
    follows the em-dash, or the em-dash (and its locations) are absent."""
    for line in rendered.splitlines():
        stripped = line.rstrip()
        assert not stripped.endswith("—…"), f"dangling '—…': {line!r}"
        assert not stripped.endswith("— …"), f"dangling '— …': {line!r}"
        assert not stripped.endswith("—"), f"bare trailing '—': {line!r}"
        # And the "— …" fragment must not appear mid-line either.
        assert "—…" not in stripped, f"'—…' fragment: {line!r}"
        assert "— …" not in stripped, f"'— …' fragment: {line!r}"


def test_sps_worst_case_tightened_and_no_dangling(mem_db):
    """The reported live-log bug: a Special Weather Statement (SPS) with wind
    gusts + motion + a long town list previously collapsed L4 to
    'Moving SW 24 mph —…' (all towns lost, dangling separator). After the fix:
    hazard is tightened, output fits 140, and L4 is either
    'Moving … — <towns>' or 'Moving …' — never a trailing '—…'."""
    long_locations = (
        "Twin Falls, Kimberly, Filer, Buhl, Hansen, Murtaugh, Hollister, "
        "Eden, Hazelton, and Rogerson"
    )
    description = (
        "HAZARD...Wind gusts in excess of 45 mph and pea size hail.\n\n"
        "SOURCE...Radar indicated.\n\n"
        f"Locations impacted include...{long_locations}"
    )
    d = {
        "eventCode": {"SAME": ["SPS"]},
        "certainty": "Observed",
        "parameters": {
            # 225 DEG 21 KT -> "Moving SW 24 mph"
            "eventMotionDescription": ["2200000T225DEG...21KT 42.5,-114.5"],
        },
        "description": description,
    }
    rendered = _render(
        event_type="Special Weather Statement", area_desc="Twin Falls County",
        geocoder_city=None, county="Twin Falls", state="ID",
        expires_epoch=1_751_400_000, lat=42.5, lon=-114.46,
        now=1_751_400_000, d=d,
    )
    assert len(rendered) <= 140, f"{len(rendered)} chars:\n{rendered!r}"
    assert "Special Weather Statement" in rendered
    # (a) hazard tightened: "Wind gusts in excess of 45 mph" -> "45mph gusts",
    #     "pea size hail" -> '0.25" hail'; filler dropped.
    assert "45mph gusts" in rendered, f"wind not tightened:\n{rendered!r}"
    assert '0.25" hail' in rendered, f"hail not numeric:\n{rendered!r}"
    assert "in excess of" not in rendered, "filler 'in excess of' survived"
    assert "· observed" in rendered, "certainty not collapsed"
    # (b) NEVER a dangling separator.
    _assert_no_dangling_separator(rendered)
    # (c) the motion line, when present, either carries a town or stands alone.
    last = rendered.splitlines()[-1]
    if last.startswith("Moving"):
        assert last == "Moving SW 24 mph" or " — " in last, (
            f"L4 neither motion-only nor motion+towns:\n{last!r}")
        if " — " in last:
            # A real town must follow the em-dash.
            tail = last.split(" — ", 1)[1].strip()
            assert tail and tail != "…", f"empty tail after em-dash:\n{last!r}"


def test_wsw_hazard_tightened_and_no_dangling(mem_db):
    """Winter Weather product (WSW SAME code): wind-gust hazard is tightened and
    no dangling '—…' can appear."""
    long_locations = (
        "Sun Valley, Ketchum, Hailey, Bellevue, Carey, Picabo, Fairfield, "
        "and Gooding"
    )
    description = (
        "HAZARD...Wind gusts up to 45 mph and heavy snow.\n\n"
        f"Locations impacted include...{long_locations}"
    )
    d = {
        "eventCode": {"SAME": ["WSW"]},
        "certainty": "Observed",
        "parameters": {
            "eventMotionDescription": ["2200000T225DEG...21KT 42.5,-114.5"],
        },
        "description": description,
    }
    rendered = _render(
        event_type="Winter Weather Advisory", area_desc="Blaine County",
        geocoder_city=None, county="Blaine", state="ID",
        expires_epoch=1_751_400_000, lat=43.5, lon=-114.3,
        now=1_751_400_000, d=d,
    )
    assert len(rendered) <= 140, f"{len(rendered)} chars:\n{rendered!r}"
    assert "45mph gusts" in rendered, f"WSW wind not tightened:\n{rendered!r}"
    assert "heavy snow" in rendered
    assert "up to" not in rendered, "filler 'up to' survived"
    _assert_no_dangling_separator(rendered)


def test_sps_pathological_towns_degrade_to_motion_only(mem_db):
    """When even a single sampled town cannot fit the remaining budget, L4 must
    degrade to motion-only ('Moving …') with NO trailing separator — never
    'Moving … —…'."""
    # One absurdly long town name that cannot coexist with the em-dash + motion
    # in the leftover budget.
    long_town = "Averyverylongimpossibletownnamethatwillnotfitthebudgetatall" * 2
    description = (
        "HAZARD...Wind gusts in excess of 45 mph.\n\n"
        f"Locations impacted include...{long_town}"
    )
    d = {
        "eventCode": {"SAME": ["SPS"]},
        "certainty": "Observed",
        "parameters": {
            "eventMotionDescription": ["2200000T225DEG...21KT 42.5,-114.5"],
        },
        "description": description,
    }
    rendered = _render(
        event_type="Special Weather Statement", area_desc="Twin Falls County",
        geocoder_city=None, county="Twin Falls", state="ID",
        expires_epoch=1_751_400_000, lat=42.5, lon=-114.46,
        now=1_751_400_000, d=d,
    )
    assert len(rendered) <= 140
    _assert_no_dangling_separator(rendered)
    last = rendered.splitlines()[-1]
    # The town can't fit, so L4 (if present) is bare motion.
    if last.startswith("Moving"):
        assert " — " not in last, f"expected motion-only, got:\n{last!r}"


def test_svr_no_dangling_separator(mem_db):
    """Re-verify SVR (the branch tightened earlier) still never dangles."""
    long_locations = (
        "Buhl, Eden, Hazelton, Murtaugh, Richfield, Dietrich, Gooding, "
        "Hagerman, Wendell, Jerome, Kimberly, Hansen, Filer, and Shoshone"
    )
    description = (
        "HAZARD...Damaging winds to 70 mph and golf ball size hail.\n\n"
        f"Locations impacted include...{long_locations}"
    )
    d = {
        "eventCode": {"SAME": ["SVR"]},
        "certainty": "Observed",
        "parameters": {
            "maxWindGust": ["70 MPH"], "maxHailSize": ["1.75"],
            "eventMotionDescription": ["2200000T254DEG...35KT 42.5,-114.5"],
        },
        "description": description,
    }
    rendered = _render(
        event_type="Severe Thunderstorm Warning", area_desc="Twin Falls County",
        geocoder_city=None, county="Twin Falls", state="ID",
        expires_epoch=1_751_400_000, lat=42.5, lon=-114.46,
        now=1_751_400_000, d=d,
    )
    assert len(rendered) <= 140
    assert "70mph winds" in rendered
    _assert_no_dangling_separator(rendered)
