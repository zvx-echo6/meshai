"""v0.6-3a.1 meshai/adapter_config package.

Public API:
    from meshai.adapter_config import (
        adapter_config,
        invalidate_cache,
        seed_defaults,
        prune_orphans,
    )

`adapter_config` is the typed accessor singleton.
`invalidate_cache()` drops the read-side cache (called by /api/adapter-config
    PUT in v0.6-3c).
`seed_defaults(conn)` populates adapter_config + adapter_meta from REGISTRY.
`prune_orphans(conn)` deletes adapter_config rows whose (adapter, key) is no
    longer in REGISTRY -- the safety net for trimming the registry between
    deploys. Every delete is logged at INFO level so docker logs carry a
    paper trail of which keys disappeared.

Both seed and prune are called from meshai.persistence.db.init_db() and are
idempotent.
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
    set_runtime_override,
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
    "set_runtime_override",
    "seed_defaults",
    "prune_orphans",
    "REGISTRY",
    "ADAPTER_META",
    "all_adapters",
    "registry_for",
]


logger = logging.getLogger(__name__)


def seed_defaults(conn: sqlite3.Connection) -> tuple[int, int]:
    """Populate adapter_config + adapter_meta from REGISTRY + ADAPTER_META.

    Idempotent: INSERT OR IGNORE never overwrites a user-edited row. Safe
    to re-run on every init_db().

    Returns:
        (config_rows_inserted, meta_rows_inserted)
    """
    now = time.time()

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

    meta_inserted = 0
    for adapter, meta in ADAPTER_META.items():
        cur = conn.execute(
            "INSERT OR IGNORE INTO adapter_meta("
            "adapter, display_name, include_in_llm_context, reminder_enabled, "
            "description, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (adapter, meta.get("display_name") or adapter,
             1 if meta.get("include_in_llm_context", True) else 0,
             1 if meta.get("reminder_enabled", False) else 0,
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


def prune_orphans(conn: sqlite3.Connection) -> int:
    """Delete adapter_config rows whose (adapter, key) is no longer in REGISTRY.

    Each delete is logged at INFO level with the prefix
    'adapter_config orphan removed:' so the docker log captures a paper
    trail. First boot after a registry trim shows N log lines (one per
    removed key); every subsequent boot shows zero.

    Idempotent. adapter_meta is intentionally NOT pruned -- meta rows are
    cheap and a previously-known adapter dropping all its config keys
    still wants the include_in_llm_context toggle preserved (e.g. itd_511
    after v0.6-3a.1).

    Returns:
        Count of rows deleted.
    """
    valid_keys = set(REGISTRY.keys())
    existing = conn.execute(
        "SELECT adapter, key, value_json FROM adapter_config"
    ).fetchall()

    removed = 0
    for r in existing:
        adapter = r["adapter"]
        key = r["key"]
        if (adapter, key) in valid_keys:
            continue
        logger.info(
            "adapter_config orphan removed: %s.%s = %s",
            adapter, key, r["value_json"],
        )
        conn.execute(
            "DELETE FROM adapter_config WHERE adapter=? AND key=?",
            (adapter, key),
        )
        removed += 1

    if removed > 0:
        # The accessor cache may hold a now-deleted key. Invalidating
        # forces every next read to round-trip through the DB (cache
        # miss -> DB miss -> registry fallback / AttributeError).
        invalidate_cache()
    return removed
