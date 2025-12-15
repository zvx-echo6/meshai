"""Conversation history management for MeshAI."""

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import aiosqlite

from .config import HistoryConfig

logger = logging.getLogger(__name__)


@dataclass
class ConversationMessage:
    """A single message in conversation history."""

    role: str  # "user" or "assistant"
    content: str
    timestamp: float


class ConversationHistory:
    """Manages per-user conversation history in SQLite."""

    def __init__(self, config: HistoryConfig):
        self.config = config
        self._db_path = Path(config.database)
        self._db: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Initialize database and create tables."""
        self._db = await aiosqlite.connect(self._db_path)

        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp REAL NOT NULL
            )
        """)

        await self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_user_timestamp
            ON conversations (user_id, timestamp)
        """)

        # Summary table for rolling summary memory
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS conversation_summaries (
                user_id TEXT PRIMARY KEY,
                summary TEXT NOT NULL,
                message_count INTEGER NOT NULL,
                updated_at REAL NOT NULL
            )
        """)

        await self._db.commit()
        logger.info(f"Conversation history initialized at {self._db_path}")

    async def close(self) -> None:
        """Close database connection."""
        if self._db:
            await self._db.close()
            self._db = None

    async def add_message(self, user_id: str, role: str, content: str) -> None:
        """Add a message to conversation history.

        Args:
            user_id: Node ID of the user
            role: "user" or "assistant"
            content: Message content
        """
        if not self._db:
            raise RuntimeError("Database not initialized")

        async with self._lock:
            await self._db.execute(
                """
                INSERT INTO conversations (user_id, role, content, timestamp)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, role, content, time.time()),
            )
            await self._db.commit()

            # Prune old messages for this user
            await self._prune_history(user_id)

    async def get_history(self, user_id: str) -> list[ConversationMessage]:
        """Get conversation history for a user.

        Args:
            user_id: Node ID of the user

        Returns:
            List of ConversationMessage objects, oldest first
        """
        if not self._db:
            raise RuntimeError("Database not initialized")

        # Check for conversation timeout
        cutoff_time = time.time() - self.config.conversation_timeout

        async with self._lock:
            cursor = await self._db.execute(
                """
                SELECT role, content, timestamp
                FROM conversations
                WHERE user_id = ? AND timestamp > ?
                ORDER BY timestamp ASC
                LIMIT ?
                """,
                (user_id, cutoff_time, self.config.max_messages_per_user * 2),
            )

            rows = await cursor.fetchall()

        return [
            ConversationMessage(role=row[0], content=row[1], timestamp=row[2]) for row in rows
        ]

    async def get_history_for_llm(self, user_id: str) -> list[dict]:
        """Get conversation history formatted for LLM API.

        Args:
            user_id: Node ID of the user

        Returns:
            List of dicts with 'role' and 'content' keys
        """
        history = await self.get_history(user_id)
        return [{"role": msg.role, "content": msg.content} for msg in history]

    async def clear_history(self, user_id: str) -> int:
        """Clear conversation history for a user.

        Args:
            user_id: Node ID of the user

        Returns:
            Number of messages deleted
        """
        if not self._db:
            raise RuntimeError("Database not initialized")

        async with self._lock:
            cursor = await self._db.execute(
                "DELETE FROM conversations WHERE user_id = ?",
                (user_id,),
            )
            await self._db.commit()
            return cursor.rowcount

    async def _prune_history(self, user_id: str) -> None:
        """Remove old messages beyond the limit for a user."""
        # Get count of messages for user
        cursor = await self._db.execute(
            "SELECT COUNT(*) FROM conversations WHERE user_id = ?",
            (user_id,),
        )
        count = (await cursor.fetchone())[0]

        # Remove oldest if over limit (keep pairs, so multiply by 2)
        max_messages = self.config.max_messages_per_user * 2
        if count > max_messages:
            excess = count - max_messages
            await self._db.execute(
                """
                DELETE FROM conversations
                WHERE id IN (
                    SELECT id FROM conversations
                    WHERE user_id = ?
                    ORDER BY timestamp ASC
                    LIMIT ?
                )
                """,
                (user_id, excess),
            )
            await self._db.commit()

    async def get_stats(self) -> dict:
        """Get statistics about conversation history.

        Returns:
            Dict with 'total_messages', 'unique_users', 'oldest_message'
        """
        if not self._db:
            raise RuntimeError("Database not initialized")

        async with self._lock:
            # Total messages
            cursor = await self._db.execute("SELECT COUNT(*) FROM conversations")
            total = (await cursor.fetchone())[0]

            # Unique users
            cursor = await self._db.execute("SELECT COUNT(DISTINCT user_id) FROM conversations")
            users = (await cursor.fetchone())[0]

            # Oldest message
            cursor = await self._db.execute("SELECT MIN(timestamp) FROM conversations")
            oldest = (await cursor.fetchone())[0]

        return {
            "total_messages": total,
            "unique_users": users,
            "oldest_message": oldest,
        }

    async def cleanup_expired(self) -> int:
        """Remove all expired conversations.

        Returns:
            Number of messages deleted
        """
        if not self._db:
            raise RuntimeError("Database not initialized")

        cutoff_time = time.time() - self.config.conversation_timeout

        async with self._lock:
            cursor = await self._db.execute(
                "DELETE FROM conversations WHERE timestamp < ?",
                (cutoff_time,),
            )
            await self._db.commit()
            deleted = cursor.rowcount

        if deleted > 0:
            logger.info(f"Cleaned up {deleted} expired conversation messages")

        return deleted

    # -------------------------------------------------------------------------
    # Summary Storage Methods (for Rolling Summary Memory)
    # -------------------------------------------------------------------------

    async def store_summary(
        self, user_id: str, summary: str, message_count: int
    ) -> None:
        """Store conversation summary.

        Args:
            user_id: Node ID of user
            summary: Summary text
            message_count: Number of messages summarized
        """
        if not self._db:
            raise RuntimeError("Database not initialized")

        async with self._lock:
            await self._db.execute(
                """
                INSERT OR REPLACE INTO conversation_summaries
                (user_id, summary, message_count, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, summary, message_count, time.time()),
            )
            await self._db.commit()

    async def get_summary(self, user_id: str) -> Optional[dict]:
        """Get conversation summary for user.

        Args:
            user_id: Node ID of user

        Returns:
            Dict with 'summary', 'message_count', 'updated_at' or None
        """
        if not self._db:
            raise RuntimeError("Database not initialized")

        async with self._lock:
            cursor = await self._db.execute(
                """
                SELECT summary, message_count, updated_at
                FROM conversation_summaries
                WHERE user_id = ?
                """,
                (user_id,),
            )
            row = await cursor.fetchone()

        if not row:
            return None

        return {
            "summary": row[0],
            "message_count": row[1],
            "updated_at": row[2],
        }

    async def clear_summary(self, user_id: str) -> None:
        """Clear summary for user (e.g., on history reset).

        Args:
            user_id: Node ID of user
        """
        if not self._db:
            raise RuntimeError("Database not initialized")

        async with self._lock:
            await self._db.execute(
                "DELETE FROM conversation_summaries WHERE user_id = ?",
                (user_id,),
            )
            await self._db.commit()
