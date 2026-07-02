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
