"""Event grouper.

Coalesces events sharing a group_key inside a time window. The most
recent event for a group_key wins; older versions are replaced.
Events without a group_key pass through immediately.

The grouper holds events in a window. tick() flushes events whose
window has expired. For Phase 2.2, tick() is called explicitly by
tests; Phase 2.3+ will integrate with the digest scheduler for live
operation.
"""

import logging
import time
from typing import Callable

from meshai.notifications.events import Event


# Categories that skip the coalescing window entirely, regardless of
# group_key. This is a CATEGORY allowlist, not a severity one -- keep
# it that way.
#
# wildfire_spotting is exempt because it is a life-safety signal (a
# fire throwing embers past its own containment line) where the
# grouper's hold window (adapter_config.pipeline.grouper_window_seconds,
# 60s live) plus FirePacer queuing would add a ~60-120s latency floor
# on the single most urgent alert in the system. It is safe to bypass
# because spotting is ALREADY rate-limited at the source: a per-fire
# 1-hour cooldown gates spotting *detection* itself
# (adapter_config.fires.spotting_cooldown_seconds, default 3600s,
# latched on fires.last_spotting_broadcast_at per irwin_id in
# gating/firms.py). N active fires therefore yield at most N spotting
# alerts per hour, independent of this bypass. The FirePacer (60s
# interval, head-of-line ordering for "immediate" severity) and the
# dispatcher's per-(toggle, CATEGORY, region) cooldown still apply
# downstream, so nothing here is unbounded.
#
# DO NOT add a severity-based bypass here (e.g. "if event.severity ==
# 'immediate'"). Commit 85d48ce3 ("fix(fire): remove immediate-severity
# exemption from grouper + cooldown") deliberately deleted exactly that
# check: EVERY fire event carries _severity_override="immediate", so a
# severity bypass exempts the entire fire family (growth, containment,
# declared, halted -- not just spotting) from both the grouper window
# AND effectively defeats rate control, reopening the fire-broadcast-
# spam hole on a public radio mesh. This has already been misdiagnosed
# and "fixed" that wrong way twice. Any future urgency-driven bypass
# MUST be scoped by event.category (added to this frozenset), never by
# event.severity.
_NEVER_COALESCE_CATEGORIES = frozenset({"wildfire_spotting"})


class Grouper:
    """Coalesce same-group_key events inside a window."""

    def __init__(
        self,
        next_handler: Callable[[Event], None],
        window_seconds: float | None = None,
    ):
        """Initialize.

        Args:
            next_handler: Callable that receives events when they
                exit the grouper (either immediately if no group_key,
                or after the window expires).
            window_seconds: Hold window before emission. None -> read
                from adapter_config.pipeline.grouper_window_seconds
                (default 60). v0.6-3b.
        """
        self._next = next_handler
        if window_seconds is None:
            from meshai.adapter_config import adapter_config
            window_seconds = float(adapter_config.pipeline.grouper_window_seconds)
        self._window = window_seconds
        # {group_key: (event, hold_until_ts)}
        self._held: dict[str, tuple[Event, float]] = {}
        self._logger = logging.getLogger("meshai.pipeline.grouper")
        self._restore_from_db()

    def _restore_from_db(self) -> None:
        try:
            from meshai.persistence import get_db
            import json
            conn = get_db()
            rows = conn.execute(
                "SELECT group_key, event_json, hold_until_at FROM grouper_held "
                "WHERE hold_until_at > ?",
                (self._now(),),
            ).fetchall()
            for r in rows:
                try:
                    ev = Event.from_dict(json.loads(r["event_json"]))
                    self._held[r["group_key"]] = (ev, float(r["hold_until_at"]))
                except Exception:
                    continue
            if self._held:
                self._logger.info("grouper: restored %d held events", len(self._held))
        except Exception:
            self._logger.exception("grouper: restore_from_db failed; using empty")

    def _persist_held(self, group_key: str, event: "Event", hold_until: float) -> None:
        try:
            import json
            from meshai.persistence import get_db
            d = event.to_dict()
            d["data"] = {k: v for k, v in d.get("data", {}).items()
                         if not callable(v) and not k.startswith("_on_")}
            event_json = json.dumps(d)
            get_db().execute(
                "INSERT OR REPLACE INTO grouper_held(group_key, event_json, "
                "hold_until_at, updated_at) VALUES (?,?,?,?)",
                (group_key, event_json, hold_until, self._now()),
            )
        except Exception:
            self._logger.exception("grouper: persist failed key=%s", group_key)

    def _delete_held(self, group_key: str) -> None:
        try:
            from meshai.persistence import get_db
            get_db().execute("DELETE FROM grouper_held WHERE group_key=?", (group_key,))
        except Exception:
            pass

    def _now(self) -> float:
        return time.time()

    def handle(self, event: Event) -> None:
        """Process an event.

        Events without group_key pass through immediately.
        Events whose category is in _NEVER_COALESCE_CATEGORIES also pass
        through immediately, group_key or not (see module docstring for
        why -- category-scoped only, never severity-scoped).
        Events with group_key are held, replacing any prior held
        event with the same group_key. The held event is emitted
        later via tick().
        """
        if not event.group_key or event.category in _NEVER_COALESCE_CATEGORIES:
            self._next(event)
            return

        now = self._now()
        hold_until = now + self._window
        prior = self._held.get(event.group_key)
        if prior is not None:
            self._logger.info(
                f"COALESCED event {event.id} into group {event.group_key!r}, "
                f"replacing prior event {prior[0].id}"
            )
        self._held[event.group_key] = (event, hold_until)
        self._persist_held(event.group_key, event, hold_until)

    def tick(self) -> int:
        """Flush events whose window has expired.

        Returns the number of events emitted.
        """
        now = self._now()
        to_emit = [
            (gk, ev) for gk, (ev, hu) in self._held.items() if hu <= now
        ]
        for gk, _ in to_emit:
            del self._held[gk]
            self._delete_held(gk)
        for _, ev in to_emit:
            self._next(ev)
        return len(to_emit)

    def flush_all(self) -> int:
        """Immediately emit every held event, regardless of window.

        Used at shutdown and by tests. Returns count emitted.
        """
        events = [ev for ev, _ in self._held.values()]
        keys = list(self._held.keys())
        self._held.clear()
        for k in keys:
            self._delete_held(k)
        for ev in events:
            self._next(ev)
        return len(events)

    def held_count(self) -> int:
        """For tests: number of events currently held."""
        return len(self._held)
