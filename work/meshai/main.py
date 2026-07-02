"""Main entry point for MeshAI."""

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional

from . import __version__
from .backends import AnthropicBackend, GoogleBackend, LLMBackend, OpenAIBackend
from .cli import run_configurator
from .commands import CommandDispatcher
from .commands.dispatcher import create_dispatcher
from .commands.status import set_start_time
from .config import Config
from .config_loader import load_config, get_config_dir_from_path
from .connector import MeshConnector, MeshMessage
from .transport.factory import build_transport
from .central_normalizer import init_geocoder_config
from .context import MeshContext
from .history import ConversationHistory
from .memory import ConversationSummary
from .responder import Responder
from .router import MessageRouter, RouteType

logger = logging.getLogger(__name__)


class MeshAI:
    """Main application class."""

    def __init__(self, config: Config):
        self.config = config
        self.connector: Optional[MeshConnector] = None
        self.history: Optional[ConversationHistory] = None
        self.dispatcher: Optional[CommandDispatcher] = None
        self.llm: Optional[LLMBackend] = None
        self.context: Optional[MeshContext] = None
        self.meshmonitor_sync = None
        self.knowledge = None
        self.data_store = None  # Replaces source_manager
        self.health_engine = None
        self.mesh_reporter = None
        self.subscription_manager = None
        self.alert_engine = None
        self.notification_router = None
        self.event_bus = None  # Notification pipeline EventBus (v0.3)
        self._pipeline_scheduler = None  # DigestScheduler from start_pipeline()
        self.env_store = None  # Environmental feeds store
        self._central_consumer = None  # Central NATS consumer (v0.4)
        self._fire_pacer = None  # FirePacer for rate-limited fire broadcasts
        self._last_sub_check: float = 0.0
        self.router: Optional[MessageRouter] = None
        self.responder: Optional[Responder] = None
        self._running = False
        self._supervisor_task = None  # watchdog (connection supervisor)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._last_cleanup: float = 0.0
        self._last_health_compute: float = 0.0
        self.broadcaster = None  # Dashboard WebSocket broadcaster

    async def start(self) -> None:
        """Start the bot."""
        logger.info(f"Starting MeshAI v{__version__}")
        set_start_time(time.time())

        # Initialize components
        await self._init_components()

        # Connect to Meshtastic
        self.connector.connect()
        self.connector.set_message_callback(self._on_message, asyncio.get_event_loop())

        # Add own node ID to context ignore list
        if self.context and self.connector.my_node_id:
            self.context._ignore_nodes.add(self.connector.my_node_id)

        self._running = True
        self._loop = asyncio.get_event_loop()
        # --- connection supervisor (watchdog): single source of truth for link
        # state + the only reconnect driver. Reconnects IN-PLACE so the container
        # never needs to restart.
        # Guard: watchdog is Meshtastic-specific — MeshCoreTransport manages its
        # own reconnect via the meshcore lib's auto_reconnect parameter.
        # When connector is a CompositeTransport, resolve the Meshtastic child
        # (if any) and run the watchdog on it; skip if absent (pure MeshCore). ---
        _mt_child = (
            self.connector
            if isinstance(self.connector, MeshConnector)
            else (
                self.connector.meshtastic_child()
                if hasattr(self.connector, "meshtastic_child")
                else None
            )
        )
        if getattr(self.config.connection, "reconnect", True) and _mt_child is not None:
            _mt_child._wake = asyncio.Event()
            _mt_child.write_link_status("up")  # we just connected ok
            self._supervisor_task = asyncio.create_task(
                self._connection_supervisor(_mt_child)
            )
            logger.info("Connection supervisor (watchdog) started")
        self._last_cleanup = time.time()
        self._last_health_compute = 0.0

        # Write PID file
        self._write_pid()

        # Start the notification pipeline's async components (digest scheduler).
        # build_pipeline() ran in _init_components(); this starts its scheduler
        # now that we are inside the running event loop.
        if self.event_bus is not None:
            from .notifications.pipeline import start_pipeline
            # v0.7-fire-tracker-4 llm_backend hook: surface the LLM into
            # the pipeline components dict BEFORE start_pipeline spawns the
            # scheduled broadcasters. FireDigestScheduler reads this.
            try:
                comps = getattr(self.event_bus, "_pipeline_components", {}) or {}
                comps["llm_backend"] = self.llm
                self.event_bus._pipeline_components = comps
            except Exception:
                logger.exception("could not seed llm_backend into pipeline components")
            self._pipeline_scheduler = await start_pipeline(self.event_bus, self.config)
            logger.info("Notification pipeline started")

            # Fire pacer: rate-limits fire broadcasts to <=1/min during both
            # drain catch-up and normal operation.
            from .notifications.pipeline.pacer import FirePacer
            self._fire_pacer = FirePacer(bus=self.event_bus, interval_seconds=60.0)
            await self._fire_pacer.start()

            from .central.consumer import CentralConsumer
            self._central_consumer = CentralConsumer(self.config.environmental, self.event_bus)
            self._central_consumer._pacer = self._fire_pacer
            await self._central_consumer.start()

        logger.info("MeshAI started successfully")

        # Keep running
        while self._running:
            await asyncio.sleep(1)

            # Periodic MeshMonitor refresh
            if self.meshmonitor_sync:
                self.meshmonitor_sync.maybe_refresh()

            # Periodic data store refresh and health computation
            if self.data_store:
                refreshed = self.data_store.refresh()
                # Recompute health after refresh
                if refreshed and self.health_engine:
                    self.health_engine.compute(self.data_store)
                    self._last_health_compute = time.time()

                    # Broadcast health update to dashboard
                    if self.broadcaster and self.health_engine.mesh_health:
                        try:
                            mh = self.health_engine.mesh_health
                            sc = mh.score
                            if sc.composite > 0 and sc.infra_total > 0:
                                health_dict = {
                                    "score": round(sc.composite, 1),
                                    "tier": sc.tier,
                                    "pillars": {
                                        "infrastructure": round(sc.infrastructure, 1),
                                        "utilization": round(sc.utilization, 1),
                                        "coverage": round(sc.coverage, 1),
                                        "behavior": round(sc.behavior, 1),
                                        "power": round(sc.power, 1),
                                    },
                                    "infra_online": sc.infra_online,
                                    "infra_total": sc.infra_total,
                                    "util_percent": round(sc.util_percent, 1),
                                    "flagged_nodes": sc.flagged_nodes,
                                    "battery_warnings": sc.battery_warnings,
                                    "total_nodes": mh.total_nodes,
                                    "total_regions": mh.total_regions,
                                    "unlocated_count": getattr(mh, "unlocated_count", 0),
                                    "last_computed": mh.last_computed,
                                    "recommendations": getattr(mh, "recommendations", []),
                                }
                                await self.broadcaster.broadcast("health_update", health_dict)
                        except Exception as e:
                            logger.debug("Dashboard broadcast error: %s", e)

                    # Check for alertable conditions
                    if self.alert_engine:
                        alerts = self.alert_engine.check()
                        if alerts:
                            await self._dispatch_alerts(alerts)

                        # Broadcast alerts to dashboard
                        if self.broadcaster:
                            for alert in alerts:
                                try:
                                    await self.broadcaster.broadcast("alert_fired", alert)
                                except Exception:
                                    pass

            # Environmental feed refresh
            if self.env_store:
                try:
                    env_changed = self.env_store.refresh()
                    if env_changed and self.alert_engine:
                        env_alerts = self.alert_engine.check_environmental(self.env_store)
                        if env_alerts:
                            await self._dispatch_alerts(env_alerts)
                            if self.broadcaster:
                                for ea in env_alerts:
                                    await self.broadcaster.broadcast("alert_fired", ea)

                    # Broadcast env updates to dashboard
                    if env_changed and self.broadcaster:
                        await self.broadcaster.broadcast("env_update", {
                            "active_count": len(self.env_store.get_active()),
                            "swpc": self.env_store.get_swpc_status(),
                            "ducting": self.env_store.get_ducting_status(),
                        })
                except Exception as e:
                    logger.debug("Env refresh error: %s", e)

            # Check scheduled subscriptions (every 60 seconds)
            if self.subscription_manager and self.mesh_reporter:
                if time.time() - self._last_sub_check >= 60:
                    await self._check_scheduled_subs()
                    self._last_sub_check = time.time()

            # Periodic cleanup
            if time.time() - self._last_cleanup >= 3600:
                await self.history.cleanup_expired()
                if self.context:
                    self.context.prune()
                self._last_cleanup = time.time()

    async def _connection_supervisor(self, c=None) -> None:
        """Watchdog: SINGLE source of truth for /tmp/meshai.link and the ONLY
        reconnect driver. Woken by connector._wake (connection.lost) or a
        health-interval timeout. Probe is socket-based (see connector.active_probe).

        *c* is the resolved MeshtasticTransport to watch — either self.connector
        directly (single-transport) or the Meshtastic child extracted from a
        CompositeTransport.  The watchdog logic itself is unchanged; only how
        we resolve *c* has moved to the caller (start()).
        """
        if c is None:
            c = self.connector
        hi = getattr(self.config.connection, "reconnect_health_interval", 30.0)
        alive_idle = 2.0 * hi
        probe_wait = 5.0
        wake = c._wake
        logger.info("watchdog loop running (health_interval=%.0fs)", hi)
        while self._running:
            try:
                try:
                    await asyncio.wait_for(wake.wait(), timeout=hi)
                except asyncio.TimeoutError:
                    pass
                wake.clear()
                if not self._running:
                    break

                idle = time.monotonic() - c.last_rx
                alive = (not c.link_suspect) and (idle < alive_idle) and c.interface_open()
                if alive:
                    c.clear_suspect()
                    c.write_link_status("up")
                    continue

                # Not trivially alive -> ACTIVE PROBE (blocking -> thread)
                logger.info("watchdog: link check (suspect=%s, idle=%.1fs) -> probing",
                            c.link_suspect, idle)
                probe_alive = await asyncio.to_thread(c.active_probe, probe_wait)
                if probe_alive:
                    c.clear_suspect()
                    c.write_link_status("up")
                    continue

                # DEAD -> write down, reconnect in place (blocking -> thread)
                logger.warning("watchdog: link DOWN -> reconnecting")
                c.write_link_status("down")
                await asyncio.to_thread(c.reconnect)
                c.write_link_status("up")
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("watchdog cycle error (continuing)")
                await asyncio.sleep(1.0)
        logger.info("watchdog loop exited")

    async def stop(self) -> None:
        """Stop the bot."""
        logger.info("Stopping MeshAI...")
        self._running = False

        if self._pipeline_scheduler is not None:
            from .notifications.pipeline import stop_pipeline
            await stop_pipeline(self._pipeline_scheduler)

        if self._central_consumer is not None:
            await self._central_consumer.stop()

        if self._fire_pacer is not None:
            await self._fire_pacer.stop()

        if self._supervisor_task is not None:
            self._supervisor_task.cancel()
            try:
                await self._supervisor_task
            except (asyncio.CancelledError, Exception):
                pass

        if self.connector:
            self.connector.disconnect()

        if self.history:
            await self.history.close()

        if self.llm:
            await self.llm.close()
        if self.knowledge:
            self.knowledge.close()
        if self.data_store:
            await self.data_store.stop_mqtt_sources()
            self.data_store.close()
        if self.subscription_manager:
            self.subscription_manager.close()

        self._remove_pid()
        logger.info("MeshAI stopped")

    async def _init_components(self) -> None:
        """Initialize all components."""
        # v0.6-3a: persistence init runs FIRST so any subsequent handler
        # or dispatcher that calls get_db() finds the v6 schema applied
        # and adapter_config seeded. The seed (INSERT OR IGNORE) is
        # idempotent on every restart.
        try:
            from meshai.persistence import init_db
            init_db()
        except Exception:
            logger.exception("persistence init_db failed at startup")

        # v0.6-3b: Initialize geocoder config from config.yaml
        try:
            gc = self.config.environmental.geocoder
            init_geocoder_config(
                url=gc.url,
                timeout=gc.timeout_seconds,
                radius=gc.radius_km,
                limit=gc.limit
            )
            logger.info("Geocoder configured: %s", gc.url)
        except Exception:
            logger.exception("geocoder init failed - using defaults")

        # Conversation history
        self.history = ConversationHistory(self.config.history)
        await self.history.initialize()

        # LLM backend
        api_key = self.config.resolve_api_key()
        if not api_key:
            logger.warning("No API key configured - LLM responses will fail")

        # Memory config
        mem_cfg = self.config.memory
        window_size = mem_cfg.window_size if mem_cfg.enabled else 0
        summarize_threshold = mem_cfg.summarize_threshold

        # Create backend
        backend = self.config.llm.backend.lower()
        if backend == "openai":
            self.llm = OpenAIBackend(
                self.config.llm, api_key, window_size, summarize_threshold
            )
        elif backend == "anthropic":
            self.llm = AnthropicBackend(
                self.config.llm, api_key, window_size, summarize_threshold
            )
        elif backend == "google":
            self.llm = GoogleBackend(
                self.config.llm, api_key, window_size, summarize_threshold
            )
        else:
            logger.warning(f"Unknown backend '{backend}', defaulting to OpenAI")
            self.llm = OpenAIBackend(
                self.config.llm, api_key, window_size, summarize_threshold
            )

        # Load persisted summaries into memory cache
        await self._load_summaries()

        # Transport connector (factory selects backend from config.connection.transport)
        self.connector = build_transport(self.config.connection)

        # Fit the NWS one-packet formatter to the active mesh transport's budget
        # (Meshtastic default 200 -> unchanged). Durable across adapter_config cache
        # invalidation via the runtime-override store.
        from meshai.adapter_config import set_runtime_override
        set_runtime_override("nws", "single_packet_max_chars", self.connector.max_chars)

        # Passive mesh context buffer
        ctx_cfg = self.config.context
        if ctx_cfg.enabled:
            self.context = MeshContext(
                observe_channels=ctx_cfg.observe_channels or None,
                ignore_nodes=ctx_cfg.ignore_nodes or None,
                max_age=ctx_cfg.max_age,
            )
            logger.info("Mesh context buffer enabled")
        else:
            self.context = None

        # MeshMonitor trigger sync
        mm_cfg = self.config.meshmonitor
        if mm_cfg.enabled and mm_cfg.url:
            from .meshmonitor import MeshMonitorSync
            self.meshmonitor_sync = MeshMonitorSync(
                url=mm_cfg.url,
                refresh_interval=mm_cfg.refresh_interval,
            )
            count = self.meshmonitor_sync.load()
            logger.info(f"MeshMonitor sync enabled, loaded {count} triggers")
        else:
            self.meshmonitor_sync = None

        # Mesh data store (replaces MeshSourceManager)
        # mesh_sources may be dicts or MeshSourceConfig objects depending on config version
        enabled_sources = [
            s for s in self.config.mesh_sources
            if (s.enabled if hasattr(s, 'enabled') else s.get('enabled', True))
        ]
        if enabled_sources:
            from .mesh_data_store import MeshDataStore
            self.data_store = MeshDataStore(
                source_configs=enabled_sources,
                db_path="/data/mesh_history.db",
                offline_threshold_hours=self.config.mesh_intelligence.offline_threshold_hours,
            )
            # Initial fetch and backfill
            self.data_store.force_refresh()
            # Start MQTT source subscription loops
            await self.data_store.start_mqtt_sources()
            # Log status
            for status in self.data_store.get_status():
                if status["is_loaded"]:
                    logger.info(
                        f"Mesh source '{status['name']}' ({status['type']}): "
                        f"{status['node_count']} nodes"
                    )
                else:
                    logger.warning(
                        f"Mesh source '{status['name']}' ({status['type']}): "
                        f"failed - {status.get('last_error', 'unknown error')}"
                    )
        else:
            self.data_store = None

        # Mesh health engine
        mi_cfg = self.config.mesh_intelligence
        if mi_cfg.enabled and self.data_store:
            from .mesh_health import MeshHealthEngine
            self.health_engine = MeshHealthEngine(
                regions=mi_cfg.regions,
                locality_radius=mi_cfg.locality_radius_miles,
                offline_threshold_hours=mi_cfg.offline_threshold_hours,
                packet_threshold=mi_cfg.packet_threshold,
                battery_warning_percent=mi_cfg.battery_warning_percent,
            )
            # Initial health computation
            mesh_health = self.health_engine.compute(self.data_store)
            self._last_health_compute = time.time()
            logger.info(
                f"Mesh intelligence enabled: {mesh_health.total_nodes} nodes, "
                f"{mesh_health.total_regions} regions, "
                f"score {mesh_health.score.composite:.0f}/100 ({mesh_health.score.tier})"
            )
        else:
            self.health_engine = None

        # Mesh reporter (for LLM prompt injection and commands)
        if self.health_engine and self.data_store:
            from .mesh_reporter import MeshReporter
            mi_regions = self.config.mesh_intelligence.regions if self.config.mesh_intelligence else []
            self.mesh_reporter = MeshReporter(self.health_engine, self.data_store, region_configs=mi_regions)
            logger.info("Mesh reporter enabled")
        else:
            self.mesh_reporter = None

        # Subscription manager (uses same db as data_store)
        if self.data_store:
            from .subscriptions import SubscriptionManager
            self.subscription_manager = SubscriptionManager(db_path="/data/mesh_history.db")
            logger.info("Subscription manager enabled")
        else:
            self.subscription_manager = None

        # Alert engine (needs health engine, reporter, and subscription manager)
        if self.health_engine and self.mesh_reporter and self.subscription_manager:
            from .alert_engine import AlertEngine
            mi = self.config.mesh_intelligence
            self.alert_engine = AlertEngine(
                health_engine=self.health_engine,
                reporter=self.mesh_reporter,
                subscription_manager=self.subscription_manager,
                config=mi,
                db_path="/data/mesh_history.db",
                timezone=self.config.timezone,
            )
            logger.info(f"Alert engine initialized (critical: {mi.critical_nodes}, channel: {mi.alert_channel})")


        # Notification router
        if self.config.notifications.enabled:
            from .notifications.router import NotificationRouter
            self.notification_router = NotificationRouter(
                config=self.config.notifications,
                connector=self.connector,
                llm_backend=self.llm,
                timezone=self.config.timezone,
            )
            logger.info("Notification router initialized")

            # Notification pipeline (v0.3 EventBus). Built here so env
            # adapters constructed below can emit Events into the live
            # pipeline at runtime via EnvironmentalStore(event_bus=...).
            from .notifications.pipeline import build_pipeline
            self.event_bus = build_pipeline(self.config, self.llm, self.connector)
            # v0.6-6: expose bus to dashboard API for live refresh hooks.
            try:
                from meshai.dashboard.server import app as _dash_app
                _dash_app.state.bus = self.event_bus
                _dash_app.state.config = self.config
            except Exception:
                logger.debug('dashboard app.state stash skipped')
            logger.info("Notification pipeline EventBus initialized")

            # v0.8 danger_zones correlator: subscribe to the EventBus to flag
            # infra nodes inside a hazard's threat radius. Guarded — needs both
            # the bus and a data_store. Reads config.danger_zones fresh per call.
            self.danger_correlator = None
            if self.event_bus is not None and self.data_store is not None:
                from .notifications.danger_zone_correlator import DangerZoneCorrelator
                self.danger_correlator = DangerZoneCorrelator(
                    self.config, self.data_store, self.connector)
                self.event_bus.subscribe(self.danger_correlator.handle)
                logger.info("Danger-zone correlator subscribed to EventBus")

        # Environmental feeds
        env_cfg = self.config.environmental
        if env_cfg.enabled:
            from .env.store import EnvironmentalStore
            # Pass region anchors for fire proximity calculation
            region_anchors = self.config.mesh_intelligence.regions if self.config.mesh_intelligence.enabled else []
            self.env_store = EnvironmentalStore(
                config=env_cfg, region_anchors=region_anchors, event_bus=self.event_bus
            )
            logger.info(f"Environmental feeds enabled ({len(self.env_store._adapters)} adapters)")
        else:
            self.env_store = None

        # Knowledge base (optional - Qdrant with SQLite fallback)
        kb_cfg = self.config.knowledge
        self.knowledge = None
        if kb_cfg.enabled:
            # Try Qdrant first if configured
            if kb_cfg.backend in ("qdrant", "auto") and kb_cfg.qdrant_host:
                try:
                    from .knowledge import QdrantKnowledgeSearch
                    qdrant = QdrantKnowledgeSearch(
                        qdrant_host=kb_cfg.qdrant_host,
                        qdrant_port=kb_cfg.qdrant_port,
                        collection=kb_cfg.qdrant_collection,
                        tei_host=kb_cfg.tei_host,
                        tei_port=kb_cfg.tei_port,
                        sparse_host=kb_cfg.sparse_host,
                        sparse_port=kb_cfg.sparse_port,
                        use_sparse=kb_cfg.use_sparse,
                        top_k=kb_cfg.top_k,
                    )
                    if qdrant.available:
                        self.knowledge = qdrant
                        logger.info("Using Qdrant knowledge backend (RECON hybrid)")
                except Exception as e:
                    logger.warning(f"Qdrant knowledge unavailable: {e}")

            # Fall back to SQLite if Qdrant failed or not configured
            if not self.knowledge and kb_cfg.backend in ("sqlite", "auto") and kb_cfg.db_path:
                try:
                    from .knowledge import KnowledgeSearch
                    self.knowledge = KnowledgeSearch(
                        db_path=kb_cfg.db_path,
                        top_k=kb_cfg.top_k,
                    )
                except ImportError as e:
                    logger.warning(f"SQLite knowledge disabled - missing dependencies: {e}")

        # Command dispatcher (needs mesh_reporter for health commands)
        self.dispatcher = create_dispatcher(
            prefix=self.config.commands.prefix,
            disabled_commands=self.config.commands.disabled_commands,
            custom_commands=self.config.commands.custom_commands,
            mesh_reporter=self.mesh_reporter,
            data_store=self.data_store,
            health_engine=self.health_engine,
            subscription_manager=self.subscription_manager,
            env_store=self.env_store,
            notification_router=self.notification_router,
        )

        # Message router
        self.router = MessageRouter(
            self.config, self.connector, self.history, self.dispatcher, self.llm,
            context=self.context,
            meshmonitor_sync=self.meshmonitor_sync,
            knowledge=self.knowledge,
            source_manager=self.data_store,
            health_engine=self.health_engine,
            mesh_reporter=self.mesh_reporter,
            env_store=self.env_store,
            # notification_router not used by MessageRouter
        )

        # Responder
        self.responder = Responder(self.config.response, self.connector)

        # Dashboard
        if hasattr(self.config, 'dashboard') and self.config.dashboard.enabled:
            try:
                from .dashboard.server import start_dashboard
                self.broadcaster = await start_dashboard(self)
                logger.info("Dashboard started on port %d", self.config.dashboard.port)
            except Exception as e:
                logger.warning("Dashboard failed to start: %s", e)
                self.broadcaster = None
        else:
            self.broadcaster = None

    async def _on_message(self, message: MeshMessage) -> None:
        """Handle incoming message."""
        try:
            # Passively observe channel broadcasts for context (before filtering)
            if self.context and not message.is_dm and message.text:
                self.context.observe(
                    sender_name=message.sender_name,
                    sender_id=message.sender_id,
                    text=message.text,
                    channel=message.channel,
                    is_dm=False,
                )

            # Check if we should respond
            if not self.router.should_respond(message):
                return

            logger.info(
                f"Processing message from {message.sender_name} ({message.sender_id}): "
                f"{message.text[:50]}..."
            )

            # Route the message
            # Capture the originating transport tag for reply routing.
            # This lets CompositeTransport route the DM reply back over the
            # same mesh the inbound message arrived on.  Single-transport
            # connectors accept and ignore this kwarg.
            originating_transport: Optional[str] = getattr(message, "transport", None)

            # Check for continuation request first
            continuation_messages = self.router.check_continuation(message)
            if continuation_messages:
                await self.responder.send_response(
                    continuation_messages,
                    destination=message.sender_id,
                    channel=message.channel,
                    transport=originating_transport,
                )
                return

            result = await self.router.route(message)

            if result.route_type == RouteType.IGNORE:
                return

            # Determine response
            if result.route_type == RouteType.COMMAND:
                if isinstance(result.response, list):
                    # Command returned pre-split messages — send directly
                    messages = result.response
                else:
                    # Single string — chunk it
                    from .chunker import chunk_response
                    messages, remaining = chunk_response(
                        result.response,
                        max_chars=min(self.config.response.max_length, self.connector.max_chars),
                        max_messages=self.config.response.max_messages,
                    )
                    if remaining:
                        self.router.continuations.store(message.sender_id, remaining)
            elif result.route_type == RouteType.LLM:
                messages = await self.router.generate_llm_response(message, result.query)
            else:
                return

            if not messages:
                return

            # Send DM response — thread the originating transport hint so
            # CompositeTransport routes the reply back over the correct mesh.
            await self.responder.send_response(
                messages,
                destination=message.sender_id,
                channel=message.channel,
                transport=originating_transport,
            )

        except Exception as e:
            logger.error(f"Error handling message: {e}", exc_info=True)

    async def _load_summaries(self) -> None:
        """Load persisted summaries from database into memory cache."""
        memory = self.llm.get_memory()
        if not memory:
            return

        if not self.history or not self.history._db:
            return

        try:
            async with self.history._lock:
                cursor = await self.history._db.execute(
                    "SELECT user_id, summary, message_count, updated_at "
                    "FROM conversation_summaries"
                )
                rows = await cursor.fetchall()

            loaded = 0
            for row in rows:
                user_id, summary_text, message_count, updated_at = row
                summary = ConversationSummary(
                    summary=summary_text,
                    last_updated=updated_at,
                    message_count=message_count,
                )
                memory.load_summary(user_id, summary)
                loaded += 1

            if loaded:
                logger.info(f"Loaded {loaded} conversation summaries from database")

        except Exception as e:
            logger.warning(f"Failed to load summaries from database: {e}")

    def _write_pid(self) -> None:
        """Write PID file."""
        pid_file = Path("/tmp/meshai.pid")
        pid_file.write_text(str(os.getpid()))

    def _remove_pid(self) -> None:
        """Remove PID file."""
        pid_file = Path("/tmp/meshai.pid")
        if pid_file.exists():
            pid_file.unlink()

    async def _dispatch_alerts(self, alerts: list[dict]) -> None:
        """Dispatch alerts to subscribers and alert channel."""
        mi = self.config.mesh_intelligence
        alert_channel = getattr(mi, 'alert_channel', -1)

        for alert in alerts:
            message = alert["message"]
            logger.info(f"ALERT: {message}")

            # Route through notification router if enabled
            if self.notification_router:
                try:
                    await self.notification_router.process_alert(alert)
                except Exception as e:
                    logger.error(f"Notification router error: {e}")

            # Fallback: Send to alert channel if no notification router
            elif alert_channel >= 0 and self.connector:
                try:
                    self.connector.send_message(
                        text=message,
                        destination=None,
                        channel=alert_channel,
                    )
                    logger.info(f"Alert sent to channel {alert_channel}")
                except Exception as e:
                    logger.error(f"Failed to send channel alert: {e}")

                # Fallback: Send DMs to matching subscribers
                if self.alert_engine and self.subscription_manager:
                    subscribers = self.alert_engine.get_subscribers_for_alert(alert)
                    for sub in subscribers:
                        user_id = sub["user_id"]
                        try:
                            await self._send_sub_dm(user_id, message)
                            logger.info(f"Alert DM sent to {user_id}: {alert['type']}")
                        except Exception as e:
                            logger.error(f"Failed to send alert DM to {user_id}: {e}")

        if self.alert_engine:
            self.alert_engine.clear_pending()

    async def _check_scheduled_subs(self) -> None:
        """Check for and deliver due scheduled reports."""
        from datetime import datetime
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(self.config.timezone)
        now = datetime.now(tz)
        current_hhmm = now.strftime("%H%M")
        current_day = now.strftime("%a").lower()

        due_subs = self.subscription_manager.get_due_subscriptions(current_hhmm, current_day)

        for sub in due_subs:
            try:
                # Generate report based on scope
                report = self._generate_sub_report(sub)
                if not report:
                    continue

                # Send DM to subscriber
                user_id = sub["user_id"]
                await self._send_sub_dm(user_id, report)

                # Mark as sent
                self.subscription_manager.mark_sent(sub["id"])
                logger.info(f"Delivered {sub['sub_type']} report to {user_id}")

            except Exception as e:
                logger.error(f"Error delivering subscription {sub['id']}: {e}")

    def _generate_sub_report(self, sub: dict) -> str:
        """Generate report content for a subscription."""
        if not self.mesh_reporter:
            return None

        sub_type = sub["sub_type"]
        scope_type = sub.get("scope_type", "mesh")
        scope_value = sub.get("scope_value")

        if scope_type == "region" and scope_value:
            # Region-scoped report
            region = self.mesh_reporter._find_region(scope_value)
            if region:
                return self.mesh_reporter.build_region_compact(region.name)
            return None
        elif scope_type == "node" and scope_value:
            # Node-scoped report
            return self.mesh_reporter.build_node_compact(scope_value)
        else:
            # Mesh-wide report
            return self.mesh_reporter.build_lora_compact(scope="mesh")

    async def _send_sub_dm(self, node_num: str, message: str) -> None:
        """Send a subscription DM to a node."""
        if not self.connector:
            return

        # Convert node_num to destination format
        try:
            dest = int(node_num)
        except ValueError:
            dest = node_num

        # Send via responder for proper chunking
        if self.responder:
            await self.responder.send_response(
                message,
                destination=dest,
                channel=0,  # DM channel
            )
        else:
            # Fallback to direct send
            self.connector.send_message(message, destination=dest)


