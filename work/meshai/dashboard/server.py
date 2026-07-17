"""FastAPI server for MeshAI dashboard."""
from meshai.dashboard.api.adapter_config_routes import router as adapter_config_router
from meshai.dashboard.api.curation_routes import router as curation_router
from meshai.dashboard.api.secrets_routes import router as secrets_router

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .ws import DashboardBroadcaster, router as ws_router

if TYPE_CHECKING:
    from ..main import MeshAI

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan context manager."""
    logger.info("Dashboard starting up")
    yield
    logger.info("Dashboard shutting down")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="MeshAI Dashboard",
        description="Web dashboard for MeshAI mesh network monitoring",
        version="0.1.0",
        lifespan=lifespan,
    )

    # CORS middleware for Vite dev server
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://localhost:8080"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Import and include API routers
    from .api.system_routes import router as system_router
    from .api.config_routes import router as config_router
    from .api.generic_sources_routes import router as generic_sources_router
    from .api.mesh_routes import router as mesh_router
    from .api.mesh_send_routes import router as mesh_send_router
    from .api.env_routes import router as env_router
    from .api.alert_routes import router as alert_router
    from .api.notification_routes import router as notification_router
    from .api.debug_routes import router as debug_router
    from .api.serial_ports_routes import router as serial_ports_router
    from .api.gauge_sites_import import router as gauge_sites_import_router

    app.include_router(system_router, prefix="/api")
    app.include_router(serial_ports_router, prefix="/api")
    app.include_router(adapter_config_router, prefix="/api")
    app.include_router(curation_router, prefix="/api")
    app.include_router(gauge_sites_import_router, prefix="/api")
    app.include_router(config_router, prefix="/api")
    app.include_router(generic_sources_router, prefix="/api")
    app.include_router(mesh_router, prefix="/api")
    app.include_router(mesh_send_router, prefix="/api")
    app.include_router(env_router, prefix="/api")
    app.include_router(alert_router, prefix="/api")

    app.include_router(notification_router, prefix="/api")
    app.include_router(secrets_router, prefix="/api")
    app.include_router(debug_router, prefix="/api")
    # WebSocket router (no prefix, path is /ws/live)
    app.include_router(ws_router)

    # Auto-refresh the live ToggleFilter after any successful notifications/
    # config PUT so enabling a family toggle takes effect WITHOUT a manual
    # POST /api/notifications/refresh-toggles. The middleware is defined in
    # config_routes.register_config_routes_hooks(); it was previously only wired
    # up in tests, so in prod a saved toggle read "enabled" in config while the
    # running filter's enabled-set was stale until the manual poke.
    from .api.config_routes import register_config_routes_hooks
    register_config_routes_hooks(app)

    # Static files setup for SPA
    static_dir = Path(__file__).parent / "static"
    index_html = static_dir / "index.html"

    if static_dir.exists() and index_html.exists():
        # Mount /assets for JS, CSS, images
        assets_dir = static_dir / "assets"
        if assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

        # SPA catch-all: serve index.html for any non-API, non-static path
        # This enables React Router client-side routing
        @app.get("/{path:path}")
        async def spa_catch_all(request: Request, path: str):
            # Let static files be served directly if they exist
            file_path = static_dir / path
            if file_path.is_file():
                return FileResponse(file_path)
            # Otherwise serve index.html for client-side routing
            return FileResponse(index_html)

        # Explicit root route
        @app.get("/")
        async def root():
            return FileResponse(index_html)
    else:
        # meshai/dashboard/static/ is a build artifact, not committed to the
        # repo (see .gitignore) -- it only exists if the frontend has been
        # built. Without it there's no route for "/", so requests would
        # otherwise 404 with no explanation. Log loudly so a fresh
        # `pip install -e .` user knows the bot/API are fine and just the
        # dashboard UI needs building.
        logger.warning(
            "Dashboard UI not found at %s -- the web dashboard will NOT be served "
            "(the bot and API are unaffected). Build it with: "
            "cd dashboard-frontend && npm ci && npm run build "
            "(then restart meshai). Docker images build this automatically.",
            index_html,
        )

    return app


async def start_dashboard(meshai_instance: "MeshAI") -> DashboardBroadcaster:
    """Start the dashboard server in the MeshAI asyncio loop.

    Args:
        meshai_instance: The running MeshAI instance

    Returns:
        DashboardBroadcaster instance for pushing updates
    """
    app = create_app()

    # Populate app.state with MeshAI internals
    app.state.config = meshai_instance.config
    app.state.config_path = meshai_instance.config._config_path
    app.state.data_store = meshai_instance.data_store
    app.state.health_engine = meshai_instance.health_engine
    app.state.mesh_reporter = getattr(meshai_instance, "mesh_reporter", None)
    app.state.alert_engine = getattr(meshai_instance, "alert_engine", None)
    app.state.env_store = getattr(meshai_instance, "env_store", None)
    app.state.notification_router = getattr(meshai_instance, "notification_router", None)
    app.state.connector = meshai_instance.connector
    app.state.bus = getattr(meshai_instance, "event_bus", None)
    app.state.danger_correlator = getattr(meshai_instance, "danger_correlator", None)
    app.state.mesh_context = getattr(meshai_instance, "context", None)

    # Create broadcaster and attach to app state
    broadcaster = DashboardBroadcaster()
    app.state.broadcaster = broadcaster

    # Configure uvicorn
    config = uvicorn.Config(
        app,
        host=meshai_instance.config.dashboard.host,
        port=meshai_instance.config.dashboard.port,
        log_level="warning",  # Don't spam meshai logs with access logs
    )
    server = uvicorn.Server(config)

    # Start server as asyncio task (runs in same event loop as MeshAI)
    asyncio.create_task(server.serve())

    return broadcaster
