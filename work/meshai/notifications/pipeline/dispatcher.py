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
        self._severity_floor_dropped = 0
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
                "dedup_dropped, cold_start_dropped, severity_floor_dropped "
                "FROM dispatcher_state WHERE id=1"
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
            self._severity_floor_dropped = int(row["severity_floor_dropped"] or 0)
            self._logger.info(
                "dispatcher state restored: cold_start_anchor=%s "
                "stale=%d cooldown=%d dedup=%d cold_start=%d sev_floor=%d",
                self._first_event_at, self._stale_dropped,
                self._cooldown_dropped, self._dedup_dropped,
                self._cold_start_dropped, self._severity_floor_dropped,
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
                "cold_start_dropped=?, severity_floor_dropped=?, updated_at=? WHERE id=1",
                (self._first_event_at, self._stale_dropped,
                 self._cooldown_dropped, self._dedup_dropped,
                 self._cold_start_dropped, self._severity_floor_dropped, time.time()),
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
            # v0.16 (Integration C1): a rule that references reusable
            # destinations fans out to each resolved destination; a rule with
            # EMPTY `destinations` delivers via its own inline delivery_type +
            # fields exactly as before (drule IS rule -> byte-identical).
            if getattr(rule, "destinations", None):
                # Opted in: fan out to resolved destinations only (unknown names
                # are skipped; no silent fallback to the rule's inline fields).
                dests = self._resolve_destinations(rule.destinations)
                delivery = [self._destination_to_rule(d, event) for d in dests
                            if getattr(d, "type", "") != "digest"]
            else:
                delivery = [rule]
            for drule in delivery:
                try:
                    channel = self._channel_factory(drule, self._connector)
                    payload = make_payload_from_event(event)
                    success = await channel.deliver(payload, drule)
                    if success:
                        self._logger.info(
                            f"Dispatched event {event.id} via {drule.delivery_type}"
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

        # ---------- Section 1.5 — region_routes matrix branch (P3) ----------
        # Authoritative on match: when the matrix is enabled AND this event's
        # region resolves to at least one cell, the matrix block handles ALL
        # delivery (dedup, cooldown, channel, audit) and returns. Sections 2–6
        # run ONLY when the matrix is disabled, has no cells for this family,
        # or no event region matches any cell — preserving byte-identical
        # behaviour for all existing config.
        rr = getattr(self._config.notifications, "region_routes", None)
        _matrix_matched = None
        if rr is not None and getattr(rr, "enabled", False):
            fam_cells = (getattr(rr, "cells", None) or {}).get(fam)
            if fam_cells:
                ev_regions = [r for r in ([event.region, *(event.regions or [])]) if r]
                _seen_r: set = set()
                _mlist: list = []
                for r in ev_regions:
                    if r in _seen_r or r not in fam_cells:
                        continue
                    _seen_r.add(r)
                    _mlist.append((r, fam_cells[r]))
                if _mlist:
                    _matrix_matched = _mlist

        if _matrix_matched is not None:
            # ---- Authoritative matrix delivery block ----
            # Collapse to one send per distinct (ch_type, chan_val), tracking
            # which regions feed it. This lets MT "SWI Alerts" carry roads +
            # fires for SW Idaho with one broadcast while MC keeps per-family
            # names distinct.
            event_rank = self.SEVERITY_RANK.get(event.severity, 0)
            _chans: dict = {}   # (ch_type, chan_val) -> [region, ...]
            for _mr, _cell in _matrix_matched:
                _enabled_cell = _cell.get("enabled", True) if isinstance(_cell, dict) \
                    else getattr(_cell, "enabled", True)
                if not _enabled_cell:
                    continue
                _floor = ((_cell.get("min_severity") if isinstance(_cell, dict)
                            else getattr(_cell, "min_severity", None)) or "routine")
                if event_rank < self.SEVERITY_RANK.get(_floor, 0):
                    self._logger.warning(
                        "severity-floor drop: source=%s category=%s id=%s severity=%s "
                        "< floor=%s (matrix ch=%s region=%s)",
                        event.source, event.category, event.id, event.severity,
                        _floor,
                        _cell.get("mt") if isinstance(_cell, dict) else getattr(_cell, "mt", "?"),
                        _mr,
                    )
                    self._severity_floor_dropped += 1
                    self._persist_state()
                    continue   # below per-cell floor for this region
                _mt = _cell.get("mt") if isinstance(_cell, dict) else getattr(_cell, "mt", None)
                _mc = _cell.get("mc") if isinstance(_cell, dict) else getattr(_cell, "mc", None)
                if _mt is not None:
                    _chans.setdefault(("mesh_broadcast", _mt), []).append(_mr)
                if _mc:   # truthy: non-empty string
                    _chans.setdefault(("meshcore_broadcast", _mc), []).append(_mr)

            if not _chans:
                # Every matched region below its floor or cell disabled/empty.
                # Authoritative no-send: do NOT fall through to toggle default.
                return

            # Compose once; reused across all channels (shadow render skipped).
            try:
                friendly = compose_mesh_message(event)
            except Exception:
                self._logger.exception(
                    "matrix: mesh composer crashed; falling back to legacy message"
                )
                friendly = None

            _cooldown_s = int(getattr(tog, "cooldown_seconds", 300) or 0)
            _dd_suffix = str((event.data or {}).get("_dedup_suffix", "") or "")
            _dd_id = (event.id or "") + ("#" + _dd_suffix if _dd_suffix else "")
            _cd_suffix = (event.data or {}).get("_cooldown_suffix", "") or ""

            _now = time.time()

            for (_ch_type, _chan_val), _regions in _chans.items():
                # Per-(transport, channel) dedup check — the key is a 2-tuple
                # (source, "id#suffix|ch_type|chan") so each channel dedups
                # independently AND the key matches the boot-restore form
                # (dispatcher construction rehydrates _dedup_lru as 2-tuples).
                # A 4-tuple here would miss after every restart and re-broadcast
                # the whole matrix backlog — a restart flood. A failed channel
                # leaves no dedup trace, so it retries next sweep.
                _dk = (event.source or "", f"{_dd_id}|{_ch_type}|{_chan_val}")
                if _dk in self._dedup_lru:
                    self._dedup_lru.move_to_end(_dk)
                    self._dedup_dropped += 1
                    self._persist_state()
                    self._persist_dedup(_dk, _now)
                    continue  # skip THIS channel only; other channels still run

                # Per-region cooldown check — skip channel only when EVERY
                # region feeding it is still within its cooldown window.
                if _cooldown_s > 0:
                    _all_cooled = True
                    for _rg in _regions:
                        _rk = (
                            getattr(tog, "name", "") or fam,
                            event.category,
                            _rg + ("|" + _cd_suffix if _cd_suffix else ""),
                        )
                        _last = self._toggle_cooldown.get(_rk)
                        if _last is None or (_now - _last) >= _cooldown_s:
                            _all_cooled = False
                            break
                    if _all_cooled:
                        self._cooldown_dropped += 1
                        self._persist_state()
                        continue  # skip channel

                # Deliver
                _rule = None
                _payload = None
                _success = False
                try:
                    _rule = self._toggle_to_rule(
                        tog, _ch_type, event,
                        mt_override=(_chan_val if _ch_type == "mesh_broadcast" else None),
                        mc_override=(_chan_val if _ch_type == "meshcore_broadcast" else None),
                    )
                    _channel = self._channel_factory(_rule, self._connector)
                    if friendly is not None and _ch_type in (
                        "mesh_broadcast", "mesh_dm", "meshcore_broadcast", "meshcore_dm"
                    ):
                        _payload = make_payload_from_event(event, message=friendly)
                    else:
                        _payload = make_payload_from_event(event)
                    _success = await _channel.deliver(_payload, _rule)
                    if _success:
                        self._logger.info(
                            "matrix: dispatched %s via %s ch=%s regions=%s",
                            event.id, _ch_type, _chan_val, _regions,
                        )
                    else:
                        self._logger.warning(
                            "matrix: channel delivery returned False %s ch=%s",
                            _ch_type, _chan_val,
                        )
                    self._post_broadcast_commit(
                        event, _payload, _rule, _ch_type, success=bool(_success)
                    )
                except Exception:
                    self._logger.exception(
                        "matrix: channel delivery failed for %s ch=%s",
                        _ch_type, _chan_val,
                    )
                    self._post_broadcast_commit(
                        event, _payload, _rule, _ch_type, success=False
                    )

                # Arm guards on success only — a failed delivery leaves no
                # trace so the next sweep can retry this channel.
                if _success:
                    _commit_now = time.time()
                    # Dedup: the same channel-qualified 2-tuple in memory and DB
                    # so mesh_broadcast and meshcore_broadcast on one event each
                    # get their own row, and the key matches the boot-restore form
                    # (survives restart -> no re-broadcast flood).
                    self._dedup_lru[_dk] = True
                    self._persist_dedup(_dk, _commit_now)
                    _lru_max = int(adapter_config.dispatcher.dedup_lru_max)
                    while len(self._dedup_lru) > _lru_max:
                        self._dedup_lru.popitem(last=False)
                    # Cooldown: arm for every region that fed this channel.
                    if _cooldown_s > 0:
                        for _rg in _regions:
                            _rk = (
                                getattr(tog, "name", "") or fam,
                                event.category,
                                _rg + ("|" + _cd_suffix if _cd_suffix else ""),
                            )
                            self._toggle_cooldown[_rk] = _commit_now
                            self._persist_cooldown(_rk, _commit_now, _cooldown_s)

            # Authoritative: the matrix handled this event; skip toggle default.
            return

        # ---------- Section 2 — region scope + severity floor + matrix ----
        # v0.6-4 (B13 fix): resolution before commitment. Region scope, the
        # min_severity floor, and severity_channels matrix resolution all
        # run BEFORE the cooldown/dedup guards, so an event that was never
        # going to deliver (wrong region, below floor, empty matrix row)
        # cannot arm a cooldown window or burn a dedup slot. Previously a
        # non-deliverable event committed both, silently suppressing later
        # deliverable events for up to the dedup retention window (7 days).
        regions = getattr(tog, "regions", None) or []
        if regions:
            ev_regions = set(filter(None, [event.region, *(event.regions or [])]))
            if not (set(regions) & ev_regions):
                return
        event_rank = self.SEVERITY_RANK.get(event.severity, 0)
        _tog_floor = getattr(tog, "min_severity", "routine")
        if event_rank < self.SEVERITY_RANK.get(_tog_floor, 0):
            self._logger.warning(
                "severity-floor drop: source=%s category=%s id=%s severity=%s "
                "< floor=%s (toggle=%s)",
                event.source, event.category, event.id, event.severity,
                _tog_floor, fam,
            )
            self._severity_floor_dropped += 1
            self._persist_state()
            return
        # v0.16 (Integration C1) — destinations vs inline routing.
        # If the toggle references reusable NotificationDestinations, deliver
        # via those resolved destinations (each carries its own delivery type),
        # gated ONLY by the region scope + min_severity floor already applied
        # above. The severity_channels matrix is intentionally BYPASSED on the
        # destination path: a shared destination is the single source of truth
        # for its delivery type + fields. When `destinations` is EMPTY (all
        # pre-C1 config) the existing severity_channels -> inline-field path
        # runs completely unchanged (zero regression).
        # Opt-in is decided by the presence of a reference list, NOT by whether
        # it resolves: a toggle that references destinations but whose names are
        # unknown delivers NOTHING (it does not silently fall back to the inline
        # fields, which would broadcast stale/duplicate config unexpectedly).
        if getattr(tog, "destinations", None):
            dests = self._resolve_destinations(tog.destinations)
            # digest-typed destinations belong to the digest scheduler, not the
            # live broadcast path (mirrors the inline "digest" exclusion below).
            delivery_plan = [("dest", d) for d in dests
                             if getattr(d, "type", "") != "digest"]
        else:
            sev_channels = getattr(tog, "severity_channels", None) or {}
            ch_types = [c for c in sev_channels.get(event.severity, []) if c != "digest"]
            delivery_plan = [("toggle", ct) for ct in ch_types]
        if not delivery_plan:
            return

        # ---------- Section 3 — per-toggle cooldown (check only) ----------
        # All severities (including immediate) obey cooldown. Fire rate
        # control was previously bypassed for immediate; the drain-mode
        # pacer handles reconnect bursts, and this cooldown handles
        # normal live operation (≤1 per cooldown window per toggle/key).
        # v0.6-4 (B13 fix): this section only CHECKS the cooldown. Arming
        # it is deferred to Section 6 and happens only after a delivery
        # actually succeeded.
        cooldown_s = int(getattr(tog, "cooldown_seconds", 300) or 0)
        ck = None
        if cooldown_s > 0:
            suffix = (event.data or {}).get("_cooldown_suffix", "")
            region_key = event.region or "*"
            if suffix:
                region_key = f"{region_key}|{suffix}"
            ck = (
                getattr(tog, "name", "") or fam,
                event.category,
                region_key,
            )
            now = time.time()
            last_fired = self._toggle_cooldown.get(ck)
            if last_fired is not None and (now - last_fired) < cooldown_s:
                self._cooldown_dropped += 1
                self._persist_state()
                return  # silent throttle — no log spam
            # v0.6-4 (B13 fix): arming moved to Section 6 (post-delivery).

        # ---------- Section 4 — (source, event.id) dedup (check only) ------
        # v0.6-4: the dedup key gains an optional handler-supplied suffix
        # (event.data["_dedup_suffix"]). Feeds like WFIGS publish the SAME
        # upstream id (IrwinID) on every sweep for the life of an incident,
        # so a bare (source, id) key permanently suppressed every later
        # lifecycle broadcast (growth/containment updates) the handler had
        # deliberately synthesized. Handlers that gate their own re-renders
        # stamp the state that justified THIS broadcast into the suffix;
        # unchanged re-deliveries still dedup, genuine updates pass.
        _dd_suffix = str((event.data or {}).get("_dedup_suffix", "") or "")
        _dd_id = event.id or ""
        if _dd_suffix:
            _dd_id = f"{_dd_id}#{_dd_suffix}"
        dk = (event.source or "", _dd_id)
        if dk in self._dedup_lru:
            # Touch to keep recent.
            self._dedup_lru.move_to_end(dk)
            self._dedup_dropped += 1
            self._persist_state()
            # Refresh seen_at on disk too -- a repeat sighting is fresh
            # evidence we're still seeing this id.
            self._persist_dedup(dk, time.time())
            return
        # v0.6-4 (B13 fix): recording moved to Section 6 (post-delivery).
        # A delivery that fails (or raises for every channel) leaves no
        # dedup trace, so the next feed sweep retries it naturally.

        # ---------- Section 5 — friendly composer wired in ----------
        # Render once per event; reused across every channel below. Wrapped
        # so a renderer fault never blocks delivery — we fall back to the
        # legacy make_payload_from_event message (event.summary|title|category).
        try:
            friendly = compose_mesh_message(event)
        except Exception:
            self._logger.exception("mesh composer crashed; falling back to legacy message")
            friendly = None

        # Phase-0b shadow render hook — inert by default.
        # Called AFTER compose_mesh_message so old_wire is the real produced
        # string.  The real path continues to use *friendly* unchanged below.
        if friendly is not None:
            try:
                from meshai.notifications.shadow import shadow_render as _shadow_render
                _shadow_render(event.category, event, old_wire=friendly)
            except Exception:  # noqa: BLE001 — shadow must never affect production
                pass

        delivered_any = False
        for _kind, _item in delivery_plan:
            rule = None
            payload = None
            try:
                if _kind == "dest":
                    ch_type = getattr(_item, "type", "")
                    rule = self._destination_to_rule(_item, event)
                else:
                    ch_type = _item
                    rule = self._toggle_to_rule(tog, ch_type, event)
                channel = self._channel_factory(rule, self._connector)
                if friendly is not None and ch_type in (
                    "mesh_broadcast", "mesh_dm", "meshcore_broadcast", "meshcore_dm"
                ):
                    payload = make_payload_from_event(event, message=friendly)
                else:
                    payload = make_payload_from_event(event)
                success = await channel.deliver(payload, rule)
                if success:
                    delivered_any = True
                    self._logger.info(f"Dispatched event {event.id} via toggle {fam}/{ch_type}")
                else:
                    self._logger.warning(f"Toggle channel delivery returned False for {fam}/{ch_type}")
                # v0.5.8b post-broadcast commit -> v20 per-mesh audit.
                # Written ONCE PER MESH CHANNEL with its own transport+success,
                # so a fan-out to both meshes yields two rows and a skip
                # (deliver()==False) is still visible as success=0. The
                # last_broadcast_* callback fires only when success is truthy.
                self._post_broadcast_commit(event, payload, rule, ch_type,
                                            success=bool(success))
            except Exception:
                self._logger.exception(f"Toggle channel delivery failed for {fam}/{ch_type}")
                # A crashed delivery is still a failed send -> success=0 row.
                self._post_broadcast_commit(event, payload, rule, ch_type,
                                            success=False)

        # ---------- Section 6 — guard commit (v0.6-4, B13 fix) ----------
        # Cooldown arming + dedup recording happen ONLY after at least one
        # channel actually delivered. A fully-failed delivery leaves no
        # guard state behind, so the next feed sweep retries naturally
        # instead of being silently suppressed.
        if not delivered_any:
            return
        commit_now = time.time()
        if ck is not None and cooldown_s > 0:
            self._toggle_cooldown[ck] = commit_now
            self._persist_cooldown(ck, commit_now, cooldown_s)
            # In-memory prune: mirror the SQLite cutoff when the map grows
            # past the threshold. The SQLite prune already ran inside
            # _persist_cooldown.
            # v0.6-3b: prune size + multiplier from adapter_config.
            _prune_size = int(adapter_config.dispatcher.cooldown_prune_size)
            _prune_mult = int(adapter_config.dispatcher.cooldown_prune_multiplier)
            if len(self._toggle_cooldown) > _prune_size:
                cutoff = commit_now - (_prune_mult * cooldown_s)
                self._toggle_cooldown = {
                    k: t for k, t in self._toggle_cooldown.items() if t >= cutoff
                }
        self._dedup_lru[dk] = True
        self._persist_dedup(dk, commit_now)
        # v0.6-3b: read cap from adapter_config (default 10_000).
        _lru_max = int(adapter_config.dispatcher.dedup_lru_max)
        while len(self._dedup_lru) > _lru_max:
            self._dedup_lru.popitem(last=False)  # evict oldest

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
            "severity_floor_dropped": self._severity_floor_dropped,
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

        # Route through rf_propagation toggle\'s configured channels.
        toggles = getattr(self._config.notifications, "toggles", None) or {}
        rf = toggles.get("rf_propagation") if isinstance(toggles, dict) else None
        if rf is None:
            self._logger.info(
                "scheduled-broadcast: rf_propagation toggle not found; dropping")
            return False

        # Resolve broadcast channel types from the toggle\'s severity_channels for
        # "priority" (band-conditions are priority-class RF propagation info).
        # Falls back to ["mesh_broadcast"] for old configs without severity_channels.
        sev_channels = getattr(rf, "severity_channels", {}) or {}
        ch_types = [
            c for c in sev_channels.get("priority", ["mesh_broadcast"])
            if c in ("mesh_broadcast", "meshcore_broadcast")
        ]
        if not ch_types:
            # Backward compat: if severity_channels has no broadcast types,
            # use mesh_broadcast when broadcast_channel is configured.
            if getattr(rf, "broadcast_channel", None) is not None:
                ch_types = ["mesh_broadcast"]
            else:
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

        delivered_any = False
        for ch_type in ch_types:
            rule = self._toggle_to_rule(rf, ch_type, ev)
            try:
                channel = self._channel_factory(rule, self._connector)
                payload = make_payload_from_event(ev, message=text)
                success = await channel.deliver(payload, rule)
            except Exception:
                self._logger.exception(
                    "scheduled-broadcast: delivery raised for %s", ch_type)
                success = False

            if success:
                delivered_any = True

            # v20 per-mesh audit row. Written once per mesh channel with its
            # own transport+success, so a fan-out to both meshes yields two
            # rows and a skip (deliver()==False) is visible as success=0.
            try:
                from meshai.persistence import get_db
                conn = get_db()
                bytes_sent = len(text.encode("utf-8")) if text else 0
                transport, channel_id, recipient = self._audit_route(rule, ch_type)
                conn.execute(
                    "INSERT INTO mesh_broadcasts_out(sent_at, recipient, "
                    "channel, text, source_event_table, source_event_pk, "
                    "bytes_sent, ack_received, transport, success) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (int(time.time()), recipient,
                     channel_id, text,
                     source_event_table, str(source_event_pk),
                     bytes_sent, 0,
                     transport, 1 if success else 0),
                )
            except Exception:
                self._logger.exception(
                    "scheduled-broadcast: audit row insert failed for %s", ch_type)
        return delivered_any

    @staticmethod
    def _audit_route(rule, ch_type: str):
        """Resolve (transport, channel_id, recipient) for a mesh delivery.

        transport is the mesh family the row belongs to ("meshtastic" /
        "meshcore"); channel_id is the Meshtastic channel INDEX or the
        MeshCore channel NAME; recipient is 'broadcast' or the DM target
        list. Mirrors create_channel()'s delivery_type routing.
        """
        if ch_type == "mesh_broadcast":
            return "meshtastic", getattr(rule, "broadcast_channel", None), "broadcast"
        if ch_type == "meshcore_broadcast":
            return "meshcore", getattr(rule, "meshcore_channel", None), "broadcast"
        if ch_type == "mesh_dm":
            node_ids = list(getattr(rule, "node_ids", []) or [])
            return "meshtastic", None, (",".join(map(str, node_ids)) or "dm")
        if ch_type == "meshcore_dm":
            contacts = list(getattr(rule, "meshcore_dm_contacts", []) or [])
            return "meshcore", None, (",".join(map(str, contacts)) or "meshcore_dm")
        # Unknown / non-mesh: leave transport NULL, fall back to legacy channel.
        return None, getattr(rule, "broadcast_channel", None), "broadcast"

    # Mesh channel types that get a mesh_broadcasts_out audit row.
    _MESH_CH_TYPES = frozenset(
        {"mesh_broadcast", "meshcore_broadcast", "mesh_dm", "meshcore_dm"}
    )

    def _post_broadcast_commit(self, event, payload, rule, ch_type: str,
                               *, success: bool = True) -> None:
        """Persistence side-effects of a per-mesh broadcast delivery.

        Called ONCE PER MESH CHANNEL (one per delivery_type family), so a
        broadcast that fans to both meshes writes TWO mesh_broadcasts_out
        rows -- each carrying its own `transport` + `success` flag.

        A row is written for EVERY mesh delivery attempt (ch_type in
        _MESH_CH_TYPES), REGARDLESS of success and REGARDLESS of whether
        the handler stamped `_broadcast_audit` on the event. Handlers that
        do stamp it supply `source_event_table`/`source_event_pk`; native
        events that do not have those fields NULL (best-effort). This makes
        region-routed traffic/weather/roads sends visible in the audit even
        though those native adapters do not set `_broadcast_audit`.

        The handler-supplied `_on_broadcast_committed` callback (which
        refreshes last_broadcast_* bookkeeping) fires ONLY when the send
        actually landed (success is truthy). Both calls are wrapped: a
        bookkeeping failure must NOT undo the actual broadcast nor break
        dispatch for sibling toggles.
        """
        data = getattr(event, "data", None) or {}
        committed_at = time.time()

        # --- Audit row (always, for any mesh delivery attempt) ---
        if ch_type in self._MESH_CH_TYPES:
            audit = data.get("_broadcast_audit") if data else None
            try:
                from meshai.persistence import get_db
                conn = get_db()
                text = payload.message if payload is not None else (event.title or "")
                bytes_sent = len(text.encode("utf-8")) if text else 0
                transport, channel, recipient = self._audit_route(rule, ch_type)
                conn.execute(
                    "INSERT INTO mesh_broadcasts_out(sent_at, recipient, channel, "
                    "text, source_event_table, source_event_pk, bytes_sent, "
                    "ack_received, transport, success) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        int(committed_at), recipient, channel, text,
                        audit.get("table") if isinstance(audit, dict) else None,
                        audit.get("pk") if isinstance(audit, dict) else None,
                        bytes_sent, 0,
                        transport, 1 if success else 0,
                    ),
                )
            except Exception:
                self._logger.exception(
                    "post-broadcast: mesh_broadcasts_out insert failed "
                    "(ch_type=%s event_id=%s)",
                    ch_type, getattr(event, "id", None),
                )

        # --- Handler callback (only on success, only when data present) ---
        if not data:
            return

        if not success:
            # A failed/skipped send is audited above but must NOT arm the
            # handler's last_broadcast_* bookkeeping.
            return

        cb = data.get("_on_broadcast_committed")
        if callable(cb):
            try:
                cb(committed_at)
            except Exception:
                self._logger.exception(
                    "post-broadcast: handler commit-callback raised"
                )

    def _resolve_destinations(self, names) -> list:
        """Resolve destination NAMES -> NotificationDestination objects via
        config.notifications.destinations (Integration C1).

        Returns [] for an empty/None input so callers fall back to the inline
        delivery path unchanged. Unknown names are skipped with a warning (never
        raised) so a dangling reference degrades gracefully rather than dropping
        the whole broadcast.
        """
        if not names:
            return []
        registry = getattr(self._config.notifications, "destinations", None)
        if not isinstance(registry, dict) or not registry:
            self._logger.warning(
                "dispatcher: destinations referenced (%s) but none configured", names)
            return []
        out = []
        for nm in names:
            dest = registry.get(nm)
            if dest is None:
                self._logger.warning(
                    "dispatcher: unknown destination %r referenced; skipping", nm)
                continue
            out.append(dest)
        return out

    def _destination_to_rule(self, dest, event: Event):
        """Synthesize a NotificationRuleConfig from a shared destination's
        fields (mirrors _toggle_to_rule) so it maps to create_channel identically
        to an inline rule."""
        from meshai.config import NotificationRuleConfig
        return NotificationRuleConfig(
            name=f"dest:{getattr(dest, 'name', '')}",
            enabled=True, trigger_type="condition",
            delivery_type=getattr(dest, "type", ""),
            broadcast_channel=(getattr(dest, "broadcast_channel", None) or 0),
            meshcore_channel=getattr(dest, "meshcore_channel", None),
            node_ids=list(getattr(dest, "node_ids", []) or []),
            meshcore_dm_contacts=list(getattr(dest, "meshcore_dm_contacts", []) or []),
            smtp_host=getattr(dest, "smtp_host", ""), smtp_port=getattr(dest, "smtp_port", 587),
            smtp_user=getattr(dest, "smtp_user", ""), smtp_password=getattr(dest, "smtp_password", ""),
            smtp_tls=getattr(dest, "smtp_tls", True), from_address=getattr(dest, "from_address", ""),
            recipients=list(getattr(dest, "recipients", []) or []),
            webhook_url=getattr(dest, "webhook_url", ""),
            webhook_headers=dict(getattr(dest, "webhook_headers", {}) or {}),
        )

    def _toggle_to_rule(self, tog, ch_type: str, event: Event, *,
                        mt_override=None, mc_override=None):
        """Synthesize a NotificationRuleConfig from a toggle's delivery fields.

        mt_override / mc_override (keyword-only): when provided by the matrix
        branch, these replace the toggle's own broadcast_channel /
        meshcore_channel so each matrix cell can route to its assigned channel
        without mutating the toggle config. Existing callers pass neither
        keyword → output is byte-identical to the pre-matrix signature.
        """
        from meshai.config import NotificationRuleConfig
        broadcast_channel = (
            mt_override if mt_override is not None
            else (getattr(tog, "broadcast_channel", None) or 0)
        )
        meshcore_channel = (
            mc_override if mc_override is not None
            else getattr(tog, "meshcore_channel", None)
        )
        return NotificationRuleConfig(
            name=f"toggle:{getattr(tog, 'name', '')}",
            enabled=True, trigger_type="condition", delivery_type=ch_type,
            broadcast_channel=broadcast_channel,
            meshcore_channel=meshcore_channel,
            node_ids=list(getattr(tog, "node_ids", []) or []),
            meshcore_dm_contacts=list(getattr(tog, "meshcore_dm_contacts", []) or []),
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
