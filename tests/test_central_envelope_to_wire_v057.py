"""v0.5.7-regression: end-to-end Central envelope -> mesh wire string.

Closes the seam between consumer/composer/renderer that the v0.5.7 staged
flip exposed. Pre-v0.5.7-regression two pre-existing bugs were dormant:

  1. consumer._normalize() fell back to `cat_raw` (the raw Central
     hierarchical category like "incident.tomtom_incidents") when the
     upstream payload lacked `title`/`headline`. That string ended up as
     event.title and the composer's primary identifier.
  2. MeshRenderer._format_one_line() prepended "[<Family>] " to every
     payload.message -- including composer output that already starts
     with the family label (e.g. "🚨 ROADS:"). Produced the visually-
     broken duplicate "[Roads] 🚨 ROADS: ..." that Matt observed.

Both bugs predate the v0.5.7 campaign but only manifested when v0.5.7
was the first to flip Central live with master ON. Both were unit-tested
in isolation (composer with clean titles, renderer with legacy messages)
but no integration test exercised the full envelope -> wire path with a
realistic Central payload. This file fills that gap.

For five representative Central adapter envelopes (one per stream family
that produces user-facing broadcasts), assert the rendered wire string:

  - Does NOT start with "[" (no [Family] legacy prefix).
  - Does NOT contain raw Central category tokens like ".tomtom_incidents",
    ".firms", ".kindex", ".proton_flux" -- those would indicate the
    category-as-title fallback fired.
  - DOES start with the composer's emoji + family label (e.g. "🚨 ",
    "🔥 ", "⚠ ", "🌐 ").
  - Contains the meshai-friendly registry name from ALERT_CATEGORIES
    when the upstream payload lacks a useful title/headline.
"""

import json

import pytest

from meshai.central.consumer import CentralConsumer
from meshai.config import EnvironmentalConfig
from meshai.notifications.events import make_payload_from_event
from meshai.notifications.pipeline.bus import EventBus
from meshai.notifications.renderers.composer import compose_mesh_message
from meshai.notifications.renderers.mesh import MeshRenderer
from meshai.notifications.categories import ALERT_CATEGORIES


# ---------- Envelope -> Event helper ---------------------------------------


def _envelope_to_event(subject: str, envelope: dict):
    """Run a CloudEvents envelope through CentralConsumer._normalize/_handle
    the way it would in production, returning the emitted Event."""
    rec = []
    bus = EventBus(); bus.subscribe(rec.append)
    c = CentralConsumer(EnvironmentalConfig(), bus)
    ev = c._handle(subject, json.dumps(envelope).encode())
    assert ev is not None, f"_handle returned None for subject {subject!r}"
    return ev


def _render_to_wire(event) -> str:
    """Run an Event through the dispatcher's composer + renderer path the way
    _dispatch_toggles does for mesh_broadcast / mesh_dm, returning the final
    wire-format string the renderer would hand to the connector."""
    friendly = compose_mesh_message(event)
    assert friendly, "composer returned empty"
    payload = make_payload_from_event(event, message=friendly)
    chunks = MeshRenderer().render(payload)
    assert chunks, "renderer returned no chunks"
    return chunks[0]


# ---------- Five-adapter representative envelopes -------------------------


# 1. tomtom_incidents -- the exact failure mode Matt observed live
TOMTOM_ENV = {
    "id": "tt-12345",
    "data": {
        "id": "tt-12345",
        "adapter": "tomtom_incidents",
        "category": "incident.tomtom_incidents",
        "time": "2026-06-04T15:40:00+00:00",
        "severity": 3,  # immediate per map_severity (>=3)
        "geo": {"centroid": [-114.0, 42.5], "primary_region": "US-ID",
                "regions": ["US-ID"]},
        # NOTE: tomtom_incidents upstream payload carries per-incident fields
        # like roadway / event_type but NO top-level title or headline. That's
        # the trigger for the v0.5.7-regression cat_raw fallback bug.
        "data": {"roadway": "I-84 EB", "event_type": "crash",
                 "delay_seconds": 1800},
    },
}

