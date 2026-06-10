"""Digest accumulator and renderer for Phase 2.4.

Logs all events between digest emissions and renders LLM-summarized
digest output per toggle. No active/resolved tracking — just a
chronological log that the LLM summarizes.

render_digest() is async and calls the LLM once per non-empty toggle.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, TYPE_CHECKING

from meshai.notifications.events import Event
from meshai.notifications.categories import get_toggle

if TYPE_CHECKING:
    from meshai.backends.base import LLMBackend


# Display labels per toggle (used in rendered output)
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

# Toggle sort order in digest output (most operationally urgent first)
TOGGLE_ORDER = [
    "weather",
    "fire",
    "seismic",
    "avalanche",
    "roads",
    "rf_propagation",
    "mesh_health",
    "tracking",
    "other",
]

# System prompt for digest summarization
DIGEST_SYSTEM_PROMPT = (
    "You are summarizing a category of mesh-network alerts for a "
    "morning digest broadcast. Given a list of events in chronological "
    "order (immediate severity first, then priority, then routine), "
    "produce ONE SHORT LINE summarizing what happened. "
    "Be specific about node IDs, places, and counts when present. "
    "Aim for 80-140 characters. Do not use markdown. No bullet points. "
    "Plain prose. End with a period."
)


@dataclass
class Digest:
    """Result of render_digest(). Carries sections and metadata."""
    rendered_at: float
    # Keep these fields for type compatibility; populated empty in Phase 2.4+
    active: dict[str, list[Event]] = field(default_factory=dict)
    since_last: dict[str, list[Event]] = field(default_factory=dict)
    mesh_chunks: list[str] = field(default_factory=list)
    mesh_compact: str = ""
    full: str = ""

    def is_empty(self) -> bool:
        return not self.mesh_chunks or (
            len(self.mesh_chunks) == 1 and "No alerts" in self.mesh_chunks[0]
        )


class DigestAccumulator:
    """Logs events and produces LLM-summarized periodic digests.

    Args:
        llm_backend: LLM backend for generating summaries. If None,
            falls back to count-based summaries.
        include_toggles: List of toggle names to include in digest output.
            If None, defaults to all toggles in TOGGLE_ORDER except
            rf_propagation.
        mesh_char_limit: Maximum characters per mesh chunk (default 200).
    """

    def __init__(
        self,
        llm_backend: Optional["LLMBackend"] = None,
        include_toggles: list[str] | None = None,
        mesh_char_limit: int = 200,
    ):
        self._llm = llm_backend
        self._events_since_last_digest: dict[str, list[Event]] = {}
        self._last_digest_at: float = 0.0
        self._mesh_char_limit = mesh_char_limit
        # Default: all known toggles except rf_propagation
        if include_toggles is None:
            self._included = set(TOGGLE_ORDER) - {"rf_propagation"}
        else:
            self._included = set(include_toggles)
        self._logger = logging.getLogger("meshai.pipeline.digest")

    # ---- ingress ----

    def enqueue(self, event: Event) -> None:
        """Log an event for the next digest."""
        toggle = get_toggle(event.category) or "other"

        # Skip non-included toggles
        if toggle not in self._included:
            self._logger.debug(
                f"skipping digest enqueue for non-included toggle {toggle}"
            )
            return

        # Append to the event log
        self._events_since_last_digest.setdefault(toggle, []).append(event)
        self._logger.debug(
            f"LOGGED event {event.id} ({toggle}/{event.category}/{event.severity})"
        )

    def tick(self, now: Optional[float] = None) -> int:
        """No-op in Phase 2.4+. Returns 0."""
        return 0

    # ---- rendering ----

    async def render_digest(self, now: Optional[float] = None) -> Digest:
        """Produce a Digest with LLM-summarized lines per toggle.

        Calls the LLM once per toggle that had activity. Empty toggles
        produce no line. Clears the event log after rendering.
        """
        if now is None:
            now = self._now()

        digest = Digest(rendered_at=now)
        time_str = time.strftime('%H%M', time.localtime(now))

        # Build summary lines per toggle
        summary_lines: list[str] = []

        for toggle in TOGGLE_ORDER:
            events = self._events_since_last_digest.get(toggle, [])
            if not events:
                continue
            if toggle not in self._included:
                continue

            label = TOGGLE_LABELS.get(toggle, toggle)
            summary = await self._summarize_toggle(toggle, events, now)
            summary_lines.append(f"[{label}] {summary}")

        # Render outputs
        if summary_lines:
            digest.mesh_chunks = self._render_mesh_chunks(summary_lines, time_str)
            digest.full = self._render_full(summary_lines, time_str)
        else:
            digest.mesh_chunks = [f"DIGEST {time_str}\nNo alerts since last digest."]
            digest.full = f"--- {time_str} Digest ---\n\nNo alerts since last digest.\n"

        # mesh_compact for backward compatibility
        if len(digest.mesh_chunks) == 1:
            digest.mesh_compact = digest.mesh_chunks[0]
        else:
            digest.mesh_compact = "\n---\n".join(digest.mesh_chunks)

        # Clear event log
        self._events_since_last_digest.clear()
        self._last_digest_at = now

        return digest

    async def _summarize_toggle(
        self,
        toggle: str,
        events: list[Event],
        now: float,
    ) -> str:
        """Generate a one-line summary for a toggle's events."""
        # Sort by severity (immediate=0, priority=1, routine=2), then timestamp
        severity_rank = {"immediate": 0, "priority": 1, "routine": 2}
        sorted_events = sorted(
            events,
            key=lambda e: (severity_rank.get(e.severity, 3), e.timestamp),
        )

        # Build LLM input
        lines = [f"Category: {toggle}", "Events:"]
        for ev in sorted_events:
            lines.append(self._format_event_for_llm(ev))
        llm_input = "\n".join(lines)

        # Try LLM summarization
        if self._llm is not None:
            try:
                response = await self._llm.generate(
                    messages=[{"role": "user", "content": llm_input}],
                    system_prompt=DIGEST_SYSTEM_PROMPT,
                    max_tokens=200,
                )
                # Take first line only
                summary = response.strip().split("\n")[0].strip()
                if summary:
                    return summary
            except Exception as e:
                self._logger.warning(f"LLM summarization failed for {toggle}: {e}")

        # Fallback: count-based summary
        return f"{len(events)} event(s) (LLM unavailable)"

    def _format_event_for_llm(self, event: Event) -> str:
        """Format one event for LLM input."""
        ts = datetime.fromtimestamp(event.timestamp)
        time_str = ts.strftime("%H:%M")
        severity = event.severity.upper()

        # Combine title and summary
        text = event.title or ""
        if event.summary and event.summary != event.title:
            if text:
                text = f"{text} — {event.summary}"
            else:
                text = event.summary
        if not text:
            text = event.category

        # Truncate long text
        if len(text) > 120:
            text = text[:117] + "..."

        return f"- [{severity} {time_str}] {text}"

    def _render_mesh_chunks(
        self,
        summary_lines: list[str],
        time_str: str,
    ) -> list[str]:
        """Pack summary lines into mesh-friendly chunks."""
        limit = self._mesh_char_limit
        chunks: list[list[str]] = []
        current_chunk: list[str] = []
        current_len = 0

        # Placeholder header
        header = f"DIGEST {time_str}"

        def start_new_chunk():
            nonlocal current_chunk, current_len
            if current_chunk:
                chunks.append(current_chunk)
            current_chunk = [header]
            current_len = len(header)

        start_new_chunk()

        for line in summary_lines:
            line_len = 1 + len(line)  # newline + line
            if current_len + line_len > limit:
                start_new_chunk()
            current_chunk.append(line)
            current_len += line_len

        # Don't forget the last chunk
        if current_chunk and len(current_chunk) > 1:
            chunks.append(current_chunk)

        # Fix up headers with chunk counts
        total = len(chunks)
        result: list[str] = []
        for idx, chunk_lines in enumerate(chunks):
            if total == 1:
                chunk_lines[0] = f"DIGEST {time_str}"
            else:
                chunk_lines[0] = f"DIGEST {time_str} ({idx + 1}/{total})"
            result.append("\n".join(chunk_lines))

        return result if result else [f"DIGEST {time_str}\nNo alerts since last digest."]

    def _render_full(self, summary_lines: list[str], time_str: str) -> str:
        """Produce full multi-line digest for email/webhook."""
        lines = [
            f"--- {time_str} Digest ---",
            "",
        ]
        lines.extend(summary_lines)
        lines.append("")
        return "\n".join(lines)

    def _now(self) -> float:
        return time.time()

    # ---- inspection (for tests and scheduler) ----

    def event_count(self, toggle: Optional[str] = None) -> int:
        """Count events logged since last digest."""
        if toggle is not None:
            return len(self._events_since_last_digest.get(toggle, []))
        return sum(len(v) for v in self._events_since_last_digest.values())

    def last_digest_at(self) -> float:
        return self._last_digest_at

    def clear(self) -> None:
        self._events_since_last_digest.clear()
        self._last_digest_at = 0.0

    # Legacy compatibility — return 0 for old tests
    def active_count(self, toggle: Optional[str] = None) -> int:
        return 0

    def since_last_count(self, toggle: Optional[str] = None) -> int:
        return 0
