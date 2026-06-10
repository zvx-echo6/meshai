"""meshai persistence layer.

SQLite-backed adapter event store + mesh telemetry. Single-writer pattern
with WAL mode; threading.local connection pool. v1 schema covers 13 data
tables + event_log + schema_meta.

Public API:
    from meshai.persistence import get_db, init_db, MESHAI_DB_PATH

The db.py module handles connection pooling and runs pending migrations
on first init_db() call. Adapter handlers are responsible for inserting
into the right table (none wired yet -- foundation only).
"""

from meshai.persistence.db import (
    get_db,
    init_db,
    close_thread_connection,
    MESHAI_DB_PATH,
    DEFAULT_DB_PATH,
    SCHEMA_VERSION,
)

__all__ = [
    "get_db",
    "init_db",
    "close_thread_connection",
    "MESHAI_DB_PATH",
    "DEFAULT_DB_PATH",
    "SCHEMA_VERSION",
]