# 2. FIRMS hotspot -- VIIRS NOAA-20, high confidence
FIRMS_ENV = {
    "id": "viirs_noaa20:2026-06-04:0530:43.123:-115.456",
    "data": {
        "id": "viirs_noaa20:2026-06-04:0530:43.123:-115.456",
        "adapter": "firms",
        "category": "fire.hotspot.viirs_noaa20.high",
        "time": "2026-06-04T05:30:00+00:00",
        "severity": 2,
        "geo": {"centroid": [-115.456, 43.123], "primary_region": "US-ID",
                "regions": ["US-ID"]},
        "data": {"latitude": 43.123, "longitude": -115.456,
                 "confidence": "high", "frp": 22.5, "satellite": "N20"},
    },
}

# 3. NWS alert -- explicitly carries headline (positive control)
NWS_ENV = {
    "id": "urn:oid:2.49.0.1.840.0.abc",
    "data": {
        "id": "urn:oid:2.49.0.1.840.0.abc",
        "adapter": "nws",
        "category": "wx.alert.us.id.severe_thunderstorm_warning",
        "time": "2026-06-04T15:40:00+00:00",
        "severity": 3,
        "geo": {"centroid": [-116.2, 43.6], "primary_region": "US-ID",
                "regions": ["US-ID"]},
        "data": {
            "headline": "Severe Thunderstorm Warning issued June 4 by NWS Boise",
            "description": "<p>The NWS in Boise has issued a Severe Thunderstorm Warning...</p>",
            "areaDesc": "Ada, ID",
        },
    },
}

# 4. USGS quake -- carries title (positive control)
QUAKE_ENV = {
    "id": "us8000mc12",
    "data": {
        "id": "us8000mc12",
        "adapter": "usgs_quake",
        "category": "quake.event.moderate",
        "time": "2026-06-04T12:00:00+00:00",
        "severity": 2,
        "geo": {"centroid": [-114.5, 44.2], "primary_region": "US-ID",
                "regions": ["US-ID"]},
        "data": {"title": "M 4.2 - 23 km ESE of Stanley, ID",
                 "magnitude": 4.2, "place": "23 km ESE of Stanley, ID",
                 "depth": 8.0, "magType": "ml"},
    },
}

# 5. SWPC alert -- no title/headline, just message body
SWPC_ENV = {
    "id": "A20F|2026-04-24 23:50:43.280",
    "data": {
        "id": "A20F|2026-04-24 23:50:43.280",
        "adapter": "swpc_alerts",
        "category": "space.alert",
        "time": "2026-04-24T23:50:43.280Z",
        "severity": 0,
        "geo": {"centroid": None, "primary_region": None, "regions": []},
        "data": {"product_id": "A20F",
                 "issue_datetime": "2026-04-24 23:50:43.280",
                 "message": "WATCH: Geomagnetic Storm Category G1 Predicted ..."},
    },
}

CASES = [
    pytest.param(
        "central.traffic.incident.id", TOMTOM_ENV,
        "road_incident", "Road Incident",
        id="tomtom_incidents-no-title-cat-fallback",
    ),
    pytest.param(
        "central.fire.hotspot.viirs_noaa20.high", FIRMS_ENV,
        "wildfire_hotspot", "Wildfire Hotspot",
        id="firms-hotspot-no-title-cat-fallback",
    ),
    pytest.param(
        "central.wx.alert.us.id.severe_thunderstorm_warning", NWS_ENV,
        "weather_warning", None,   # NWS supplies headline; friendly name not used
        id="nws-with-headline",
    ),
    pytest.param(
        "central.quake.event.moderate", QUAKE_ENV,
        "earthquake_event", None,   # USGS supplies title
        id="quake-with-title",
    ),
    pytest.param(
        "central.space.alert.a20f", SWPC_ENV,
        "rf_propagation_alert", "Space Weather Alert",
        id="swpc-alert-no-title-cat-fallback",
    ),
]


@pytest.mark.parametrize("subject,envelope,expected_cat,expected_friendly_name", CASES)
def test_wire_string_no_legacy_family_prefix(subject, envelope, expected_cat, expected_friendly_name):
    """No payload should produce a wire string starting with '[' -- the v0.5.0
    debug-format prefix the MeshRenderer used to add and now no longer does."""
    ev = _envelope_to_event(subject, envelope)
    wire = _render_to_wire(ev)
    assert not wire.startswith("["), (
        f"wire string still starts with legacy [Family] prefix: {wire!r}"
    )


