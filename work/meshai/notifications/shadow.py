"""Phase-0b dry-run shadow comparator.

Gated by env var MESHAI_SHADOW_CATEGORIES (comma-separated category names).
Empty/unset  → fully OFF (zero overhead, zero side-effects on the real path).

Safety contract — this module MUST uphold all of the following:
  * NEVER calls GateResult.commit()
  * NEVER calls bus.emit()
  * NEVER writes to any DB table
  * NEVER returns a value that the caller uses for a routing decision
  * All exceptions swallowed + logged at DEBUG (shadow must never affect production)
  * Filesystem writes only to _SHADOW_DIR (append-only JSONL, never read-back)

Both public entry points — shadow_gate and shadow_render — return None always.
"""
from __future__ import annotations

import functools
import json
import logging
import os
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from meshai.notifications.events import Event

logger = logging.getLogger("meshai.notifications.shadow")

# JSONL output directory (inside the container data volume).
_SHADOW_DIR = "/app/data/shadow"


# ---------------------------------------------------------------------------
# Env-var gate
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def _load_shadow_categories() -> frozenset:
    """Parse MESHAI_SHADOW_CATEGORIES into a frozenset.  Cached after first call.

    Call _clear_enabled_cache() (e.g. from tests) to force re-parse after
    mutating the env var.
    """
    val = os.environ.get("MESHAI_SHADOW_CATEGORIES", "")
    if not val.strip():
        return frozenset()
    return frozenset(c.strip() for c in val.split(",") if c.strip())


def _clear_enabled_cache() -> None:
    """Clear the lru_cache so tests can mutate MESHAI_SHADOW_CATEGORIES."""
    _load_shadow_categories.cache_clear()


def enabled_for(category: str) -> bool:
    """Return True iff shadow comparison is enabled for *category*.

    Fast path: returns False immediately when the env var is unset.
    """
    return category in _load_shadow_categories()


# ---------------------------------------------------------------------------
# Internal JSONL writer — never raises, never blocks the caller
# ---------------------------------------------------------------------------

