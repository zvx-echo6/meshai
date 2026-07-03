"""Alert API routes."""

from fastapi import APIRouter, Request, Query
from typing import Optional

router = APIRouter(tags=["alerts"])


@router.get("/alerts/active")
async def get_active_alerts(request: Request):
    """Get currently active alerts."""
    alert_engine = getattr(request.app.state, "alert_engine", None)

    if not alert_engine:
        return []

    alerts = []

    # Try get_pending_alerts first (our method)
    if hasattr(alert_engine, "get_pending_alerts"):
        try:
            raw_alerts = alert_engine.get_pending_alerts()
            for alert in raw_alerts:
                alerts.append({
                    "type": alert.get("type", "unknown"),
                    "severity": _map_severity(alert),
                    "message": alert.get("message", ""),
                    "timestamp": alert.get("timestamp"),
                    "scope_type": alert.get("scope_type"),
                    "scope_value": alert.get("scope_value"),
                })
        except Exception:
            pass

    return alerts


@router.get("/alerts/history")
async def get_alert_history(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    type: Optional[str] = Query(None),
    severity: Optional[str] = Query(None),
):
    """Get historical alerts with pagination and filtering.

    Note: Alert history persistence is not yet implemented.
    Returns empty array for now.
    """
    # Future: Query SQLite for historical alerts
    # For now, return empty with proper structure
    return {
        "items": [],
        "total": 0,
    }


@router.get("/activity")
async def get_activity(
    request: Request,
    limit: int = Query(100, ge=1, le=500),
):
    """Activity Log: most recent outbound mesh broadcasts, newest first.

    Reads mesh_broadcasts_out from the persistence/migration DB (get_db) and
    returns every column as a plain dict. Legacy rows keep NULL
    transport/success. If the table doesn't exist yet, returns [].
    """
    from meshai.persistence import get_db

    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT * FROM mesh_broadcasts_out "
            "ORDER BY sent_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except Exception:
        return []
    return [dict(r) for r in rows]


def _map_severity(alert: dict) -> str:
    """Map alert properties to severity level."""
    if alert.get("is_critical"):
        return "critical"
    alert_type = alert.get("type", "")
    if "emergency" in alert_type:
        return "emergency"
    if "critical" in alert_type:
        return "critical"
    if "warning" in alert_type:
        return "warning"
    if "watch" in alert_type:
        return "watch"
    return "info"
