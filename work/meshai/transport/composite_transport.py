"""CompositeTransport — fan-out driver for dual Meshtastic + MeshCore meshes.

Holds an ordered list of child transports and:
  - fans broadcasts to ALL connected children;
  - routes hinted DM replies back over the originating child;
  - falls back to best-effort per-child resolution for unhinted DMs;
  - self-filters inbound messages per child (drops own echoes);
  - exposes ``meshtastic_child()`` for the supervisor watchdog.

This transport is DORMANT unless ``transport: both`` in config.  Single-
transport paths (``transport: meshtastic`` / ``transport: meshcore``) are
byte-identical to Phase 3 behaviour because the factory never instantiates
this class for them.
"""

import asyncio
import logging
from typing import Callable, List, Optional

from .base import MeshTransport

logger = logging.getLogger(__name__)


def _child_name(child: MeshTransport) -> str:
    """Return the transport-name tag for a child, deriving it from the class
    when no explicit ``transport_name`` attribute is present.

    Priority:
      1. ``child.transport_name``  (set on MeshtasticTransport / MeshCoreTransport)
      2. Class name lowercased, with "transport" suffix stripped.
    """
    name = getattr(child, "transport_name", None)
    if name:
        return name
    cls = type(child).__name__.lower()
    if cls.endswith("transport"):
        cls = cls[: -len("transport")]
    return cls or "unknown"


