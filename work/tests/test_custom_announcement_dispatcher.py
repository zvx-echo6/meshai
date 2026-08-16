"""Dispatcher.dispatch_scheduled_custom_broadcast -- own explicit channel
list (no toggle / no region_routes matrix), unlimited mixed targets, one
mesh_broadcasts_out audit row per target, cold-start grace.

Follows the RecChannel recorder pattern from
tests/test_wzdx_summary_region_routing.py.
"""
from __future__ import annotations

import asyncio

import pytest

from meshai.config import Config
from meshai.notifications.pipeline.dispatcher import Dispatcher
from meshai.persistence import get_db


class RecChannel:
    """Records each delivery's transport + channel value + message."""

    def __init__(self, rec: list, succeed=True):
        self.rec = rec
        self.succeed = succeed

    async def deliver(self, payload, rule):
        ok = self.succeed(rule) if callable(self.succeed) else self.succeed
        self.rec.append({
            "delivery_type": rule.delivery_type,
            "broadcast_channel": getattr(rule, "broadcast_channel", None),
            "meshcore_channel": getattr(rule, "meshcore_channel", None),
            "message": payload.message if payload else None,
            "ok": ok,
        })
        return ok


def _cfg(cold_start_grace=0):
    cfg = Config()
    cfg.notifications.rules = []
    cfg.notifications.cold_start_grace_seconds = cold_start_grace
    return cfg


def _dispatcher(cfg, succeed=True):
    rec: list = []
    d = Dispatcher(cfg, lambda rule, conn: RecChannel(rec, succeed), connector=None)
    return d, rec


def _run(coro):
    return asyncio.run(coro)


# ============================================================================
# Multi-target fan-out (any number, mixed transports)
# ============================================================================


def test_five_plus_mixed_targets_all_delivered_with_own_audit_row():
    """5+ mixed targets (3 meshtastic + 2 meshcore) -> every one delivered,
    every one gets its own mesh_broadcasts_out row."""
    cfg = _cfg()
    d, rec = _dispatcher(cfg, succeed=True)
    conn = get_db()

    channels = [
        {"transport": "meshtastic", "channel": 0},
        {"transport": "meshtastic", "channel": 2},
        {"transport": "meshtastic", "channel": 5},
        {"transport": "meshcore", "channel": "#sw-id-aida"},
        {"transport": "meshcore", "channel": "#sc-id-aida"},
    ]

    ok = _run(d.dispatch_scheduled_custom_broadcast(
        text="hello everyone", announcement_id=42, slot_key="2026-08-17T08:00",
        channels=channels,
    ))
    assert ok is True
    assert len(rec) == 5

    mt = [r for r in rec if r["delivery_type"] == "mesh_broadcast"]
    mc = [r for r in rec if r["delivery_type"] == "meshcore_broadcast"]
    assert {r["broadcast_channel"] for r in mt} == {0, 2, 5}
    assert {r["meshcore_channel"] for r in mc} == {"#sw-id-aida", "#sc-id-aida"}
    assert all(r["message"] == "hello everyone" for r in rec)

    audit_rows = conn.execute(
        "SELECT transport, channel, source_event_table, source_event_pk, success "
        "FROM mesh_broadcasts_out WHERE source_event_table='custom_announcements' "
        "ORDER BY id"
    ).fetchall()
    assert len(audit_rows) == 5
    for r in audit_rows:
        assert r["source_event_table"] == "custom_announcements"
        assert r["source_event_pk"] == "42:2026-08-17T08:00"
        assert r["success"] == 1
    audit_channels_mt = {r["channel"] for r in audit_rows if r["transport"] == "meshtastic"}
    audit_channels_mc = {r["channel"] for r in audit_rows if r["transport"] == "meshcore"}
    assert audit_channels_mt == {0, 2, 5}
    assert audit_channels_mc == {"#sw-id-aida", "#sc-id-aida"}


def test_no_cap_on_number_of_targets():
    """40 mixed targets all get delivered -- no artificial cap anywhere."""
    cfg = _cfg()
    d, rec = _dispatcher(cfg, succeed=True)
    channels = [{"transport": "meshtastic", "channel": i} for i in range(20)]
    channels += [{"transport": "meshcore", "channel": f"#ch{i}"} for i in range(20)]

    ok = _run(d.dispatch_scheduled_custom_broadcast(
        text="big fan-out", announcement_id=1, slot_key="s", channels=channels,
    ))
    assert ok is True
    assert len(rec) == 40


def test_empty_channel_list_drops_with_no_delivery():
    cfg = _cfg()
    d, rec = _dispatcher(cfg, succeed=True)
    ok = _run(d.dispatch_scheduled_custom_broadcast(
        text="nowhere to go", announcement_id=1, slot_key="s", channels=[],
    ))
    assert ok is False
    assert rec == []


# ============================================================================
# A missing/unrecognised target must not abort the rest of the announcement
# ============================================================================


def test_one_failing_target_does_not_abort_the_others(caplog):
    """One target 'fails' (e.g. channel no longer configured on the radio --
    deliver() returns False); the other targets still go out, and the
    failure still gets its own audit row with success=0."""
    cfg = _cfg()
    conn = get_db()

    def succeed_unless_missing(rule):
        return getattr(rule, "meshcore_channel", None) != "#gone"

    d, rec = _dispatcher(cfg, succeed=succeed_unless_missing)
    channels = [
        {"transport": "meshtastic", "channel": 1},
        {"transport": "meshcore", "channel": "#gone"},
        {"transport": "meshcore", "channel": "#still-here"},
    ]

    ok = _run(d.dispatch_scheduled_custom_broadcast(
        text="partial failure ok", announcement_id=7, slot_key="s",
        channels=channels,
    ))
    assert ok is True   # at least one delivery succeeded
    assert len(rec) == 3

    audit_rows = conn.execute(
        "SELECT channel, success FROM mesh_broadcasts_out "
        "WHERE source_event_pk='7:s' ORDER BY id"
    ).fetchall()
    assert len(audit_rows) == 3
    by_channel = {r["channel"]: r["success"] for r in audit_rows}
    assert by_channel["#gone"] == 0
    assert by_channel["#still-here"] == 1
    assert by_channel[1] == 1


def test_unknown_transport_is_skipped_not_fatal():
    cfg = _cfg()
    d, rec = _dispatcher(cfg, succeed=True)
    channels = [
        {"transport": "carrier_pigeon", "channel": "n/a"},
        {"transport": "meshtastic", "channel": 3},
    ]

    ok = _run(d.dispatch_scheduled_custom_broadcast(
        text="skip bad transport", announcement_id=1, slot_key="s",
        channels=channels,
    ))
    assert ok is True
    assert len(rec) == 1
    assert rec[0]["broadcast_channel"] == 3


# ============================================================================
# Cold-start grace (consistent with the other scheduled broadcasts)
# ============================================================================


def test_cold_start_grace_suppresses_first_broadcast():
    cfg = _cfg(cold_start_grace=3600)
    d, rec = _dispatcher(cfg, succeed=True)
    ok = _run(d.dispatch_scheduled_custom_broadcast(
        text="too soon", announcement_id=1, slot_key="s",
        channels=[{"transport": "meshtastic", "channel": 0}],
    ))
    assert ok is False
    assert rec == []
