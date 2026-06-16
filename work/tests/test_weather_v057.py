"""v0.5.7-weather: NWS HTML strip + ALERT_CATEGORIES weather audit.

Covers three things shipped in v0.5.7-weather:

1. strip_html_tags() — NWS data.description / data.instruction arrive as raw
   HTML (per Central guide §Surprise 3). Verify tags are stripped, entities
   decoded, paragraph breaks become spaces, plain text is a no-op.
2. compose_mesh_message() integration — an Event whose title contains HTML
   produces a clean LoRa string (no literal <p>/<br>).
3. Weather category parity — ALERT_CATEGORIES{toggle=weather} is exactly the
   set that nws.py._derive_category() can emit. Fail loudly if either side
   drifts so the weather family stays "every event meshai sees is selectable".
"""

import inspect

import pytest

from meshai.notifications.categories import ALERT_CATEGORIES
from meshai.notifications.events import make_event
from meshai.notifications.renderers.composer import (
    compose_mesh_message,
    strip_html_tags,
)


# ---------- strip_html_tags() ----------------------------------------------


def test_strip_html_tags_removes_simple_tags():
    assert strip_html_tags("<p>Severe</p>") == "Severe"


def test_strip_html_tags_br_becomes_space():
    # <br> separates two sentences in NWS bodies; must not fuse.
    assert strip_html_tags("hello<br>world") == "hello world"


def test_strip_html_tags_paragraph_break_becomes_space():
    assert strip_html_tags("<p>hello</p><p>world</p>") == "hello world"


def test_strip_html_tags_decodes_entities():
    assert strip_html_tags("Wind gusts 25 &amp; 35 mph") == "Wind gusts 25 & 35 mph"
    # &nbsp; decodes to U+00A0 which the whitespace collapse normalizes to a
    # regular space — tight ASCII whitespace is what we want on LoRa.
    assert strip_html_tags("Twin Falls&nbsp;County") == "Twin Falls County"
    assert strip_html_tags("12 &mdash; 35 mph") == "12 — 35 mph"


def test_strip_html_tags_nested_and_attrs():
    raw = '<div class="alert"><p style="color:red">Tornado <strong>WARNING</strong></p></div>'
    assert strip_html_tags(raw) == "Tornado WARNING"


def test_strip_html_tags_plain_text_noop():
    assert strip_html_tags("Red Flag Warning until 04:00Z") == "Red Flag Warning until 04:00Z"


def test_strip_html_tags_empty_inputs():
    assert strip_html_tags("") == ""
    assert strip_html_tags(None) == ""  # type: ignore[arg-type]


def test_strip_html_tags_collapses_whitespace():
    raw = "<p>line  1</p>\n<p>line\t2</p>"
    assert strip_html_tags(raw) == "line 1 line 2"


# ---------- compose_mesh_message integration -------------------------------


def test_compose_mesh_message_strips_html_in_title():
    event = make_event(
        source="nws",
        category="weather_warning",
        severity="priority",
        title="<p>Severe Thunderstorm Warning</p>",
        summary="",
        region="Twin Falls",
    )
    line = compose_mesh_message(event)
    # No literal markup escapes onto the wire.
    assert "<" not in line
    assert "</p>" not in line
    assert "Severe Thunderstorm Warning" in line


def test_compose_mesh_message_strips_html_with_entities_and_br():
    event = make_event(
        source="nws",
        category="weather_advisory",
        severity="routine",
        title="Wind Advisory&nbsp;&mdash;<br>SW gusts 50 mph",
        summary="",
        region="Magic Valley",
    )
    line = compose_mesh_message(event)
    assert "<br>" not in line
    assert "&nbsp;" not in line
    assert "&mdash;" not in line
    # Byte budget still holds.
    assert len(line.encode("utf-8")) <= 150


def test_compose_mesh_message_html_fallthrough_to_summary():
    # title empty -> summary path also strips HTML.
    event = make_event(
        source="nws",
        category="weather_statement",
        severity="routine",
        title="",
        summary="<p>Special Weather Statement</p>",
    )
    line = compose_mesh_message(event)
    assert "<" not in line
    assert "Special Weather Statement" in line


# ---------- ALERT_CATEGORIES weather audit ---------------------------------


def _nws_emitted_categories() -> set[str]:
    """Walk nws.py source for every literal returned by _derive_category().

    Reflection-style audit: read the method body's source and collect the
    quoted return values. Keeps the test honest if someone adds a 5th branch
    without thinking about ALERT_CATEGORIES.
    """
    from meshai.env.nws import NWSAlertsAdapter
    src = inspect.getsource(NWSAlertsAdapter._derive_category)
    import re
    return set(re.findall(r'return\s+"([a-z_]+)"', src))


def test_nws_emits_exactly_four_weather_categories():
    emitted = _nws_emitted_categories()
    assert emitted == {
        "weather_warning",
        "weather_watch",
        "weather_advisory",
        "weather_statement",
    }, f"nws.py emission set drifted: {emitted}"


def test_alert_categories_weather_complete():
    """Every weather category nws.py can emit must exist in ALERT_CATEGORIES
    with toggle='weather'. Anything tagged toggle='weather' that nws.py
    cannot emit is an orphan (no UI selectable event would ever surface it).
    """
    registry_weather = {
        cid for cid, info in ALERT_CATEGORIES.items()
        if info.get("toggle") == "weather"
    }
    emitted = _nws_emitted_categories()
    missing = emitted - registry_weather
    orphans = registry_weather - emitted
    assert not missing, f"nws.py emits categories missing from ALERT_CATEGORIES: {missing}"
    assert not orphans, f"ALERT_CATEGORIES has orphan weather entries: {orphans}"


@pytest.mark.parametrize(
    "cat",
    ["weather_warning", "weather_watch", "weather_advisory", "weather_statement"],
)
def test_weather_categories_have_required_fields(cat):
    info = ALERT_CATEGORIES[cat]
    assert info["toggle"] == "weather"
    assert info["name"]
    assert info["description"]
    assert info["default_severity"] in {"routine", "priority", "immediate"}
    assert info["example_message"]
