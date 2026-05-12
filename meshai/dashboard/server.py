"""FastAPI server for MeshAI dashboard."""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
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
    from .api.mesh_routes import router as mesh_router
    from .api.env_routes import router as env_router
    from .api.alert_routes import router as alert_router

    app.include_router(system_router, prefix="/api")
    app.include_router(config_router, prefix="/api")
    app.include_router(mesh_router, prefix="/api")
    app.include_router(env_router, prefix="/api")
    app.include_router(alert_router, prefix="/api")

    # WebSocket router (no prefix, path is /ws/live)
    app.include_router(ws_router)

    # Static files - mount LAST so /api routes take priority
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

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
    app.state.alert_engine = getattr(meshai_instance, "alert_engine", None)
    app.state.env_store = getattr(meshai_instance, "env_store", None)
    app.state.subscription_manager = meshai_instance.subscription_manager

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
