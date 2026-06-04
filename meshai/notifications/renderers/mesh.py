"""Mesh channel renderer.

Produces a list of short strings (each ≤200 chars by default)
suitable for mesh radio broadcast. Reuses the digest's chunking
pattern: never split a single "line" across chunks; add (k/N)
counters when the rendered output produces more than one chunk.

The mesh renderer is symmetric with the digest accumulator's
mesh chunk renderer — same chunk-packing algorithm, same char
limit semantics.
"""

import logging
from typing import Optional

from meshai.notifications.events import NotificationPayload
from meshai.notifications.renderers.base import Renderer


class MeshRenderer(Renderer):
    """Produce mesh-compact chunks for a single payload."""

    def __init__(self, char_limit: int = 200):
        self._limit = char_limit
        self._logger = logging.getLogger("meshai.renderers.mesh")

    def render(self, payload: NotificationPayload) -> list[str]:
        """Render the payload as 1+ mesh-compact chunks.

        Algorithm:
          - Build the full message line (event_type, severity hint,
            and message text — see _format_one_line)
          - If the line fits in self._limit chars, return [line].
          - If the line exceeds self._limit, split across multiple
            chunks at word boundaries when possible, and add
            "(k/N)" counters at the start of each chunk.
          - If a single word exceeds the chunk limit, hard-split
            mid-word (rare — long URLs etc.)
        """
        line = self._format_one_line(payload)
        if len(line) <= self._limit:
            return [line]

        return self._chunk_long_line(line)

    def _format_one_line(self, p: NotificationPayload) -> str:
        """Return the payload message verbatim for mesh delivery.

        v0.5.7-regression: previously prepended "[<Family>] " (the legacy
        v0.5.0 debug format) to every payload. Since v0.5.2 the dispatcher
        hands `payload.message` from compose_mesh_message(), whose output
        ALREADY starts with the category emoji + family label (e.g.
        "🚨 ROADS: ...", "🔥 FIRE: ..."). The renderer's wrap produced a
        visually-broken duplicate "[Roads] 🚨 ROADS: ..." that hit the live
        mesh during the v0.5.7 staged flip. The bug had been dormant since
        v0.5.2 because no live broadcasts had ever fired in production
        (safe-mode held the whole time).

        Now: pass `payload.message` through unchanged. The composer is the
        single source of truth for mesh formatting. `_toggle_label` and the
        `TOGGLE_LABELS` table below are kept because the digest renderer
        still uses them (see meshai/notifications/pipeline/digest.py) for
        the multi-line summary format -- do not remove them here.
        """
        return p.message or ""

    def _toggle_label(self, event_type: Optional[str]) -> Optional[str]:
        """Map an event category to a short toggle label.

        Looks up the toggle the category belongs to and returns
        the toggle's display label. If unknown, returns None
        (no prefix added).
        """
        if not event_type:
            return None
        from meshai.notifications.categories import get_toggle
        toggle = get_toggle(event_type)
        if not toggle:
            return None
        # Same label set used by the digest renderer
        TOGGLE_LABELS = {
            "mesh_health": "Mesh",
            "weather": "Weather",
            "fire": "Fire",
            "rf_propagation": "RF",
            "roads": "Roads",
            "avalanche": "Avalanche",
            "seismic": "Seismic",
            "tracking": "Tracking",
            "other": "Other",
        }
        return TOGGLE_LABELS.get(toggle, toggle.title())

    def _chunk_long_line(self, line: str) -> list[str]:
        """Split a long line into chunks ≤ self._limit each.

        Reserves ~10 chars per chunk for the "(k/N)" counter
        suffix. Splits at word boundaries; falls back to
        mid-word split if a single word exceeds the budget.
        """
        # Reserve space for " (k/N)" — generous to 10 chars
        body_budget = self._limit - 10
        if body_budget <= 0:
            body_budget = self._limit  # extreme small limit; ignore counter

        words = line.split(" ")
        chunks: list[str] = []
        current = ""
        for word in words:
            # Hard-split words longer than body_budget (rare)
            while len(word) > body_budget:
                if current:
                    chunks.append(current)
                    current = ""
                chunks.append(word[:body_budget])
                word = word[body_budget:]
            # Add word to current chunk if it fits
            tentative = (current + " " + word) if current else word
            if len(tentative) <= body_budget:
                current = tentative
            else:
                chunks.append(current)
                current = word
        if current:
            chunks.append(current)

        # Add counters: "DIGEST"-style "(k/N)" suffix
        total = len(chunks)
        if total == 1:
            return chunks
        return [f"{chunk} ({i+1}/{total})" for i, chunk in enumerate(chunks)]