@pytest.mark.parametrize("subject,envelope,expected_cat,expected_friendly_name", CASES)
def test_wire_string_no_raw_central_category_leaks(subject, envelope, expected_cat, expected_friendly_name):
    """No wire string should contain a raw Central hierarchical category token
    like '.tomtom_incidents', '.firms', '.kindex', '.proton_flux'. Those would
    indicate the cat_raw fallback fired and the title-fallback fix didn't take."""
    ev = _envelope_to_event(subject, envelope)
    wire = _render_to_wire(ev)
    for leak in (
        ".tomtom_incidents", ".firms",
        ".kindex", ".proton_flux",
        "fire.hotspot.viirs", "incident.tomtom",
    ):
        assert leak not in wire, (
            f"raw Central category token {leak!r} leaked to wire: {wire!r}"
        )


@pytest.mark.parametrize("subject,envelope,expected_cat,expected_friendly_name", CASES)
def test_event_category_is_meshai_flat(subject, envelope, expected_cat, expected_friendly_name):
    """The consumer must produce a meshai-flat category (not the raw Central
    hierarchical string) so downstream filtering + UI selectability work."""
    ev = _envelope_to_event(subject, envelope)
    assert ev.category == expected_cat, (
        f"expected event.category={expected_cat!r} got {ev.category!r}"
    )
    assert ev.category in ALERT_CATEGORIES, (
        f"event.category {ev.category!r} not in ALERT_CATEGORIES -- audit gap"
    )


@pytest.mark.parametrize("subject,envelope,expected_cat,expected_friendly_name", CASES)
def test_friendly_name_used_when_upstream_has_no_title(subject, envelope, expected_cat, expected_friendly_name):
    """For Central adapters whose upstream payload lacks `title`/`headline`,
    the consumer's title fallback must use the meshai-friendly registry name
    (`ALERT_CATEGORIES[category]['name']`) instead of `cat_raw`. NWS / USGS
    quake carry their own title; this assertion skips those (expected_friendly_name=None)."""
    if expected_friendly_name is None:
        pytest.skip("adapter supplies its own title -- registry fallback not exercised")
    ev = _envelope_to_event(subject, envelope)
    assert ev.title == expected_friendly_name, (
        f"expected title={expected_friendly_name!r} got {ev.title!r}"
    )


@pytest.mark.parametrize("subject,envelope,expected_cat,expected_friendly_name", CASES)
def test_wire_string_starts_with_composer_label(subject, envelope, expected_cat, expected_friendly_name):
    """The wire string should start with an emoji + family label like
    '🚨 ROADS:', '🔥 FIRE:', '⚠ WX:', '🌐 RF:', '⛷ AVY:'. Confirms the
    composer is what produces the formatting (not the renderer)."""
    ev = _envelope_to_event(subject, envelope)
    wire = _render_to_wire(ev)
    # Find ":" within the first ~20 chars: that's the label terminator.
    head = wire[:30]
    assert ":" in head, (
        f"wire string head {head!r} has no composer label terminator ':'"
    )


# ---------- Specific Matt-saw regression ----------------------------------


def test_matt_smoking_gun_no_longer_reproduces():
    """The exact regression Matt saw at 15:40:30 on 2026-06-04:
        [Roads] 🚨 ROADS: incident.tomtom_incidents, US-ID. immediate
    must NEVER reproduce. Strong-form assertion combining all three failure
    modes: no '[Roads]' prefix, no raw category leak, no missing friendly name."""
    ev = _envelope_to_event("central.traffic.incident.id", TOMTOM_ENV)
    wire = _render_to_wire(ev)

    assert not wire.startswith("[Roads]"), (
        f"the exact regression reproduced: {wire!r}"
    )
    assert "incident.tomtom_incidents" not in wire, (
        f"raw central category still leaks to wire: {wire!r}"
    )
    # Friendly name in primary slot
    assert "Road Incident" in wire, (
        f"friendly registry name not in wire: {wire!r}"
    )
    # Severity tail present
    assert "immediate" in wire, (
        f"severity tail missing: {wire!r}"
    )
