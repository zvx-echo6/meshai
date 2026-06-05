"""Pytest fixture isolation for meshai persistence (v0.6-2).

Before v0.6-2 the dispatcher held all state in instance memory, so tests
that constructed `Dispatcher(...)` were inert w.r.t. SQLite. v0.6-2 made
`Dispatcher.__init__` read/restore from the persistence layer, which by
default points at `/data/meshai.sqlite`. Without isolation every test
would now read+write production state, polluting across tests and across
pytest invocations.

This autouse fixture redirects `MESHAI_DB_PATH` to a per-test tmp file
and clears the persistence-layer threading.local caches around each test.
Existing tests that don't reference any fixture get isolation for free;
tests that explicitly use a `db_path` (or similar) fixture can still
override the env var inside their own fixture body -- last setenv wins.
"""
import pytest

from meshai.persistence import close_thread_connection
from meshai.persistence import db as _persistence_db


@pytest.fixture(autouse=True)
def _isolate_meshai_db(tmp_path, monkeypatch):
    """Point MESHAI_DB_PATH at a tmp file per test."""
    p = str(tmp_path / "meshai-test-isolated.sqlite")
    monkeypatch.setenv("MESHAI_DB_PATH", p)
    _persistence_db._initialised.clear()
    close_thread_connection()
    yield p
    close_thread_connection()
    _persistence_db._initialised.discard(p)
