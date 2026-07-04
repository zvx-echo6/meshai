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
import logging
import threading
import time as _time
from typing import Callable, Optional

from .base import MeshTransport
from ..connector import MeshMessage

logger = logging.getLogger(__name__)

# Default timeout for command futures (seconds).
_COMMAND_TIMEOUT = 10.0


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

        Refreshes the roster first (ensure_contacts) so the lib can upgrade the
        6-byte prefix to the full 32-byte key and run reset_path->flood. Returns
        None if the contact can't be resolved. This mirrors what every working
        meshcore project does before send_msg_with_retry (never send to a bare prefix).
        """
        if self._mc is None:
            return None
        ensure = getattr(self._mc, "ensure_contacts", None)
        if ensure is not None:
            try:
                self._run_coro(ensure(), timeout=15)
            except Exception:
                logger.debug("MeshCore: ensure_contacts (resolve) failed", exc_info=True)
        try:
            return self._mc.get_contact_by_key_prefix(dest)
        except Exception:
            logger.debug("MeshCore: get_contact_by_key_prefix failed for %s", dest, exc_info=True)
            return None

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

    async def _periodic_advert_loop(self, interval: int) -> None:
        """Periodic self-advertisement coroutine (runs as a Task on the dedicated loop).

        Sleeps *interval* seconds, sends one flood advert, repeats.  Stops on
        CancelledError (raised by ``_cancel_periodic_advert`` at disconnect) or
        when the transport drops its connection.  No overlap is possible because
        the loop awaits the sleep before each send.
        """
        try:
            while True:
                await asyncio.sleep(interval)
                if not self._connected or self._mc is None:
                    return
                try:
                    await self._mc.commands.send_advert(flood=True)
                    self._last_advert_sent = _time.time()
                    logger.info("MeshCore: sent periodic self-advert")
                except Exception as exc:
                    logger.warning("MeshCore: periodic send_advert failed: %s", exc)
        except asyncio.CancelledError:
            logger.debug("MeshCore: periodic advert task cancelled")
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

    # ------------------------------------------------------------------
    # Internal coroutines (run on the dedicated loop)
    # ------------------------------------------------------------------

    async def _do_connect(self, host: str, port: int,
                          auto_reconnect: bool, max_attempts: int):
        """Lazy-import meshcore and create the TCP client."""
        from meshcore import MeshCore  # noqa: PLC0415 (lazy import intentional)
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
        try:
            await self._mc.ensure_contacts()
        except Exception:
            logger.debug("MeshCore: ensure_contacts failed (non-fatal)", exc_info=True)
        await self._mc.start_auto_message_fetching()
        logger.info("MeshCore: subscriptions registered; auto message-fetch started")

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
        """Connect to the pyMC companion TCP frame server."""
        host = getattr(self.config, "meshcore_host", "100.64.0.9")
        port = getattr(self.config, "meshcore_port", 5050)
        auto_reconnect = getattr(self.config, "meshcore_auto_reconnect", True)
        max_attempts = getattr(self.config, "meshcore_max_reconnect_attempts", 5)

        logger.info("MeshCoreTransport: connecting to %s:%d …", host, port)

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
                self._do_connect(host, port, auto_reconnect, max_attempts),
                timeout=30.0,
            )
        except Exception as exc:
            logger.error("MeshCoreTransport: connect failed: %s", exc)
            self._stop_loop()
            raise

        if mc is None:
            self._stop_loop()
            raise RuntimeError(
                f"MeshCore.create_tcp({host}:{port}) returned None — connection failed"
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

        logger.info(
            "MeshCoreTransport: connected as %s (pubkey %s)",
            self._self_info.get("name", "unknown"),
            self._self_info.get("public_key", "?"),
        )

    def disconnect(self) -> None:
        """Disconnect and stop the event loop thread."""
        # Cancel periodic advert before tearing down the loop.
        self._cancel_periodic_advert()
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
                        "MeshCore: could not resolve a contact for DM dest %s; cannot address reply "
                        "(recipient not in roster)", destination,
                    )
                    return False
                label = contact.get("adv_name") or contact.get("name") or destination
                logger.debug("MeshCore: sending DM to %s via resolved contact", label)
                # Pass the CONTACT OBJECT (not the bare prefix) so the lib can upgrade to the
                # full key and reset_path->flood works — the pattern used by all working projects.
                result = self._run_coro(
                    self._mc.commands.send_msg_with_retry(contact, text),
                    timeout=40,
                )
                if result is None:
                    logger.warning("MeshCoreTransport: DM to %s not ACKed (delivery not confirmed)", label)
                    return False
                success = not result.is_error()
                if not success:
                    logger.warning("MeshCoreTransport: DM send returned error event")
                return success
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
