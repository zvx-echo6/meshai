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

v0.6-2: state persistence (audit doc finding #1). The cold-start anchor,
the four drop counters, the per-(toggle,category,region) cooldown map,
and the (source,event_id) dedup OrderedDict now write through to SQLite
on every mutation. The dispatcher restores them on __init__ so they
survive container restart. In-memory caches stay authoritative on the
fast path; SQLite is the durability layer + the LLM's "what's been
suppressed?" query target (commit #5: env_reporter).

Cumulative-since-install counters: the four `_*_dropped` ints are NOT
reset on boot. They carry forward from the dispatcher_state singleton
row. A `dispatch_stats()` call returns the in-memory (=most-recent)
values, which mirror the on-disk values exactly.
"""

import logging
import time
from collections import OrderedDict
from typing import Callable, Optional
from meshai.adapter_config import adapter_config

from meshai.notifications.events import Event, make_payload_from_event
from meshai.notifications.categories import get_toggle
from meshai.notifications.renderers.composer import compose_mesh_message


# Bounded (source, event.id) LRU set — see _dispatch_toggles Section 3.
# Holds the in-memory fast-path cap; SQLite dispatcher_dedup retains a
# 7-day window which can exceed this. On boot we restore the most-recent
# _DEDUP_LRU_MAX rows into this OrderedDict.
_DEDUP_LRU_MAX = 10_000

# v0.6-2 SQLite dedup retention. Anything older than this is deleted on
# every dispatcher_dedup insert.
_DEDUP_DB_RETENTION_S = 7 * 86400   # 604_800

# In-memory cooldown map prune threshold (entries). When the map grows past
# this we re-apply the 2*cooldown_s cutoff so it stays bounded. The SQLite
# prune fires on every cooldown write regardless.
_COOLDOWN_INMEM_PRUNE_THRESHOLD = 1024


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
        # v0.6-2: restored from dispatcher_state on __init__ via _restore_from_db.
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
        # v0.6-2: hydrate from SQLite. Graceful no-op if persistence is
        # unavailable -- the dispatcher still works, just without
        # cross-restart durability.
        self._restore_from_db()

    # ---------- v0.6-2 persistence -----------------------------------------

    def _restore_from_db(self) -> None:
        """Hydrate in-memory state from dispatcher_state + dispatcher_cooldowns
        + dispatcher_dedup on dispatcher construction. Idempotent.

        Defensive against missing tables: if the v5 migration hasn't run yet
        (e.g. fresh DB created by a test fixture before migrations apply),
        any sqlite OperationalError is caught and the dispatcher falls back
        to fresh in-memory state. The fast path is unaffected."""
        try:
            from meshai.persistence import get_db
            conn = get_db()
        except Exception:
            self._logger.exception(
                "dispatcher: persistence unavailable on init; using fresh "
                "in-memory state (counters reset, no cooldown/dedup carryover)"
            )
            return

        # State singleton. Wrap in try/except so a missing dispatcher_state
        # table (pre-v5 schema) degrades gracefully to fresh state instead
        # of raising into the Dispatcher constructor.
        try:
            row = conn.execute(
                "SELECT cold_start_anchor, stale_dropped, cooldown_dropped, "
                "dedup_dropped, cold_start_dropped FROM dispatcher_state WHERE id=1"
            ).fetchone()
        except Exception:
            self._logger.debug(
                "dispatcher: v5 tables not present yet; using fresh state"
            )
            return
        if row is not None:
            self._first_event_at = row["cold_start_anchor"]
            self._stale_dropped = int(row["stale_dropped"] or 0)
            self._cooldown_dropped = int(row["cooldown_dropped"] or 0)
            self._dedup_dropped = int(row["dedup_dropped"] or 0)
            self._cold_start_dropped = int(row["cold_start_dropped"] or 0)
            self._logger.info(
                "dispatcher state restored: cold_start_anchor=%s "
                "stale=%d cooldown=%d dedup=%d cold_start=%d",
                self._first_event_at, self._stale_dropped,
                self._cooldown_dropped, self._dedup_dropped,
                self._cold_start_dropped,
            )

        # Cooldowns: every row restored verbatim (the in-memory prune
        # threshold of 1024 will fire on the first cooldown write if the
        # restored set is bigger, so even pathological histories self-bound).
        for r in conn.execute(
            "SELECT toggle, category, region, last_fired_at "
            "FROM dispatcher_cooldowns"
        ).fetchall():
            self._toggle_cooldown[(r["toggle"], r["category"], r["region"])] = \
                float(r["last_fired_at"])

        # Dedup LRU: restore the most-recent _DEDUP_LRU_MAX rows in
        # newest-first order, then re-add into the OrderedDict oldest-first
        # so the natural insertion order matches the OrderedDict-as-LRU
        # contract (oldest = first-evicted on overflow). On-disk retains a
        # 7-day window which may exceed the in-memory cap; the LLM still
        # sees the full window via direct SELECT.
        # v0.6-3b: restore cap from adapter_config.
        _restore_cap = int(adapter_config.dispatcher.dedup_lru_max)
        rows = conn.execute(
            "SELECT source, event_id FROM dispatcher_dedup "
            "ORDER BY seen_at DESC LIMIT ?",
            (_restore_cap,),
        ).fetchall()
        for r in reversed(rows):
            self._dedup_lru[(r["source"], r["event_id"])] = True

        if self._toggle_cooldown or self._dedup_lru:
            self._logger.info(
                "dispatcher caches restored: cooldowns=%d dedup_lru=%d",
                len(self._toggle_cooldown), len(self._dedup_lru),
            )

    def _persist_state(self) -> None:
        """Write the current counters + cold_start_anchor to dispatcher_state.
        Called whenever any of those values change."""
        try:
            from meshai.persistence import get_db
            conn = get_db()
            conn.execute(
                "UPDATE dispatcher_state SET cold_start_anchor=?, "
                "stale_dropped=?, cooldown_dropped=?, dedup_dropped=?, "
                "cold_start_dropped=?, updated_at=? WHERE id=1",
                (self._first_event_at, self._stale_dropped,
                 self._cooldown_dropped, self._dedup_dropped,
                 self._cold_start_dropped, time.time()),
            )
        except Exception:
            self._logger.exception(
                "dispatcher: state write-through failed; in-memory still ok"
            )

    def _persist_cooldown(self, key: tuple[str, str, str],
                            now: float, cooldown_s: int) -> None:
        """UPSERT a single (toggle, category, region) cooldown row + prune
        rows older than (2 * cooldown_s). Mirrors the in-memory prune
        semantics moved off the per-1024-grow check."""
        try:
            from meshai.persistence import get_db
            conn = get_db()
            toggle, category, region = key
            conn.execute(
                "INSERT OR REPLACE INTO dispatcher_cooldowns("
                "toggle, category, region, last_fired_at, updated_at) "
                "VALUES (?,?,?,?,?)",
                (toggle, category, region, now, now),
            )
            # v0.6-3b: prune multiplier from adapter_config.
            if cooldown_s > 0:
                _mult = int(adapter_config.dispatcher.cooldown_prune_multiplier)
                cutoff = now - (_mult * cooldown_s)
                conn.execute(
                    "DELETE FROM dispatcher_cooldowns WHERE last_fired_at < ?",
                    (cutoff,),
                )
        except Exception:
            self._logger.exception(
                "dispatcher: cooldown write-through failed for %s", key
            )

    def _persist_dedup(self, key: tuple[str, str], now: float) -> None:
        """INSERT a single (source, event_id) dedup row + prune rows older
        than _DEDUP_DB_RETENTION_S. Same key arriving twice updates seen_at."""
        try:
            from meshai.persistence import get_db
            conn = get_db()
            source, event_id = key
            conn.execute(
                "INSERT OR REPLACE INTO dispatcher_dedup("
                "source, event_id, seen_at) VALUES (?,?,?)",
                (source, event_id, now),
            )
            # v0.6-3b: retention window from adapter_config (days * 86400).
            retention_s = int(adapter_config.dispatcher.dedup_db_retention_days) * 86400
            cutoff = now - retention_s
            conn.execute(
                "DELETE FROM dispatcher_dedup WHERE seen_at < ?",
                (cutoff,),
            )
        except Exception:
            self._logger.exception(
                "dispatcher: dedup write-through failed for %s", key
            )

    # ---------- core dispatch ----------------------------------------------

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

        v0.6-2: every mutation of the four drop counters, the cold-start
        anchor, the cooldown map, and the dedup LRU writes through to
        SQLite via the _persist_* helpers. Read fast-path stays in-memory.
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
                self._persist_state()    # anchor armed -- durable
                self._logger.info(
                    "cold-start grace anchor set: t0=%.3f window=%ds",
                    now_anchor, grace_s,
                )
            if (now_anchor - self._first_event_at) < grace_s:
                self._cold_start_dropped += 1
                self._persist_state()
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
        # v0.6-3b: fire toggle uses wfigs adapter_config freshness (0 = disabled)
        if fam == "fire":
            freshness_s = int(adapter_config.wfigs.freshness_seconds)
        else:
            freshness_s = int(getattr(tog, "freshness_seconds", 600) or 600)
        if event.timestamp and freshness_s > 0:
            age = time.time() - event.timestamp
            if age > freshness_s:
                self._stale_dropped += 1
                self._persist_state()
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
                self._persist_state()
                return  # silent throttle — no log spam
            self._toggle_cooldown[ck] = now
            self._persist_cooldown(ck, now, cooldown_s)
            # In-memory prune: mirror the SQLite cutoff when the map grows
            # past the threshold. The SQLite prune already ran inside
            # _persist_cooldown.
            # v0.6-3b: prune size + multiplier from adapter_config.
            _prune_size = int(adapter_config.dispatcher.cooldown_prune_size)
            _prune_mult = int(adapter_config.dispatcher.cooldown_prune_multiplier)
            if len(self._toggle_cooldown) > _prune_size:
                cutoff = now - (_prune_mult * cooldown_s)
                self._toggle_cooldown = {
                    k: t for k, t in self._toggle_cooldown.items() if t >= cutoff
                }

        # ---------- Section 3 — (source, event.id) dedup ----------
        dk = (event.source or "", event.id or "")
        if dk in self._dedup_lru:
            # Touch to keep recent.
            self._dedup_lru.move_to_end(dk)
            self._dedup_dropped += 1
            self._persist_state()
            # Refresh seen_at on disk too -- a repeat sighting is fresh
            # evidence we're still seeing this id.
            self._persist_dedup(dk, time.time())
            return
        self._dedup_lru[dk] = True
        self._persist_dedup(dk, time.time())
        # v0.6-3b: read cap from adapter_config (default 10_000).
        _lru_max = int(adapter_config.dispatcher.dedup_lru_max)
        while len(self._dedup_lru) > _lru_max:
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
        """Expose v0.5.2 toggle-path guard counters for ops/health endpoints.

        Returns the in-memory (= write-through current) values. Equivalent to
        SELECT from dispatcher_state but avoids the DB round-trip on every
        call. The numbers are cumulative-since-install (NOT since-boot).
        """
        return {
            "stale_dropped": self._stale_dropped,
            "cooldown_dropped": self._cooldown_dropped,
            "dedup_dropped": self._dedup_dropped,
            "cold_start_dropped": self._cold_start_dropped,
            "cold_start_anchor_at": self._first_event_at,
            "cooldown_keys": len(self._toggle_cooldown),
            "dedup_lru_size": len(self._dedup_lru),
        }

    async def dispatch_scheduled_broadcast(self, text: str, *,
                                             source_event_table: str,
                                             source_event_pk: str,
                                             ) -> bool:
        """v0.5.11 scheduled broadcast entry point.

        Bypasses the normal toggle / rules / freshness-gate pipeline
        because scheduled broadcasts are intentionally periodic and
        already pre-composed. Cold-start grace still applies so the
        very first scheduled broadcast after meshai starts is
        suppressed (consistent with how event-driven adapters behave).

        Channel selection: routes through the rf_propagation toggle\'s
        broadcast_channel since band conditions IS RF-propagation info.
        If that toggle is not configured with a channel, the broadcast
        is dropped (with a log).

        Returns True on successful mesh delivery, False on grace-drop
        or any other suppression.
        """
        # Cold-start grace (mirrors _dispatch_toggles Section 0).
        grace_s = int(getattr(self._config.notifications,
                                "cold_start_grace_seconds", 60) or 0)
        if grace_s > 0:
            now_anchor = time.time()
            if self._first_event_at is None:
                self._first_event_at = now_anchor
                self._persist_state()
            if (now_anchor - self._first_event_at) < grace_s:
                self._cold_start_dropped += 1
                self._persist_state()
                self._logger.info(
                    "cold-start grace: dropping scheduled broadcast "
                    "(table=%s pk=%s)",
                    source_event_table, source_event_pk)
                return False

        # Route through rf_propagation toggle\'s broadcast_channel.
        toggles = getattr(self._config.notifications, "toggles", None) or {}
        rf = toggles.get("rf_propagation") if isinstance(toggles, dict) else None
        if rf is None or not getattr(rf, "broadcast_channel", None):
            self._logger.info(
                "scheduled-broadcast: rf_propagation channel not "
                "configured; dropping")
            return False

        # Build a synthetic Event purely to reuse _toggle_to_rule + the
        # NotificationPayload constructor. Severity \'priority\' keeps it
        # out of quiet-hours suppression unless explicitly overridden.
        from meshai.notifications.events import (
            make_event,
            make_payload_from_event,
        )
        ev = make_event(
            source="band_conditions", category="rf_propagation",
            severity="priority", title=text,
        )
        ev.data["_meshai_precomposed"] = True
        rule = self._toggle_to_rule(rf, "mesh_broadcast", ev)
        try:
            channel = self._channel_factory(rule, self._connector)
            payload = make_payload_from_event(ev, message=text)
            success = await channel.deliver(payload, rule)
        except Exception:
            self._logger.exception(
                "scheduled-broadcast: delivery raised; treating as failed")
            return False

        if success:
            # Audit row -- mirrors _post_broadcast_commit for scheduled.
            try:
                from meshai.persistence import get_db
                conn = get_db()
                bytes_sent = len(text.encode("utf-8")) if text else 0
                conn.execute(
                    "INSERT INTO mesh_broadcasts_out(sent_at, recipient, "
                    "channel, text, source_event_table, source_event_pk, "
                    "bytes_sent, ack_received) VALUES (?,?,?,?,?,?,?,?)",
                    (int(time.time()), "broadcast",
                     rf.broadcast_channel, text,
                     source_event_table, str(source_event_pk),
                     bytes_sent, 0),
                )
            except Exception:
                self._logger.exception(
                    "scheduled-broadcast: audit row insert failed")
        return bool(success)

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
