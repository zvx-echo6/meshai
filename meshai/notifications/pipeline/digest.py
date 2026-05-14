"""Digest accumulator and renderer for Phase 2.3a.

Holds priority and routine events between digest emissions, tracks
active vs recently-resolved events, and renders the two-section
digest output (ACTIVE NOW + SINCE LAST DIGEST) when called.

No scheduling logic here. render_digest() is called explicitly by
the future scheduler (Phase 2.3b) or by tests.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from meshai.notifications.events import Event
from meshai.notifications.categories import get_toggle


# Lowercase substrings in event.title that indicate the event is
# a resolution of a prior alert. Conservative list — easy to extend.
RESOLUTION_MARKERS = (
    "cleared",
    "reopened",
    "ended",
    "resolved",
    "back online",
    "recovered",
    "lifted",
)

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


@dataclass
class Digest:
    """Result of render_digest(). Carries both sections and metadata."""
    rendered_at: float
    active: dict[str, list[Event]] = field(default_factory=dict)
    since_last: dict[str, list[Event]] = field(default_factory=dict)
    mesh_chunks: list[str] = field(default_factory=list)
    mesh_compact: str = ""
    full: str = ""

    def is_empty(self) -> bool:
        return not self.active and not self.since_last


class DigestAccumulator:
    """Tracks priority/routine events and produces periodic digests.

    Args:
        mesh_char_limit: Maximum characters per mesh chunk (default 200).
        include_toggles: List of toggle names to include in digest output.
            If None, defaults to all toggles in TOGGLE_ORDER except
            rf_propagation. Unknown toggle names in the list are silently
            accepted (TOGGLE_ORDER drives display order, include_toggles
            drives which toggles are tracked).
    """

    def __init__(
        self,
        mesh_char_limit: int = 200,
        include_toggles: list[str] | None = None,
    ):
        self._active: dict[str, list[Event]] = {}  # toggle -> events
        self._since_last: dict[str, list[Event]] = {}  # toggle -> events
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
        """SeverityRouter calls this for priority/routine events."""
        toggle = get_toggle(event.category) or "other"

        # Skip non-included toggles
        if toggle not in self._included:
            self._logger.debug(
                f"skipping digest enqueue for non-included toggle {toggle}"
            )
            return

        active_for_toggle = self._active.setdefault(toggle, [])

        # Resolution detection
        if self._is_resolution(event, self._now()):
            self._move_to_since_last_by_group(event, toggle)
            return

        # In-place update if same id
        for i, existing in enumerate(active_for_toggle):
            if existing.id == event.id:
                active_for_toggle[i] = event
                self._logger.debug(
                    f"UPDATED active event {event.id} in {toggle}"
                )
                return

        # Otherwise it's a new active event
        active_for_toggle.append(event)
        self._logger.debug(
            f"ADDED active event {event.id} ({toggle}/{event.category})"
        )

    def tick(self, now: Optional[float] = None) -> int:
        """Move expired events from active to since_last.

        Returns the number of events moved.
        """
        if now is None:
            now = self._now()
        moved = 0
        for toggle in list(self._active.keys()):
            still_active = []
            for ev in self._active[toggle]:
                if ev.expires is not None and ev.expires <= now:
                    self._since_last.setdefault(toggle, []).append(ev)
                    moved += 1
                else:
                    still_active.append(ev)
            self._active[toggle] = still_active
        return moved

    # ---- rendering ----

    def render_digest(self, now: Optional[float] = None) -> Digest:
        """Produce a Digest of current state, then clear since_last."""
        if now is None:
            now = self._now()
        # tick() first so expired actives roll into since_last
        self.tick(now)

        digest = Digest(rendered_at=now)
        # Defensive: skip non-included toggles when building output
        digest.active = {
            k: list(v) for k, v in self._active.items()
            if v and k in self._included
        }
        digest.since_last = {
            k: list(v) for k, v in self._since_last.items()
            if v and k in self._included
        }
        digest.mesh_chunks = self._render_mesh_chunks(digest, now)
        # mesh_compact: join chunks for backward compatibility
        if len(digest.mesh_chunks) == 1:
            digest.mesh_compact = digest.mesh_chunks[0]
        else:
            digest.mesh_compact = "\n---\n".join(digest.mesh_chunks)
        digest.full = self._render_full(digest, now)

        # Clear since_last; active stays for the next cycle
        self._since_last.clear()
        self._last_digest_at = now
        return digest

    def _render_mesh_chunks(self, digest: Digest, now: float) -> list[str]:
        """Produce mesh-radio-friendly compact chunks.

        Returns a list of strings, each ≤ self._mesh_char_limit chars.
        Single-chunk output has no "(1/N)" suffix. Multi-chunk output
        has "(k/N)" counters and "(cont)" suffixes on section headers
        that span chunks.
        """
        time_str = time.strftime('%H%M', time.localtime(now))

        # Empty digest case
        if not digest.active and not digest.since_last:
            return [f"DIGEST {time_str}\nNo alerts since last digest."]

        # Build logical lines with section markers
        # Each item is (section, line) where section is "active", "resolved", or None
        logical_lines: list[tuple[str | None, str]] = []

        if digest.active:
            logical_lines.append(("active", "ACTIVE NOW"))
            for toggle in TOGGLE_ORDER:
                events = digest.active.get(toggle)
                if not events:
                    continue
                logical_lines.append(("active", self._compact_toggle_line(toggle, events)))

        if digest.since_last:
            logical_lines.append(("resolved", "RESOLVED"))
            for toggle in TOGGLE_ORDER:
                events = digest.since_last.get(toggle)
                if not events:
                    continue
                logical_lines.append(("resolved", self._compact_toggle_line(toggle, events)))

        # Pack lines into chunks
        return self._pack_lines_into_chunks(logical_lines, time_str)

    def _pack_lines_into_chunks(
        self,
        logical_lines: list[tuple[str | None, str]],
        time_str: str,
    ) -> list[str]:
        """Pack logical lines into chunks respecting char limit.

        Args:
            logical_lines: List of (section, line) tuples where section
                is "active", "resolved", or None for headers.
            time_str: Time string for headers (e.g., "0700").

        Returns:
            List of chunk strings, each ≤ self._mesh_char_limit.
        """
        if not logical_lines:
            return [f"DIGEST {time_str}\nNo alerts since last digest."]

        limit = self._mesh_char_limit
        chunks: list[list[str]] = []  # List of line lists
        current_chunk: list[str] = []
        current_len = 0
        last_section_in_chunk: str | None = None
        sections_started: set[str] = set()

        # Placeholder header - will be fixed up later
        header_placeholder = f"DIGEST {time_str}"

        def start_new_chunk():
            nonlocal current_chunk, current_len, last_section_in_chunk
            if current_chunk:
                chunks.append(current_chunk)
            current_chunk = [header_placeholder]
            current_len = len(header_placeholder)
            last_section_in_chunk = None

        start_new_chunk()

        i = 0
        while i < len(logical_lines):
            section, line = logical_lines[i]
            is_section_header = line in ("ACTIVE NOW", "RESOLVED")

            # Check if this is a section header - ensure it has at least one
            # toggle line following it in this chunk
            if is_section_header:
                # Look ahead for the next toggle line
                next_toggle_idx = i + 1
                if next_toggle_idx < len(logical_lines):
                    _, next_line = logical_lines[next_toggle_idx]
                    # Calculate space needed for header + newline + next line
                    needed = len(line) + 1 + len(next_line)
                    if current_len + 1 + needed > limit:
                        # Section header + next line won't fit, start new chunk
                        start_new_chunk()
                        sections_started.add(section)
                        last_section_in_chunk = section
                        current_chunk.append(line)
                        current_len += 1 + len(line)
                        i += 1
                        continue

            # Calculate line length with newline
            line_with_newline = 1 + len(line)  # newline before line

            # Would this line fit?
            if current_len + line_with_newline > limit:
                # Start new chunk
                start_new_chunk()

                # If continuing a section, add "(cont)" header
                if section and section in sections_started and not is_section_header:
                    cont_header = "ACTIVE NOW (cont)" if section == "active" else "RESOLVED (cont)"
                    current_chunk.append(cont_header)
                    current_len += 1 + len(cont_header)
                    last_section_in_chunk = section

            # Add the line
            if is_section_header:
                sections_started.add(section)
                last_section_in_chunk = section
            current_chunk.append(line)
            current_len += 1 + len(line)
            i += 1

        # Don't forget the last chunk
        if current_chunk and len(current_chunk) > 1:  # More than just header
            chunks.append(current_chunk)
        elif current_chunk and len(current_chunk) == 1:
            # Only header in chunk - shouldn't happen but handle gracefully
            if chunks:
                # Merge with previous chunk if possible
                pass
            else:
                chunks.append(current_chunk)

        # Fix up headers with chunk counts
        total_chunks = len(chunks)
        result: list[str] = []

        for idx, chunk_lines in enumerate(chunks):
            # Fix header line
            if total_chunks == 1:
                chunk_lines[0] = f"DIGEST {time_str}"
            else:
                chunk_lines[0] = f"DIGEST {time_str} ({idx + 1}/{total_chunks})"
            result.append("\n".join(chunk_lines))

        return result if result else [f"DIGEST {time_str}\nNo alerts since last digest."]

    def _compact_toggle_line(self, toggle: str, events: list[Event]) -> str:
        """Build one compact line for a toggle: [Label] headline (+N)"""
        label = TOGGLE_LABELS.get(toggle, toggle)
        sorted_events = self._sort_events(events)
        top_event = sorted_events[0]

        # Get headline text
        headline = top_event.summary or top_event.title or top_event.category

        # Truncate headline at ~60 chars to keep lines readable
        max_headline = 60
        if len(headline) > max_headline:
            headline = headline[:max_headline - 1] + "…"

        # Append (+N) if more than one event
        overflow = len(events) - 1
        if overflow > 0:
            return f"[{label}] {headline} (+{overflow})"
        else:
            return f"[{label}] {headline}"

    def _render_full(self, digest: Digest, now: float) -> str:
        """Produce the full multi-line digest for email/webhook."""
        lines = [
            f"--- {time.strftime('%H%M', time.localtime(now))} Digest ---",
            "",
        ]

        if not digest.active and not digest.since_last:
            lines.append("No alerts since last digest.")
            lines.append("")
        else:
            if digest.active:
                lines.append("ACTIVE NOW:")
                for toggle in TOGGLE_ORDER:
                    events = digest.active.get(toggle)
                    if not events:
                        continue
                    label = TOGGLE_LABELS.get(toggle, toggle)
                    for ev in self._sort_events(events):
                        lines.append(f"  [{label}] {self._format_event_line(ev)}")
                lines.append("")

            if digest.since_last:
                lines.append("SINCE LAST DIGEST:")
                for toggle in TOGGLE_ORDER:
                    events = digest.since_last.get(toggle)
                    if not events:
                        continue
                    label = TOGGLE_LABELS.get(toggle, toggle)
                    for ev in self._sort_events(events):
                        lines.append(f"  [{label}] {self._format_event_line(ev)}")
                lines.append("")

        return "\n".join(lines).rstrip() + "\n"

    def _format_event_line(self, event: Event) -> str:
        """Single-line summary of an event for digest output."""
        # Prefer event.summary if set, else fall back to title, then category
        text = event.summary or event.title or event.category
        # Trim runaway text — keep digest readable
        if len(text) > 140:
            text = text[:139] + "…"
        return text

    def _sort_events(self, events: list[Event]) -> list[Event]:
        """Sort within a toggle: immediate first, then priority,
        then routine, then by timestamp newest first."""
        rank = {"immediate": 0, "priority": 1, "routine": 2}
        return sorted(
            events,
            key=lambda e: (rank.get(e.severity, 3), -e.timestamp),
        )

    # ---- helpers ----

    def _is_resolution(self, event: Event, now: float) -> bool:
        if event.expires is not None and event.expires <= now:
            return True
        title_lc = (event.title or "").lower()
        return any(marker in title_lc for marker in RESOLUTION_MARKERS)

    def _move_to_since_last_by_group(self, event: Event, toggle: str) -> None:
        """Remove any active event matching event's group_key (or id)
        and place this resolution event into since_last.
        """
        active_list = self._active.get(toggle, [])
        # Match by group_key if set, else by id
        match_key = event.group_key
        if match_key:
            self._active[toggle] = [
                e for e in active_list
                if e.group_key != match_key
            ]
        else:
            self._active[toggle] = [
                e for e in active_list if e.id != event.id
            ]
        self._since_last.setdefault(toggle, []).append(event)
        self._logger.debug(
            f"RESOLVED in {toggle}: {event.id} ({event.title!r})"
        )

    def _now(self) -> float:
        return time.time()

    # ---- inspection (for tests and future scheduler) ----

    def active_count(self, toggle: Optional[str] = None) -> int:
        if toggle is not None:
            return len(self._active.get(toggle, []))
        return sum(len(v) for v in self._active.values())

    def since_last_count(self, toggle: Optional[str] = None) -> int:
        if toggle is not None:
            return len(self._since_last.get(toggle, []))
        return sum(len(v) for v in self._since_last.values())

    def last_digest_at(self) -> float:
        return self._last_digest_at

    def clear(self) -> None:
        self._active.clear()
        self._since_last.clear()
        self._last_digest_at = 0.0
