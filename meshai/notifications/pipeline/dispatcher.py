"""Immediate event dispatcher.

The dispatcher routes immediate-severity events through the existing
NotificationRuleConfig rules and delivers via channels.py. This is the
transitional bridge between the new Event pipeline and the existing
channel implementations.

Phase 2.5a: dispatch() is now async, takes a connector at construction,
and properly awaits channel.deliver(payload, rule).

v0.5.2: toggle path gains three guards at the entrance (staleness, per-toggle
cooldown, (source,id) LRU dedup) plus the friendly mesh-broadcast composer so
the toggle path stops emitting raw `[Family] central.category` debug strings.
The legacy rules path is intentionally left untouched (no regression risk).
"""

import logging
import time
from collections import OrderedDict
from typing import Callable, Optional

from meshai.notifications.events import Event, make_payload_from_event
from meshai.notifications.categories import get_toggle
from meshai.notifications.renderers.composer import compose_mesh_message


# Bounded (source, event.id) LRU set — see _dispatch_toggles Section 3.
_DEDUP_LRU_MAX = 10_000


class Dispatcher:
    """Dispatches immediate events to channels matching configured rules."""

    SEVERITY_RANK = {"routine": 0, "priority": 1, "immediate": 2}

    def __init__(self, config, channel_factory: Callable, connector=None):
        """Initialize.

        Args:
            config: The full Config object (provides config.notifications.rules)
            channel_factory: Callable taking (rule, connector) and returning
                a NotificationChannel. This is create_channel from
                meshai/notifications/channels.py.
            connector: MeshConnector instance for mesh channel deliveries.
        """
        self._config = config
        self._channel_factory = channel_factory
        self._connector = connector
        self._logger = logging.getLogger("meshai.pipeline.dispatcher")
        # v0.5.2 — toggle-path guards (ops counters exposed via dispatch_stats()):
        self._stale_dropped = 0
        self._cooldown_dropped = 0
        self._dedup_dropped = 0
        # v0.5.8b cold-start grace: anchor lazily on FIRST event the
        # dispatcher sees through an enabled toggle. Grace window read
        # from config so it can be tuned at runtime via /api/config PUT.
        self._first_event_at: Optional[float] = None
        self._cold_start_dropped = 0
        # (toggle.name, category, region) -> last-fire wall-clock seconds
        self._toggle_cooldown: dict[tuple[str, str, str], float] = {}
        # Insertion-ordered (source, event.id) -> sentinel; evict oldest at cap.
        self._dedup_lru: "OrderedDict[tuple[str, str], bool]" = OrderedDict()

    async def dispatch(self, event: Event) -> None:
        """Deliver via matching rules AND enabled family toggles (parallel, v0.5)."""
        await self._dispatch_rules(event)
        await self._dispatch_toggles(event)

    async def _dispatch_rules(self, event: Event) -> None:
        """Deliver an immediate-severity event to all matching channels.
        
        This method is async and awaits each channel.deliver() call.
        """
        rules = self._matching_rules(event)
        if not rules:
            self._logger.debug(
                f"No matching rules for {event.source}/{event.category}, skipping"
            )
            return
        for rule in rules:
            try:
                channel = self._channel_factory(rule, self._connector)
                payload = make_payload_from_event(event)
                success = await channel.deliver(payload, rule)
                if success:
                    self._logger.info(
                        f"Dispatched event {event.id} via {rule.delivery_type}"
                    )
                else:
                    self._logger.warning(
                        f"Channel delivery returned False for rule {rule.name}"
                    )
            except Exception:
                self._logger.exception(
                    f"Channel delivery failed for rule {rule.name}"
                )

    async def _dispatch_toggles(self, event: Event) -> None:
        """Route an event through its family master-toggle (parallel to rules).

        v0.5.2 guards (run in order, at the entrance):
          1. Staleness   — drop events older than `toggle.freshness_seconds`.
                           Solves the restart-wave problem definitively: a
                           backlog of stale events from durable storage gets
                           dropped here, never broadcast.
          2. Cooldown    — per (toggle.name, category, region) throttle keyed
                           on `toggle.cooldown_seconds`. Silent, no log spam.
          3. Dedup       — bounded LRU on (source, event.id); catches Central
                           re-delivery during reconnect.
        Then composes a friendly mesh string instead of the prior raw
        `[Family] central.category` debug format.
        """
        toggles = getattr(self._config.notifications, "toggles", None)
        if not isinstance(toggles, dict) or not toggles:
            return
        fam = get_toggle(event.category)
        if not fam:
            return
        tog = toggles.get(fam)
        if tog is None or not getattr(tog, "enabled", False):
            return

        # ---------- Section 0 — cold-start grace (v0.5.8b) ----------
        # First event ever to reach an enabled toggle anchors the grace
        # window. Any broadcast attempt inside the window is dropped, but
        # the event still flowed through the consumer -> handler chain
        # before us, so persistence rows have already been written. Only
        # the broadcast is suppressed.
        grace_s = int(getattr(self._config.notifications, "cold_start_grace_seconds", 60) or 0)
        if grace_s > 0:
            now_anchor = time.time()
            if self._first_event_at is None:
                self._first_event_at = now_anchor
                self._logger.info(
                    "cold-start grace anchor set: t0=%.3f window=%ds",
                    now_anchor, grace_s,
                )
            if (now_anchor - self._first_event_at) < grace_s:
                self._cold_start_dropped += 1
                self._logger.info(
                    "cold-start grace: dropping broadcast source=%s category=%s "
                    "elapsed=%.1fs window=%ds",
                    event.source, event.category,
                    now_anchor - self._first_event_at, grace_s,
                )
                return

        # ---------- Section 1 — staleness filter ----------
        # `event.timestamp` is the upstream-published wall-clock the adapter
        # sets when minting the event. For Central-sourced events that's the
        # inner Event.timestamp; for native adapters it's the upstream API's
        # timestamp. Receive-time is NOT used (it's meshai-side and tells us
        # nothing about how stale the underlying alert is).
        freshness_s = int(getattr(tog, "freshness_seconds", 600) or 600)
        if event.timestamp and freshness_s > 0:
            age = time.time() - event.timestamp
            if age > freshness_s:
                self._stale_dropped += 1
                self._logger.debug(
                    "dispatcher: dropping stale event source=%s category=%s "
                    "age=%.0fs > freshness=%ds",
                    event.source, event.category, age, freshness_s,
                )
                return

        # ---------- Section 2 — per-toggle cooldown ----------
        cooldown_s = int(getattr(tog, "cooldown_seconds", 300) or 0)
        if cooldown_s > 0:
            ck = (
                getattr(tog, "name", "") or fam,
                event.category,
                event.region or "*",
            )
            now = time.time()
            last_fired = self._toggle_cooldown.get(ck)
            if last_fired is not None and (now - last_fired) < cooldown_s:
                self._cooldown_dropped += 1
                return  # silent throttle — no log spam
            self._toggle_cooldown[ck] = now
            # Lazy prune: keep map bounded at ~2x the largest cooldown by
            # discarding entries older than 2 * cooldown_s. Cheap; runs only
            # when the map grows past a threshold so it's not per-event work.
            if len(self._toggle_cooldown) > 1024:
                cutoff = now - (2 * cooldown_s)
                self._toggle_cooldown = {
                    k: t for k, t in self._toggle_cooldown.items() if t >= cutoff
                }

        # ---------- Section 3 — (source, event.id) dedup ----------
        dk = (event.source or "", event.id or "")
        if dk in self._dedup_lru:
            # Touch to keep recent.
            self._dedup_lru.move_to_end(dk)
            self._dedup_dropped += 1
            return
        self._dedup_lru[dk] = True
        while len(self._dedup_lru) > _DEDUP_LRU_MAX:
            self._dedup_lru.popitem(last=False)  # evict oldest

        regions = getattr(tog, "regions", None) or []
        if regions:
            ev_regions = set(filter(None, [event.region, *(event.regions or [])]))
            if not (set(regions) & ev_regions):
                return
        event_rank = self.SEVERITY_RANK.get(event.severity, 0)
        if event_rank < self.SEVERITY_RANK.get(getattr(tog, "min_severity", "routine"), 0):
            return

        # ---------- Section 4 — friendly composer wired in ----------
        # Render once per event; reused across every channel below. Wrapped
        # so a renderer fault never blocks delivery — we fall back to the
        # legacy make_payload_from_event message (event.summary|title|category).
        try:
            friendly = compose_mesh_message(event)
        except Exception:
            self._logger.exception("mesh composer crashed; falling back to legacy message")
            friendly = None

        sev_channels = getattr(tog, "severity_channels", None) or {}
        for ch_type in sev_channels.get(event.severity, []):
            if ch_type == "digest":
                continue
            try:
                rule = self._toggle_to_rule(tog, ch_type, event)
                channel = self._channel_factory(rule, self._connector)
                if friendly is not None and ch_type in ("mesh_broadcast", "mesh_dm"):
                    payload = make_payload_from_event(event, message=friendly)
                else:
                    payload = make_payload_from_event(event)
                success = await channel.deliver(payload, rule)
                if success:
                    self._logger.info(f"Dispatched event {event.id} via toggle {fam}/{ch_type}")
                    # v0.5.8b post-broadcast commit. Persistence-side
                    # bookkeeping that should only happen when a delivery
                    # actually went out: mesh_broadcasts_out audit row +
                    # handler-supplied last_broadcast_* UPDATE callback.
                    self._post_broadcast_commit(event, payload, rule, ch_type)
                else:
                    self._logger.warning(f"Toggle channel delivery returned False for {fam}/{ch_type}")
            except Exception:
                self._logger.exception(f"Toggle channel delivery failed for {fam}/{ch_type}")

    def dispatch_stats(self) -> dict:
        """Expose v0.5.2 toggle-path guard counters for ops/health endpoints."""
        return {
            "stale_dropped": self._stale_dropped,
            "cooldown_dropped": self._cooldown_dropped,
            "dedup_dropped": self._dedup_dropped,
            "cold_start_dropped": self._cold_start_dropped,
            "cold_start_anchor_at": self._first_event_at,
            "cooldown_keys": len(self._toggle_cooldown),
            "dedup_lru_size": len(self._dedup_lru),
        }

    def _post_broadcast_commit(self, event, payload, rule, ch_type: str) -> None:
        """Persistence side-effects of an actually-successful broadcast.

        Inserts the mesh_broadcasts_out audit row when the handler signalled
        it wants one via `event.data["_broadcast_audit"]`, then invokes the
        handler-supplied `_on_broadcast_committed` callback so the handler
        can refresh its own last_broadcast_* bookkeeping. Both calls are
        wrapped: a bookkeeping failure must NOT undo the actual broadcast
        nor break dispatch for sibling toggles.
        """
        data = getattr(event, "data", None) or {}
        if not data:
            return
        committed_at = time.time()

        audit = data.get("_broadcast_audit")
        if isinstance(audit, dict):
            try:
                from meshai.persistence import get_db
                conn = get_db()
                text = payload.message if payload is not None else (event.title or "")
                bytes_sent = len(text.encode("utf-8")) if text else 0
                if ch_type == "mesh_dm":
                    node_ids = list(getattr(rule, "node_ids", []) or [])
                    recipient = ",".join(map(str, node_ids)) or "dm"
                else:
                    recipient = "broadcast"
                channel = getattr(rule, "broadcast_channel", None)
                conn.execute(
                    "INSERT INTO mesh_broadcasts_out(sent_at, recipient, channel, "
                    "text, source_event_table, source_event_pk, bytes_sent, "
                    "ack_received) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        int(committed_at), recipient, channel, text,
                        audit.get("table"), audit.get("pk"),
                        bytes_sent, 0,
                    ),
                )
            except Exception:
                self._logger.exception(
                    "post-broadcast: mesh_broadcasts_out insert failed "
                    "(table=%s pk=%s)",
                    audit.get("table"), audit.get("pk"),
                )

        cb = data.get("_on_broadcast_committed")
        if callable(cb):
            try:
                cb(committed_at)
            except Exception:
                self._logger.exception(
                    "post-broadcast: handler commit-callback raised"
                )

    def _toggle_to_rule(self, tog, ch_type: str, event: Event):
        from meshai.config import NotificationRuleConfig
        return NotificationRuleConfig(
            name=f"toggle:{getattr(tog, 'name', '')}",
            enabled=True, trigger_type="condition", delivery_type=ch_type,
            broadcast_channel=(getattr(tog, "broadcast_channel", None) or 0),
            node_ids=list(getattr(tog, "node_ids", []) or []),
            smtp_host=getattr(tog, "smtp_host", ""), smtp_port=getattr(tog, "smtp_port", 587),
            smtp_user=getattr(tog, "smtp_user", ""), smtp_password=getattr(tog, "smtp_password", ""),
            smtp_tls=getattr(tog, "smtp_tls", True), from_address=getattr(tog, "from_address", ""),
            recipients=list(getattr(tog, "recipients", []) or []),
            webhook_url=getattr(tog, "webhook_url", ""),
            webhook_headers=dict(getattr(tog, "webhook_headers", {}) or {}),
            override_quiet=bool(getattr(tog, "quiet_hours_override", False) and event.severity == "immediate"),
        )

    def _matching_rules(self, event: Event) -> list:
        """Return enabled condition rules matching this event's category
        and severity threshold."""
        event_rank = self.SEVERITY_RANK.get(event.severity, 0)
        matches = []
        for rule in self._config.notifications.rules:
            if not rule.enabled:
                continue
            if rule.trigger_type != "condition":
                continue
            if rule.categories and event.category not in rule.categories:
                continue
            min_rank = self.SEVERITY_RANK.get(rule.min_severity, 0)
            if event_rank < min_rank:
                continue
            scope = getattr(rule, "region_scope", None) or []
            if scope:
                ev_regions = set(filter(None, [event.region, *(event.regions or [])]))
                if not (set(scope) & ev_regions):
                    continue
            matches.append(rule)
        return matches