def setup_logging(verbose: bool = False) -> None:
    """Configure logging."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="MeshAI - LLM-powered Meshtastic assistant",
        prog="meshai",
    )
    parser.add_argument(
        "--version", "-V", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--config", "-c", action="store_true", help="Launch configuration tool"
    )
    parser.add_argument(
        "--config-file",
        "-f",
        type=Path,
        default=Path("config.yaml"),
        help="Path to config file (default: config.yaml)",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")

    args = parser.parse_args()

    setup_logging(args.verbose)

    # Launch configurator if requested
    if args.config:
        run_configurator(args.config_file)
        return

    # Load config - support both old (/data/config.yaml) and new (/data/config/) layouts
    config_path = args.config_file
    config_dir = get_config_dir_from_path(config_path)

    # Check for new multi-file layout first
    if (config_dir / "config.yaml").exists():
        logger.info(f"Loading config from multi-file layout: {config_dir}")
        config = load_config(config_dir)
    elif config_path.exists():
        # Fall back to legacy single-file loading
        logger.info(f"Loading legacy config: {config_path}")
        from .config import load_config as legacy_load
        config = legacy_load(config_path)
    else:
        logger.warning(f"Config not found at {config_path} or {config_dir}")
        logger.info("Run 'meshai --config' to create one, or copy config.example.yaml")
        sys.exit(1)

    # Create and run bot
    bot = MeshAI(config)

    # Handle signals
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def signal_handler(sig, frame):
        logger.info(f"Received signal {sig}")
        loop.create_task(bot.stop())

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        loop.run_until_complete(bot.start())
    except KeyboardInterrupt:
        pass
    finally:
        loop.run_until_complete(bot.stop())
        loop.close()


if __name__ == "__main__":
    main()
