"""Staged cutover gate for migrated hazard categories.

A migrated category (quake, swpc geomag/flare, avalanche) is built in Phase-1
with a formatter+decider registered but initially runs SHADOW-ONLY on deploy:
  - The OLD handler path is used for the LIVE broadcast.
  - The new formatter+decider run dry-run inside shadow hooks, logging any
    SHADOW_MISMATCH records to /app/data/shadow/<category>.jsonl.

Once a category has been validated in shadow (mismatches investigated and
resolved), it is EXPLICITLY cut over by adding its name to the env var:

    MESHAI_CUTOVER_CATEGORIES=earthquake_event

At that point:
  - The new formatter is used by compose_mesh_message for the LIVE render.
  - The new decider is used by the bridges and store._emit_event.
  - Shadow hooks skip the category (nothing left to compare).

Syntax:  comma-separated category names, e.g.
    MESHAI_CUTOVER_CATEGORIES=earthquake_event,geomagnetic_storm

Empty / unset → NO category cut over (shadow-only bake state).

API:
    is_cutover(category: str) -> bool
    _clear_cache() -> None            (tests only — forces re-parse after env change)
"""
from __future__ import annotations

import functools
import os

# ── Native-adapter always-decide/always-render set ───────────────────────────
# The native WFIGS fire path (env/fires.py) must run the shared gating decider
# AND the shared fire formatter DETERMINISTICALLY, independent of the shadow-bake
# MESHAI_CUTOVER_CATEGORIES env var: the decider IS the fire gate (fires table +
# 8h cooldown), so it cannot be left to an operator toggle. These categories are
# emitted ONLY by the native fire adapter in this standalone deployment (Central
# is off, feed_source=native), so forcing decider+formatter here has no
# Central-render impact. Consumed by store._emit_event (decider gate) and
# renderers.composer.compose_mesh_message (formatter gate) so native fires take
# the exact same single render path a cut-over category would.
#
# stream_flood_warning / stream_high_water (env/usgs.py) are the same shape of
# problem: they are emitted ONLY by the native USGS adapter (the Central nwis
# path emits the separate `stream_flow` category instead -- see
# notifications/gating/__init__.py and notifications/formatters/__init__.py).
# Before this fix neither category had a registered decider/formatter at all,
# so every elevated gauge reading was silently dropped (no formatter -> no
# renderable wire; get_toggle() resolving to "seismic" made no difference
# since nothing was registered under that name either). Forcing them onto the
# shared hydro decider+formatter here, like fire above, has no Central-render
# impact (Central never emits these two category strings).
NATIVE_ALWAYS_DECIDE = frozenset(
    {
        "wildfire_incident", "wildfire_declared", "wildfire_closed",
        "stream_flood_warning", "stream_high_water",
    }
)


@functools.lru_cache(maxsize=1)
def _load_cutover_categories() -> frozenset:
    """Parse MESHAI_CUTOVER_CATEGORIES into a frozenset.  Cached after first call.

    Call _clear_cache() (e.g. from tests) to force re-parse after mutating the
    env var.
    """
    val = os.environ.get("MESHAI_CUTOVER_CATEGORIES", "")
    if not val.strip():
        return frozenset()
    return frozenset(c.strip() for c in val.split(",") if c.strip())


def _clear_cache() -> None:
    """Clear the lru_cache so tests can mutate MESHAI_CUTOVER_CATEGORIES."""
    _load_cutover_categories.cache_clear()


def is_cutover(category: str) -> bool:
    """Return True iff *category* has been explicitly cut over to the new path.

    Fast path: returns False immediately when the env var is unset (default
    deploy state — no category is cut over).
    """
    return category in _load_cutover_categories()
