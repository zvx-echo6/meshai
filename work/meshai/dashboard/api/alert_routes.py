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
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    transport: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
):
    """Activity Log: outbound mesh broadcasts, newest first.

    Reads the FULL mesh_broadcasts_out audit log from the persistence DB
    (get_db) and returns every column as a plain dict. This is the whole
    outbound feed -- every category (weather/fires/satpass/band/traffic)
    across BOTH transports (meshtastic + meshcore). Nothing is filtered by
    default; the chatty scheduled categories (satpass, band) no longer crowd
    lower-frequency ones out of view because callers can page (offset) and
    narrow (transport / category).

    Params:
      limit    -- page size (default 100, max 1000), newest-first.
      offset   -- rows to skip for pagination (default 0).
      transport-- OPTIONAL: 'meshtastic' | 'meshcore'. Omit = both.
      category -- OPTIONAL: source_event_table value (e.g. 'nws_alerts',
                  'fires', 'satpass_events'). Omit = all categories.

    Legacy pre-audit rows keep NULL transport/success and are still returned
    on the unfiltered feed. If the table doesn't exist yet, returns [].
    """
    from meshai.persistence import get_db

    where = []
    params: list = []
    if transport:
        where.append("transport = ?")
        params.append(transport)
    if category:
        where.append("source_event_table = ?")
        params.append(category)

    sql = "SELECT * FROM mesh_broadcasts_out"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY sent_at DESC, id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    try:
        conn = get_db()
        rows = conn.execute(sql, params).fetchall()
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
