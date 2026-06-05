"""v0.5.13 tests for consumer._normalize() default-deny gate.

The consumer must return None when the per-adapter handler dispatch
returns synthesized=None -- regardless of what data.title / data.headline
say. Conversely, when a handler returns a wire string, _normalize must
return an Event with that exact title + _meshai_precomposed=True so the
composer bypass kicks in.

Covers four cases:
  (a) envelope with NO matching handler (adapter='avalanche' has no
       Central adapter wired)  -> _normalize returns None
  (b) envelope hits handler, handler returns None (e.g. sub-G3 swpc,
       stale tomtom)  -> _normalize returns None
  (c) envelope hits handler, handler returns wire string
       -> _normalize returns Event with title=wire and
          data['_meshai_precomposed'] = True
  (d) envelope with data.title and data.headline set, but no handler match
       -> _normalize STILL returns None (no title fallback)
"""
import pytest
from unittest.mock import patch, MagicMock

from meshai.config import Config
from meshai.central.consumer import CentralConsumer
from meshai.persistence import close_thread_connection, init_db
from meshai.persistence import db as persistence_db


@pytest.fixture
def mem_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "v0513-test.sqlite")
    monkeypatch.setenv("MESHAI_DB_PATH", db_path)
    persistence_db._initialised.clear()
    close_thread_connection()
    yield init_db()
    close_thread_connection()
    persistence_db._initialised.discard(db_path)


@pytest.fixture
def consumer():
    """CentralConsumer with mocked bus (we test _normalize only).
    CentralConsumer.__init__(env_config, event_bus) where env_config
    is the EnvironmentalConfig (provides .central + per-adapter source).
    """
    cfg = Config()
    cfg.notifications.cold_start_grace_seconds = 0
    bus = MagicMock()
    c = CentralConsumer(cfg.environmental, bus)
    return c


# ---------- envelope builders ----------------------------------------------


def _make_envelope(adapter, category, *, inner_id="test_001",
                    title=None, headline=None, severity="routine",
                    extra_data=None):
    inner_data = dict(extra_data or {})
    if title is not None:
        inner_data["title"] = title
    if headline is not None:
        inner_data["headline"] = headline
    return {
        "subject": f"central.{adapter}.test",
        "id":      f"env_{inner_id}",
        "data": {
            "id":       inner_id,
            "adapter":  adapter,
            "category": category,
            "severity": severity,
            "time":     "2026-06-05T15:00:00Z",
            "geo":      {"primary_region": "US-ID"},
            "data":     inner_data,
        },
    }


# ============================================================================
# (a) envelope with NO matching handler -> default-deny
# ============================================================================


def test_no_handler_match_returns_none(consumer, mem_db):
    """Avalanche has no handler (no Central adapter). envelope must
    drop at consumer._normalize as the default-deny baseline."""
    env = _make_envelope("avalanche", "avalanche.forecast",
                          inner_id="aval_001")
    out = consumer._normalize(env["subject"], env)
    assert out is None


def test_unknown_adapter_returns_none(consumer, mem_db):
    """Any future adapter that meshai doesn't know about must default-deny."""
    env = _make_envelope("future_adapter", "some.category.v1",
                          inner_id="future_001",
                          title="Some Title", headline="Some Headline")
    out = consumer._normalize(env["subject"], env)
    assert out is None


# ============================================================================
# (b) handler returns None -> default-deny (regardless of data.title)
# ============================================================================


def test_handler_returns_none_drops_event(consumer, mem_db, monkeypatch):
    """Stale tomtom incident -> incident_handler returns None -> drop."""
    env = _make_envelope("tomtom_incidents", "incident.tomtom_incidents",
                          inner_id="ID:tomtom:TTI-stale",
                          title="Old Jam", headline="Headline Jam",
                          extra_data={
                              "id": "ID:tomtom:TTI-stale",
                              "magnitude_of_delay": 2,
                              "icon_category": 6,
                              "time_validity": "past",  # filtered
                              "start_time": "2024-01-01T00:00:00Z",
                              "latitude": 43.5, "longitude": -116.0,
                          })
    out = consumer._normalize(env["subject"], env)
    assert out is None, "default-deny: handler None -> no Event"


