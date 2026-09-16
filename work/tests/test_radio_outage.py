"""Tests for the "all radios down" ops email alert (RadioOutageMonitor).

Uses asyncio.run() (no pytest-asyncio in this container, see
test_pipeline_scheduler.py). smtplib is always mocked -- no real network
calls, no real email is ever sent. The per-test isolated sqlite DB comes
from tests/conftest.py's autouse `_isolate_meshai_db` fixture.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meshai.adapter_config._accessor import set_runtime_override, _overrides
from meshai.notifications.channels import EmailChannel
from meshai.notifications.radio_outage import RadioOutageMonitor, all_down, radio_states
from meshai.persistence import get_db


# ============================================================================
# Fakes
# ============================================================================


class FakeMeshtastic:
    transport_name = "meshtastic"

    def __init__(self, up: bool = True):
        self.link_up = up


class FakeMeshtasticTimed:
    """Simulates the F1 bug scenario: link_up reads True (stale) for the
    first `up_seconds` seconds after `switch_at` (the point of a simulated
    restart), then flips to False -- mirroring main.py's unconditional
    write_link_status("up") right after connect(), corrected only once the
    connection supervisor's own health_interval + probe_wait have elapsed.
    """
    transport_name = "meshtastic"

    def __init__(self, clock, switch_at: float, up_seconds: float = 35.0):
        self._clock = clock
        self._down_from = switch_at + up_seconds

    @property
    def link_up(self) -> bool:
        return self._clock() < self._down_from


class FakeMeshCore:
    transport_name = "meshcore"

    def __init__(self, up: bool = True):
        self.connected = up


class FakeComposite:
    def __init__(self, mt: Optional[FakeMeshtastic] = None, mc: Optional[FakeMeshCore] = None):
        self.mt = mt
        self.mc = mc

    def meshtastic_child(self):
        return self.mt

    def meshcore_child(self):
        return self.mc


@dataclass
class FakeDestination:
    name: str = "ops_email"
    type: str = "email"
    smtp_host: str = "smtp.example.com"
    smtp_port: int = 587
    smtp_user: str = "user"
    smtp_password: str = "pw"
    smtp_tls: bool = True
    from_address: str = "no-reply@example.com"
    recipients: list = field(default_factory=lambda: ["mj@k7zvx.com"])


@dataclass
class FakeNotificationsConfig:
    destinations: dict = field(default_factory=dict)


@dataclass
class FakeConfig:
    timezone: str = "America/Boise"
    notifications: FakeNotificationsConfig = field(default_factory=FakeNotificationsConfig)


def make_config(dest_name: Optional[str] = "ops_email") -> FakeConfig:
    cfg = FakeConfig()
    if dest_name:
        cfg.notifications.destinations[dest_name] = FakeDestination(name=dest_name)
    return cfg


class FakeClock:
    def __init__(self, start: float = 1_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


_DEFAULT_SETTINGS = {
    "enabled": True,
    "threshold_seconds": 300,
    "check_interval_seconds": 15,
    "startup_grace_seconds": 60,
    "destination": "",
    "email_retry_seconds": 300,
}


@contextmanager
def radio_outage_settings(**overrides):
    """Override adapter_config.radio_outage.* for the duration of a test."""
    merged = dict(_DEFAULT_SETTINGS)
    merged.update(overrides)
    for k, v in merged.items():
        set_runtime_override("radio_outage", k, v)
    try:
        yield
    finally:
        for k in merged:
            _overrides.pop(("radio_outage", k), None)


def _count(event_type: str) -> int:
    conn = get_db()
    row = conn.execute(
        "SELECT COUNT(*) c FROM mesh_health_events WHERE event_type=?", (event_type,)
    ).fetchone()
    return row["c"]


# ============================================================================
# 1. Down-detection helper
# ============================================================================


def test_radio_states_composite_both_down():
    comp = FakeComposite(FakeMeshtastic(up=False), FakeMeshCore(up=False))
    states = radio_states(comp)
    assert states == {"meshtastic": False, "meshcore": False}
    assert all_down(states)


def test_radio_states_composite_one_up():
    comp = FakeComposite(FakeMeshtastic(up=True), FakeMeshCore(up=False))
    states = radio_states(comp)
    assert states == {"meshtastic": True, "meshcore": False}
    assert not all_down(states)


def test_radio_states_meshtastic_only_bare_transport():
    mt = FakeMeshtastic(up=False)
    states = radio_states(mt)
    assert states == {"meshtastic": False}
    assert all_down(states)


def test_radio_states_meshcore_only_config():
    comp = FakeComposite(mt=None, mc=FakeMeshCore(up=False))
    states = radio_states(comp)
    assert states == {"meshcore": False}
    assert all_down(states)


def test_radio_states_no_radios():
    assert radio_states(None) == {}
    assert not all_down({})


# ============================================================================
# 2. Startup grace
# ============================================================================


def test_grace_period_delays_start_row():
    async def scenario():
        clock = FakeClock()
        comp = FakeComposite(FakeMeshtastic(up=False), FakeMeshCore(up=False))
        m = RadioOutageMonitor(connector=comp, config=make_config(), clock=clock)
        with radio_outage_settings(startup_grace_seconds=60):
            await m.check_once()
            assert _count("radio_outage_start") == 0

            clock.advance(59)
            await m.check_once()
            assert _count("radio_outage_start") == 0

            clock.advance(2)  # total 61s
            await m.check_once()
            assert _count("radio_outage_start") == 1

    asyncio.run(scenario())


# ============================================================================
# 3. Start row + alert at threshold with correct subject/body; no 2nd alert
# ============================================================================


def test_start_then_alert_at_threshold_correct_subject_body_no_second_alert():
    async def scenario():
        clock = FakeClock()
        comp = FakeComposite(FakeMeshtastic(up=False), FakeMeshCore(up=False))
        m = RadioOutageMonitor(connector=comp, config=make_config(), clock=clock, hostname="testhost")

        sent = []

        def fake_send(dest, subject, body):
            sent.append((dest, subject, body))
            return True

        with radio_outage_settings(destination="ops_email", threshold_seconds=300,
                                    startup_grace_seconds=0), \
                patch.object(m, "_send", AsyncMock(side_effect=fake_send)):
            await m.check_once()  # start row
            assert _count("radio_outage_start") == 1

            clock.advance(299)
            await m.check_once()
            assert sent == []

            clock.advance(1)  # total 300s
            await m.check_once()
            assert len(sent) == 1
            dest, subject, body = sent[0]
            assert dest == "ops_email"
            assert subject == "[meshai] All radios down for 5 min"
            assert "All mesh radios have been disconnected since" in body
            assert "meshcore: down" in body
            assert "meshtastic: down" in body
            assert "Alerts cannot reach the mesh until a radio reconnects." in body
            assert "Host: testhost" in body
            assert _count("radio_outage_alert") == 1

            # Further checks while still down -> no second alert.
            clock.advance(1000)
            await m.check_once()
            assert len(sent) == 1
            assert _count("radio_outage_alert") == 1

    asyncio.run(scenario())


# ============================================================================
# 4. Email failure -> no alert row; retry only after email_retry_seconds
# ============================================================================


def test_alert_email_failure_then_retry_then_success():
    async def scenario():
        clock = FakeClock()
        comp = FakeComposite(FakeMeshtastic(up=False), FakeMeshCore(up=False))
        m = RadioOutageMonitor(connector=comp, config=make_config(), clock=clock)

        attempts = []

        def fake_send(dest, subject, body):
            attempts.append(clock.now)
            return len(attempts) >= 3  # fail, fail, succeed

        with radio_outage_settings(destination="ops_email", threshold_seconds=100,
                                    startup_grace_seconds=0, email_retry_seconds=50), \
                patch.object(m, "_send", AsyncMock(side_effect=fake_send)):
            await m.check_once()  # start
            clock.advance(100)
            await m.check_once()  # attempt 1 -> fail
            assert len(attempts) == 1
            assert _count("radio_outage_alert") == 0

            clock.advance(10)  # inside retry window
            await m.check_once()
            assert len(attempts) == 1  # no retry yet

            clock.advance(45)  # total 55s since attempt 1 -> past 50s gate
            await m.check_once()  # attempt 2 -> fail
            assert len(attempts) == 2
            assert _count("radio_outage_alert") == 0

            clock.advance(51)
            await m.check_once()  # attempt 3 -> success
            assert len(attempts) == 3
            assert _count("radio_outage_alert") == 1

    asyncio.run(scenario())


# ============================================================================
# 5. No destination -> no send, warning logged once, rows still written
# ============================================================================


def test_no_destination_warns_once_but_still_records_rows(caplog):
    async def scenario():
        clock = FakeClock()
        mt = FakeMeshtastic(up=False)
        mc = FakeMeshCore(up=False)
        comp = FakeComposite(mt, mc)
        m = RadioOutageMonitor(connector=comp, config=make_config(dest_name=None), clock=clock)

        with radio_outage_settings(destination="", threshold_seconds=100, startup_grace_seconds=0), \
                patch.object(m, "_send", AsyncMock()) as send_mock:
            await m.check_once()  # start
            clock.advance(100)
            with caplog.at_level(logging.WARNING):
                await m.check_once()  # threshold -> no destination -> warn once
                await m.check_once()  # again -> must not warn again
            send_mock.assert_not_called()
            warnings = [r for r in caplog.records
                        if "no destination configured" in r.getMessage()]
            assert len(warnings) == 1

            # Recovery: outage still gets its end row, still no email.
            mt.link_up = True
            mc.connected = True
            await m.check_once()
            assert _count("radio_outage_end") == 1
            assert _count("radio_outage_alert") == 0
            send_mock.assert_not_called()

    asyncio.run(scenario())


# ============================================================================
# 6. Short outage (recovers before threshold) -> start+end rows, no email
# ============================================================================


def test_short_outage_no_alert_start_and_end_only():
    async def scenario():
        clock = FakeClock()
        mt = FakeMeshtastic(up=False)
        mc = FakeMeshCore(up=False)
        comp = FakeComposite(mt, mc)
        m = RadioOutageMonitor(connector=comp, config=make_config(), clock=clock)

        with radio_outage_settings(destination="ops_email", threshold_seconds=300,
                                    startup_grace_seconds=0), \
                patch.object(m, "_send", AsyncMock()) as send_mock:
            await m.check_once()  # start
            clock.advance(50)  # well under threshold
            mt.link_up = True
            await m.check_once()  # end -- never alerted
            assert _count("radio_outage_start") == 1
            assert _count("radio_outage_end") == 1
            assert _count("radio_outage_alert") == 0
            send_mock.assert_not_called()

    asyncio.run(scenario())


# ============================================================================
# 7. Recovery email after an alert (with duration); gives up after 3 failures
# ============================================================================


def test_recovery_email_sent_after_alert_with_duration():
    async def scenario():
        clock = FakeClock()
        mt = FakeMeshtastic(up=False)
        mc = FakeMeshCore(up=False)
        comp = FakeComposite(mt, mc)
        m = RadioOutageMonitor(connector=comp, config=make_config(), clock=clock, hostname="testhost")

        sent = []

        def fake_send(dest, subject, body):
            sent.append((dest, subject, body))
            return True

        with radio_outage_settings(destination="ops_email", threshold_seconds=100,
                                    startup_grace_seconds=0), \
                patch.object(m, "_send", AsyncMock(side_effect=fake_send)):
            await m.check_once()  # start
            clock.advance(100)
            await m.check_once()  # alert
            assert len(sent) == 1

            clock.advance(3500)  # total outage duration = 3600s (1h)
            mt.link_up = True
            mc.connected = True
            await m.check_once()  # end + recovery
            assert len(sent) == 2
            dest, subject, body = sent[1]
            assert dest == "ops_email"
            assert subject == "[meshai] Radios back after 1h 0m"
            assert "Radio connectivity restored at" in body
            assert "Outage lasted 1h 0m" in body
            assert "meshtastic: up" in body
            assert "meshcore: up" in body
            assert "Host: testhost" in body
            assert _count("radio_outage_end") == 1

    asyncio.run(scenario())


def test_recovery_email_gives_up_after_three_failures():
    async def scenario():
        clock = FakeClock()
        mt = FakeMeshtastic(up=False)
        mc = FakeMeshCore(up=False)
        comp = FakeComposite(mt, mc)
        m = RadioOutageMonitor(connector=comp, config=make_config(), clock=clock)

        with radio_outage_settings(destination="ops_email", threshold_seconds=100,
                                    startup_grace_seconds=0, email_retry_seconds=20), \
                patch.object(m, "_send", AsyncMock(return_value=True)):
            await m.check_once()  # start
            clock.advance(100)
            await m.check_once()  # alert succeeds

        mt.link_up = True
        mc.connected = True
        recovery_calls = []

        def failing_send(dest, subject, body):
            recovery_calls.append((dest, subject, body))
            return False

        with radio_outage_settings(destination="ops_email", threshold_seconds=100,
                                    startup_grace_seconds=0, email_retry_seconds=20), \
                patch.object(m, "_send", AsyncMock(side_effect=failing_send)):
            await m.check_once()  # end row + recovery attempt 1 -> fail
            assert len(recovery_calls) == 1
            assert _count("radio_outage_end") == 1

            clock.advance(21)
            await m.check_once()  # attempt 2 -> fail
            assert len(recovery_calls) == 2

            clock.advance(21)
            await m.check_once()  # attempt 3 -> fail -> give up
            assert len(recovery_calls) == 3
            assert m._pending_recovery is None

            clock.advance(21)
            await m.check_once()  # no further attempts
            assert len(recovery_calls) == 3

    asyncio.run(scenario())


# ============================================================================
# 8. Restart safety
# ============================================================================


def test_restart_safety_ignores_transient_link_up_during_grace_then_alerts():
    """F1 replay: an open outage at T0; a restart at T0+120 where Meshtastic
    reads UP for the first 35s (stale link_up=True written unconditionally
    by main.py right after connect(), corrected only once the supervisor's
    health_interval + probe have elapsed) while MeshCore stays down
    throughout. The outage must NOT be closed by that transient reading,
    and the alert must fire at T0+threshold (not delayed further by the
    restart's own grace window)."""
    async def scenario():
        clock = FakeClock(start=1_000_000.0)
        comp = FakeComposite(FakeMeshtastic(up=False), FakeMeshCore(up=False))

        m1 = RadioOutageMonitor(connector=comp, config=make_config(), clock=clock)
        with radio_outage_settings(destination="ops_email", threshold_seconds=300,
                                    startup_grace_seconds=60):
            await m1.check_once()  # t=0 relative to process start -> inside grace
            assert _count("radio_outage_start") == 0

            clock.advance(61)
            await m1.check_once()  # past grace -> start row written
            assert _count("radio_outage_start") == 1
            conn = get_db()
            outage_start_ts = conn.execute(
                "SELECT detected_at FROM mesh_health_events WHERE event_type='radio_outage_start'"
            ).fetchone()["detected_at"]

        # --- simulate a container restart ~120s into the outage ---
        restart_at = outage_start_ts + 120
        clock.now = restart_at
        # Meshtastic: stale link_up=True for the first 35s post-restart
        # (health_interval=30 + probe_wait=5), matching main.py's
        # unconditional write_link_status("up") right after connect().
        mt2 = FakeMeshtasticTimed(clock, switch_at=restart_at, up_seconds=35.0)
        mc2 = FakeMeshCore(up=False)  # down throughout
        comp2 = FakeComposite(mt2, mc2)

        m2 = RadioOutageMonitor(connector=comp2, config=make_config(), clock=clock)
        # m2._process_start == restart_at; its OWN startup grace (60s) covers
        # the whole 35s of stale meshtastic "up" readings.
        with radio_outage_settings(destination="ops_email", threshold_seconds=300,
                                    startup_grace_seconds=60), \
                patch.object(m2, "_send", AsyncMock(return_value=True)) as send_mock:
            m2._load_open_outage()
            assert m2._outage_id is not None
            assert m2._alerted is False

            # t=0 post-restart: meshtastic reads UP (stale) -- must be a
            # complete no-op: no end row, outage stays open.
            await m2.check_once()
            assert _count("radio_outage_end") == 0
            assert m2._outage_id is not None
            send_mock.assert_not_called()

            # t=20s post-restart: still stale-up, still within grace -> no-op.
            clock.now = restart_at + 20
            await m2.check_once()
            assert _count("radio_outage_end") == 0
            assert m2._outage_id is not None

            # t=40s post-restart: meshtastic now genuinely reads down (past
            # the 35s stale window) -- but still inside the 60s grace, so
            # STILL no transition (no premature alert either).
            clock.now = restart_at + 40
            await m2.check_once()
            assert _count("radio_outage_end") == 0
            send_mock.assert_not_called()

            # t=61s post-restart: grace has ended; both radios genuinely
            # down; outage duration since the ORIGINAL start is only
            # 120+61=181s, still under the 300s threshold -> no alert yet.
            clock.now = restart_at + 61
            await m2.check_once()
            assert _count("radio_outage_end") == 0
            send_mock.assert_not_called()

            # Reaching T0+threshold (relative to the ORIGINAL outage start,
            # not the restart) fires the alert -- proving the restart's own
            # grace window never pushed the schedule back.
            clock.now = outage_start_ts + 300
            await m2.check_once()
            send_mock.assert_called_once()

        assert _count("radio_outage_alert") == 1
        assert _count("radio_outage_end") == 0

        # --- a second restart after the alert must not re-alert ---
        m3 = RadioOutageMonitor(connector=comp, config=make_config(), clock=clock)
        with radio_outage_settings(destination="ops_email", threshold_seconds=300,
                                    startup_grace_seconds=60):
            m3._load_open_outage()
            assert m3._alerted is True
            with patch.object(m3, "_send", AsyncMock(return_value=True)) as send_mock2:
                clock.advance(1000)
                await m3.check_once()
                send_mock2.assert_not_called()

        assert _count("radio_outage_alert") == 1

    asyncio.run(scenario())


def test_restart_after_grace_genuinely_up_radio_closes_outage_normally():
    """Second F1 case: once the grace window has elapsed, a radio that is
    GENUINELY back up must still close the outage normally (the fix must
    not suppress real transitions past the grace window)."""
    async def scenario():
        clock = FakeClock(start=1_000_000.0)
        comp = FakeComposite(FakeMeshtastic(up=False), FakeMeshCore(up=False))

        m1 = RadioOutageMonitor(connector=comp, config=make_config(), clock=clock)
        with radio_outage_settings(destination="ops_email", threshold_seconds=300,
                                    startup_grace_seconds=60):
            await m1.check_once()  # inside grace -> no-op
            clock.advance(61)
            await m1.check_once()  # start row
        conn = get_db()
        outage_start_ts = conn.execute(
            "SELECT detected_at FROM mesh_health_events WHERE event_type='radio_outage_start'"
        ).fetchone()["detected_at"]

        # Restart shortly after; this time Meshtastic is GENUINELY back up
        # from the very first check (no stale-flag involved).
        restart_at = outage_start_ts + 50
        clock.now = restart_at
        mt2 = FakeMeshtastic(up=True)
        mc2 = FakeMeshCore(up=False)
        comp2 = FakeComposite(mt2, mc2)

        m2 = RadioOutageMonitor(connector=comp2, config=make_config(), clock=clock)
        with radio_outage_settings(destination="ops_email", threshold_seconds=300,
                                    startup_grace_seconds=60), \
                patch.object(m2, "_send", AsyncMock(return_value=True)) as send_mock:
            m2._load_open_outage()
            assert m2._outage_id is not None

            # Still inside the restart's own grace window -> no transition
            # yet, even though the radio is genuinely up.
            await m2.check_once()
            assert _count("radio_outage_end") == 0

            # Past grace -> the genuinely-up radio now closes the outage.
            clock.now = restart_at + 61
            await m2.check_once()
            assert _count("radio_outage_end") == 1
            # Never alerted (outage never reached the 300s threshold) ->
            # no recovery email either.
            send_mock.assert_not_called()

    asyncio.run(scenario())


# ============================================================================
# 9. enabled=False -> nothing
# ============================================================================


def test_disabled_monitor_does_nothing():
    async def scenario():
        clock = FakeClock()
        comp = FakeComposite(FakeMeshtastic(up=False), FakeMeshCore(up=False))
        m = RadioOutageMonitor(connector=comp, config=make_config(), clock=clock)

        with radio_outage_settings(enabled=False, startup_grace_seconds=0,
                                    destination="ops_email"), \
                patch.object(m, "_send", AsyncMock()) as send_mock:
            await m.check_once()
            clock.advance(10_000)
            await m.check_once()
            conn = get_db()
            total = conn.execute("SELECT COUNT(*) c FROM mesh_health_events").fetchone()["c"]
            assert total == 0
            send_mock.assert_not_called()

    asyncio.run(scenario())


# ============================================================================
# 10. EmailChannel Message-ID / Date / bare envelope sender
# ============================================================================


def test_email_channel_sets_message_id_date_and_bare_envelope_sender():
    channel = EmailChannel(
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_user="user",
        smtp_password="pw",
        smtp_tls=True,
        from_address="no-reply@example.com",
        recipients=["mj@k7zvx.com"],
    )

    mock_server = MagicMock()
    mock_server.__enter__.return_value = mock_server
    mock_server.__exit__.return_value = False

    with patch("meshai.notifications.channels.smtplib.SMTP", return_value=mock_server), \
            patch("meshai.notifications.channels.ssl.create_default_context"):
        channel._send_email("Test subject", "Test body")

    mock_server.send_message.assert_called_once()
    args, kwargs = mock_server.send_message.call_args
    msg = args[0]

    assert kwargs["from_addr"] == "no-reply@example.com"
    assert kwargs["to_addrs"] == ["mj@k7zvx.com"]

    message_id = msg["Message-ID"]
    assert message_id
    assert "example.com" in message_id

    date_header = msg["Date"]
    assert date_header
    import email.utils as _eu
    assert _eu.parsedate(date_header) is not None


# ============================================================================
# 11. Monitor start/stop wiring in main.py
# ============================================================================


def test_meshai_start_stop_wires_radio_outage_monitor():
    async def scenario():
        from meshai.main import MeshAI

        app = MeshAI.__new__(MeshAI)
        app.connector = FakeComposite(FakeMeshtastic(up=True), FakeMeshCore(up=True))
        app.config = make_config()
        app._radio_outage_monitor = None

        await app._start_radio_outage_monitor()
        assert app._radio_outage_monitor is not None
        task = app._radio_outage_monitor._task
        assert task is not None
        assert not task.done()

        await app._stop_radio_outage_monitor()
        assert app._radio_outage_monitor is None
        assert task.cancelled() or task.done()

    asyncio.run(scenario())


# ============================================================================
# 12. Meshtastic link_up tracks the connection supervisor's up/down calls
# ============================================================================


def test_meshtastic_link_up_tracks_write_link_status(tmp_path):
    from meshai.config import ConnectionConfig
    from meshai.connector import MeshtasticTransport

    t = MeshtasticTransport(ConnectionConfig())
    t._link_path = str(tmp_path / "meshai.link")

    assert t.link_up is True  # default, before the supervisor has spoken

    t.write_link_status("down")
    assert t.link_up is False
    assert (tmp_path / "meshai.link").read_text() == "down"

    t.write_link_status("up")
    assert t.link_up is True
    assert (tmp_path / "meshai.link").read_text() == "up"


# ============================================================================
# 13 (F4). Local time formatting uses the top-level Config.timezone field
# ============================================================================


def test_fmt_local_uses_top_level_config_timezone():
    """config.py:1141 Config.timezone ("IANA timezone for local time
    display") is a top-level field on the SAME Config object main.py hands
    the monitor (self.config) -- so getattr(self._config, "timezone", None)
    already resolves correctly with no lookup fix needed. This proves it,
    and proves the None-timezone fallback."""
    clock = FakeClock()
    comp = FakeComposite(FakeMeshtastic(), FakeMeshCore())
    ts = 1_700_000_000.0  # arbitrary fixed epoch

    cfg = make_config()
    assert cfg.timezone == "America/Boise"
    m = RadioOutageMonitor(connector=comp, config=cfg, clock=clock)
    local = m._fmt_local(ts)
    assert local is not None
    assert local.endswith(("MST", "MDT"))  # America/Boise abbreviations

    cfg_no_tz = make_config()
    cfg_no_tz.timezone = ""
    m2 = RadioOutageMonitor(connector=comp, config=cfg_no_tz, clock=clock)
    assert m2._fmt_local(ts) is None


def test_local_time_appears_in_alert_body_when_timezone_set():
    async def scenario():
        clock = FakeClock()
        comp = FakeComposite(FakeMeshtastic(up=False), FakeMeshCore(up=False))
        m = RadioOutageMonitor(connector=comp, config=make_config(), clock=clock)

        sent = []

        def fake_send(dest, subject, body):
            sent.append((dest, subject, body))
            return True

        with radio_outage_settings(destination="ops_email", threshold_seconds=100,
                                    startup_grace_seconds=0), \
                patch.object(m, "_send", AsyncMock(side_effect=fake_send)):
            await m.check_once()
            clock.advance(100)
            await m.check_once()

        assert len(sent) == 1
        body = sent[0][2]
        # Local time appears parenthetically alongside the UTC ISO timestamp.
        assert " (" in body
        assert body.split(" (", 1)[1].split(")")[0].endswith(("MST", "MDT"))

    asyncio.run(scenario())
