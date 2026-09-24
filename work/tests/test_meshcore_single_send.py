"""Single-send-by-default DM delivery (2026-09-24 AIDA triple-DM incident).

Diagnosis: the triple reply was OUTBOUND duplication, not inbound -- one
question got one LLM reply, sent 3x on air. Two independent retry state
machines were stacked: meshai's own no-ACK fallback here (path discovery +
resend) AND MeshMonitor's virtual node, which has its own ACK tracker and
its own RF-level retries. meshai's local resend landed on top of
MeshMonitor's own retry, producing a third, byte-identical transmission.

Fix (``ConnectionConfig.meshcore_client_retry``, default False): meshai
sends each DM exactly ONCE and only logs whether the ACK arrived; it never
resends or runs path discovery on a missing ACK. ``meshcore_client_retry=
True`` restores the old behavior for a bare MeshCore link with no external
retry layer of its own (see test_meshcore_dm_delivery.py, which exercises
that legacy path).
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

from meshai.config import ConnectionConfig
from meshai.transport.meshcore_transport import MeshCoreTransport

_CONTACT = {"public_key": "a" * 64, "adv_name": "Bob", "out_path_len": -1}


def _ok_event():
    ev = MagicMock()
    ev.is_error.return_value = False
    ev.payload = {"type": 0, "expected_ack": "deadbeef"}
    return ev


def _err_event():
    ev = MagicMock()
    ev.is_error.return_value = True
    ev.payload = {"reason": "test error"}
    return ev


def _transport(client_retry: bool = False):
    cfg = ConnectionConfig(meshcore_host="127.0.0.1", meshcore_client_retry=client_retry)
    t = MeshCoreTransport(cfg)
    mc = MagicMock()
    mc.get_contact_by_key_prefix.return_value = _CONTACT
    mc.commands.send_msg = AsyncMock(return_value=_ok_event())
    mc.commands.send_path_discovery_sync = AsyncMock(
        return_value=MagicMock(is_error=lambda: False)
    )
    mc.commands.get_advert_path = AsyncMock(return_value=_err_event())
    mc.commands.update_contact = AsyncMock(return_value=_ok_event())
    t._mc = mc
    t._connected = True

    def _sync_run_coro(coro, timeout=None):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    t._run_coro = _sync_run_coro
    return t, mc


# ---------------------------------------------------------------------------
# Sync send_message() DM path
# ---------------------------------------------------------------------------


def test_default_no_ack_sends_exactly_once_no_discovery():
    """meshcore_client_retry defaults to False: a missing ACK must NOT
    trigger a resend or path discovery."""
    t, mc = _transport(client_retry=False)
    t._wait_for_ack = MagicMock(return_value=False)

    result = t.send_message("reply text", destination="aabbccdd1122")

    assert mc.commands.send_msg.await_count == 1
    mc.commands.send_path_discovery_sync.assert_not_awaited()
    assert result is True  # best-effort: the one send was accepted (non-error)


def test_default_ack_sends_exactly_once():
    t, mc = _transport(client_retry=False)
    t._wait_for_ack = MagicMock(return_value=True)

    result = t.send_message("reply text", destination="aabbccdd1122")

    assert mc.commands.send_msg.await_count == 1
    mc.commands.send_path_discovery_sync.assert_not_awaited()
    assert result is True


def test_default_no_ack_error_event_returns_false_single_send():
    t, mc = _transport(client_retry=False)
    mc.commands.send_msg = AsyncMock(return_value=_err_event())
    t._wait_for_ack = MagicMock(return_value=False)

    result = t.send_message("reply text", destination="aabbccdd1122")

    assert mc.commands.send_msg.await_count == 1
    mc.commands.send_path_discovery_sync.assert_not_awaited()
    assert result is False


def test_default_no_ack_logs_single_send_mode(caplog):
    import logging
    t, mc = _transport(client_retry=False)
    t._wait_for_ack = MagicMock(return_value=False)

    with caplog.at_level(logging.INFO):
        t.send_message("reply text", destination="aabbccdd1122")

    assert any("single-send mode" in r.getMessage() for r in caplog.records)


def test_client_retry_enabled_restores_legacy_resend_behavior():
    """meshcore_client_retry=True: a missing ACK DOES trigger path discovery
    and a second send (the pre-fix behavior)."""
    t, mc = _transport(client_retry=True)
    t._wait_for_ack = MagicMock(return_value=False)

    result = t.send_message("reply text", destination="aabbccdd1122")

    assert mc.commands.send_msg.await_count == 2
    mc.commands.send_path_discovery_sync.assert_awaited()
    assert result is True


def test_client_retry_enabled_with_ack_sends_exactly_once():
    """Even with retry enabled, an ACKed first send never triggers discovery."""
    t, mc = _transport(client_retry=True)
    t._wait_for_ack = MagicMock(return_value=True)

    t.send_message("reply text", destination="aabbccdd1122")

    assert mc.commands.send_msg.await_count == 1
    mc.commands.send_path_discovery_sync.assert_not_awaited()


# ---------------------------------------------------------------------------
# Async _do_mc_dm_send_async() path (used by send_message_async / the
# per-radio send queue, and therefore by the "still thinking" notice too --
# see router._await_llm_with_thinking_notice, which sends through the same
# connector.send_message_async() call).
# ---------------------------------------------------------------------------


def test_async_default_no_ack_sends_exactly_once_no_discovery():
    t, mc = _transport(client_retry=False)
    t._wait_for_ack_async = AsyncMock(return_value=False)

    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(
            t._do_mc_dm_send_async("hi", "aabbccdd1122")
        )
    finally:
        loop.close()

    assert mc.commands.send_msg.await_count == 1
    mc.commands.send_path_discovery_sync.assert_not_awaited()
    assert result is True


def test_async_default_ack_sends_exactly_once():
    t, mc = _transport(client_retry=False)
    t._wait_for_ack_async = AsyncMock(return_value=True)

    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(
            t._do_mc_dm_send_async("hi", "aabbccdd1122")
        )
    finally:
        loop.close()

    assert mc.commands.send_msg.await_count == 1
    mc.commands.send_path_discovery_sync.assert_not_awaited()
    assert result is True


def test_async_client_retry_enabled_restores_legacy_resend_behavior():
    t, mc = _transport(client_retry=True)
    t._wait_for_ack_async = AsyncMock(return_value=False)

    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(
            t._do_mc_dm_send_async("hi", "aabbccdd1122")
        )
    finally:
        loop.close()

    assert mc.commands.send_msg.await_count == 2
    mc.commands.send_path_discovery_sync.assert_awaited()
    assert result is True


# ---------------------------------------------------------------------------
# Channel broadcast: already single-send (no ACK/resend loop exists for
# broadcasts at all) -- confirms meshcore_client_retry has no bearing on it,
# i.e. the "apply the same single-send rule to channel sends" requirement
# was already satisfied.
# ---------------------------------------------------------------------------


def test_channel_broadcast_is_single_send_regardless_of_client_retry():
    for client_retry in (False, True):
        t, mc = _transport(client_retry=client_retry)
        t._chan_name_to_idx = {"#aida": 2}
        mc.commands.send_chan_msg = AsyncMock(return_value=_ok_event())

        result = t.send_message("hello", meshcore_channel="#aida")

        assert mc.commands.send_chan_msg.await_count == 1
        assert result is True


# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------


def test_config_defaults_single_send_and_30s_ack_wait():
    cfg = ConnectionConfig()
    assert cfg.meshcore_client_retry is False
    assert cfg.meshcore_ack_wait_seconds == 30.0