def _append_jsonl(category: str, record: dict) -> None:
    """Append one JSON line to /app/data/shadow/<category>.jsonl.

    Any I/O failure is logged at DEBUG and silently discarded.
    """
    try:
        os.makedirs(_SHADOW_DIR, exist_ok=True)
        path = os.path.join(_SHADOW_DIR, f"{category}.jsonl")
        line = json.dumps(record, default=str) + "\n"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        logger.debug(
            "shadow: JSONL write failed for category=%s", category, exc_info=True
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def shadow_gate(
    category: str,
    data: dict,
    *,
    source: str,
    now: float,
    old_broadcast: bool = False,
) -> None:
    """Dry-run the new gating decider for *category* and log mismatches.

    Called from consumer._normalize, BEFORE the default-deny return.
    The old_broadcast flag is synthesized is not None (True = would broadcast).
    The old suffixes (_severity_override, _dedup_suffix, _cooldown_suffix) are
    read from *data* — they are the values the legacy handler stamped.

    Contract:
      * NEVER calls GateResult.commit()
      * NEVER emits any event
      * NEVER writes any DB table
      * All exceptions swallowed; returns None always
    """
    # Once a category is cut over its new path IS the live path — there is
    # nothing to shadow-compare.  Skip early so the hook is a true no-op.
    try:
        from meshai.notifications.cutover import is_cutover
        if is_cutover(category):
            return
    except Exception:
        pass  # belt-and-suspenders: never let cutover import block shadow
    if not enabled_for(category):
        return
    try:
        from meshai.notifications.gating import get_decider
        decider = get_decider(category)
        if decider is None:
            # No decider registered yet (Phase 0 — all DECIDERS are empty).
            # Nothing to compare; exit silently.
            return

        # ---- Phase 1+: a real decider is registered ----
        # Extract old-path suffixes from the data dict the handler populated.
        old_sev = (data or {}).get("_severity_override")
        old_dedup = (data or {}).get("_dedup_suffix")
        old_cool = (data or {}).get("_cooldown_suffix")

        # Call the new decider.  data serves as a proxy for the full Event here
        # (both carry _dedup_suffix, _cooldown_suffix, _severity_override).
        # Phase 1 deciders that require a full Event object will need Hook 1
        # to be relocated to post-normalize; Phase 0 never reaches here.
        try:
            new_result = decider(data, now=now)
        except Exception:
            logger.debug(
                "shadow_gate: decider raised for category=%s", category, exc_info=True
            )
            return

        # SAFETY CHECKPOINT: we inspect new_result but NEVER call .commit().
        new_broadcast = new_result.broadcast if new_result is not None else False
        new_patch = (new_result.data_patch or {}) if new_result is not None else {}

        if old_broadcast != new_broadcast:
            record = {
                "tag": "SHADOW_MISMATCH",
                "type": "gate",
                "category": category,
                "source": source,
                "at": now,
                "old_broadcast": old_broadcast,
                "new_broadcast": new_broadcast,
                "old_severity_override": old_sev,
                "old_dedup_suffix": old_dedup,
                "old_cooldown_suffix": old_cool,
                "new_severity_override": new_patch.get("_severity_override"),
                "new_dedup_suffix": new_patch.get("_dedup_suffix"),
                "new_cooldown_suffix": new_patch.get("_cooldown_suffix"),
                "lifecycle": getattr(new_result, "lifecycle", ""),
                "reason": getattr(new_result, "reason", ""),
            }
            _append_jsonl(category, record)
            logger.debug(
                "shadow_gate MISMATCH cat=%s old=%s new=%s reason=%s",
                category, old_broadcast, new_broadcast,
                getattr(new_result, "reason", ""),
            )
    except Exception:
        # Belt-and-suspenders: any unexpected error is silently absorbed.
        logger.debug(
            "shadow_gate: unhandled error for category=%s", category, exc_info=True
        )


def shadow_render(category: str, event: "Event", *, old_wire: str) -> None:
    """Dry-run the new formatter for *category* and log wire-string mismatches.

    Called from dispatcher._dispatch_toggle_path, after compose_mesh_message
    has already produced *old_wire*.  The real path keeps using old_wire
    unchanged; this function only observes.

    Contract:
      * NEVER modifies old_wire or event
      * NEVER emits, commits, or writes any DB table
      * All exceptions swallowed; returns None always
    """
    # Once a category is cut over the new formatter IS composing old_wire —
    # there is nothing to compare against.  Skip early.
    try:
        from meshai.notifications.cutover import is_cutover
        if is_cutover(category):
            return
    except Exception:
        pass  # belt-and-suspenders: never let cutover import block shadow
    if not enabled_for(category):
        return
    try:
        from meshai.notifications.formatters import get_formatter
        formatter = get_formatter(category)
        if formatter is None:
            # No formatter registered yet (Phase 0 — all FORMATTERS are empty).
            return

        # ---- Phase 1+: a real formatter is registered ----
        from meshai.notifications.formatters._budget import budget_for
        budget = budget_for(getattr(event, "source", "") or "")
        try:
            new_wire = formatter(event, now=time.time(), budget=budget)
        except Exception:
            logger.debug(
                "shadow_render: formatter raised for category=%s",
                category, exc_info=True,
            )
            return

        if new_wire != old_wire:
            record = {
                "tag": "SHADOW_MISMATCH",
                "type": "render",
                "category": category,
                "event_id": getattr(event, "id", None),
                "at": time.time(),
                "old_wire": old_wire,
                "new_wire": new_wire,
            }
            _append_jsonl(category, record)
            logger.debug("shadow_render MISMATCH cat=%s", category)
    except Exception:
        logger.debug(
            "shadow_render: unhandled error for category=%s", category, exc_info=True
        )
