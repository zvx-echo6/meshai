"""SQLite persistence connection management + migration runner.

Single-writer SQLite pattern with WAL journal mode for reader concurrency.
One connection per thread (threading.local) -- callers should not share
connections across threads.

Path resolution:
    1. MESHAI_DB_PATH env var (explicit override)
    2. DEFAULT_DB_PATH = /data/meshai.sqlite (prod container mount)

Special value ":memory:" or any path containing "memory" routes to an
in-memory SQLite for tests.

Migrations live in meshai/persistence/migrations/v*.sql. The runner
applies them in version order, recording the applied version in
schema_meta. Idempotent re-run is a no-op.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)


DEFAULT_DB_PATH = "/data/meshai.sqlite"
MESHAI_DB_PATH_ENV = "MESHAI_DB_PATH"
SCHEMA_VERSION = 1
SCHEMA_META_TABLE = "schema_meta"
MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# Per-thread connection pool. Each thread that calls get_db() gets its
# own sqlite3.Connection cached on threading.local. Tests can clear
# via close_thread_connection() between cases.
_local = threading.local()
# Module-level lock guards init_db() so concurrent first-callers don't
# race on migration application.
_init_lock = threading.Lock()
# Cache of initialised database paths in this process so init_db() is
# idempotent without re-reading migration files on every call.
_initialised: set[str] = set()


def MESHAI_DB_PATH() -> str:
    """Resolve the active SQLite path (env var override or default)."""
    return os.environ.get(MESHAI_DB_PATH_ENV) or DEFAULT_DB_PATH


def _is_memory_path(path: str) -> bool:
    return path == ":memory:" or "mode=memory" in path or path.startswith("file::memory:")


def _connect(path: str) -> sqlite3.Connection:
    """Open a SQLite connection with sane defaults for this project."""
    if _is_memory_path(path):
        # For in-memory tests, use a shared cache so multiple connections
        # in the same thread can see the same DB. Tests that want isolation
        # call close_thread_connection() between cases.
        uri = "file::memory:?cache=shared"
        conn = sqlite3.connect(uri, uri=True, isolation_level=None,
                                check_same_thread=False)
    else:
        # Ensure parent dir exists for file-backed DBs.
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, isolation_level=None,
                                check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    # Enable foreign keys (off by default in SQLite); WAL mode for reader
    # concurrency; reasonable busy timeout for the single-writer pattern.
    conn.execute("PRAGMA foreign_keys = ON")
    if not _is_memory_path(path):
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def get_db(path: Optional[str] = None) -> sqlite3.Connection:
    """Return a SQLite connection for the current thread (cached).

    First call initialises the database (runs pending migrations).
    Subsequent calls in the same thread return the cached connection.
    """
    target = path or MESHAI_DB_PATH()
    cached = getattr(_local, "conn", None)
    cached_path = getattr(_local, "path", None)
    if cached is not None and cached_path == target:
        return cached
    # Different path requested or no cached conn -- (re)open.
    if cached is not None:
        try: cached.close()
        except Exception: pass
    conn = _connect(target)
    _local.conn = conn
    _local.path = target
    if target not in _initialised:
        with _init_lock:
            if target not in _initialised:
                _apply_migrations(conn)
                _initialised.add(target)
    return conn


def close_thread_connection() -> None:
    """Close + drop the cached connection for the current thread.

    Tests call this between cases to ensure a clean slate. The shared-cache
    in-memory database is reset on the LAST close in the process.
    """
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try: conn.close()
        except Exception: pass
    if hasattr(_local, "conn"): del _local.conn
    if hasattr(_local, "path"): del _local.path


def init_db(path: Optional[str] = None) -> sqlite3.Connection:
    """Explicit init entry point (idempotent). Equivalent to get_db()
    semantically but documents intent at startup. Returns the connection."""
    return get_db(path)


def _read_migration_files() -> list[tuple[int, str, str]]:
    """Return [(version_int, filename, sql_text), ...] sorted ascending."""
    if not MIGRATIONS_DIR.is_dir():
        return []
    out: list[tuple[int, str, str]] = []
    for p in sorted(MIGRATIONS_DIR.iterdir()):
        if not p.is_file() or p.suffix.lower() != ".sql":
            continue
        # Filename format: v<N>.sql or v<N>_<label>.sql
        stem = p.stem
        if not stem.startswith("v"):
            continue
        n_str = stem[1:].split("_", 1)[0]
        try: n = int(n_str)
        except ValueError: continue
        out.append((n, p.name, p.read_text()))
    return out


def _current_version(conn: sqlite3.Connection) -> int:
    """Return the highest applied migration version, or 0 if none."""
    # Does the schema_meta table exist?
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (SCHEMA_META_TABLE,),
    ).fetchone()
    if row is None:
        return 0
    row = conn.execute(
        f"SELECT value FROM {SCHEMA_META_TABLE} WHERE key='version'",
    ).fetchone()
    if row is None:
        return 0
    try: return int(row["value"])
    except (TypeError, ValueError): return 0


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply any pending migrations in version order. Idempotent."""
    migrations = _read_migration_files()
    if not migrations:
        logger.warning("persistence: no migration files found in %s", MIGRATIONS_DIR)
        return
    current = _current_version(conn)
    pending = [(n, name, sql) for n, name, sql in migrations if n > current]
    if not pending:
        logger.debug("persistence: schema up to date at v%d", current)
        return
    for version, filename, sql in pending:
        logger.info("persistence: applying migration %s (v%d)", filename, version)
        # sqlite3.Connection.executescript() ISSUES ITS OWN COMMIT before
        # starting the script and another at the end, so wrapping it in an
        # explicit BEGIN/COMMIT is both unnecessary and broken (the
        # ROLLBACK in the except clause would fire against an already-
        # committed-or-empty transaction). Migration atomicity is bounded
        # by executescript's own transaction.
        try:
            conn.executescript(sql)
            conn.execute(
                f"INSERT OR REPLACE INTO {SCHEMA_META_TABLE}(key, value) VALUES('version', ?)",
                (str(version),),
            )
        except Exception:
            logger.exception("persistence: migration %s failed", filename)
            raise
    logger.info("persistence: schema now at v%d", pending[-1][0])
