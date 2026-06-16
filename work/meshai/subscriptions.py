"""Subscription management for scheduled reports and alerts."""

import logging
import sqlite3
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Valid subscription types
VALID_SUB_TYPES = {"daily", "weekly", "alerts"}
VALID_DAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
VALID_SCOPE_TYPES = {"mesh", "region", "node"}


class SubscriptionManager:
    """Manages user subscriptions with SQLite storage."""

    def __init__(self, db_path: str):
        """Initialize subscription manager.

        Args:
            db_path: Path to SQLite database (same as mesh_history.db)
        """
        self._db_path = db_path
        self._db: Optional[sqlite3.Connection] = None
        self._init_db()

    def _init_db(self):
        """Initialize database connection and schema."""
        self._db = sqlite3.connect(self._db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row

        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                sub_type TEXT NOT NULL,
                schedule_time TEXT,
                schedule_day TEXT,
                scope_type TEXT DEFAULT 'mesh',
                scope_value TEXT,
                created_at REAL NOT NULL,
                last_sent REAL DEFAULT 0,
                enabled INTEGER DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_sub_user ON subscriptions(user_id);
            CREATE INDEX IF NOT EXISTS idx_sub_type ON subscriptions(sub_type);
        """)
        self._db.commit()
        logger.info("Subscription manager initialized")

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        """Convert sqlite Row to dict."""
        return dict(row)

    def add(self, user_id: str, sub_type: str, schedule_time: str = None,
            schedule_day: str = None, scope_type: str = "mesh",
            scope_value: str = None) -> dict:
        """Add a subscription.

        Args:
            user_id: Subscriber node_num
            sub_type: "daily", "weekly", or "alerts"
            schedule_time: HHMM format (required for daily/weekly)
            schedule_day: mon-sun (required for weekly)
            scope_type: "mesh", "region", or "node"
            scope_value: Region name or node identifier

        Returns:
            Created subscription dict

        Raises:
            ValueError: If validation fails
        """
        # Validate sub_type
        if sub_type not in VALID_SUB_TYPES:
            raise ValueError(f"Invalid type '{sub_type}'. Use: daily, weekly, or alerts")

        # Validate schedule_time for daily/weekly
        if sub_type in ("daily", "weekly"):
            if not schedule_time:
                raise ValueError(f"Time required for {sub_type} subscription. Use HHMM format (e.g., 1830)")
            if not self._validate_time(schedule_time):
                raise ValueError("Invalid time format. Use HHMM (e.g., 1830 for 6:30 PM)")

        # Validate schedule_day for weekly
        if sub_type == "weekly":
            if not schedule_day:
                raise ValueError("Day required for weekly subscription. Use: mon, tue, wed, thu, fri, sat, sun")
            if schedule_day.lower() not in VALID_DAYS:
                raise ValueError("Invalid day. Use: mon, tue, wed, thu, fri, sat, sun")
            schedule_day = schedule_day.lower()

        # Validate scope_type
        if scope_type not in VALID_SCOPE_TYPES:
            raise ValueError(f"Invalid scope '{scope_type}'. Use: mesh, region, or node")

        # Check for duplicates
        existing = self._db.execute("""
            SELECT id FROM subscriptions
            WHERE user_id = ? AND sub_type = ? AND scope_type = ?
            AND (scope_value = ? OR (scope_value IS NULL AND ? IS NULL))
            AND enabled = 1
        """, (user_id, sub_type, scope_type, scope_value, scope_value)).fetchone()

        if existing:
            scope_desc = f" for {scope_type} {scope_value}" if scope_value else ""
            raise ValueError(f"Already subscribed to {sub_type}{scope_desc}")

        # Insert subscription
        now = time.time()
        cursor = self._db.execute("""
            INSERT INTO subscriptions (user_id, sub_type, schedule_time, schedule_day,
                                       scope_type, scope_value, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (user_id, sub_type, schedule_time, schedule_day, scope_type, scope_value, now))
        self._db.commit()

        sub_id = cursor.lastrowid
        return self._get_by_id(sub_id)

    def _validate_time(self, time_str: str) -> bool:
        """Validate HHMM time format."""
        if not time_str or len(time_str) != 4 or not time_str.isdigit():
            return False
        hours = int(time_str[:2])
        minutes = int(time_str[2:])
        return 0 <= hours <= 23 and 0 <= minutes <= 59

    def _get_by_id(self, sub_id: int) -> dict:
        """Get subscription by ID."""
        row = self._db.execute(
            "SELECT * FROM subscriptions WHERE id = ?", (sub_id,)
        ).fetchone()
        return self._row_to_dict(row) if row else None

    def remove(self, user_id: str, sub_type: str = None) -> int:
        """Remove subscription(s).

        Args:
            user_id: Subscriber node_num
            sub_type: "daily", "weekly", "alerts", or None for all

        Returns:
            Number of subscriptions removed
        """
        if sub_type and sub_type != "all":
            cursor = self._db.execute(
                "DELETE FROM subscriptions WHERE user_id = ? AND sub_type = ?",
                (user_id, sub_type)
            )
        else:
            cursor = self._db.execute(
                "DELETE FROM subscriptions WHERE user_id = ?",
                (user_id,)
            )
        self._db.commit()
        return cursor.rowcount

    def get_user_subs(self, user_id: str) -> list[dict]:
        """Get all subscriptions for a user."""
        rows = self._db.execute(
            "SELECT * FROM subscriptions WHERE user_id = ? AND enabled = 1 ORDER BY created_at",
            (user_id,)
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_due_subscriptions(self, current_time_hhmm: str, current_day: str) -> list[dict]:
        """Get subscriptions that should fire right now.

        Args:
            current_time_hhmm: Current time as "HHMM" (e.g., "1830")
            current_day: Current day as 3-letter lowercase (e.g., "sun")

        Returns:
            List of subscription dicts that are due
        """
        now = time.time()
        due = []

        # Get all daily/weekly subscriptions
        rows = self._db.execute("""
            SELECT * FROM subscriptions
            WHERE sub_type IN ('daily', 'weekly') AND enabled = 1
        """).fetchall()

        current_minutes = int(current_time_hhmm[:2]) * 60 + int(current_time_hhmm[2:])

        for row in rows:
            sub = self._row_to_dict(row)
            schedule_time = sub.get("schedule_time")
            if not schedule_time:
                continue

            schedule_minutes = int(schedule_time[:2]) * 60 + int(schedule_time[2:])

            # 5-minute matching window
            if abs(schedule_minutes - current_minutes) > 5:
                continue

            sub_type = sub["sub_type"]
            last_sent = sub.get("last_sent", 0) or 0

            if sub_type == "daily":
                # Don't fire if sent within last 23 hours
                if now - last_sent < 23 * 3600:
                    continue
                due.append(sub)

            elif sub_type == "weekly":
                # Check day matches
                schedule_day = sub.get("schedule_day", "").lower()
                if schedule_day != current_day.lower():
                    continue
                # Don't fire if sent within last 6 days
                if now - last_sent < 6 * 24 * 3600:
                    continue
                due.append(sub)

        return due

    def get_alert_subscribers(self, scope_type: str = None, scope_value: str = None) -> list[dict]:
        """Get users subscribed to alerts matching a scope.

        Args:
            scope_type: "mesh", "region", or "node"
            scope_value: Region name or node identifier

        Returns:
            List of subscription dicts where scope matches
        """
        # Get all alert subscriptions
        rows = self._db.execute("""
            SELECT * FROM subscriptions
            WHERE sub_type = 'alerts' AND enabled = 1
        """).fetchall()

        matching = []
        for row in rows:
            sub = self._row_to_dict(row)
            sub_scope = sub.get("scope_type", "mesh")
            sub_value = sub.get("scope_value")

            # Mesh scope gets ALL alerts
            if sub_scope == "mesh":
                matching.append(sub)
            # Region scope gets alerts for that region
            elif sub_scope == "region" and scope_type == "region":
                if sub_value and scope_value and sub_value.lower() == scope_value.lower():
                    matching.append(sub)
            # Node scope gets alerts for that node
            elif sub_scope == "node" and scope_type == "node":
                if sub_value and scope_value and sub_value.lower() == scope_value.lower():
                    matching.append(sub)

        return matching

    def mark_sent(self, subscription_id: int):
        """Update last_sent timestamp to now."""
        self._db.execute(
            "UPDATE subscriptions SET last_sent = ? WHERE id = ?",
            (time.time(), subscription_id)
        )
        self._db.commit()

    def get_all_subs(self) -> list[dict]:
        """Get all subscriptions (for admin view)."""
        rows = self._db.execute(
            "SELECT * FROM subscriptions WHERE enabled = 1 ORDER BY user_id, created_at"
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def close(self):
        """Close database connection."""
        if self._db:
            self._db.close()
            self._db = None
