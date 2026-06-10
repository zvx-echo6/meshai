"""work_zone mesh-string renderer.

Consumes a `central_normalizer.normalize()` output dict (the data layer)
and produces a friendly mesh-broadcast string under an 80-byte UTF-8 cap.
Pure formatting; no adapter knowledge.

Format (per Matt-approved spec):

    🚧 <road> @ mile <start>[–<end>][, <dist> mi <bearing> of <town>]:
        <direction phrase>, <sub_type>[, ends <when>]

Optional segments are dropped wholesale (lowest-priority first) when the
byte budget is over:

    1. ends <when>      (lowest priority — dropped first)
    2. <bearing> of <town> distance segment
    3. <sub_type>
    4. <direction phrase>

Required: emoji + road. If even those overrun, the road is truncated
codepoint-safe with an ellipsis. Emojis count as 4 bytes each in UTF-8.
"""

from datetime import datetime, timedelta
from typing import Optional


_BYTE_BUDGET = 80


def _bytelen(s: str) -> int:
    return len(s.encode("utf-8"))


def _format_end_short(ends_at: Optional[datetime], now: Optional[datetime] = None) -> Optional[str]:
    """Format a future datetime as a tight mesh-friendly string.

    - within 24h        -> 'today 6pm' or 'tomorrow 9am'
    - within 7 days     -> 'Fri 6pm'
    - 7-365 days        -> 'Jun 15'
    - past or far future-> None (caller drops the segment)
    """
    if ends_at is None: return None
    if now is None: now = datetime.now()
    # Normalize to naive (renderer doesn't care about tz here; both inputs
    # should be Boise-local in practice since the upstream feed uses local).
    if ends_at.tzinfo is not None:
        ends_at = ends_at.replace(tzinfo=None)
    if now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    delta = ends_at - now
    if delta.total_seconds() < 0:
        return None
    hour = ends_at.hour
    minute = ends_at.minute
    if hour == 0:
        time_part = "12am" if minute == 0 else f"12:{minute:02d}am"
    elif hour < 12:
        time_part = f"{hour}am" if minute == 0 else f"{hour}:{minute:02d}am"
    elif hour == 12:
        time_part = "12pm" if minute == 0 else f"12:{minute:02d}pm"
    else:
        time_part = f"{hour-12}pm" if minute == 0 else f"{hour-12}:{minute:02d}pm"

    if delta < timedelta(hours=24) and ends_at.date() == now.date():
        return f"today {time_part}"
    if delta < timedelta(hours=48) and ends_at.date() == (now + timedelta(days=1)).date():
        return f"tomorrow {time_part}"
    if delta < timedelta(days=7):
        wd = ends_at.strftime("%a")  # 'Mon' .. 'Sun'
        return f"{wd} {time_part}"
    if delta < timedelta(days=365):
        return ends_at.strftime("%b %-d") if hasattr(datetime, "now") else ends_at.strftime("%b ") + str(ends_at.day)
    return None


def _format_direction_phrase(direction: Optional[str]) -> Optional[str]:
    """Render the normalized direction as a mesh-friendly noun phrase."""
    if not direction or direction == "unknown":
        return None
    if direction == "both":
        return "both directions"
    return direction


def _format_mile_segment(mile_start: Optional[int], mile_end: Optional[int]) -> Optional[str]:
    if mile_start is None: return None
    if mile_end is not None and mile_end != mile_start:
        return f"@ mile {mile_start}–{mile_end}"
    return f"@ mile {mile_start}"


def _format_distance_segment(distance_mi: Optional[int], bearing: Optional[str], town: Optional[str]) -> Optional[str]:
    if not town: return None
    # Issue 2 polish: distances under 1 mi are uninformative ("0 mi S of
    # McCall" reads worse than "near McCall"). Drop the distance/bearing
    # pair and fall back to plain "near <town>" form.
    if distance_mi is not None and bearing and distance_mi >= 1:
        return f"{distance_mi} mi {bearing} of {town}"
    return f"near {town}"


