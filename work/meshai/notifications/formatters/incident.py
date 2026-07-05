"""Incident/Roads event formatter — Phase-2 reference implementation.

Absorbs two legacy renderers into one entry-point dispatched by event.category:

  category == "work_zone"
      → _render_work_zone(): replicates renderers.work_zone.format_work_zone_mesh()
        byte-for-byte.  80-byte UTF-8 cap (internal constant); the outer
        ``budget`` parameter is unused on this path (the 80-byte cap is
        inherent to the mesh format for work-zone events).

  category in ("road_incident", "road_closure", "traffic_congestion", other)
      → _render_incident(): replicates incident_handler._render() byte-for-byte.
        Fitted to the injected ``budget`` character limit.

Canonical event.data schemas
----------------------------
Work-zone events (category == "work_zone"):
    road          str | None   — normalised road name
    direction     str | None   — full form: "northbound"/"southbound"/…/"both"/"unknown"
    mile_start    int | None
    mile_end      int | None
    sub_type      str | None   — human-readable: "paving", "road construction", …
    impact        str | None   — "full_closure" | "partial"
    ends_at_epoch float | None — ends_at.timestamp() (reconstructed to datetime in formatter)
    town          str | None   — pre-computed anchor (populated by bridge/normalizer)
    distance_mi   int | None
    bearing       str | None
    lat           float | None — raw coords for resolve_anchor fallback
    lon           float | None

Incident events (other categories):
    external_id   str
    source        str
    sub_type      str          — snake_case: "accident", "road_works", "closure", …
    road          str | None
    direction     str | None   — short: "N"/"S"/"E"/"W"/"both"
    from_loc      str | None
    to_loc        str | None
    mile_marker   float | None
    lanes_affected str | None
    comment       str | None
    impact        str | None   — "all lanes closed" | None
    county        str | None
    state         str | None
    lat           float | None
    lon           float | None
    geocoder_city str | None
    landclass     str | None
    magnitude     int | None
    delay_seconds int | None
    icon_category str | None

Reconciled sub_type taxonomy
----------------------------
Two distinct vocabularies coexist, disambiguated by event.category:

  Incident snake_case (all non-work_zone categories):
      accident, jam, road_closed, closure, road_works, lane_closed,
      ramp_closed, debris, vehicle_on_fire, disabled_vehicle, ice, fog,
      flooding, wind, broken_down, danger, rain, incident, special_event, parade

  Work-zone human-readable (category == "work_zone"):
      paving, road construction, construction work, bridge construction,
      bridge maintenance, bridge inspection, pavement marking,
      emergency repairs, utility work, guardrail repairs, shoulder work,
      brush control, flagging, alternating one-way, maintenance,
      minor repair, roadside work, overhead work, subsurface work,
      barrier work, surface work, painting, roadway relocation,
      new construction, road work, construction, emergency repairs,
      detour, lanes reduced, one-way alternating
      (+ wzdx impact fold-ins: "lanes reduced, paving" etc.)

The formatter selects the rendering path from event.category; no cross-
vocabulary lookup is needed.

Time contract: ``now`` (float epoch) is accepted by format() and passed
through to the work-zone ends-at renderer as a naive datetime.  Incident
rendering is time-independent (no relative-time strings).
All clock reads MUST go through meshai.notifications.clock — never stdlib.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Optional

from meshai.notifications.formatters._budget import fit_to_budget

if TYPE_CHECKING:
    from meshai.notifications.events import Event


# ── Work-zone byte budget (mirrors renderers.work_zone._BYTE_BUDGET) ──────
_WZ_BYTE_BUDGET = 80


def _bytelen(s: str) -> int:
    return len(s.encode("utf-8"))


# ── Incident sub_type lookup tables (mirrors incident_handler exactly) ─────

_SUB_TYPE_EMOJI = {
    "accident":         "🚨",
    "jam":              "🚗",
    "road_closed":      "🚫",
    "closure":          "🚫",
    "road_works":       "🚧",
    "lane_closed":      "🟠",
    "ramp_closed":      "🟠",
    "debris":           "⚠️",
    "vehicle_on_fire":  "🔥",
    "disabled_vehicle": "🛑",
    "ice":              "⚠️",
    "fog":              "⚠️",
    "flooding":         "🌊",
    "wind":             "🌬️",
    "broken_down":      "🛞",
    "danger":           "⚠️",
    "rain":             "⚠️",
    "incident":         "⚠️",
    "special_event":    "🎪",
    "parade":           "🎪",
}

_SUB_TYPE_DISPLAY = {
    "accident":         "Crash",
    "jam":              "Stationary Traffic",
    "road_closed":      "Road Closed",
    "closure":          "Closure",
    "road_works":       "Road Works",
    "lane_closed":      "Lane Reduction",
    "ramp_closed":      "Ramp Closed",
    "debris":           "Debris on Roadway",
    "vehicle_on_fire":  "Vehicle Fire",
    "disabled_vehicle": "Disabled Vehicle",
    "ice":              "Icy Conditions",
    "fog":              "Fog",
    "flooding":         "Flooding",
    "wind":             "High Winds",
    "broken_down":      "Broken-Down Vehicle",
    "danger":           "Dangerous Conditions",
    "rain":             "Heavy Rain",
    "incident":         "Road Incident",
    "special_event":    "Special Event",
    "parade":           "Parade",
}

_DIRECTION_LONG = {
    "North": "Northbound", "N": "Northbound", "NB": "Northbound",
    "north": "Northbound", "nb": "Northbound",
    "South": "Southbound", "S": "Southbound", "SB": "Southbound",
    "south": "Southbound", "sb": "Southbound",
    "East":  "Eastbound",  "E": "Eastbound",  "EB": "Eastbound",
    "east":  "Eastbound",  "eb": "Eastbound",
    "West":  "Westbound",  "W": "Westbound",  "WB": "Westbound",
    "west":  "Westbound",  "wb": "Westbound",
    "Both":  "Both Directions", "both": "Both Directions",
}


# ── Work-zone rendering helpers (mirrors renderers.work_zone exactly) ──────

def _wz_format_end_short(
    ends_at: Optional[datetime], now: datetime
) -> Optional[str]:
    """Mirror of renderers.work_zone._format_end_short — byte-identical.

    ``now`` is required (never reads the live clock; caller must pass the
    pinned clock value).  Matches the contract enforced by
    test_formatters_no_clock.
    """
    from datetime import timedelta
    if ends_at is None:
        return None
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
    if (delta < timedelta(hours=48)
            and ends_at.date() == (now + timedelta(days=1)).date()):
        return f"tomorrow {time_part}"
    if delta < timedelta(days=7):
        wd = ends_at.strftime("%a")
        return f"{wd} {time_part}"
    if delta < timedelta(days=365):
        return (ends_at.strftime("%b %-d")
                if hasattr(datetime, "now") else ends_at.strftime("%b ") + str(ends_at.day))
    return None


def _wz_format_direction_phrase(direction: Optional[str]) -> Optional[str]:
    if not direction or direction == "unknown":
        return None
    if direction == "both":
        return "both directions"
    return direction


def _wz_format_mile_segment(
    mile_start: Optional[int], mile_end: Optional[int]
) -> Optional[str]:
    if mile_start is None:
        return None
    if mile_end is not None and mile_end != mile_start:
        return f"@ mile {mile_start}–{mile_end}"
    return f"@ mile {mile_start}"


def _wz_format_distance_segment(
    distance_mi: Optional[int], bearing: Optional[str], town: Optional[str]
) -> Optional[str]:
    if not town:
        return None
    if distance_mi is not None and bearing and distance_mi >= 1:
        return f"{distance_mi} mi {bearing} of {town}"
    return f"near {town}"


def _wz_truncate_road(road: str, budget: int) -> str:
    if _bytelen(road) <= budget:
        return road
    cut = road
    while cut and _bytelen(cut + "…") > budget:
        cut = cut[:-1]
    return cut + "…" if cut else "…"


def _render_work_zone(d: dict, now_dt: Optional[datetime]) -> str:
    """Byte-identical replica of renderers.work_zone.format_work_zone_mesh()."""
    emoji = "🚧"
    raw_road = d.get("road")
    town = d.get("town")

    if raw_road:
        road = raw_road
        head = f"{emoji} {road}"
        suppress_distance_seg = False
    elif town:
        road = town
        head = f"{emoji} {_wz_format_distance_segment(d.get('distance_mi'), d.get('bearing'), town)}"
        suppress_distance_seg = True
    else:
        road = "Road event"
        head = f"{emoji} {road}"
        suppress_distance_seg = False

    mile_seg = (
        _wz_format_mile_segment(d.get("mile_start"), d.get("mile_end"))
        if raw_road else None
    )
    dist_seg = (
        None if suppress_distance_seg
        else _wz_format_distance_segment(d.get("distance_mi"), d.get("bearing"), town)
    )
    dir_phrase = _wz_format_direction_phrase(d.get("direction"))
    sub = d.get("sub_type")
    impact = d.get("impact")
    if impact == "full_closure":
        sub = f"all lanes closed{' (' + sub + ')' if sub else ''}"

    # Reconstruct ends_at from epoch for the time formatter.
    # ends_at_epoch is stored as calendar.timegm(naive_dt.timetuple()) —
    # i.e., the wall-clock value of the naive datetime treated as UTC.
    # datetime.utcfromtimestamp() reconstructs the same naive datetime
    # regardless of the process TZ.  This mirrors how the old renderers
    # strip tzinfo and compare naive datetimes directly.
    ends_at: Optional[datetime] = None
    ends_epoch = d.get("ends_at_epoch")
    if ends_epoch is not None:
        try:
            ends_at = datetime.utcfromtimestamp(float(ends_epoch))
        except (TypeError, ValueError, OSError):
            ends_at = None

    ends_seg = _wz_format_end_short(ends_at, now=now_dt)

    segs: list[tuple[int, str, str]] = []
    if mile_seg:
        segs.append((10, " ", mile_seg))
    if dist_seg:
        segs.append((20, ", ", dist_seg))
    if dir_phrase or sub:
        segs.append((30, ": ", ""))
    if dir_phrase:
        segs.append((30, "", dir_phrase))
    if sub:
        segs.append((40, ", " if dir_phrase else "", sub))
    if ends_seg:
        segs.append((50, ", ends ", ends_seg))

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
            if last_was_colon:
                out += text
                last_was_colon = False
            else:
                out += joiner + text
        if _bytelen(out) <= _WZ_BYTE_BUDGET:
            return out
        droppable = [i for i in kept if segs[i][2] != ""]
        if not droppable:
            budget_for_road = _WZ_BYTE_BUDGET - _bytelen(emoji + " ")
            return f"{emoji} {_wz_truncate_road(road, budget_for_road)}"
        worst = max(droppable, key=lambda i: segs[i][0])
        kept.remove(worst)
        remaining_after_colon = any(
            segs[i][2] != "" for i in kept
            if any(
                segs[j][0] == 30 and segs[j][2] == "" for j in range(len(segs)) if j < i
            )
        )
        if not remaining_after_colon:
            kept = [i for i in kept
                    if not (segs[i][2] == "" and segs[i][1] == ": ")]


# ── Incident rendering (mirrors incident_handler._render exactly) ──────────

def _render_incident(d: dict, budget: int) -> str:
    """Byte-identical replica of incident_handler._render()."""
    sub_type = d.get("sub_type") or "incident"
    emoji = _SUB_TYPE_EMOJI.get(sub_type, "⚠️")
    display = _SUB_TYPE_DISPLAY.get(sub_type, "Road Incident")

    # Line 1: emoji + display + city/county
    anchor = d.get("geocoder_city") or d.get("county")
    state = d.get("state") or ""
    if anchor:
        anchor_part = f"Near {anchor}, {state}".rstrip(", ")
        if not d.get("geocoder_city") and d.get("county"):
            anchor_part = f"Near {anchor} Co, {state}".rstrip(", ")
    else:
        anchor_part = state or ""
    line1 = f"{emoji} {display} — {anchor_part}".rstrip(" —")

    # Line 2: road + direction (+ MP) + lane-status joined by " · ".
    road = d.get("road")
    direction = d.get("direction")
    dir_long = _DIRECTION_LONG.get(direction, direction) if direction else None
    mile = d.get("mile_marker")
    from_loc = d.get("from_loc")
    to_loc = d.get("to_loc")
    seg: list[str] = []
    if road and dir_long:
        seg.append(f"{road} {dir_long}")
    elif road:
        seg.append(road)
    elif from_loc and to_loc:
        seg.append(f"{from_loc} → {to_loc}")
    elif from_loc:
        seg.append(from_loc)
    if mile is not None:
        seg.append(f"MP {mile}")
    lanes = d.get("lanes_affected")
    if lanes and lanes.strip().lower() not in ("no data", ""):
        seg.append(lanes.strip())
    line2 = " · ".join(seg)

    msg = "\n".join(l for l in (line1, line2) if l)

    comment = d.get("comment")
    if comment and comment.strip():
        comment_normalized = comment.strip().lower()
        lanes_normalized = (lanes or "").strip().lower()
        if comment_normalized != lanes_normalized:
            msg = f"{msg}\n{comment.strip()}" if msg else comment.strip()

    return fit_to_budget(msg, budget)


# ── Resolve anchor for incidents that lack geocoder_city ──────────────────

def _resolve_incident_anchor(d: dict) -> tuple[Optional[str], Optional[str]]:
    """Return (anchor_city, state) for line-1 of the incident render.

    Priority mirrors incident_handler._render() exactly:
      1. geocoder_city (direct)
      2. county  (with "Co" suffix)
    Falls through to (None, state) when neither is available.
    """
    city = d.get("geocoder_city")
    county = d.get("county")
    state = d.get("state") or ""
    if city:
        return city, state
    if county:
        return county, state
    return None, state


# ── Public API ────────────────────────────────────────────────────────────


def format(event: "Event", *, now: float, budget: int) -> str:
    """Render an incident/roads event to a mesh wire string.

    Dispatches on event.category:
      "work_zone"    → work-zone renderer (80-byte UTF-8 cap, ignores budget)
      everything else → incident renderer (budget character cap)

    Parameters
    ----------
    event  : Event — reads from event.data (canonical schema above).
    now    : float — frozen-clock epoch.  Used only for work-zone ends-at
             rendering; incident rendering is time-independent.
    budget : int   — mesh packet character budget (incident path only).

    Returns
    -------
    str — UTF-8 wire string fitting within the appropriate budget.
    """
    d = event.data or {}
    category = event.category or ""

    if category == "work_zone":
        # Reconstruct naive local datetime for the ends-at renderer.
        now_dt = datetime.fromtimestamp(now)
        return _render_work_zone(d, now_dt=now_dt)

    return _render_incident(d, budget=budget)
