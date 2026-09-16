"""Tests for env/watchduty.py's pure helpers + the v31 migration.

Covers: strip_html / normalize_evac_field text normalization,
fit_to_budget_with_suffix (notifications/formatters/_budget.py), and the
v31 migration's schema additions. No network calls.
"""
from __future__ import annotations

from meshai.env.watchduty import (
    incident_url,
    normalize_evac_field,
    strip_html,
)
from meshai.notifications.formatters._budget import (
    fit_to_budget_with_suffix,
)
from meshai.persistence import get_db


# ---------- strip_html -----------------------------------------------------


def test_strip_html_entities():
    assert strip_html("AT&amp;T &amp; more &mdash; done") == "AT&T & more — done"


def test_strip_html_none_and_empty():
    assert strip_html(None) == ""
    assert strip_html("") == ""


def test_strip_html_plain_string_passthrough():
    assert strip_html("no markup here") == "no markup here"


# ---------- normalize_evac_field --------------------------------------------


def test_normalize_evac_field_none():
    assert normalize_evac_field(None) == ""


def test_normalize_evac_field_live_html_string():
    raw = (
        '<p>A Level 3 (Go Now) Evacuation is in effect for the area depicted '
        'in red on the <a href="x">Kittitas County Evacuation Map</a></p>'
    )
    out = normalize_evac_field(raw)
    assert "<" not in out and ">" not in out
    assert "A Level 3 (Go Now) Evacuation is in effect for the area depicted" in out
    assert "Kittitas County Evacuation Map" in out


def test_normalize_evac_field_list_html_string():
    raw = (
        "<ul><li><p>Shale Creek and Clark Creek Area</p></li>"
        "<li><p>Everything above the Clark Creek and Shale Creek area.</p></li></ul>"
    )
    out = normalize_evac_field(raw)
    assert "<" not in out and ">" not in out
    assert "Shale Creek and Clark Creek Area" in out
    assert "Everything above the Clark Creek and Shale Creek area." in out


def test_normalize_evac_field_list_of_str():
    out = normalize_evac_field(["Zone A", "Zone B"])
    assert out == "Zone A; Zone B"


def test_normalize_evac_field_list_of_dicts():
    raw = [
        {"text": "Zone A evacuate now"},
        {"description": "<p>Zone B shelter in place</p>"},
    ]
    out = normalize_evac_field(raw)
    assert out == "Zone A evacuate now; Zone B shelter in place"


def test_normalize_evac_field_dict():
    raw = {"message": "<b>Level 2</b> evacuation zone"}
    out = normalize_evac_field(raw)
    assert out == "Level 2 evacuation zone"


def test_normalize_evac_field_dict_no_known_key_is_empty():
    assert normalize_evac_field({"foo": "bar"}) == ""


# ---------- incident_url -----------------------------------------------------


def test_incident_url():
    assert incident_url("abc123") == "https://app.watchduty.org/i/abc123"


# ---------- fit_to_budget_with_suffix ---------------------------------------


def test_fit_to_budget_with_suffix_trims_body_keeps_suffix():
    body = "word " * 60  # way over any reasonable limit
    suffix = "https://app.watchduty.org/i/abc123"
    limit = 140
    out = fit_to_budget_with_suffix(body, suffix, limit)
    assert len(out) <= limit
    assert out.endswith(suffix)
    assert out.split("\n")[-1] == suffix


def test_fit_to_budget_with_suffix_short_body_untouched():
    body = "short body"
    suffix = "https://app.watchduty.org/i/abc123"
    limit = 140
    out = fit_to_budget_with_suffix(body, suffix, limit)
    assert out == f"{body}\n{suffix}"
    assert len(out) <= limit


def test_fit_to_budget_with_suffix_suffix_longer_than_limit_returns_suffix_alone():
    suffix = "https://app.watchduty.org/i/" + ("x" * 200)
    out = fit_to_budget_with_suffix("some body text", suffix, 140)
    assert out == suffix


# ---------- v31 migration ---------------------------------------------------


def test_migration_v31_adds_fires_columns():
    conn = get_db()
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(fires)")}
    for col in (
        "watchduty_event_id", "watchduty_name", "watchduty_matched_at",
        "watchduty_is_active", "watchduty_evac_state",
        "watchduty_evac_zone_text", "watchduty_evac_updated_at",
        "watchduty_evac_broadcast_at",
    ):
        assert col in cols, f"missing fires column: {col}"


def test_migration_v31_creates_watchduty_reports_sent_table():
    conn = get_db()
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='watchduty_reports_sent'"
    ).fetchone()
    assert row is not None
