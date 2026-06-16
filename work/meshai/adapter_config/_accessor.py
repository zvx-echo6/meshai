"""v0.6-3a typed accessor over the adapter_config table.

Handlers use the singleton `adapter_config` from this package:

    from meshai.adapter_config import adapter_config

    cooldown_s = adapter_config.wfigs.cooldown_seconds   # int
    severities = adapter_config.nws.broadcast_severities # list[str]

Reads are dict-cached. The cache invalidates on `invalidate_cache()`
(called from the REST API's PUT handler in v0.6-3c). The cache is
per-process; meshai is single-process so there is no cross-process
coherence problem.

Fallback ordering on read:
    1. In-memory cache hit -> return immediately.
    2. SQL `SELECT value_json, type FROM adapter_config WHERE adapter=? AND key=?`
       -> decode + cache + return.
    3. Registry fallback (defaults.REGISTRY) -> return without caching, log
       at WARNING level (this shouldn't happen post-seed but defends
       against schema drift).
    4. Raise AttributeError -> the key is unknown to both DB and registry.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)


# Process-wide cache. Reads use the GIL-atomic dict get/set; the lock
# guards multi-statement sequences (gen bump + clear).
_CACHE_LOCK = threading.Lock()
_cache: dict[tuple[str, str], Any] = {}


def invalidate_cache() -> None:
    """Drop every cached value. Called by the REST API on PUT/reset."""
    with _CACHE_LOCK:
        _cache.clear()
    logger.debug("adapter_config: cache invalidated")


# ---------- internals ----------------------------------------------------


def _decode(value_json: str, type_: str) -> Any:
    """JSON-decoded value coerced to the declared Python type."""
    raw = json.loads(value_json)
    if type_ == "int":
        if raw is None: return None
        return int(raw)
    if type_ == "float":
        if raw is None: return None
        return float(raw)
    if type_ == "str":
        if raw is None: return None
        return str(raw)
    if type_ == "bool":
        if raw is None: return None
        return bool(raw)
    if type_ == "json":
        return raw
    # Unknown tag -- return the JSON-decoded form and log.
    logger.warning("adapter_config: unknown type tag %r; returning raw decoded", type_)
    return raw


def _load_from_db(adapter: str, key: str) -> tuple[bool, Any]:
    """Returns (found, value). found=False means no row exists."""
    try:
        from meshai.persistence import get_db
        conn = get_db()
        row = conn.execute(
            "SELECT value_json, type FROM adapter_config "
            "WHERE adapter=? AND key=?",
            (adapter, key),
        ).fetchone()
    except Exception:
        logger.exception("adapter_config: DB read failed for %s.%s", adapter, key)
        return (False, None)
    if row is None:
        return (False, None)
    return (True, _decode(row["value_json"], row["type"]))


def _load_from_registry(adapter: str, key: str) -> tuple[bool, Any]:
    """Returns (found, default_value) from defaults.REGISTRY."""
    from meshai.adapter_config.defaults import REGISTRY
    spec = REGISTRY.get((adapter, key))
    if spec is None:
        return (False, None)
    return (True, spec["default"])


# ---------- public accessor ----------------------------------------------


class _AdapterSection:
    """Returned by `adapter_config.<adapter>`. Resolves attribute access
    to the typed value via the read pipeline."""

    __slots__ = ("_adapter",)

    def __init__(self, adapter: str):
        object.__setattr__(self, "_adapter", adapter)

    def __getattr__(self, key: str) -> Any:
        # Avoid recursion when Python probes for dunder attributes.
        if key.startswith("__"):
            raise AttributeError(key)
        return _resolve(self._adapter, key)

    def __setattr__(self, key: str, value: Any) -> None:
        raise AttributeError(
            "adapter_config is read-only at the accessor level; "
            "use the /api/adapter-config PUT endpoint to mutate."
        )

    def __repr__(self) -> str:
        return f"<adapter_config.{self._adapter}>"


class AdapterConfig:
    """Singleton-style accessor: `adapter_config.<adapter>.<key>`."""

    __slots__ = ()

    def __getattr__(self, adapter: str) -> _AdapterSection:
        if adapter.startswith("__"):
            raise AttributeError(adapter)
        return _AdapterSection(adapter)

    def get(self, adapter: str, key: str) -> Any:
        """Programmatic accessor that mirrors `<self>.<adapter>.<key>`."""
        return _resolve(adapter, key)

    def invalidate(self) -> None:
        invalidate_cache()


def _resolve(adapter: str, key: str) -> Any:
    """Read pipeline: cache -> DB -> registry."""
    cache_key = (adapter, key)
    cached = _cache.get(cache_key, _SENTINEL)
    if cached is not _SENTINEL:
        return cached

    # DB.
    found, value = _load_from_db(adapter, key)
    if found:
        with _CACHE_LOCK:
            _cache[cache_key] = value
        return value

    # Registry fallback. Don't cache this -- when the DB catches up
    # (next seed_defaults / migration / write), we want the DB value to
    # win without a manual invalidation.
    found, default = _load_from_registry(adapter, key)
    if found:
        logger.warning(
            "adapter_config: %s.%s missing from DB; using registry default. "
            "(seed_defaults() should have populated this -- check init_db order.)",
            adapter, key,
        )
        return default

    raise AttributeError(
        f"adapter_config: unknown key {adapter!r}.{key!r} "
        f"(not in DB and not in defaults.REGISTRY)"
    )


# Sentinel to distinguish "cache miss" from "cache hit with value None".
_SENTINEL = object()


# The singleton instance used by handler code.
adapter_config = AdapterConfig()
