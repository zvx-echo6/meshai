"""Radio outage monitor -- "all radios down" ops email alert.

Watches every configured mesh radio (Meshtastic, MeshCore, or both via
CompositeTransport) and, when EVERY one of them is down at once for at
least ``threshold_seconds``, emails a configured ops destination. Sends a
follow-up recovery email once a radio reconnects, if an alert was sent.

This is a direct ops notification -- it does NOT go through the
dispatcher's alert routing / Event categories. It reuses the existing
EmailChannel + ``notifications.destinations`` registry only for the
actual SMTP send.

Lifecycle mirrors FirePacer (see notifications/pipeline/pacer.py):
``start()``/``stop()`` around a single asyncio background task, wired up
in main.py next to the fire pacer.

State machine is persisted in ``mesh_health_events`` (event_type
"radio_outage_start" / "radio_outage_alert" / "radio_outage_end") so an
open outage survives a meshai restart -- the host watchdog restarts the
container roughly every 2 minutes while the Meshtastic port stays
unreachable, and losing the original start time on every restart would
mean the alert (gated on elapsed time since the outage began) might
never fire.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
from datetime import datetime, timezone
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# ============================================================================
# Per-radio down detection
# ============================================================================


def radio_states(connector) -> dict[str, bool]:
    """Return {radio_name: is_up} for every currently configured radio.

    - CompositeTransport (duck-typed via meshtastic_child()/meshcore_child()):
      one entry per child that is actually present.
    - A bare single transport: one entry, keyed by its transport_name
      (defaults to "meshtastic" if the attribute is absent).
    - connector is None: {} -- "no radios at all", never treated as an
      outage by callers (see all_down()).
    """
    if connector is None:
        return {}

    meshtastic_child = getattr(connector, "meshtastic_child", None)
    meshcore_child = getattr(connector, "meshcore_child", None)
    if callable(meshtastic_child) and callable(meshcore_child):
        states: dict[str, bool] = {}
        mt = meshtastic_child()
        if mt is not None:
            states["meshtastic"] = _radio_up(mt)
        mc = meshcore_child()
        if mc is not None:
            states["meshcore"] = _radio_up(mc)
        return states

    name = getattr(connector, "transport_name", None) or "meshtastic"
    return {name: _radio_up(connector)}


def _radio_up(transport) -> bool:
    """The most accurate "can this radio send right now" signal available.

    Meshtastic: MeshtasticTransport._connected is never cleared on link
    loss (see connector.py's _on_connection_lost -- it only flags
    link_suspect), so it is not trustworthy during an outage. The
    connection supervisor in main.py is the single source of truth for
    link state; connector.py's ``link_up`` property is kept in sync with
    it at the exact point the supervisor writes /tmp/meshai.link (see
    MeshtasticTransport.write_link_status()), so it correctly reflects
    "down" for the whole reconnect-in-progress window too.

    MeshCore: MeshCoreTransport.connected is driven directly by the
    lib's CONNECTED/DISCONNECTED events, so it is used as-is.
    """
    link_up = getattr(transport, "link_up", None)
    if link_up is not None:
        return bool(link_up)
    return bool(getattr(transport, "connected", False))


def all_down(states: dict[str, bool]) -> bool:
    """True only when there is at least one configured radio and every one
    of them is down. An empty dict (no radios configured) is never an
    outage."""
    return bool(states) and not any(states.values())


# ============================================================================
# Settings (adapter_config; hot -- re-read every check)
# ============================================================================


class _Settings:
    __slots__ = (
        "enabled",
        "threshold_seconds",
        "check_interval_seconds",
        "startup_grace_seconds",
        "destination",
        "email_retry_seconds",
    )

    def __init__(self, ns) -> None:
        self.enabled = bool(ns.enabled)
        self.threshold_seconds = int(ns.threshold_seconds)
        self.check_interval_seconds = int(ns.check_interval_seconds)
        self.startup_grace_seconds = int(ns.startup_grace_seconds)
        self.destination = ns.destination or ""
        self.email_retry_seconds = int(ns.email_retry_seconds)


def _load_settings() -> _Settings:
    from meshai.adapter_config import adapter_config
    return _Settings(adapter_config.radio_outage)


# ============================================================================
# Email delivery -- reuse EmailChannel + notifications.destinations
# ============================================================================


def _build_email_channel(config, destination_name: str):
    """Resolve a notifications.destinations entry by name and build an
    EmailChannel from it -- mirrors what Dispatcher._destination_to_rule()
    + create_channel() do for delivery_type=="email", without needing a
    full Dispatcher/Event. Returns None if the name is unknown or the
    destination isn't type=email."""
    notif = getattr(config, "notifications", None)
    registry = getattr(notif, "destinations", None)
    if not isinstance(registry, dict):
        return None
    dest = registry.get(destination_name)
    if dest is None:
        return None
    if getattr(dest, "type", "") != "email":
        logger.warning(
            "radio_outage: destination %r is not type=email (got %r)",
            destination_name, getattr(dest, "type", None),
        )
        return None
    from meshai.notifications.channels import EmailChannel
    return EmailChannel(
        smtp_host=getattr(dest, "smtp_host", ""),
        smtp_port=getattr(dest, "smtp_port", 587),
        smtp_user=getattr(dest, "smtp_user", ""),
        smtp_password=getattr(dest, "smtp_password", ""),
        smtp_tls=getattr(dest, "smtp_tls", True),
        from_address=getattr(dest, "from_address", ""),
        recipients=list(getattr(dest, "recipients", []) or []),
    )


