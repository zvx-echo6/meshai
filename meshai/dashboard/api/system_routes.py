"""System status and control API routes."""

import time
from pathlib import Path

from fastapi import APIRouter, Request

from meshai import __version__
from meshai.commands.status import _start_time

router = APIRouter(tags=["system"])


@router.get("/status")
async def get_status(request: Request):
    """Get system status information."""
    config = request.app.state.config
    data_store = request.app.state.data_store

    # Calculate uptime
    uptime_seconds = time.time() - _start_time if _start_time else 0

    # Connection info
    conn = config.connection
    if conn.type == "tcp":
        connection_target = f"{conn.tcp_host}:{conn.tcp_port}"
    else:
        connection_target = conn.serial_port

    # Count nodes and sources
    node_count = 0
    source_count = 0
    connected = False

    if data_store:
        try:
            nodes = data_store.get_all_nodes()
            node_count = len(nodes) if nodes else 0
            source_count = data_store.source_count
            connected = any(s.is_loaded for s in data_store._sources.values())
        except Exception:
            pass

    return {
        "version": __version__,
        "uptime_seconds": round(uptime_seconds, 1),
        "bot_name": config.bot.name,
        "connection_type": conn.type,
        "connection_target": connection_target,
        "connected": connected,
        "node_count": node_count,
        "source_count": source_count,
        "env_feeds_enabled": request.app.state.env_store is not None,
        "dashboard_port": config.dashboard.port,
    }


@router.post("/restart")
async def restart_bot():
    """Signal the bot to restart."""
    restart_file = Path("/tmp/meshai_restart")
    restart_file.touch()
    return {"restarting": True}
