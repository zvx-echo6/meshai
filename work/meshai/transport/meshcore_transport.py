"""MeshCore transport backend (Phase 2).

Implements MeshTransport over a pyMC companion TCP frame server using the
meshcore lib.  The meshcore lib is fully async; we bridge it into the sync
MeshTransport interface via a dedicated asyncio event loop running in a
daemon thread.  Commands are dispatched with
``asyncio.run_coroutine_threadsafe()``.

The meshcore lib is LAZY-IMPORTED inside methods so this module can be
imported (and the test suite can run) without the lib installed.
"""

import asyncio
import concurrent.futures
import logging
import threading
import time as _time
from typing import Callable, Optional

from .base import MeshTransport
from .send_queue import RadioSendQueue
from ..connector import MeshMessage

logger = logging.getLogger(__name__)

# Default timeout for command futures (seconds).
_COMMAND_TIMEOUT = 10.0

# --- Telemetry auto-poll tuning -------------------------------------------
# Hard floor on the auto-poll interval (seconds) — airtime protection: a
# misconfigured tiny interval can never flood the mesh with telemetry requests.
_TELEMETRY_MIN_INTERVAL_SECONDS = 300
# Consecutive-timeout threshold before a contact is marked unavailable and
# dropped from the auto-poll rotation (a manual "Poll now" un-sticks it).
_TELEMETRY_MAX_FAILURES = 3

# Numeric Cayenne-LPP type id → decoded field name.  Ids not in this map are
# passed through as ``lpp_<id>`` so nothing is silently dropped.
_LPP_ID_TO_FIELD = {
    101: "illuminance",
    103: "temperature",
    104: "humidity",
    115: "barometer",
    116: "voltage",
    117: "current",
    120: "battery_pct",
    121: "altitude",
    128: "power",
    130: "distance",
    136: "gps",
}


def mc_context_allows(cfg, msg, idx_to_name):
    """Return True if a MeshCore inbound MeshMessage should be forwarded.

    cfg: MeshCoreContextConfig or None. idx_to_name: dict[int,str] channel-idx->name.
    """
    if cfg is None:
        return True
    if msg.is_dm:
        if not cfg.respond_to_dms:
            return False
        # ignore_contacts matches the pubkey prefix (sender_id) OR the contact name (sender_name)
        if msg.sender_id in cfg.ignore_contacts or msg.sender_name in cfg.ignore_contacts:
            return False
        return True
    # channel (non-DM) message -> only relevant for passive context
    if not cfg.enable_passive_context:
        return False
    # observe_channels is opt-in: empty = observe NONE; a channel message
    # passes ONLY if observe_channels is non-empty AND the resolved channel
    # name is in the list.
    if not cfg.observe_channels:
        return False
    name = idx_to_name.get(msg.channel)
    if name is None or name not in cfg.observe_channels:
        return False
    return True


