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

Phase-0 addition: a session-scoped TZ fixture validates that
America/Boise resolves via zoneinfo, exercising the tzdata guard needed
in CI environments.  The fixture saves and restores the original TZ so
that existing tests running with naive datetimes are unaffected.
"""
import os
import time
import zoneinfo

import pytest

from meshai.persistence import close_thread_connection
from meshai.persistence import db as _persistence_db


@pytest.fixture(scope="session", autouse=True)
def _tz_boise():
    """Set TZ=America/Boise, call tzset(), assert zoneinfo resolves, then restore.

    This validates that the tzdata package (or system zoneinfo) is available
    in CI before any formatter/renderer test runs.  The original TZ is restored
    so that existing tests using naive datetimes are not affected by the
    Mountain Time offset.
    """
    original_tz = os.environ.get("TZ")
    os.environ["TZ"] = "America/Boise"
    time.tzset()
    # Assert that zoneinfo can resolve the timezone (requires tzdata package
    # or /usr/share/zoneinfo/America/Boise on the system).
    zoneinfo.ZoneInfo("America/Boise")
    # Restore the original TZ so that tests using naive datetime / time.localtime()
    # behave the same as they did before this session fixture was added.
    if original_tz is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = original_tz
    time.tzset()
    yield


@pytest.fixture(autouse=True)
def _isolate_meshai_db(tmp_path, monkeypatch):
    """Point MESHAI_DB_PATH at a tmp file per test + run init_db so the
    v6 adapter_config seed lands before any test code runs.

    v0.6-3a: init_db() applies pending migrations AND seeds
    adapter_config from the defaults registry. Tests that read via
     need the seed in place.
    """
    p = str(tmp_path / "meshai-test-isolated.sqlite")
    monkeypatch.setenv("MESHAI_DB_PATH", p)
    _persistence_db._initialised.clear()
    close_thread_connection()
    # Clear the adapter_config accessor cache so the prior tests
    # in-memory cache does not leak into this test.
    try:
        from meshai.adapter_config import invalidate_cache
        invalidate_cache()
    except Exception:
        pass
    # Eager init: applies v1..v6 migrations + seeds adapter_config.
    from meshai.persistence import init_db
    init_db()
    yield p
    close_thread_connection()
    _persistence_db._initialised.discard(p)
    try:
        from meshai.adapter_config import invalidate_cache
        invalidate_cache()
    except Exception:
        pass
