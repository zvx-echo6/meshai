"""Severity-based event inhibitor.

Suppresses lower-severity events when a higher-severity event for the
same logical incident (matching inhibit_keys) is already active.

Inhibit keys are operator-defined strings on the Event that identify
the underlying incident. Two events sharing an inhibit_key refer to
the same situation. If a critical event for "battery:BLD-MTN" fired
recently, a subsequent warning event with the same key gets suppressed.
"""

import logging
import time
from typing import Callable

from meshai.notifications.events import Event


class Inhibitor:
    """Suppress lower-severity events when higher-severity is active."""

    SEVERITY_RANK = {"routine": 0, "priority": 1, "immediate": 2}

    def __init__(
        self,
        next_handler: Callable[[Event], None],
        ttl_seconds: float | None = None,
    ):
        """Initialize.

        Args:
            next_handler: Callable that receives non-suppressed events.
            ttl_seconds: How long an inhibit_key remains active after
                the originating event. None -> read from
                adapter_config.pipeline.inhibitor_ttl_seconds (default
                1800). v0.6-3b: explicit value still wins for tests.
        """
        self._next = next_handler
        if ttl_seconds is None:
            from meshai.adapter_config import adapter_config
            ttl_seconds = float(adapter_config.pipeline.inhibitor_ttl_seconds)
        self._ttl = ttl_seconds
        # {inhibit_key: (rank, expires_at)}
        self._active: dict[str, tuple[int, float]] = {}
        self._logger = logging.getLogger("meshai.pipeline.inhibitor")
        # v0.6-6: restore non-expired rows from inhibit_state on construct.
        self._restore_from_db()

    def _restore_from_db(self) -> None:
        try:
            from meshai.persistence import get_db
            conn = get_db()
            rows = conn.execute(
                "SELECT key, rank, expires_at FROM inhibit_state "
                "WHERE expires_at > ?",
                (self._now(),),
            ).fetchall()
            for r in rows:
                self._active[r["key"]] = (int(r["rank"]), float(r["expires_at"]))
            if self._active:
                self._logger.info("inhibitor: restored %d active keys", len(self._active))
        except Exception:
            self._logger.exception("inhibitor: restore_from_db failed; using empty")

    def _persist_key(self, key: str, rank: int, expires_at: float) -> None:
        try:
            from meshai.persistence import get_db
            conn = get_db()
            conn.execute(
                "INSERT OR REPLACE INTO inhibit_state(key, rank, expires_at, updated_at) "
                "VALUES (?,?,?,?)",
                (key, rank, expires_at, self._now()),
            )
        except Exception:
            self._logger.exception("inhibitor: persist failed key=%s", key)

    def _now(self) -> float:
        # Hookable for tests
        return time.time()

    def _prune_expired(self, now: float) -> None:
        expired = [k for k, (_, exp) in self._active.items() if exp <= now]
        for k in expired:
            del self._active[k]
        # v0.6-6: keep on-disk in sync; cheap single DELETE per prune cycle.
        if expired:
            try:
                from meshai.persistence import get_db
                get_db().execute(
                    "DELETE FROM inhibit_state WHERE expires_at <= ?", (now,)
                )
            except Exception:
                pass

    def handle(self, event: Event) -> None:
        """Process an event: either suppress it or pass it on.

        If any of the event's inhibit_keys is currently active at a
        higher-or-equal rank, the event is suppressed. Otherwise, the
        event's inhibit_keys are recorded/upgraded, and the event is
        passed to the next handler.
        """
        now = self._now()
        self._prune_expired(now)

        event_rank = self.SEVERITY_RANK.get(event.severity, 0)

        # Check suppression
        for key in event.inhibit_keys:
            entry = self._active.get(key)
            if entry is not None:
                active_rank, _ = entry
                if active_rank >= event_rank:
                    self._logger.info(
                        f"SUPPRESSED event {event.id} ({event.severity}) "
                        f"by active key {key!r} at rank {active_rank}"
                    )
                    return

        # Record / upgrade entries + write-through to inhibit_state.
        new_expires = now + self._ttl
        for key in event.inhibit_keys:
            existing = self._active.get(key)
            if existing is None or existing[0] < event_rank:
                self._active[key] = (event_rank, new_expires)
                self._persist_key(key, event_rank, new_expires)

        # Pass through
        self._next(event)

    def active_keys(self) -> dict[str, tuple[int, float]]:
        """For tests: snapshot of currently-active inhibit keys."""
        return dict(self._active)

    def clear(self) -> None:
        """For tests: reset both in-memory state and the inhibit_state table."""
        self._active.clear()
        try:
            from meshai.persistence import get_db
            get_db().execute("DELETE FROM inhibit_state")
        except Exception:
            pass
