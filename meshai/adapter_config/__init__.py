"""v0.6-3a meshai/adapter_config package.

Public API:
    from meshai.adapter_config import adapter_config, invalidate_cache, seed_defaults

`adapter_config` is the typed accessor singleton. `invalidate_cache()` drops
the read-side cache (called by /api/adapter-config PUT in v0.6-3c).
`seed_defaults(conn)` is called from `meshai.persistence.db.init_db()` after
the migration runner finishes; it INSERT OR IGNOREs one row per registry
entry so first-deploy behavior matches every existing handler constant
exactly.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any

from meshai.adapter_config._accessor import (
    adapter_config,
    invalidate_cache,
)
from meshai.adapter_config.defaults import (
    REGISTRY,
    ADAPTER_META,
    all_adapters,
    registry_for,
)

__all__ = [
    "adapter_config",
    "invalidate_cache",
    "seed_defaults",
    "REGISTRY",
    "ADAPTER_META",
    "all_adapters",
    "registry_for",
]


logger = logging.getLogger(__name__)


def seed_defaults(conn: sqlite3.Connection) -> tuple[int, int]:
    """Populate adapter_config + adapter_meta from the defaults registry.

    Idempotent: INSERT OR IGNORE never overwrites a user-edited row. Run
    after the v6 migration creates the tables; safe to re-run on every
    init_db().

    Returns:
        (config_rows_inserted, meta_rows_inserted) -- 0/0 when fully seeded.
    """
    now = time.time()

    # adapter_config rows.
    cfg_inserted = 0
    for (adapter, key), spec in REGISTRY.items():
        default_json = json.dumps(spec["default"])
        cur = conn.execute(
            "INSERT OR IGNORE INTO adapter_config("
            "adapter, key, value_json, default_json, type, description, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (adapter, key, default_json, default_json,
             spec["type"], spec.get("description") or "", now),
        )
        if cur.rowcount > 0:
            cfg_inserted += 1

    # adapter_meta rows.
    meta_inserted = 0
    for adapter, meta in ADAPTER_META.items():
        cur = conn.execute(
            "INSERT OR IGNORE INTO adapter_meta("
            "adapter, display_name, include_in_llm_context, description, updated_at) "
            "VALUES (?,?,?,?,?)",
            (adapter, meta.get("display_name") or adapter,
             1 if meta.get("include_in_llm_context", True) else 0,
             meta.get("description") or "", now),
        )
        if cur.rowcount > 0:
            meta_inserted += 1

    if cfg_inserted or meta_inserted:
        logger.info(
            "adapter_config: seed_defaults inserted %d config rows + %d meta rows",
            cfg_inserted, meta_inserted,
        )
    return cfg_inserted, meta_inserted