def _truncate_road(road: str, budget: int) -> str:
    """Truncate `road` to fit in `budget` UTF-8 bytes, codepoint-safe."""
    if _bytelen(road) <= budget:
        return road
    cut = road
    while cut and _bytelen(cut + "…") > budget:
        cut = cut[:-1]
    return cut + "…" if cut else "…"


def format_work_zone_mesh(n: dict, now: Optional[datetime] = None) -> str:
    """Render a normalized work_zone dict to a mesh-friendly string.

    Always returns a string; drops segments to fit the 80-byte cap.
    """
    emoji = "🚧"
    raw_road = n.get("road")
    town = n.get("town")

    # Issue 3d: when the road is uninformative (set None by the normalizer
    # for "Exit 80 Southbound On Ramp" shapes) AND we have a town, lead
    # with the town as the head instead of a useless placeholder.
    if raw_road:
        road = raw_road
        head = f"{emoji} {road}"
        suppress_distance_seg = False
    elif town:
        # "🚧 near <town>" or "🚧 <dist> mi <bearing> of <town>" as head.
        # distance/bearing are folded INTO the head; suppress the separate
        # distance segment below to avoid duplication.
        road = town  # used only by the last-resort road-truncation branch
        head = f"{emoji} {_format_distance_segment(n.get('distance_mi'), n.get('bearing'), town)}"
        suppress_distance_seg = True
    else:
        road = "Road event"
        head = f"{emoji} {road}"
        suppress_distance_seg = False

    mile_seg = _format_mile_segment(n.get("mile_start"), n.get("mile_end")) if raw_road else None
    dist_seg = None if suppress_distance_seg else \
        _format_distance_segment(n.get("distance_mi"), n.get("bearing"), town)
    dir_phrase = _format_direction_phrase(n.get("direction"))
    sub = n.get("sub_type")
    impact = n.get("impact")
    if impact == "full_closure":
        # Promote full-closure into the description slot so it's louder.
        sub = f"all lanes closed{' (' + sub + ')' if sub else ''}"
    ends_seg = _format_end_short(n.get("ends_at"), now=now)

    # Optional segments in build order; each has a drop_priority where
    # HIGHER number = dropped FIRST when over budget.
    Segment = tuple  # (drop_priority, joiner_before, text)
    segs: list[tuple[int, str, str]] = []
    if mile_seg:
        segs.append((10, " ",  mile_seg))      # mile segment is high-value, keep longest
    if dist_seg:
        segs.append((20, ", ", dist_seg))
    if dir_phrase or sub:
        segs.append((30, ": ", ""))             # marker for the colon transition
    if dir_phrase:
        segs.append((30, "",   dir_phrase))
    if sub:
        segs.append((40, ", " if dir_phrase else "", sub))
    if ends_seg:
        segs.append((50, ", ends ", ends_seg))

    # Iteratively assemble; drop highest-priority segments until under budget.
    kept = list(range(len(segs)))
    while True:
        out = head
        last_was_colon = False
        for i in kept:
            prio, joiner, text = segs[i]
            if text == "" and joiner == ": ":
                out += ": "
                last_was_colon = True
                continue
            # If we're right after a ": " marker, the first content segment
            # joins with no extra delimiter even if its joiner was ", ".
            if last_was_colon:
                out += text
                last_was_colon = False
            else:
                out += joiner + text
        if _bytelen(out) <= _BYTE_BUDGET:
            return out
        # Find the highest-priority kept segment with non-empty content;
        # drop it (and its preceding colon marker if it was the only one
        # past the colon).
        droppable = [i for i in kept if segs[i][2] != ""]
        if not droppable:
            # All optional segments gone; truncate road.
            budget_for_road = _BYTE_BUDGET - len((emoji + " ").encode("utf-8"))
            return f"{emoji} {_truncate_road(road, budget_for_road)}"
        worst = max(droppable, key=lambda i: segs[i][0])
        kept.remove(worst)
        # If we dropped both dir_phrase and sub, remove the ": " marker too.
        remaining_after_colon = any(
            segs[i][2] != "" for i in kept
            if any(segs[j][0] == 30 and segs[j][2] == "" for j in range(len(segs)) if j < i)
        )
        if not remaining_after_colon:
            kept = [i for i in kept if not (segs[i][2] == "" and segs[i][1] == ": ")]
