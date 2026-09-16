"""Shared per-adapter mesh packet budget helpers.

Every broadcast handler fits its final wire string to the live mesh
transport's single-packet character budget. main.py injects the active
transport's `max_chars` (140 for the current LoRa configs) into
adapter_config via set_runtime_override for each broadcast adapter, so
`budget_for(adapter)` returns the runtime value durably across cache
invalidation. Default 140 when no override is present (e.g. unit tests).
"""
from __future__ import annotations

from meshai.adapter_config import adapter_config


def budget_for(adapter: str, default: int = 140) -> int:
    """Per-adapter mesh packet budget. Reads adapter_config.<adapter>.single_packet_max_chars,
    which main.py overrides at runtime to the live transport max_chars (140). Default 140."""
    try:
        return int(getattr(getattr(adapter_config, adapter), "single_packet_max_chars", default))
    except Exception:
        return default


def fit_to_budget(s: str, limit: int) -> str:
    """Trim s to <= limit chars at a word boundary, appending an ellipsis. Never chops a word mid-word."""
    if len(s) <= limit:
        return s
    cut = s[: max(0, limit - 1)].rstrip()
    cut = cut.rsplit(" ", 1)[0] if " " in cut else cut
    if not cut:
        cut = s[: max(0, limit - 1)]
    return cut.rstrip() + "…"


def fit_to_budget_with_suffix(body: str, suffix: str, limit: int) -> str:
    """Fit `body` to `limit`, reserving room for a `suffix` that must survive
    intact on its own trailing line (e.g. a Watch Duty incident link).

    Reserves len(suffix) + 1 (the "\\n" joiner) off the top, fits ONLY the
    body into what remains via `fit_to_budget`, then joins body + "\\n" +
    suffix. The suffix is NEVER truncated: if there isn't room for even an
    empty body plus the suffix, returns the suffix alone (which may then
    exceed `limit` -- that's preferred to cutting the link).
    """
    body_limit = limit - len(suffix) - 1
    if body_limit <= 0:
        return suffix
    fitted_body = fit_to_budget(body, body_limit)
    return f"{fitted_body}\n{suffix}"