def test_data_title_does_not_rescue_handler_none(consumer, mem_db):
    """v0.5.13: even when envelope has data.title set, if no handler\
    synthesized, the broadcast is denied."""
    env = _make_envelope("swpc_kindex", "space.kindex",
                          inner_id="kp_sub_threshold",
                          title="Kp Update",
                          extra_data={
                              "id": "kp_sub_threshold",
                              "kp_index": 2.0,   # well below G3 (Kp>=7)
                              "time": "2026-06-05T15:00:00Z",
                          })
    out = consumer._normalize(env["subject"], env)
    assert out is None
    assert out is None  # double-check


# ============================================================================
# (c) handler returns wire string -> Event emitted with precomposed marker
# ============================================================================


def test_handler_returns_wire_event_emitted(consumer, mem_db, monkeypatch):
    """Fresh tomtom envelope passes the handler gate -> Event created."""
    # Disable Photon to avoid network calls in test.
    import meshai.central_normalizer as cn
    monkeypatch.setattr(cn, "_photon_reverse_places", lambda lat, lon: [])
    if hasattr(cn, "_H3_NEAREST_CACHE"):
        cn._H3_NEAREST_CACHE.clear()

    import time
    now_iso = "2026-06-05T15:00:00Z"
    # Build a fresh envelope (start_time = now-300s would require dynamic
    # clock control; we instead set the freshness window via the envelope
    # built relative to a fixed time and mock the handler to bypass freshness).
    env = _make_envelope(
        "tomtom_incidents", "incident.tomtom_incidents",
        inner_id="ID:tomtom:TTI-aaaa1111-2222-3333-4444-555555555555-TTR1",
        extra_data={
            "id": "ID:tomtom:TTI-aaaa1111-2222-3333-4444-555555555555-TTR1",
            "magnitude_of_delay": 2,
            "icon_category": 6,
            "time_validity": "present",
            "start_time": now_iso,
            "latitude":  43.6, "longitude": -116.2,
            "delay":     180, "from": "A St", "to": "B St",
            "road_numbers": ["I-84"],
            "state_code": "ID",
            "_enriched": {"geocoder": {"city": "Boise", "county": "Ada",
                                          "state": "ID"}},
        },
    )

    # Use a "now" that aligns with start_time so freshness gate passes.
    import datetime as _dt
    now_epoch = int(_dt.datetime.fromisoformat(
        now_iso.replace("Z","+00:00")).timestamp()) + 60  # 1 min after start

    with patch("time.time", return_value=now_epoch):
        out = consumer._normalize(env["subject"], env)

    assert out is not None, "fresh tomtom should produce an Event"
    assert out.data.get("_meshai_precomposed") is True
    assert out.title.startswith("🚗")  # jam emoji
    assert "Boise" in out.title


# ============================================================================
# (d) envelope with title but no handler still drops (no title fallback)
# ============================================================================


def test_envelope_with_title_still_drops_without_handler(consumer, mem_db):
    """Regression guard: the v0.5.7-fallback path (data.title -> headline ->\
    friendly_name -> cat_raw) is GONE in v0.5.13. Uses an unhandled adapter
    (avalanche) since v0.6-1 added the FIRMS handler."""
    env = _make_envelope("avalanche", "avalanche.forecast",
                          inner_id="aval_with_title",
                          title="Avalanche Warning",
                          headline="Backcountry advisory")
    out = consumer._normalize(env["subject"], env)
    assert out is None, (
        "v0.5.13 default-deny: data.title and data.headline must NOT rescue\n"
        "an envelope that no handler synthesized for."
    )


# ============================================================================
# (e) memory rule 19 -- confirms _normalize ENTRY logging behavior
# ============================================================================


def test_default_deny_path_is_silent_at_INFO(consumer, mem_db, caplog):
    """Default-deny paths log at DEBUG, not INFO/WARNING. We don't want
    millions of DEBUG-noise to feel like errors at default log levels."""
    import logging
    caplog.set_level(logging.INFO, logger="meshai.central.consumer")
    env = _make_envelope("firms", "fire.hotspot.viirs",
                          inner_id="silent_check")
    out = consumer._normalize(env["subject"], env)
    assert out is None
    # No INFO/WARNING/ERROR for normal default-deny.
    info_or_higher = [r for r in caplog.records
                       if r.levelno >= logging.INFO
                       and r.name == "meshai.central.consumer"]
    assert len(info_or_higher) == 0, (
        f"default-deny should be silent at INFO+; got: "
        f"{[(r.levelname, r.message) for r in info_or_higher]}"
    )