class MeshCoreTransport(MeshTransport):
    """MeshTransport implementation over a pyMC companion TCP frame server.

    Async bridge: a dedicated asyncio event loop runs in a daemon thread
    (``self._loop`` / ``self._loop_thread``).  All coroutines are dispatched
    via ``asyncio.run_coroutine_threadsafe(..., self._loop).result(timeout)``.
    The meshcore client (``self._mc``) is created and used exclusively on that
    loop.

    The transport is dormant until ``connect()`` is called.  The factory
    only instantiates this class when ``meshcore_host`` is non-empty, so
    there is zero cost to Meshtastic-only deployments.
    """

    # Name tag used by CompositeTransport for routing hints.
    transport_name: str = "meshcore"

    def __init__(self, config, meshcore_context=None) -> None:
        self.config = config
        # MeshCore passive-context / bot-behavior filter (MeshCoreContextConfig
        # or None). None = pass-through (no filtering). Injected at construction
        # time by the factory; can also be (re)set via set_context_config().
        self._mc_context = meshcore_context
        # DM reply tuning (config knobs; getattr defaults keep old test configs valid).
        self._ack_wait = float(getattr(config, "meshcore_ack_wait_seconds", 6.0))
        self._discovery_wait = float(getattr(config, "meshcore_discovery_wait_seconds", 8.0))
        self._mc = None                          # meshcore.MeshCore instance
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._connected: bool = False
        self._self_info: dict = {}
        self._message_callback: Optional[Callable] = None
        self._callback_loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_ready: threading.Event = threading.Event()
        # Companion channel table: channel NAME -> slot index, built at
        # connect time by _enumerate_channels().  Empty until connected.
        self._chan_name_to_idx: dict[str, int] = {}
        # Self-advertisement tracking.
        self._last_advert_sent: Optional[float] = None   # epoch seconds or None
        # asyncio.Task handle for the periodic advert loop; None when inactive.
        self._advert_task = None
        # asyncio.Task handle for the telemetry auto-poll loop; None when inactive.
        self._telemetry_task = None
        # Telemetry availability/bookkeeping (shared by poller + on-demand):
        #   _telemetry_cache: contact-id -> {contact, data, polled_at, available}
        #   _telemetry_failures: contact-id -> consecutive-timeout count
        self._telemetry_cache: dict[str, dict] = {}
        self._telemetry_failures: dict[str, int] = {}
        # --- per-radio send queue (on the MC dedicated loop) ---
        self._mc_send_queue: Optional[asyncio.Queue] = None
        self._mc_drain_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run_coro(self, coro, timeout: float = _COMMAND_TIMEOUT):
        """Submit *coro* to the dedicated event loop and block until done.

        Raises RuntimeError if the loop isn't running.
        """
        if self._loop is None or not self._loop.is_running():
            raise RuntimeError("MeshCoreTransport: event loop is not running")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def _stop_loop(self) -> None:
        """Signal the event loop to stop and join the thread."""
        if self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                pass
        if self._loop_thread is not None and self._loop_thread.is_alive():
            self._loop_thread.join(timeout=5.0)
        self._loop = None
        self._loop_thread = None

    def _resolve_contact(self, dest: str):
        """Resolve a pubkey prefix (or key) to the full MeshCore contact dict.

        Fast path: if the lib's cached contact mirror already has a match for
        *dest*, return it immediately with no serial round-trip.

        On a cache miss, force a full contact re-fetch directly from the
        firmware (``get_contacts(lastmod=0)``).  ``lastmod=0`` fetches ALL
        contacts, bypassing the no-op cache guard in ``ensure_contacts()``
        (which skips the fetch once ``self._contacts`` is already populated).
        This pulls in any contacts the firmware has auto-added since the last
        sync (e.g. the sender of an inbound DM).  The lib merges the result
        into its ``_contacts`` cache automatically via its CONTACTS/NEXT_CONTACT
        event handler, so a subsequent ``get_contact_by_key_prefix`` will find
        the freshly-learned entry.

        Returns None if the firmware genuinely has no such contact.
        """
        if self._mc is None:
            return None
        # Fast path: contact already in the lib's cached mirror.
        try:
            contact = self._mc.get_contact_by_key_prefix(dest)
            if contact is not None:
                return contact
        except Exception:
            logger.debug(
                "MeshCore: get_contact_by_key_prefix (fast path) failed for %s", dest, exc_info=True
            )
            return None
        # Miss: force a full re-fetch so the firmware's auto-added contacts
        # (including the sender of the inbound DM we're about to reply to)
        # are merged into the lib's cache before we retry.
        try:
            self._run_coro(self._mc.commands.get_contacts(lastmod=0), timeout=15)
        except Exception:
            logger.debug(
                "MeshCore: get_contacts(lastmod=0) refetch failed for %s", dest, exc_info=True
            )
        # Retry with the freshly merged roster.
        try:
            return self._mc.get_contact_by_key_prefix(dest)
        except Exception:
            logger.debug(
                "MeshCore: get_contact_by_key_prefix (retry) failed for %s", dest, exc_info=True
            )
            return None

    def _establish_direct_path(self, contact: dict, dst: str) -> None:
        """Best-effort: establish a DIRECT route to *contact* before replying.

        Runs path discovery (CMD 52) so the recipient returns a PATH packet; the
        lib updates the contact's out_path and subsequent send_msg goes DIRECT
        instead of flood (flood DMs are silently rejected on this mesh).

        Falls back to seeding the path from a cached advert path if discovery
        times out and the contact is still marked as flood (out_path_len == -1).

        Never raises; all steps are individually guarded.
        """
        label = contact.get("adv_name") or dst

        # Step 1: trigger path discovery (CMD 52 → 0x34); wait for PATH_RESPONSE.
        # The lib updates the contact's out_path internally when it receives the
        # response, so a subsequent send_msg will use the direct route.
        try:
            path_event = self._run_coro(
                self._mc.commands.send_path_discovery_sync(contact, timeout=self._discovery_wait),
                timeout=self._discovery_wait + 3,
            )
            if path_event is not None and not path_event.is_error():
                logger.info(
                    "MeshCore: path discovery to %s succeeded (PATH_RESPONSE received)", label
                )
            else:
                logger.info(
                    "MeshCore: path discovery to %s — no PATH_RESPONSE; trying advert-path fallback",
                    label,
                )
        except Exception as exc:
            logger.debug("MeshCore: send_path_discovery_sync to %s failed: %s", dst, exc)

        # Step 2 (fallback): if the contact is still flood after discovery,
        # seed the route from a cached advert path.
        try:
            fresh_contact = self._resolve_contact(dst) or contact
            if fresh_contact.get("out_path_len", -1) < 0:
                try:
                    ap_event = self._run_coro(
                        self._mc.commands.get_advert_path(fresh_contact),
                        timeout=10,
                    )
                    if (
                        ap_event is not None
                        and not ap_event.is_error()
                        and isinstance(getattr(ap_event, "payload", None), dict)
                    ):
                        path_hex = ap_event.payload.get("path", "")
                        path_len = ap_event.payload.get("path_len", -1)
                        if path_len > 0 and path_hex:
                            self._run_coro(
                                self._mc.commands.update_contact(fresh_contact, path=path_hex),
                                timeout=10,
                            )
                            logger.info(
                                "MeshCore: seeded direct path for %s from advert-path cache (len=%d)",
                                fresh_contact.get("adv_name") or dst,
                                path_len,
                            )
                        else:
                            logger.debug(
                                "MeshCore: advert-path for %s is flood/empty — leaving as flood", dst
                            )
                except Exception as exc:
                    logger.debug("MeshCore: advert-path fallback for %s failed: %s", dst, exc)
        except Exception as exc:
            logger.debug("MeshCore: _establish_direct_path step-2 error for %s: %s", dst, exc)

    @staticmethod
    def _extract_expected_ack(result):
        """Pull the ``expected_ack`` value off a MSG_SENT event, or None.

        The lib's reader puts the raw 4-byte ack code at
        ``payload["expected_ack"]`` (see reader.py MSG_SENT branch). Returns it
        as-is (bytes on the real lib; may be a hex str in tests) or None if the
        event has no dict payload / no ack.
        """
        payload = getattr(result, "payload", None)
        if isinstance(payload, dict):
            return payload.get("expected_ack")
        return None

    def _wait_for_ack(self, expected_ack, timeout: float) -> bool:
        """Block until the delivery ACK matching *expected_ack* arrives, or timeout.

        Mirrors ``send_msg_with_retry``'s ACK wait (messaging.py): the firmware
        emits ``EventType.ACK`` with a hex ``code`` attribute equal to the
        MSG_SENT ``expected_ack`` rendered as ``.hex()`` (reader.py ACK branch).
        We run the lib's dispatcher wait on the transport loop via ``_run_coro``.

        Returns True only if a matching ACK is dispatched before *timeout*;
        False when *expected_ack* is falsy (can't confirm) or on any
        timeout/exception. Coexists with the standing ``_on_ack_event``
        subscription — the dispatcher supports concurrent subscribers + waiters.
        """
        if not expected_ack:
            return False
        from meshcore import EventType  # noqa: PLC0415 (lazy import, matches module)
        try:
            code = (
                expected_ack.hex()
                if isinstance(expected_ack, (bytes, bytearray))
                else str(expected_ack)
            )
        except Exception:
            return False
        try:
            event = self._run_coro(
                self._mc.dispatcher.wait_for_event(
                    EventType.ACK,
                    attribute_filters={"code": code},
                    timeout=timeout,
                ),
                timeout=timeout + 2,
            )
            return event is not None
        except Exception:
            logger.debug(
                "MeshCore: ACK wait failed for code %r", expected_ack, exc_info=True
            )
            return False

    # ------------------------------------------------------------------
    # Async send helpers — run ON the MC dedicated loop (no _run_coro).
    # These are called from the MC drain task directly.
    # ------------------------------------------------------------------

    async def _resolve_contact_async(self, dest: str):
        """Resolve contact on the MC loop (no _run_coro, no deadlock risk)."""
        if self._mc is None:
            return None
        try:
            contact = self._mc.get_contact_by_key_prefix(dest)
            if contact is not None:
                return contact
        except Exception:
            return None
        # Cache miss: force a full re-fetch from firmware.
        try:
            await asyncio.wait_for(self._mc.commands.get_contacts(lastmod=0), timeout=15)
        except Exception:
            logger.debug("MC: _resolve_contact_async get_contacts failed for %s", dest, exc_info=True)
        try:
            return self._mc.get_contact_by_key_prefix(dest)
        except Exception:
            return None

    async def _send_dm_once_async(self, contact: dict, text: str, destination: str):
        """Send one DM frame on the MC loop; returns MSG_SENT event or None."""
        try:
            result = await asyncio.wait_for(
                self._mc.commands.send_msg(contact, text), timeout=15
            )
        except Exception as exc:
            logger.warning("MC: _send_dm_once_async to %s failed: %s", destination, exc)
            return None
        if result is None:
            logger.warning("MC: _send_dm_once_async to %s — no send result", destination)
            return None
        try:
            sent_type = (
                result.payload.get("type")
                if hasattr(result, "payload") and isinstance(result.payload, dict)
                else None
            )
            logger.info(
                "MC: DM to %s sent (route=%s)",
                contact.get("adv_name") or destination,
                "flood" if sent_type == 1 else ("direct" if sent_type == 0 else "?"),
            )
        except Exception:
            pass
        return result

    async def _wait_for_ack_async(self, expected_ack, timeout: float) -> bool:
        """Wait for delivery ACK on the MC loop."""
        if not expected_ack:
            return False
        from meshcore import EventType  # noqa: PLC0415
        try:
            code = (
                expected_ack.hex()
                if isinstance(expected_ack, (bytes, bytearray))
                else str(expected_ack)
            )
        except Exception:
            return False
        try:
            event = await asyncio.wait_for(
                self._mc.dispatcher.wait_for_event(
                    EventType.ACK,
                    attribute_filters={"code": code},
                    timeout=timeout,
                ),
                timeout=timeout + 2,
            )
            return event is not None
        except Exception:
            logger.debug("MC: _wait_for_ack_async failed for %r", expected_ack, exc_info=True)
            return False

    async def _establish_direct_path_async(self, contact: dict, dst: str) -> None:
        """Path discovery on the MC loop (async version of _establish_direct_path)."""
        label = contact.get("adv_name") or dst
        try:
            path_event = await asyncio.wait_for(
                self._mc.commands.send_path_discovery_sync(contact, timeout=self._discovery_wait),
                timeout=self._discovery_wait + 3,
            )
            if path_event is not None and not path_event.is_error():
                logger.info("MC: path discovery to %s succeeded", label)
            else:
                logger.info("MC: path discovery to %s no PATH_RESPONSE; trying advert-path fallback", label)
        except Exception as exc:
            logger.debug("MC: send_path_discovery_sync to %s failed: %s", dst, exc)

        try:
            fresh_contact = await self._resolve_contact_async(dst) or contact
            if fresh_contact.get("out_path_len", -1) < 0:
                try:
                    ap_event = await asyncio.wait_for(
                        self._mc.commands.get_advert_path(fresh_contact), timeout=10
                    )
                    if (
                        ap_event is not None
                        and not ap_event.is_error()
                        and isinstance(getattr(ap_event, "payload", None), dict)
                    ):
                        path_hex = ap_event.payload.get("path", "")
                        path_len = ap_event.payload.get("path_len", -1)
                        if path_len > 0 and path_hex:
                            await asyncio.wait_for(
                                self._mc.commands.update_contact(fresh_contact, path=path_hex),
                                timeout=10,
                            )
                            logger.info(
                                "MC: seeded direct path for %s (len=%d)",
                                fresh_contact.get("adv_name") or dst, path_len,
                            )
                except Exception as exc:
                    logger.debug("MC: advert-path fallback for %s failed: %s", dst, exc)
        except Exception as exc:
            logger.debug("MC: _establish_direct_path_async step-2 for %s: %s", dst, exc)

    async def _enumerate_channels_async(self) -> None:
        """Channel enumeration on the MC loop (async version of _enumerate_channels)."""
        self._chan_name_to_idx = {}
        try:
            empty_run = 0
            for idx in range(40):
                try:
                    event = await asyncio.wait_for(
                        self._mc.commands.get_channel(idx), timeout=5
                    )
                except Exception as exc:
                    logger.debug("MC: get_channel(%d) async failed, ending: %s", idx, exc)
                    break
                if not event:
                    break
                is_err = getattr(event, "is_error", None)
                if callable(is_err) and event.is_error():
                    break
                payload = event.payload or {}
                name = payload.get("channel_name", "")
                slot = payload.get("channel_idx", idx)
                if not name:
                    empty_run += 1
                    if empty_run >= 3:
                        break
                    continue
                self._chan_name_to_idx[name] = slot
                empty_run = 0
        except Exception as exc:
            logger.warning("MC: async channel enumeration error: %s", exc)
            self._chan_name_to_idx = {}
        logger.info("MC: async enumerated %d named channel(s)", len(self._chan_name_to_idx))

    async def _do_mc_dm_send_async(self, text: str, destination: str) -> bool:
        """Full DM send path on the MC loop (replaces send_message() DM branch)."""
        if self._mc is None:
            return False
        contact = await self._resolve_contact_async(destination)
        if contact is None:
            logger.warning("MC: could not resolve contact %s; cannot DM", destination)
            return False
        label = contact.get("adv_name") or destination

        # Fast path: send and wait for ACK.
        result = await self._send_dm_once_async(contact, text, destination)
        if result is None:
            return False
        exp_ack = self._extract_expected_ack(result)
        if await self._wait_for_ack_async(exp_ack, self._ack_wait):
            logger.info("MC: DM to %s ACKed (direct)", label)
            return True

        # No ACK: path discovery + retry.
        logger.info("MC: no ACK from %s in %.1fs; running path discovery", label, self._ack_wait)
        await self._establish_direct_path_async(contact, destination)
        contact = await self._resolve_contact_async(destination) or contact
        result = await self._send_dm_once_async(contact, text, destination)
        if result is None:
            return False
        exp_ack = self._extract_expected_ack(result)
        acked = await self._wait_for_ack_async(exp_ack, self._ack_wait)
        logger.info(
            "MC: DM to %s %s after discovery",
            contact.get("adv_name") or destination,
            "ACKed" if acked else "no ACK (sent best-effort)",
        )
        return acked or (not result.is_error())

    async def _do_mc_broadcast_async(self, text: str, meshcore_channel: str) -> bool:
        """Channel broadcast on the MC loop (replaces send_message() broadcast branch)."""
        if self._mc is None:
            return False
        idx = self._chan_name_to_idx.get(meshcore_channel)
        if idx is None:
            # Lazy async re-enumeration (no _run_coro deadlock risk).
            await self._enumerate_channels_async()
            idx = self._chan_name_to_idx.get(meshcore_channel)
        if idx is None:
            logger.warning("MC channel '%s' not on companion; skipping", meshcore_channel)
            return False
        try:
            result = await asyncio.wait_for(
                self._mc.commands.send_chan_msg(idx, text), timeout=10
            )
            success = not result.is_error()
            if not success:
                logger.warning("MC: broadcast returned error event")
            return success
        except Exception as exc:
            logger.error("MC: _do_mc_broadcast_async failed: %s", exc)
            return False

    async def _do_mc_advert_async(self) -> bool:
        """Send self-advert on the MC loop (queued version)."""
        if self._mc is None or not self._connected:
            return False
        try:
            await self._mc.commands.send_advert(flood=True)
            self._last_advert_sent = _time.time()
            return True
        except Exception as exc:
            logger.warning("MC: queued advert failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # MC send queue management
    # ------------------------------------------------------------------

    async def _mc_drain_loop(self) -> None:
        """Drain loop for the MC send queue — runs on the MC dedicated loop."""
        while True:
            item = await self._mc_send_queue.get()
            async_fn, cfut = item
            result = False
            try:
                result = await async_fn()
            except asyncio.CancelledError:
                if cfut is not None and not cfut.done():
                    cfut.cancel()
                raise
            except Exception as exc:
                logger.error("MC queue drain: send job raised: %s", exc, exc_info=True)
                if cfut is not None and not cfut.done():
                    cfut.set_exception(exc)
            else:
                if cfut is not None and not cfut.done():
                    cfut.set_result(result)

            # Pace: read live from config.
            pacing = max(0.25, getattr(self.config, "meshcore_send_pacing_seconds", 2.0))
            await asyncio.sleep(pacing)

    def _start_mc_queue(self) -> None:
        """Arm the MC send queue + drain task on the MC dedicated loop.
        Call via loop.call_soon_threadsafe from the main thread after connect."""
        self._mc_send_queue = asyncio.Queue()
        self._mc_drain_task = asyncio.get_event_loop().create_task(
            self._mc_drain_loop(), name="mc-send-queue-drain"
        )
        logger.debug("MC: send queue drain task started")

    def _cancel_mc_queue(self) -> None:
        """Cancel the MC drain task (thread-safe).  Called at disconnect."""
        task = self._mc_drain_task
        self._mc_drain_task = None
        if task is not None and self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(task.cancel)

    async def send_message_async(
        self,
        text: str,
        destination: Optional[str] = None,
        channel: int = 0,
        transport: Optional[str] = None,
        meshcore_channel: Optional[str] = None,
    ) -> bool:
        """Async send through the MC per-radio queue (called from the main loop).

        Enqueues the job on the MC loop's queue and awaits the actual send
        result via a concurrent.futures.Future bridge.
        """
        if self._mc is None or not self._connected:
            return False
        if self._mc_send_queue is None or self._loop is None or not self._loop.is_running():
            # Queue not started yet (e.g. initial advert at connect) — fall back.
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None,
                lambda: self.send_message(text, destination, channel, transport, meshcore_channel),
            )

        cfut: concurrent.futures.Future = concurrent.futures.Future()

        if destination:
            async def _job() -> bool:
                return await self._do_mc_dm_send_async(text, destination)
        else:
            if meshcore_channel is None:
                logger.debug("MC: send_message_async meshcore_channel=None, skipping broadcast")
                return True
            async def _job() -> bool:
                return await self._do_mc_broadcast_async(text, meshcore_channel)

        # Enqueue on MC loop from main loop.
        asyncio.run_coroutine_threadsafe(
            self._mc_send_queue.put((_job, cfut)),
            self._loop,
        )

        main_loop = asyncio.get_event_loop()
        return await asyncio.wrap_future(cfut, loop=main_loop)

    def _enqueue_mc_fire_forget(self, async_fn) -> None:
        """Enqueue fire-and-forget from any thread (for advert/telemetry loops)."""
        if self._mc_send_queue is None or self._loop is None or not self._loop.is_running():
            logger.debug("MC: fire-and-forget dropped (queue not started)")
            return
        asyncio.run_coroutine_threadsafe(
            self._mc_send_queue.put((async_fn, None)),
            self._loop,
        )

    async def _enqueue_mc_loop_send(self, async_fn) -> bool:
        """Enqueue from within the MC loop and await the result (for advert/telemetry loops)."""
        if self._mc_send_queue is None:
            # Queue not started; fall through to direct call.
            return await async_fn()
        cfut: concurrent.futures.Future = concurrent.futures.Future()
        await self._mc_send_queue.put((async_fn, cfut))
        return await asyncio.wrap_future(cfut)

    def _send_dm_once(self, contact: dict, text: str, destination: str):
        """Send one DM frame (no discovery, no retry) and log its route.

        Uses plain ``send_msg`` — NOT ``send_msg_with_retry``, which calls
        ``reset_path`` and forces flood, defeating any direct route we hold.
        Returns the MSG_SENT event, or None if the radio produced no result.
        """
        result = self._run_coro(
            self._mc.commands.send_msg(contact, text),
            timeout=15,
        )
        if result is None:
            logger.warning("MeshCoreTransport: DM to %s — no send result", destination)
            return None
        # Log whether the radio sent DIRECT or FLOOD from the MSG_SENT type field.
        try:
            sent_type = (
                result.payload.get("type")
                if hasattr(result, "payload") and isinstance(result.payload, dict)
                else None
            )
            logger.info(
                "MeshCore: DM to %s sent (route=%s)",
                contact.get("adv_name") or destination,
                "flood" if sent_type == 1 else ("direct" if sent_type == 0 else "?"),
            )
        except Exception:
            pass
        return result

    # ------------------------------------------------------------------
    # Channel table enumeration
    # ------------------------------------------------------------------

    def _enumerate_channels(self) -> None:
        """Build ``self._chan_name_to_idx`` from the companion's channel table.

        MeshCore channels are {name, PSK} pairs living in numbered slots (up
        to 40+, unlike Meshtastic's 0-7).  We ask the companion for each slot
        in turn via ``get_channel(idx)`` and record NAME → slot for every
        named (non-empty) slot, so send_message can resolve a per-family
        channel NAME to the right slot at send time.

        Robustness:
          - Any error yields an empty (or partial) map — never raises out.
          - Enumeration stops on the first error/None result (end of table)
            or after 3 consecutive empty slots (contiguous provisioning),
            with a hard cap of 40 slots.
        """
        self._chan_name_to_idx = {}
        try:
            empty_run = 0
            for idx in range(40):
                try:
                    event = self._run_coro(self._mc.commands.get_channel(idx))
                except Exception as exc:
                    logger.debug(
                        "MeshCore: get_channel(%d) failed, ending enumeration: %s",
                        idx, exc,
                    )
                    break
                # Falsy / None / ERROR event → end of enumeration.
                if not event:
                    break
                is_err = getattr(event, "is_error", None)
                if callable(is_err) and event.is_error():
                    break
                payload = event.payload or {}
                name = payload.get("channel_name", "")
                slot = payload.get("channel_idx", idx)
                if not name:
                    # Empty/unset slot; stop after a contiguous run of empties.
                    empty_run += 1
                    if empty_run >= 3:
                        break
                    continue
                # Named slot: exact, case-sensitive (firmware name is already
                # null-truncated / utf-8-decoded — do NOT trim or lowercase).
                self._chan_name_to_idx[name] = slot
                empty_run = 0
        except Exception as exc:
            logger.warning("MeshCore: channel enumeration error: %s", exc)
            self._chan_name_to_idx = {}
        logger.info(
            "MeshCore: enumerated %d named channel(s)", len(self._chan_name_to_idx)
        )

    def known_channels(self) -> list[str]:
        """Enumerated MeshCore channel names (from _chan_name_to_idx, populated at connect)."""
        return list(self._chan_name_to_idx.keys())

    def get_contacts(self) -> list[dict]:
        """Roster of known MeshCore contacts. [] if not connected."""
        if self._mc is None or not self._connected:
            return []
        try:
            ensure = getattr(self._mc, "ensure_contacts", None)
            if ensure is not None:
                self._run_coro(ensure())
        except Exception:
            pass
        contacts = getattr(self._mc, "contacts", None) or {}
        roster: list[dict] = []
        for pubkey_hex, c in contacts.items():
            if not isinstance(c, dict):
                continue
            roster.append({
                "name": c.get("adv_name"),
                "pubkey": c.get("public_key") or pubkey_hex,
                "type": c.get("type"),
                "last_advert": c.get("last_advert"),
                "lat": c.get("adv_lat"),
                "lon": c.get("adv_lon"),
                "out_path_len": c.get("out_path_len"),
            })
        return roster

    def self_info(self) -> dict:
        """Companion self/connection status. {connected: False} if not connected."""
        if self._mc is None or not self._connected:
            return {"connected": False}
        info = self._self_info or {}
        return {
            "name": info.get("name"),
            "pubkey": info.get("public_key"),
            "connected": True,
            "host": getattr(self.config, "meshcore_host", "100.64.0.9"),
            "port": getattr(self.config, "meshcore_port", 5050),
            "channel_count": len(self.known_channels()),
            "last_advert_sent": self._last_advert_sent,
        }

    def set_context_config(self, cfg) -> None:
        """Set (or clear) the MeshCore passive-context filter config.

        cfg: MeshCoreContextConfig or None (None = pass-through).
        """
        self._mc_context = cfg

    # ------------------------------------------------------------------
    # Self-advertisement
    # ------------------------------------------------------------------

    def send_advert(self) -> bool:
        """Broadcast a signed self-advertisement to the mesh (flood=True).

        Bridges the async ``mc.commands.send_advert`` call to the dedicated
        event loop via ``_run_coro``.  Safe no-op returning False when not
        connected or when the lib command raises.

        Callers must log the human-readable context (manual / on-connect);
        this method is intentionally silent on success to avoid duplicate
        log lines across call sites.
        """
        if self._mc is None or not self._connected:
            logger.debug("MeshCore: send_advert skipped — not connected")
            return False
        try:
            self._run_coro(self._mc.commands.send_advert(flood=True))
            self._last_advert_sent = _time.time()
            return True
        except Exception as exc:
            logger.warning("MeshCore: send_advert failed: %s", exc)
            return False

    async def send_advert_async(self) -> bool:
        """Async self-advertisement through the MC per-radio queue (main-loop caller).

        Mirrors ``send_message_async`` — enqueues ``_do_mc_advert_async`` on the
        MC loop queue from the main loop, awaits the result via a
        concurrent.futures.Future bridge.  Falls back to a thread-executor
        wrapping the synchronous ``send_advert`` when the queue isn't running.
        """
        if self._mc is None or not self._connected:
            return False
        if self._mc_send_queue is None or self._loop is None or not self._loop.is_running():
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self.send_advert)

        cfut: concurrent.futures.Future = concurrent.futures.Future()
        asyncio.run_coroutine_threadsafe(
            self._mc_send_queue.put((self._do_mc_advert_async, cfut)),
            self._loop,
        )
        main_loop = asyncio.get_event_loop()
        return await asyncio.wrap_future(cfut, loop=main_loop)

    async def _periodic_advert_loop(self, interval: int) -> None:
        """Periodic self-advertisement coroutine (runs as a Task on the dedicated loop).

        Sleeps *interval* seconds, sends one flood advert via the send queue
        (serialized with other MC sends), repeats.  Stops on CancelledError.
        """
        try:
            while True:
                await asyncio.sleep(interval)
                if not self._connected or self._mc is None:
                    return
                try:
                    ok = await self._enqueue_mc_loop_send(self._do_mc_advert_async)
                    if ok:
                        logger.info("MC: sent periodic self-advert")
                    else:
                        logger.warning("MC: periodic send_advert returned False")
                except Exception as exc:
                    logger.warning("MC: periodic send_advert failed: %s", exc)
        except asyncio.CancelledError:
            logger.debug("MC: periodic advert task cancelled")
            raise

    def _schedule_periodic_advert(self, interval: int) -> None:
        """Create the periodic advert asyncio.Task on the dedicated loop (thread-safe).

        Called from the main thread after connect(); the Task is created ON the
        dedicated loop via call_soon_threadsafe so asyncio.create_task() fires
        in the right context.
        """
        def _arm() -> None:
            self._advert_task = asyncio.get_event_loop().create_task(
                self._periodic_advert_loop(interval)
            )
        self._loop.call_soon_threadsafe(_arm)

    def _cancel_periodic_advert(self) -> None:
        """Cancel the periodic advert task (thread-safe).  Called at disconnect."""
        task = self._advert_task
        self._advert_task = None
        if task is not None and self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(task.cancel)
        # Also cancel the MC send queue drain.
        self._cancel_mc_queue()

    # ------------------------------------------------------------------
    # Telemetry (MeshCore sensor auto-poll)
    # ------------------------------------------------------------------

    def _resolve_contact(self, contact_id: str):
        """Resolve *contact_id* (a pubkey/prefix OR a name) to a contact dict.

        Mirrors DM / get_node_name resolution: try key-prefix first, then name.
        Returns the raw contact dict, or None if not resolvable / not connected.
        """
        if self._mc is None or not self._connected:
            return None
        try:
            contact = self._mc.get_contact_by_key_prefix(contact_id)
            if contact:
                return contact
        except Exception:
            pass
        try:
            contact = self._mc.get_contact_by_name(contact_id)
            if contact:
                return contact
        except Exception:
            pass
        return None

    @staticmethod
    def _decode_lpp(lpp) -> dict:
        """Decode a MeshCore telemetry ``lpp`` list into a flat field dict.

        Each element is ``{"channel": int, "type": int, "value": <num|dict>}``.
        The numeric ``type`` id is mapped to a field name via _LPP_ID_TO_FIELD;
        unknown ids become ``lpp_<id>``.  The original list is always preserved
        under the ``raw`` key.
        """
        out: dict = {}
        for elem in (lpp or []):
            if not isinstance(elem, dict):
                continue
            lpp_id = elem.get("type")
            field_name = _LPP_ID_TO_FIELD.get(lpp_id, f"lpp_{lpp_id}")
            out[field_name] = elem.get("value")
        out["raw"] = lpp
        return out

    def _record_telemetry_result(self, contact_id: str, data) -> None:
        """Update the shared cache/failure bookkeeping for a poll result.

        ``data`` is a decoded dict on success or None on timeout/no-response.
        On success: cache the reading, reset the failure counter, available=True.
        On None: bump the failure counter; once it reaches _TELEMETRY_MAX_FAILURES
        the entry is marked available=False (last data retained); before that the
        entry stays available with its previous data (only polled_at is refreshed).
        """
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        if data is not None:
            self._telemetry_failures[contact_id] = 0
            self._telemetry_cache[contact_id] = {
                "contact": contact_id,
                "data": data,
                "polled_at": now,
                "available": True,
            }
            return
        # Miss: increment consecutive-failure counter.
        fails = self._telemetry_failures.get(contact_id, 0) + 1
        self._telemetry_failures[contact_id] = fails
        prev = self._telemetry_cache.get(contact_id, {})
        entry = {
            "contact": contact_id,
            "data": prev.get("data"),
            "polled_at": now,
            "available": prev.get("available", True),
        }
        if fails >= _TELEMETRY_MAX_FAILURES:
            entry["available"] = False
        self._telemetry_cache[contact_id] = entry

    async def _req_telemetry_async(self, contact_id):
        """Resolve, request+await, decode telemetry for *contact_id* (on the loop).

        Runs entirely on the dedicated event loop.  Now routes the actual radio
        request through the MC send queue so it serializes with other MC sends.
        Returns the decoded dict, or None on unresolved/timeout/no-response/error.
        """
        contact = self._resolve_contact(contact_id)
        if contact is None:
            return None

        async def _telem_job() -> bool:
            """Wraps the telemetry fetch; result is stored in cache, job returns True on data."""
            try:
                lpp = await self._mc.commands.req_telemetry_sync(contact, min_timeout=5)
            except Exception as exc:
                logger.warning("MC: req_telemetry(%s) error: %s", contact_id, exc)
                self._record_telemetry_result(contact_id, None)
                return False
            if lpp is None:
                self._record_telemetry_result(contact_id, None)
                return False
            data = self._decode_lpp(lpp)
            self._record_telemetry_result(contact_id, data)
            return True

        await self._enqueue_mc_loop_send(_telem_job)
        # Return the now-updated cache entry's data field.
        entry = self._telemetry_cache.get(contact_id)
        return entry.get("data") if entry else None

    def req_telemetry(self, contact_id):
        """On-demand telemetry poll for *contact_id* (sync, bridged to the loop).

        A successful manual poll resets the contact's failure counter and flips
        it back to available in the cache (un-sticks an unavailable node).
        Returns the decoded telemetry dict, or None if unresolved / no response /
        not connected.
        """
        if self._mc is None or not self._connected:
            return None
        try:
            return self._run_coro(
                self._req_telemetry_async(contact_id), timeout=25.0
            )
        except Exception as exc:
            logger.warning("MeshCore: req_telemetry(%s) failed: %s", contact_id, exc)
            return None

    async def req_telemetry_async(self, contact_id: str):
        """Async on-demand telemetry poll (main-loop caller, routes through MC queue).

        Mirrors ``req_telemetry`` but awaitable from the main asyncio event loop.
        Enqueues the poll job on the MC loop queue via a concurrent.futures.Future
        bridge, then returns the cache entry's data.  Falls back to run_in_executor
        wrapping the synchronous ``req_telemetry`` when the queue isn't running.
        """
        if self._mc is None or not self._connected:
            return None
        if self._mc_send_queue is None or self._loop is None or not self._loop.is_running():
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, lambda: self.req_telemetry(contact_id))

        cfut: concurrent.futures.Future = concurrent.futures.Future()

        async def _telem_job_outer() -> bool:
            """Bridge: runs _req_telemetry_async on the MC loop; caches result."""
            try:
                data = await self._req_telemetry_async(contact_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("MC: req_telemetry_async(%s) failed: %s", contact_id, exc)
                return False
            return data is not None

        asyncio.run_coroutine_threadsafe(
            self._mc_send_queue.put((_telem_job_outer, cfut)),
            self._loop,
        )
        main_loop = asyncio.get_event_loop()
        await asyncio.wrap_future(cfut, loop=main_loop)
        # Result is in the telemetry cache after the job completes.
        entry = self._telemetry_cache.get(contact_id)
        return entry.get("data") if entry else None

    def get_telemetry_cache(self) -> list[dict]:
        """Return the current telemetry cache entries (list of dicts)."""
        return list(self._telemetry_cache.values())

    def _effective_telemetry_interval(self):
        """Compute the effective auto-poll interval (seconds), or None if disabled.

        raw <= 0 → disabled (None).  Otherwise the raw interval clamped UP to the
        _TELEMETRY_MIN_INTERVAL_SECONDS floor (airtime protection).
        """
        raw = getattr(self.config, "meshcore_telemetry_interval_seconds", 1800)
        if raw <= 0:
            return None
        return max(raw, _TELEMETRY_MIN_INTERVAL_SECONDS)

    async def _telemetry_poll_loop(self) -> None:
        """Auto-poll selected contacts for telemetry (Task on the dedicated loop).

        Airtime guards:
          - min-floor: interval is clamped up to _TELEMETRY_MIN_INTERVAL_SECONDS.
          - selected-only: iterates ONLY config.meshcore_telemetry_contacts, never
            the whole roster.
          - sequential + gap: one contact at a time with a 2 s gap between each
            (the lib also serializes mesh requests with an internal lock).
          - availability stop: contacts at/over _TELEMETRY_MAX_FAILURES are skipped
            (not auto-polled) until a manual poll un-sticks them.
        Stops on CancelledError (disconnect) or when the transport drops its link.
        """
        interval = self._effective_telemetry_interval()
        if interval is None:
            return  # disabled
        try:
            while True:
                await asyncio.sleep(interval)
                if not self._connected or self._mc is None:
                    return
                # Read the SELECTED list fresh each cycle (GUI may have changed it).
                contacts = list(
                    getattr(self.config, "meshcore_telemetry_contacts", []) or []
                )
                for c in contacts:
                    if not self._connected or self._mc is None:
                        return
                    # Availability stop: don't auto-poll a stuck contact.
                    if self._telemetry_failures.get(c, 0) >= _TELEMETRY_MAX_FAILURES:
                        continue
                    try:
                        data = await self._req_telemetry_async(c)
                        if data is not None:
                            logger.info("MeshCore: telemetry polled %s", c)
                        else:
                            logger.debug("MeshCore: telemetry miss for %s", c)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.warning(
                            "MeshCore: telemetry poll error for %s: %s", c, exc
                        )
                    # Sequential gap between contacts (airtime spacing).
                    await asyncio.sleep(2)
        except asyncio.CancelledError:
            logger.debug("MeshCore: telemetry poll task cancelled")
            raise
        except Exception as exc:
            logger.warning("MeshCore: telemetry poll loop error: %s", exc)

    def _schedule_telemetry_poll(self) -> None:
        """Create the telemetry auto-poll Task on the dedicated loop (thread-safe)."""
        def _arm() -> None:
            self._telemetry_task = asyncio.get_event_loop().create_task(
                self._telemetry_poll_loop()
            )
        self._loop.call_soon_threadsafe(_arm)

    def _cancel_telemetry_poll(self) -> None:
        """Cancel the telemetry auto-poll task (thread-safe).  Called at disconnect."""
        task = self._telemetry_task
        self._telemetry_task = None
        if task is not None and self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(task.cancel)

    # ------------------------------------------------------------------
    # Internal coroutines (run on the dedicated loop)
    # ------------------------------------------------------------------

    async def _do_connect(self, conn_type: str, target: str,
                          auto_reconnect: bool, max_attempts: int,
                          host: str = "", port: int = 5050,
                          serial_port: str = "", baud: int = 115200,
                          ble_address: str = ""):
        """Lazy-import meshcore and create the appropriate client.

        ``conn_type`` selects tcp | serial | ble.  ``target`` is a human-readable
        string used only for logging (built in connect()).
        """
        from meshcore import MeshCore  # noqa: PLC0415 (lazy import intentional)
        if conn_type == "serial":
            mc = await MeshCore.create_serial(
                serial_port,
                baudrate=baud,
                auto_reconnect=auto_reconnect,
                max_reconnect_attempts=max_attempts,
            )
        elif conn_type == "ble":
            mc = await MeshCore.create_ble(
                address=(ble_address or None),
                auto_reconnect=auto_reconnect,
                max_reconnect_attempts=max_attempts,
            )
        else:  # tcp (default)
            mc = await MeshCore.create_tcp(
                host, port,
                auto_reconnect=auto_reconnect,
                max_reconnect_attempts=max_attempts,
            )
        return mc

    async def _setup_subscriptions(self) -> None:
        """Subscribe to inbound events, then start auto message fetching."""
        from meshcore import EventType  # noqa: PLC0415
        # Subscribe BEFORE starting auto-fetch: start_auto_message_fetching() drains
        # the companion queue immediately, which would dispatch CONTACT_MSG_RECV before
        # our handler is registered and silently lose a DM queued at connect time.
        # (Canonical lib order: subscribe -> ensure_contacts -> start fetching.)
        self._mc.subscribe(EventType.CONTACT_MSG_RECV, self._on_dm_event)
        self._mc.subscribe(EventType.CHANNEL_MSG_RECV, self._on_channel_event)
        self._mc.subscribe(EventType.DISCONNECTED, self._on_disconnect_event)
        self._mc.subscribe(EventType.CONNECTED, self._on_connect_event)
        self._mc.subscribe(EventType.ACK, self._on_ack_event)
        self._mc.subscribe(EventType.NEW_CONTACT, self._on_new_contact)
        try:
            await self._mc.ensure_contacts()
        except Exception:
            logger.debug("MeshCore: ensure_contacts failed (non-fatal)", exc_info=True)
        if getattr(self.config, "meshcore_auto_add_contacts", True):
            try:
                await self._mc.commands.set_autoadd_config(1)
                logger.info("MeshCore: firmware auto-add-contacts ENABLED (AIDA will auto-add any node it hears)")
            except Exception as exc:  # older firmware may not support CMD 58
                logger.warning("MeshCore: set_autoadd_config not supported/failed (non-fatal): %s", exc)
        await self._mc.start_auto_message_fetching()
        # Arm the MC send queue on this dedicated loop (runs after connect).
        self._start_mc_queue()
        logger.info("MeshCore: subscriptions registered; auto message-fetch started; send queue armed")

    async def _do_disconnect(self) -> None:
        """Stop fetching and close the meshcore connection."""
        try:
            await self._mc.stop_auto_message_fetching()
        except Exception as exc:
            logger.warning("MeshCoreTransport: stop_auto_message_fetching error: %s", exc)
        try:
            await self._mc.disconnect()
        except Exception as exc:
            logger.warning("MeshCoreTransport: mc.disconnect error: %s", exc)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Connect to the MeshCore companion (tcp, serial, or ble)."""
        conn_type = getattr(self.config, "meshcore_conn_type", "tcp") or "tcp"
        host = getattr(self.config, "meshcore_host", "")
        port = getattr(self.config, "meshcore_port", 5050)
        serial_port = getattr(self.config, "meshcore_serial_port", "")
        baud = getattr(self.config, "meshcore_baud", 115200)
        ble_address = getattr(self.config, "meshcore_ble_address", "")
        auto_reconnect = getattr(self.config, "meshcore_auto_reconnect", True)
        max_attempts = getattr(self.config, "meshcore_max_reconnect_attempts", 5)

        # Build a human-readable target string for logging.
        if conn_type == "serial":
            target = f"serial:{serial_port}@{baud}"
        elif conn_type == "ble":
            target = f"ble:{ble_address or 'auto'}"
        else:
            target = f"{host}:{port}"

        logger.info("MeshCoreTransport: connecting to %s …", target)

        # Start the dedicated event loop in a daemon thread.
        self._loop = asyncio.new_event_loop()
        self._loop_ready.clear()

        def _run_loop() -> None:
            asyncio.set_event_loop(self._loop)
            self._loop.call_soon(self._loop_ready.set)
            self._loop.run_forever()

        self._loop_thread = threading.Thread(
            target=_run_loop,
            name="meshcore-loop",
            daemon=True,
        )
        self._loop_thread.start()
        self._loop_ready.wait(timeout=5.0)

        try:
            mc = self._run_coro(
                self._do_connect(
                    conn_type, target,
                    auto_reconnect, max_attempts,
                    host=host, port=port,
                    serial_port=serial_port, baud=baud,
                    ble_address=ble_address,
                ),
                timeout=30.0,
            )
        except Exception as exc:
            logger.error("MeshCoreTransport: connect failed: %s", exc)
            self._stop_loop()
            raise

        if mc is None:
            self._stop_loop()
            raise RuntimeError(
                f"MeshCore connect ({target}) returned None — connection failed"
            )

        self._mc = mc
        self._self_info = mc.self_info or {}
        self._connected = True

        # Subscribe to inbound events on the dedicated loop.
        self._run_coro(self._setup_subscriptions())

        # Build the channel NAME → slot map from the live companion table so
        # per-family broadcasts can resolve their channel name to a slot.
        self._enumerate_channels()

        # Announce ourselves so other nodes can discover and DM us.
        try:
            if self.send_advert():
                logger.info("MeshCore: sent self-advert on connect")
            else:
                logger.warning("MeshCore: send_advert on connect returned False")
        except Exception as exc:
            logger.warning("MeshCore: send_advert on connect error: %s", exc)

        # Arm periodic re-advertisement if configured (default 3 h; 0 = disabled).
        interval = getattr(self.config, "meshcore_advert_interval_seconds", 10800)
        if interval > 0:
            self._schedule_periodic_advert(interval)

        # Arm telemetry auto-poll if configured (0 = disabled).
        telem_interval = getattr(
            self.config, "meshcore_telemetry_interval_seconds", 1800
        )
        if telem_interval > 0:
            self._schedule_telemetry_poll()

        logger.info(
            "MeshCoreTransport: connected as %s (pubkey %s)",
            self._self_info.get("name", "unknown"),
            self._self_info.get("public_key", "?"),
        )

    def disconnect(self) -> None:
        """Disconnect and stop the event loop thread."""
        # Cancel periodic advert + telemetry poll before tearing down the loop.
        self._cancel_periodic_advert()
        self._cancel_telemetry_poll()
        if self._mc is not None:
            try:
                self._run_coro(self._do_disconnect(), timeout=10.0)
            except Exception as exc:
                logger.warning("MeshCoreTransport: disconnect error: %s", exc)
            self._mc = None
        self._connected = False
        self._stop_loop()
        logger.info("MeshCoreTransport: disconnected")

    # ------------------------------------------------------------------
    # Message I/O
    # ------------------------------------------------------------------

    def send_message(
        self,
        text: str,
        destination: Optional[str] = None,
        channel: int = 0,
        transport: Optional[str] = None,  # routing hint — accepted and IGNORED by single-transport impl
        meshcore_channel: Optional[str] = None,
    ) -> bool:
        """Send a message via MeshCore.

        Args:
            text: Message text (caller is responsible for length limits; see
                  ``mesh_max_chars`` config field).
            destination: hex pubkey string for a DM, or None for channel send.
            channel: Channel index for channel sends (Meshtastic semantics; ignored here).
            transport: Optional routing hint (for CompositeTransport); ignored here.
            meshcore_channel: Per-family MeshCore channel NAME for broadcasts.
                Resolved to a companion slot via the live channel table.
                When None on a broadcast, the send is skipped (family not
                configured for MeshCore — no fallback, no default).
                An unknown name is never blind-sent: it warns and returns False.

        Returns:
            True if the send succeeded (not an error event).
            True also for a no-op skip (meshcore_channel=None on broadcast) so
            the caller (CompositeTransport) doesn't treat a silent skip as failure.
        """
        if self._mc is None:
            logger.error("MeshCoreTransport: cannot send, not connected")
            return False

        try:
            if destination:
                contact = self._resolve_contact(destination)
                if contact is None:
                    logger.warning(
                        "MeshCore: could not resolve contact for DM dest %s; cannot reply",
                        destination,
                    )
                    return False
                label = contact.get("adv_name") or destination
                # FAST PATH: send the reply DIRECTLY (no discovery) and confirm
                # by the delivery ACK the firmware returns. This is the common
                # case (~1-3s) — the firmware already holds a usable route after
                # the inbound DM primed it. We do NOT pay the path-discovery cost
                # up front (that 25s wait was the ~25s reply-latency bug).
                result = self._send_dm_once(contact, text, destination)
                if result is None:
                    return False
                exp_ack = self._extract_expected_ack(result)
                if self._wait_for_ack(exp_ack, self._ack_wait):
                    logger.info("MeshCore: DM to %s ACKed (direct)", label)
                    return True
                # No ACK -> route is stale/unknown. NOW run path discovery to
                # learn a direct route, resend, and re-confirm by ACK.
                logger.info(
                    "MeshCore: no ACK from %s in %.1fs; running path discovery and retrying",
                    label, self._ack_wait,
                )
                self._establish_direct_path(contact, destination)
                # Re-resolve so we send with the freshly-learned out_path.
                contact = self._resolve_contact(destination) or contact
                result = self._send_dm_once(contact, text, destination)
                if result is None:
                    return False
                exp_ack = self._extract_expected_ack(result)
                acked = self._wait_for_ack(exp_ack, self._ack_wait)
                logger.info(
                    "MeshCore: DM to %s %s after discovery",
                    contact.get("adv_name") or destination,
                    "ACKed" if acked else "no ACK (sent best-effort)",
                )
                # Best-effort success if the frame was at least accepted by the radio.
                return acked or (not result.is_error())
            else:
                # Channel broadcast.
                # Channel-index semantics do NOT cross transports: the passed
                # `channel` carries Meshtastic channel-index semantics (e.g.
                # index 8) that have no relationship to MeshCore's separate
                # channel table.
                #
                # Per-family routing: meshcore_channel is a channel NAME.
                # If meshcore_channel is None, this family is not configured
                # for MeshCore → silent no-op (return True).
                if meshcore_channel is None:
                    logger.debug(
                        "MeshCoreTransport: meshcore_channel=None, skipping broadcast"
                    )
                    return True
                # Resolve NAME → slot against the live companion channel table.
                idx = self._chan_name_to_idx.get(meshcore_channel)
                if idx is None:
                    # One lazy re-enumeration in case the table changed since
                    # connect (e.g. a channel was provisioned after startup).
                    self._enumerate_channels()
                    idx = self._chan_name_to_idx.get(meshcore_channel)
                if idx is None:
                    # Never blind-send to a guessed slot.
                    logger.warning(
                        "MeshCore channel '%s' not on companion; skipping",
                        meshcore_channel,
                    )
                    return False
                result = self._run_coro(
                    self._mc.commands.send_chan_msg(idx, text)
                )
            success = not result.is_error()
            if not success:
                logger.warning("MeshCoreTransport: send returned error event")
            return success
        except Exception as exc:
            logger.error("MeshCoreTransport: send_message failed: %s", exc)
            return False

    def set_message_callback(
        self,
        callback: Callable,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        """Store the meshai callback and its event loop for inbound dispatch."""
        self._message_callback = callback
        self._callback_loop = loop

    # ------------------------------------------------------------------
    # Event normalization — factored out for hermetic unit testing
    # ------------------------------------------------------------------

    def _normalize_dm_event(self, event) -> Optional[MeshMessage]:
        """Map a CONTACT_MSG_RECV event payload → MeshMessage.

        Separated from the subscription handler so tests can call it directly
        without spinning up the loop thread.
        """
        try:
            payload = event.payload or {}
            text = payload.get("text", "")
            if not text:
                return None
            pubkey_prefix: str = payload.get("pubkey_prefix", "")

            # Best-effort contact name resolution.
            sender_name = pubkey_prefix
            if self._mc is not None:
                try:
                    contact = self._mc.get_contact_by_key_prefix(pubkey_prefix)
                    if contact:
                        sender_name = contact.get("adv_name", pubkey_prefix) or pubkey_prefix
                except Exception:
                    pass

            return MeshMessage(
                sender_id=pubkey_prefix,
                sender_name=sender_name,
                text=text,
                channel=0,
                is_dm=True,
                packet=None,
                transport="meshcore",
            )
        except Exception as exc:
            logger.error("MeshCoreTransport: error normalizing DM event: %s", exc)
            return None

    def _normalize_channel_event(self, event) -> Optional[MeshMessage]:
        """Map a CHANNEL_MSG_RECV event payload → MeshMessage.

        Separated from the subscription handler so tests can call it directly
        without spinning up the loop thread.
        """
        try:
            payload = event.payload or {}
            text = payload.get("text", "")
            if not text:
                return None
            channel_idx: int = payload.get("channel_idx", 0)

            # Channel messages carry no per-sender pubkey in the meshcore API.
            channel_marker = f"chan:{channel_idx}"

            return MeshMessage(
                sender_id=channel_marker,
                sender_name=channel_marker,
                text=text,
                channel=channel_idx,
                is_dm=False,
                packet=None,
                transport="meshcore",
            )
        except Exception as exc:
            logger.error("MeshCoreTransport: error normalizing channel event: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Internal event handlers (called by meshcore's event system)
    # ------------------------------------------------------------------

    def _on_dm_event(self, event) -> None:
        """Handle CONTACT_MSG_RECV: normalize, filter, and dispatch to meshai."""
        try:
            _p = event.payload or {}
            _sender = _p.get("pubkey_prefix", "?")
            _preview = str(_p.get("text", ""))[:40]
        except Exception:
            _sender = repr(event)[:40]
            _preview = ""
        logger.info("MeshCore: inbound DM from %s: %r", _sender, _preview)
        msg = self._normalize_dm_event(event)
        if msg is None:
            logger.debug("MeshCore: DM from %s dropped (normalize returned None)", _sender)
            return
        if not mc_context_allows(
            self._mc_context, msg, {v: k for k, v in self._chan_name_to_idx.items()}
        ):
            logger.debug("MeshCore: DM from %s dropped by context gate", _sender)
            return
        self._dispatch_message(msg)

    def _on_channel_event(self, event) -> None:
        """Handle CHANNEL_MSG_RECV: normalize, filter, and dispatch to meshai."""
        msg = self._normalize_channel_event(event)
        if msg is None or not mc_context_allows(
            self._mc_context, msg, {v: k for k, v in self._chan_name_to_idx.items()}
        ):
            return
        self._dispatch_message(msg)

    def _dispatch_message(self, msg: Optional[MeshMessage]) -> None:
        """Marshal a MeshMessage onto the meshai event loop (thread-safe).

        Mirrors exactly what MeshtasticTransport._on_receive does:
            loop.call_soon_threadsafe(lambda m=msg: asyncio.create_task(cb(m)))
        """
        if msg is None or self._message_callback is None or self._callback_loop is None:
            logger.debug(
                "MeshCoreTransport: _dispatch_message dropped (msg=%s callback=%s loop=%s)",
                msg is not None, self._message_callback is not None,
                self._callback_loop is not None,
            )
            return
        try:
            self._callback_loop.call_soon_threadsafe(
                lambda m=msg: asyncio.create_task(self._message_callback(m))
            )
            logger.debug("MeshCoreTransport: dispatched message to meshai")
        except Exception as exc:
            logger.error("MeshCoreTransport: error dispatching message: %s", exc)

    def _on_ack_event(self, event) -> None:
        logger.info("MeshCore: ACK event received: %r", getattr(event, "payload", None))

    def _on_new_contact(self, event) -> None:
        """Firmware auto-added a node from a fresh advert — log it and refresh our roster."""
        try:
            payload = getattr(event, "payload", None) or {}
            name = payload.get("adv_name") or payload.get("public_key", "")[:12] or "?"
            logger.info("MeshCore: new contact auto-added: %s", name)
        except Exception:
            logger.debug("MeshCore: _on_new_contact logging failed", exc_info=True)
        # Refresh roster on the transport's own loop (fire-and-forget, non-blocking).
        # We must NOT use _run_coro here: the lib calls this callback from within
        # the asyncio event loop, so blocking with .result() would deadlock.
        # asyncio.run_coroutine_threadsafe without .result() is safe from any context.
        try:
            ensure = getattr(self._mc, "ensure_contacts", None)
            loop = getattr(self, "_loop", None)
            if ensure is not None and loop is not None and loop.is_running():
                asyncio.run_coroutine_threadsafe(ensure(), loop)
        except Exception:
            logger.debug("MeshCore: roster refresh after new contact failed", exc_info=True)

    def _on_disconnect_event(self, event=None) -> None:
        """Track link state: DISCONNECTED."""
        self._connected = False
        logger.warning("MeshCoreTransport: DISCONNECTED event received")

    def _on_connect_event(self, event=None) -> None:
        """Track link state: CONNECTED (auto-reconnect succeeded)."""
        self._connected = True
        logger.info("MeshCoreTransport: CONNECTED event received")

    # ------------------------------------------------------------------
    # Node identity / topology (MeshTransport abstract methods)
    # ------------------------------------------------------------------

    @property
    def my_node_id(self) -> Optional[str]:
        """Our own public key (hex string), or None before connect."""
        return self._self_info.get("public_key") or None

    @property
    def connected(self) -> bool:
        """True when the transport has an active connection."""
        return self._connected and self._mc is not None

    @property
    def max_chars(self) -> int:
        return self.config.mesh_max_chars

    def get_node_name(self, node_id: str) -> str:
        """Resolve a pubkey prefix to a contact display name, or return node_id."""
        if self._mc is None:
            return node_id
        try:
            contact = self._mc.get_contact_by_key_prefix(node_id)
            if contact:
                return contact.get("adv_name", node_id) or node_id
        except Exception:
            pass
        return node_id

    def get_node_position(self, node_id: str) -> Optional[tuple]:
        """Return (adv_lat, adv_lon) for a contact, or None if not available."""
        if self._mc is None:
            return None
        try:
            contact = self._mc.get_contact_by_key_prefix(node_id)
            if contact:
                lat = contact.get("adv_lat")
                lon = contact.get("adv_lon")
                if lat is not None and lon is not None:
                    return (lat, lon)
        except Exception:
            pass
        return None
