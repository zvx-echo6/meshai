"""Digest scheduler — fires the digest at a configured time of day.

Reads schedule and channel routing from config; calls
accumulator.render_digest() at the scheduled time; delivers the
result to all rules matching trigger_type=='schedule' and
schedule_match=='digest'.
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Callable, Optional

from meshai.notifications.pipeline.digest import DigestAccumulator


class DigestScheduler:
    """Fires digest at configured time and routes to matching channels."""

    def __init__(
        self,
        accumulator: DigestAccumulator,
        config,
        channel_factory: Callable,
        clock: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], "asyncio.Future"]] = None,
    ):
        self._accumulator = accumulator
        self._config = config
        self._channel_factory = channel_factory
        self._clock = clock or time.time
        self._sleep = sleep or asyncio.sleep
        self._task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._last_fire_at: float = 0.0
        self._logger = logging.getLogger("meshai.pipeline.scheduler")

    async def start(self) -> None:
        """Begin the scheduler loop as an asyncio task."""
        if self._task is not None and not self._task.done():
            raise RuntimeError("Scheduler already running")
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="digest-scheduler")
        self._logger.info(
            f"Digest scheduler started, schedule={self._schedule_str()!r}"
        )

    async def stop(self) -> None:
        """Signal stop and wait for the task to finish."""
        if self._task is None:
            return
        if self._stop_event:
            self._stop_event.set()
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            # Cancellation is expected; other exceptions already logged
            pass
        self._task = None
        self._logger.info("Digest scheduler stopped")

    async def _run(self) -> None:
        """Main loop: sleep until next fire, fire, repeat."""
        try:
            while self._stop_event and not self._stop_event.is_set():
                now = self._clock()
                next_fire = self._next_fire_at(now)
                delay = max(0.0, next_fire - now)
                self._logger.info(
                    f"Next digest at {datetime.fromtimestamp(next_fire):%Y-%m-%d %H:%M}, "
                    f"sleeping {delay:.0f}s"
                )
                # Interruptible sleep — wakes early if stop() is called
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=delay,
                    )
                    # If we got here without timeout, stop was requested
                    return
                except asyncio.TimeoutError:
                    pass  # Timeout fired = digest time arrived

                if self._stop_event.is_set():
                    return
                try:
                    await self._fire(self._clock())
                except Exception:
                    self._logger.exception("Digest fire failed; will retry next cycle")
        except asyncio.CancelledError:
            raise
        except Exception:
            self._logger.exception("Scheduler loop crashed unexpectedly")
            raise

    async def _fire(self, now: float) -> None:
        """Render and deliver one digest."""
        self._logger.info(f"Firing digest at {datetime.fromtimestamp(now):%H:%M}")
        digest = self._accumulator.render_digest(now)
        self._last_fire_at = now

        rules = self._matching_rules()
        if not rules:
            self._logger.warning(
                "No digest delivery rules configured (need rules with "
                "trigger_type=='schedule' and schedule_match=='digest')"
            )
            return

        for rule in rules:
            try:
                await self._deliver_to_rule(rule, digest, now)
            except Exception:
                self._logger.exception(
                    f"Digest delivery failed for rule {rule.name!r}"
                )

    async def _deliver_to_rule(self, rule, digest, now: float) -> None:
        """Hand the rendered digest to a channel based on rule.delivery_type."""
        channel = self._channel_factory(rule)
        delivery_type = rule.delivery_type

        if delivery_type in ("mesh_broadcast", "mesh_dm"):
            # One deliver call per chunk
            chunks = digest.mesh_chunks
            total = len(chunks)
            for i, chunk in enumerate(chunks, start=1):
                payload = {
                    "category": "digest",
                    "severity": "routine",
                    "message": chunk,
                    "node_id": None,
                    "region": None,
                    "timestamp": now,
                    "chunk_index": i,
                    "chunk_total": total,
                }
                channel.deliver(payload)
            self._logger.info(
                f"Delivered {total} mesh chunk(s) to rule {rule.name!r}"
            )
        else:
            # Single full-form delivery
            payload = {
                "category": "digest",
                "severity": "routine",
                "message": digest.full,
                "node_id": None,
                "region": None,
                "timestamp": now,
            }
            channel.deliver(payload)
            self._logger.info(
                f"Delivered digest to rule {rule.name!r} via {delivery_type}"
            )

    def _matching_rules(self) -> list:
        """Find enabled schedule rules tagged as digest deliveries."""
        matches = []
        for rule in self._config.notifications.rules:
            if not rule.enabled:
                continue
            if rule.trigger_type != "schedule":
                continue
            # schedule_match is the discriminator. Operators set it to
            # "digest" to receive the morning digest. Other values
            # reserved for future schedule types.
            schedule_match = getattr(rule, "schedule_match", None)
            if schedule_match != "digest":
                continue
            matches.append(rule)
        return matches

    def _next_fire_at(self, now: float) -> float:
        """Compute the next epoch timestamp when the digest should fire.

        Reads schedule HH:MM from config. If today's fire time has
        already passed, returns tomorrow's. Uses local timezone.
        """
        schedule_str = self._schedule_str()
        h, m = self._parse_schedule(schedule_str)
        now_dt = datetime.fromtimestamp(now)
        target_today = now_dt.replace(hour=h, minute=m, second=0, microsecond=0)
        if target_today.timestamp() <= now:
            target = target_today + timedelta(days=1)
        else:
            target = target_today
        return target.timestamp()

    def _schedule_str(self) -> str:
        digest_cfg = getattr(self._config.notifications, "digest", None)
        if digest_cfg is None:
            return "07:00"
        return getattr(digest_cfg, "schedule", "07:00")

    @staticmethod
    def _parse_schedule(s: str) -> tuple[int, int]:
        """Parse 'HH:MM' to (hour, minute). Falls back to 07:00 on bad input."""
        try:
            hh, mm = s.strip().split(":", 1)
            h = int(hh)
            m = int(mm)
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError(f"out of range: {s}")
            return h, m
        except (ValueError, AttributeError):
            # Fall back to 07:00 rather than crash the loop
            return 7, 0

    def last_fire_at(self) -> float:
        return self._last_fire_at