class CompositeTransport(MeshTransport):
    """MeshTransport that drives multiple child transports simultaneously.

    Instantiate with a list of concrete MeshTransport children, e.g.::

        CompositeTransport([MeshtasticTransport(cfg), MeshCoreTransport(cfg)])

    The children are contacted in list order; the *first* child is the
    canonical source for ``my_node_id`` (coarse fallback — real self-
    filtering is per-child in the inbound wrapper).
    """

    def __init__(self, children: List[MeshTransport], config=None) -> None:
        if not children:
            raise ValueError("CompositeTransport requires at least one child")
        self._children = list(children)
        self._config = config  # ConnectionConfig; used for the universal mesh_max_chars budget
        # Build a stable name → child mapping for O(1) routing-hint lookup.
        self._by_name: dict[str, MeshTransport] = {
            _child_name(c): c for c in self._children
        }

    # ------------------------------------------------------------------
    # Public accessors
    # ------------------------------------------------------------------

    @property
    def children(self) -> List[MeshTransport]:
        """Ordered list of child transports."""
        return list(self._children)

    def meshtastic_child(self) -> Optional[MeshTransport]:
        """Return the MeshtasticTransport child, or None if absent.

        Used by the supervisor watchdog so it can apply Meshtastic-specific
        liveness probes (socket_state, active_probe, reconnect) without
        knowing whether it is talking to a bare MeshtasticTransport or a
        CompositeTransport.
        """
        from meshai.connector import MeshtasticTransport  # local import avoids cycle
        for c in self._children:
            if isinstance(c, MeshtasticTransport):
                return c
        return None

    # ------------------------------------------------------------------
    # Routing decision helpers (factored out for unit-test access)
    # ------------------------------------------------------------------

    def _resolve_child_for_hint(self, transport_hint: str) -> Optional[MeshTransport]:
        """Return the child whose name matches *transport_hint*, or None."""
        return self._by_name.get(transport_hint)

    def _best_child_for_destination(self, destination: str) -> Optional[MeshTransport]:
        """Return the child most likely to know *destination*, or None.

        A child "resolves" the destination when ``get_node_name()`` returns
        something other than the raw *destination* string itself (i.e. the
        child has the node in its name cache).  The first such child wins.
        If no child resolves the destination, None is returned and the
        caller falls back to broadcasting to all children.
        """
        for child in self._children:
            if not child.connected:
                continue
            try:
                resolved = child.get_node_name(destination)
                if resolved and resolved != destination:
                    return child
            except Exception:
                pass
        return None

    def _should_drop(self, msg, child_node_id: Optional[str]) -> bool:
        """Return True when *msg* should be self-filtered (echo from self).

        Factored out so unit tests can call it directly.
        """
        if child_node_id is None:
            return False
        return msg.sender_id == child_node_id

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Connect all children.

        A child failing to connect is logged but does NOT prevent the
        remaining children from connecting.  ``connected`` returns True if
        at least one child is connected after the loop.
        """
        for child in self._children:
            name = _child_name(child)
            try:
                child.connect()
                logger.info("CompositeTransport: child %r connected", name)
            except Exception as exc:
                logger.error(
                    "CompositeTransport: child %r failed to connect: %s", name, exc
                )

    def disconnect(self) -> None:
        """Disconnect all children, guarding each with try/except."""
        for child in self._children:
            name = _child_name(child)
            try:
                child.disconnect()
                logger.info("CompositeTransport: child %r disconnected", name)
            except Exception as exc:
                logger.warning(
                    "CompositeTransport: child %r disconnect error: %s", name, exc
                )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        """True if at least one child is connected."""
        return any(c.connected for c in self._children)

    @property
    def my_node_id(self) -> Optional[str]:
        """Return the first child's node ID (coarse fallback).

        Real self-filtering is per-child (each child's own my_node_id is
        used in the inbound wrapper registered by set_message_callback).
        """
        return self._children[0].my_node_id if self._children else None

    @property
    def max_chars(self) -> int:
        """Return the universal mesh message budget from config.

        Sourced directly from ``config.mesh_max_chars`` (default 140) — the
        single fixed constant that governs all transports regardless of which
        radios are connected.  MeshCore is the LCD, so 140 is the right value
        for every path.
        """
        if self._config is not None:
            return self._config.mesh_max_chars
        # Fallback for tests that build CompositeTransport without a config.
        return 140

    # ------------------------------------------------------------------
    # Message I/O
    # ------------------------------------------------------------------

    def send_message(
        self,
        text: str,
        destination: Optional[str] = None,
        channel: int = 0,
        transport: Optional[str] = None,
        meshcore_channel: Optional[str] = None,
    ) -> bool:
        """Send a message, routing based on destination + hint.

        Routing rules
        -------------
        1. **Broadcast** (``destination is None``):
           Fan out to connected children with per-transport channel selection:
           - Meshtastic child receives ``channel`` (Meshtastic channel index).
           - MeshCore child receives ``meshcore_channel``; if that is None the
             MeshCore child is silently skipped (family not configured for MeshCore).
           Return True if at least one child succeeded; log per-child failures.

        2. **DM with routing hint** (``destination`` set AND ``transport`` given):
           Send ONLY via the child whose name == ``transport``.  This is the
           reply-routing path: the originating transport tag is threaded from
           the inbound MeshMessage all the way to here so the reply goes back
           over the same mesh it arrived on.

        3. **DM without hint** (``destination`` set, ``transport`` is None):
           Best-effort: prefer the child that can resolve *destination* via
           ``get_node_name`` (i.e. has the node in its cache).  If no child
           resolves, send via ALL connected children and log a warning.
           This fallback handles subscription DMs and other direct sends that
           don't carry an originating transport tag.
        """
        if destination is None:
            # --- Rule 1: broadcast ---
            return self._broadcast(text, channel, meshcore_channel=meshcore_channel,
                                   transport=transport)

        if transport is not None:
            # --- Rule 2: hinted DM ---
            return self._send_hinted(text, destination, channel, transport)

        # --- Rule 3: unhinted DM ---
        return self._send_unhinted(text, destination, channel)

    def _broadcast(self, text: str, channel: int, meshcore_channel: Optional[str] = None,
                   transport: Optional[str] = None) -> bool:
        """Fan text out to connected children with per-transport channel routing.

        If ``transport`` is given, send ONLY to the child whose name matches —
        this is the explicit per-mesh delivery path (mesh_broadcast → "meshtastic",
        meshcore_broadcast → "meshcore").  If ``transport`` is None, keep the
        legacy fan-out: all connected children with per-transport channel routing.

        For the Meshtastic child, ``channel`` (Meshtastic channel index) is used.
        For the MeshCore child:
          - ``meshcore_channel`` set → route that channel NAME to MeshCore.
          - ``meshcore_channel`` is None → skip the MeshCore child entirely
            (family not configured for MeshCore; no fallback to a default).

        Returns True if at least one child succeeded.
        """
        if transport is not None:
            # Hinted broadcast: send ONLY to the named child.
            child = self._by_name.get(transport)
            if child is None:
                logger.debug(
                    "CompositeTransport: broadcast hint %r not found; known: %s",
                    transport, list(self._by_name),
                )
                return False
            name = _child_name(child)
            if not child.connected:
                logger.debug(
                    "CompositeTransport: hinted broadcast child %r not connected", name
                )
                return False
            child_channel = meshcore_channel if name == "meshcore" else channel
            try:
                return child.send_message(text, destination=None, channel=child_channel)
            except Exception as exc:
                logger.error(
                    "CompositeTransport: hinted broadcast via %r raised: %s", name, exc
                )
                return False

        # No hint: fan to all connected children (backward-compat, no-hint path).
        any_ok = False
        for child in self._children:
            name = _child_name(child)
            if not child.connected:
                logger.debug(
                    "CompositeTransport: skipping broadcast to %r (not connected)", name
                )
                continue
            # Per-family MeshCore routing: skip MeshCore child when unset.
            if name == "meshcore" and meshcore_channel is None:
                logger.debug(
                    "CompositeTransport: skipping meshcore broadcast "
                    "(meshcore_channel=None for this family)"
                )
                continue
            # Route each child on its own channel semantics.
            child_channel = meshcore_channel if name == "meshcore" else channel
            try:
                ok = child.send_message(text, destination=None, channel=child_channel)
                if ok:
                    any_ok = True
                else:
                    logger.warning(
                        "CompositeTransport: broadcast via %r returned False", name
                    )
            except Exception as exc:
                logger.error(
                    "CompositeTransport: broadcast via %r raised: %s", name, exc
                )
        return any_ok

    def _send_hinted(
        self, text: str, destination: str, channel: int, transport_hint: str
    ) -> bool:
        """Send DM only via the child matching *transport_hint*."""
        child = self._resolve_child_for_hint(transport_hint)
        if child is None:
            logger.error(
                "CompositeTransport: no child with name %r; known: %s",
                transport_hint,
                list(self._by_name),
            )
            return False
        name = _child_name(child)
        if not child.connected:
            logger.warning(
                "CompositeTransport: hinted child %r not connected", name
            )
            return False
        try:
            return child.send_message(text, destination=destination, channel=channel)
        except Exception as exc:
            logger.error(
                "CompositeTransport: hinted send via %r raised: %s", name, exc
            )
            return False

    def _send_unhinted(self, text: str, destination: str, channel: int) -> bool:
        """Best-effort DM: prefer the resolving child; else fan to all.

        This fallback handles subscription DMs, alert DMs, and other direct
        sends that don't carry an originating transport tag.  When multiple
        children know the destination, the first one in list order wins.
        If none resolve the destination, all connected children receive the
        DM so at least one can deliver it — callers see a warning so the
        behaviour is visible in logs.
        """
        child = self._best_child_for_destination(destination)
        if child is not None:
            name = _child_name(child)
            try:
                return child.send_message(text, destination=destination, channel=channel)
            except Exception as exc:
                logger.error(
                    "CompositeTransport: unhinted send via %r raised: %s", name, exc
                )
                return False

        # No child resolved the destination — send via all and warn.
        logger.warning(
            "CompositeTransport: destination %r unresolved; fanning DM to all children",
            destination,
        )
        any_ok = False
        for child in self._children:
            name = _child_name(child)
            if not child.connected:
                continue
            try:
                ok = child.send_message(text, destination=destination, channel=channel)
                if ok:
                    any_ok = True
            except Exception as exc:
                logger.error(
                    "CompositeTransport: DM fan via %r raised: %s", name, exc
                )
        return any_ok

    def set_message_callback(
        self,
        callback: Callable,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        """Register per-child inbound wrappers.

        Each child gets its OWN wrapper that:
          (a) self-filters — drops the message if sender_id == that child's
              my_node_id (each child knows its own ID);
          (b) ensures msg.transport is set to the child's name if missing;
          (c) forwards to the meshai callback.

        Each child already marshals its callback onto *loop* via
        ``loop.call_soon_threadsafe``; we preserve that — the wrapper is
        just a thin decorator around the meshai callback.
        """
        for child in self._children:
            child_name = _child_name(child)

            # Capture child_name and child identity in the closure.
            def _make_wrapper(name: str, c: MeshTransport) -> Callable:
                async def _wrapper(msg) -> None:
                    # (a) self-filter
                    if self._should_drop(msg, c.my_node_id):
                        logger.debug(
                            "CompositeTransport: dropping echo from %r (self)", name
                        )
                        return
                    # (b) ensure transport tag
                    if not msg.transport:
                        msg.transport = name
                    # (c) forward
                    await callback(msg)

                return _wrapper

            child.set_message_callback(_make_wrapper(child_name, child), loop)

    # ------------------------------------------------------------------
    # Node identity / topology
    # ------------------------------------------------------------------

    def get_node_name(self, node_id: str) -> str:
        """Try each child in order; return the first non-identity result."""
        for child in self._children:
            try:
                name = child.get_node_name(node_id)
                if name and name != node_id:
                    return name
            except Exception:
                pass
        return node_id

    def get_node_position(self, node_id: str) -> Optional[tuple]:
        """Try each child in order; return the first non-None position."""
        for child in self._children:
            try:
                pos = child.get_node_position(node_id)
                if pos is not None:
                    return pos
            except Exception:
                pass
        return None
