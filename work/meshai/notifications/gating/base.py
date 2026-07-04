"""GateResult — the return type for all gating/budgeting decision functions.

A decider registered in meshai.notifications.gating.DECIDERS receives an
Event and returns a GateResult.  The dispatcher inspects `broadcast` to
decide whether to send, merges `data_patch` into event.data, and calls
`commit(now)` exactly once per channel on confirmed delivery.

Deferred-commit contract
------------------------
`commit(now: float)` is called ONCE, ONLY after the message has been
successfully delivered to a mesh channel.  Rules:

  * Idempotent: the callback MUST use UPSERTs (INSERT OR REPLACE / ON
    CONFLICT DO UPDATE), never bare INSERTs, so a retry on failure does
    not create duplicate rows.

  * One call per channel: if the same event is broadcast on N mesh
    channels, commit is called N times (once per successful deliver()).
    The UPSERT requirement makes that safe.

  * No exceptions may escape commit() — wrap all DB writes in try/except
    and log failures; a crashing commit would abort the send loop.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class GateResult:
    """Decision from a gating function.

    Attributes:
        broadcast: True → send the event; False → suppress.
        lifecycle: Short label for logging/audit (e.g. "new", "update",
                   "cooldown", "suppress").
        reason:    Human-readable explanation used in debug logs.
        data_patch: Dict merged into event.data before the message is
                    rendered and sent (use for attaching callbacks, etc.).
        commit:    Optional callable invoked once per channel on confirmed
                   delivery.  Signature: commit(now: float) -> None.
                   See module docstring for the full contract.
    """
    broadcast: bool
    lifecycle: str = ""
    reason: str = ""
    data_patch: dict = field(default_factory=dict)
    commit: Optional[Callable[[float], None]] = None