# ============================================================================
# Message formatting
# ============================================================================


def _fmt_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fmt_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, _ = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def _radio_lines(states: dict[str, bool]) -> list[str]:
    return [f"{name}: {'up' if states[name] else 'down'}" for name in sorted(states)]


# ============================================================================
# Monitor
# ============================================================================


class RadioOutageMonitor:
    """Detects "every configured mesh radio is down" and emails ops.

    See module docstring for the full state machine.
    """

    def __init__(
        self,
        connector,
        config,
        conn_factory: Optional[Callable] = None,
        clock: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], "asyncio.Future"]] = None,
        hostname: Optional[str] = None,
    ):
        self._connector = connector
        self._config = config
        if conn_factory is None:
            from meshai.persistence import get_db
            conn_factory = get_db
        self._conn_factory = conn_factory
        self._clock = clock or time.time
        self._sleep = sleep or asyncio.sleep
        self._hostname = hostname or socket.gethostname()
        self._process_start = self._clock()
        self._task: Optional[asyncio.Task] = None

        # Open-outage bookkeeping; resumed from DB in start().
        self._outage_id: Optional[int] = None
        self._outage_start: Optional[float] = None
        self._alerted: bool = False
        self._last_alert_fail_at: Optional[float] = None
        self._warned_no_destination_for: Optional[int] = None

        # In-memory-only recovery-email retry state (mirrors FirePacer's
        # in-memory-only queue -- losing a pending recovery retry on a
        # restart is acceptable; losing the outage START time is not).
        self._pending_recovery: Optional[dict] = None

    # ---- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        """Resume any open outage from the DB, then spawn the check loop."""
        if self._task is not None:
            return
        self._load_open_outage()
        self._task = asyncio.create_task(self._loop())
        logger.info("radio outage monitor started")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        logger.info("radio outage monitor stopped")

    async def _loop(self) -> None:
        while True:
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("radio outage monitor: check failed")
            interval = _load_settings().check_interval_seconds
            try:
                await self._sleep(interval)
            except asyncio.CancelledError:
                return

    # ---- one check ---------------------------------------------------

    async def check_once(self) -> None:
        settings = _load_settings()
        if not settings.enabled:
            return

        states = radio_states(self._connector)
        if not states:
            return  # no radios configured at all -- nothing to monitor

        now = self._clock()

        if now - self._process_start < settings.startup_grace_seconds:
            # Absorb normal boot-time connect delay -- AND the connection
            # supervisor's own health-check latency. main.py writes
            # link_up=True unconditionally right after connect() (before
            # the supervisor has run even once), and MeshtasticTransport
            # defaults link_up=True on construction, so for the first
            # health_interval + probe_wait seconds after every process
            # start (restart included) a still-dead Meshtastic radio can
            # read as "up". Make NO state transition of any kind during
            # this window -- no start row, no end row, no alert, no
            # recovery attempt -- so a resumed OPEN outage is never closed
            # by that transient false "up" reading. See
            # radio_outage.startup_grace_seconds's description for the
            # required grace > health_interval + probe relationship.
            return

        if self._pending_recovery is not None:
            await self._retry_recovery(settings, now)

        if all_down(states):
            await self._handle_all_down(settings, states, now)
        else:
            await self._handle_any_up(settings, states, now)

    async def _handle_all_down(self, settings: _Settings, states: dict[str, bool], now: float) -> None:
        if self._outage_id is None:
            self._outage_id = self._write_start(states, now)
            self._outage_start = now
            self._alerted = False
            self._warned_no_destination_for = None
            logger.warning("radio_outage: all radios down (%s)", states)
            return

        if self._alerted:
            return
        if now - self._outage_start < settings.threshold_seconds:
            return
        await self._maybe_alert(settings, states, now)

    async def _handle_any_up(self, settings: _Settings, states: dict[str, bool], now: float) -> None:
        if self._outage_id is None:
            return  # nothing open

        duration = now - self._outage_start
        outage_id = self._outage_id
        was_alerted = self._alerted
        self._write_end(outage_id, states, now, duration)
        logger.info("radio_outage: outage ended (outage_id=%s, duration=%.0fs)", outage_id, duration)

        self._outage_id = None
        self._outage_start = None
        self._alerted = False
        self._last_alert_fail_at = None
        self._warned_no_destination_for = None

        if not was_alerted:
            return  # short outage that never alerted -> no recovery email

        dest_name = settings.destination.strip()
        if not dest_name:
            return

        self._pending_recovery = {
            "outage_id": outage_id,
            "states": dict(states),
            "duration": duration,
            "ended_at": now,
            "attempts": 0,
            "last_fail_at": None,
        }
        await self._retry_recovery(settings, now)

    async def _maybe_alert(self, settings: _Settings, states: dict[str, bool], now: float) -> None:
        dest_name = settings.destination.strip()
        if not dest_name:
            if self._warned_no_destination_for != self._outage_id:
                logger.warning(
                    "radio_outage: no destination configured (radio_outage.destination); "
                    "alert email not sent (outage_id=%s)", self._outage_id,
                )
                self._warned_no_destination_for = self._outage_id
            return

        if (self._last_alert_fail_at is not None
                and (now - self._last_alert_fail_at) < settings.email_retry_seconds):
            return

        duration = now - self._outage_start
        subject, body = self._alert_message(states, duration)
        ok = await self._send(dest_name, subject, body)
        if ok:
            self._write_alert(states, now, duration)
            self._alerted = True
            self._last_alert_fail_at = None
            logger.info("radio_outage: alert email sent (outage_id=%s)", self._outage_id)
        else:
            self._last_alert_fail_at = now
            logger.warning(
                "radio_outage: alert email failed; will retry in >=%ss (outage_id=%s)",
                settings.email_retry_seconds, self._outage_id,
            )

    async def _retry_recovery(self, settings: _Settings, now: float) -> None:
        pending = self._pending_recovery
        if pending is None:
            return

        if (pending["last_fail_at"] is not None
                and (now - pending["last_fail_at"]) < settings.email_retry_seconds):
            return

        dest_name = settings.destination.strip()
        if not dest_name:
            self._pending_recovery = None
            return

        subject, body = self._recovery_message(
            pending["states"], pending["ended_at"], pending["duration"]
        )
        ok = await self._send(dest_name, subject, body)
        if ok:
            logger.info("radio_outage: recovery email sent (outage_id=%s)", pending["outage_id"])
            self._pending_recovery = None
        else:
            pending["attempts"] += 1
            pending["last_fail_at"] = now
            if pending["attempts"] >= 3:
                logger.warning(
                    "radio_outage: recovery email failed 3 times; giving up (outage_id=%s)",
                    pending["outage_id"],
                )
                self._pending_recovery = None
            else:
                logger.warning(
                    "radio_outage: recovery email failed (attempt %d); will retry (outage_id=%s)",
                    pending["attempts"], pending["outage_id"],
                )

    # ---- email send --------------------------------------------------

    async def _send(self, dest_name: str, subject: str, body: str) -> bool:
        channel = _build_email_channel(self._config, dest_name)
        if channel is None:
            logger.warning("radio_outage: destination %r not found/invalid; email not sent", dest_name)
            return False
        try:
            await asyncio.to_thread(channel._send_email, subject, body)
            return True
        except Exception:
            logger.exception("radio_outage: email send failed (destination=%r)", dest_name)
            return False

    # ---- message formatting -------------------------------------------

    def _fmt_local(self, ts: float) -> Optional[str]:
        tz_name = getattr(self._config, "timezone", None)
        if not tz_name:
            return None
        try:
            from zoneinfo import ZoneInfo
            dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(ZoneInfo(tz_name))
            return dt.strftime("%Y-%m-%d %H:%M:%S %Z")
        except Exception:
            return None

    def _alert_message(self, states: dict[str, bool], duration: float) -> tuple[str, str]:
        minutes = int(duration // 60)
        subject = f"[meshai] All radios down for {minutes} min"
        start_utc = _fmt_utc(self._outage_start)
        local = self._fmt_local(self._outage_start)
        if local:
            lead = f"All mesh radios have been disconnected since {start_utc} ({local})."
        else:
            lead = f"All mesh radios have been disconnected since {start_utc}."
        lines = [lead, *_radio_lines(states),
                 "Alerts cannot reach the mesh until a radio reconnects.",
                 f"Host: {self._hostname}"]
        return subject, "\n".join(lines)

    def _recovery_message(self, states: dict[str, bool], ended_at: float, duration: float) -> tuple[str, str]:
        dur_str = _fmt_duration(duration)
        subject = f"[meshai] Radios back after {dur_str}"
        now_utc = _fmt_utc(ended_at)
        local = self._fmt_local(ended_at)
        if local:
            lead = f"Radio connectivity restored at {now_utc} ({local}). Outage lasted {dur_str}."
        else:
            lead = f"Radio connectivity restored at {now_utc}. Outage lasted {dur_str}."
        lines = [lead, *_radio_lines(states), f"Host: {self._hostname}"]
        return subject, "\n".join(lines)

    # ---- persistence (mesh_health_events) ------------------------------

    def _load_open_outage(self) -> None:
        """Resume the latest open outage (a radio_outage_start row with no
        matching radio_outage_end), if any."""
        conn = self._conn_factory()
        rows = conn.execute(
            "SELECT id, event_type, detected_at, detail_json FROM mesh_health_events "
            "WHERE event_type IN ('radio_outage_start', 'radio_outage_alert', 'radio_outage_end') "
            "ORDER BY id ASC"
        ).fetchall()

        starts: dict[int, float] = {}
        alerted_ids: set = set()
        ended_ids: set = set()
        for r in rows:
            try:
                detail = json.loads(r["detail_json"] or "{}")
            except Exception:
                detail = {}
            if r["event_type"] == "radio_outage_start":
                starts[r["id"]] = r["detected_at"]
            elif r["event_type"] == "radio_outage_alert":
                oid = detail.get("outage_id")
                if oid is not None:
                    alerted_ids.add(oid)
            elif r["event_type"] == "radio_outage_end":
                oid = detail.get("outage_id")
                if oid is not None:
                    ended_ids.add(oid)

        if not starts:
            return
        latest_id = max(starts)
        if latest_id in ended_ids:
            return  # latest outage already closed; nothing open

        self._outage_id = latest_id
        self._outage_start = starts[latest_id]
        self._alerted = latest_id in alerted_ids
        logger.info(
            "radio_outage: resumed open outage (outage_id=%s, alerted=%s)",
            latest_id, self._alerted,
        )

    def _write_start(self, states: dict[str, bool], now: float) -> int:
        conn = self._conn_factory()
        detail = {"radios": dict(states)}
        cur = conn.execute(
            "INSERT INTO mesh_health_events(event_type, node_id, detected_at, severity, detail_json) "
            "VALUES ('radio_outage_start', NULL, ?, 'warning', ?)",
            (int(now), json.dumps(detail)),
        )
        outage_id = cur.lastrowid
        detail["outage_id"] = outage_id
        conn.execute(
            "UPDATE mesh_health_events SET detail_json = ? WHERE id = ?",
            (json.dumps(detail), outage_id),
        )
        return outage_id

    def _write_alert(self, states: dict[str, bool], now: float, duration: float) -> None:
        conn = self._conn_factory()
        detail = {"outage_id": self._outage_id, "radios": dict(states), "duration_seconds": duration}
        conn.execute(
            "INSERT INTO mesh_health_events(event_type, node_id, detected_at, severity, detail_json) "
            "VALUES ('radio_outage_alert', NULL, ?, 'critical', ?)",
            (int(now), json.dumps(detail)),
        )

    def _write_end(self, outage_id: int, states: dict[str, bool], now: float, duration: float) -> None:
        conn = self._conn_factory()
        detail = {"outage_id": outage_id, "radios": dict(states), "duration_seconds": duration}
        conn.execute(
            "INSERT INTO mesh_health_events(event_type, node_id, detected_at, severity, detail_json) "
            "VALUES ('radio_outage_end', NULL, ?, 'info', ?)",
            (int(now), json.dumps(detail)),
        )
