"""WebSocket support for real-time dashboard updates."""

import logging
from typing import Set

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

router = APIRouter()


class DashboardBroadcaster:
    """Manages active WebSocket connections for real-time updates."""

    def __init__(self):
        self._connections: Set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        """Accept and register a new WebSocket connection."""
        await websocket.accept()
        self._connections.add(websocket)
        logger.debug(f"WebSocket connected, total: {len(self._connections)}")

    def disconnect(self, websocket: WebSocket) -> None:
        """Remove a WebSocket connection."""
        self._connections.discard(websocket)
        logger.debug(f"WebSocket disconnected, total: {len(self._connections)}")

    async def broadcast(self, msg_type: str, data: dict) -> None:
        """Broadcast a message to all connected clients.

        Args:
            msg_type: Message type (e.g., "health_update", "alert_fired")
            data: Message payload
        """
        if not self._connections:
            return

        message = {"type": msg_type, "data": data}
        dead_connections = set()

        for websocket in self._connections:
            try:
                await websocket.send_json(message)
            except Exception as e:
                logger.debug(f"WebSocket send failed: {e}")
                dead_connections.add(websocket)

        # Remove dead connections
        for ws in dead_connections:
            self._connections.discard(ws)

    @property
    def connection_count(self) -> int:
        """Get number of active connections."""
        return len(self._connections)


def _serialize_health(mesh_health) -> dict:
    """Serialize MeshHealth for WebSocket transmission."""
    if not mesh_health:
        return {"score": 0, "tier": "Unknown", "message": "No data"}

    score = mesh_health.score
    return {
        "score": round(score.composite, 1),
        "tier": score.tier,
        "pillars": {
            "infrastructure": round(score.infrastructure, 1),
            "utilization": round(score.utilization, 1),
            "coverage": round(score.coverage, 1),
            "behavior": round(score.behavior, 1),
            "power": round(score.power, 1),
        },
        "infra_online": score.infra_online,
        "infra_total": score.infra_total,
        "util_percent": round(score.util_percent, 1),
        "flagged_nodes": score.flagged_nodes,
        "battery_warnings": score.battery_warnings,
        "total_nodes": mesh_health.total_nodes,
        "total_regions": mesh_health.total_regions,
        "last_computed": mesh_health.last_computed,
    }


@router.websocket("/ws/live")
async def ws_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time updates."""
    # Get broadcaster from app state
    app_state = websocket.app.state
    broadcaster = getattr(app_state, "broadcaster", None)

    if not broadcaster:
        await websocket.close(code=1011, reason="Broadcaster not initialized")
        return

    await broadcaster.connect(websocket)

    try:
        # Send initial state snapshot on connect
        health_engine = getattr(app_state, "health_engine", None)
        if health_engine and health_engine.mesh_health:
            score = health_engine.mesh_health.score
            if score.composite > 0 and score.infra_total > 0:
                await websocket.send_json({
                    "type": "health_update",
                    "data": _serialize_health(health_engine.mesh_health)
                })

        # Keep connection alive, receive client keepalive pings
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        broadcaster.disconnect(websocket)
    except Exception as e:
        logger.debug(f"WebSocket error: {e}")
        broadcaster.disconnect(websocket)
