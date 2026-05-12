"""Alert API routes."""

from fastapi import APIRouter, Request

router = APIRouter(tags=["alerts"])


@router.get("/alerts/active")
async def get_active_alerts(request: Request):
    """Get currently active alerts."""
    alert_engine = request.app.state.alert_engine

    if not alert_engine:
        return []

    # Get recent alerts from alert engine if it has internal state
    alerts = []

    # Check for AlertState or similar if available
    if hasattr(alert_engine, "get_active_alerts"):
        try:
            raw_alerts = alert_engine.get_active_alerts()
            for alert in raw_alerts:
                alerts.append({
                    "type": alert.get("type", "unknown"),
                    "severity": alert.get("severity", "info"),
                    "message": alert.get("message", ""),
                    "timestamp": alert.get("timestamp"),
                    "scope_type": alert.get("scope_type"),
                    "scope_value": alert.get("scope_value"),
                })
        except Exception:
            pass
    elif hasattr(alert_engine, "_recent_alerts"):
        try:
            for alert in alert_engine._recent_alerts:
                alerts.append({
                    "type": alert.get("type", "unknown"),
                    "severity": alert.get("severity", "info"),
                    "message": alert.get("message", ""),
                    "timestamp": alert.get("timestamp"),
                })
        except Exception:
            pass

    return alerts


@router.get("/alerts/history")
async def get_alert_history(
    request: Request,
    limit: int = 50,
    offset: int = 0,
):
    """Get historical alerts with pagination."""
    # Historical alert data would come from SQLite
    # For now, return empty list
    return []


@router.get("/subscriptions")
async def get_subscriptions(request: Request):
    """Get all alert subscriptions."""
    subscription_manager = request.app.state.subscription_manager

    if not subscription_manager:
        return []

    try:
        subs = subscription_manager.get_all_subs()
        return [
            {
                "id": sub["id"],
                "user_id": sub["user_id"],
                "sub_type": sub["sub_type"],
                "schedule_time": sub.get("schedule_time"),
                "schedule_day": sub.get("schedule_day"),
                "scope_type": sub.get("scope_type", "mesh"),
                "scope_value": sub.get("scope_value"),
                "enabled": sub.get("enabled", 1) == 1,
            }
            for sub in subs
        ]
    except Exception:
        return []
