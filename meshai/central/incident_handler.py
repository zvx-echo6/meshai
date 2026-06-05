"""v0.5.9 unified incident handler.

Three sources collapse into one persistence-backed change-detection pipeline:

  * tomtom_incidents   (real-time crashes/jams/closures, TTI-uuid stable ID)
  * state_511_atis     (ITD incidents + closures + special events --
                        the EventType branching the v0.5.8 parser missed)
  * itd_511            (ITD's newer direct feed, Convention A subject)

State_511_atis with category=work_zone continues to flow through the existing
v0.5.8 _parse_state_511_atis -> work_zone renderer (untouched). Only the
THREE non-work-zone EventTypes route here.

Filtering at handler entrance (Matt's v0.5.9 §6):
  * tomtom magnitude_of_delay==0 -> drop (no event_log row, no broadcast)
  * tomtom time_validity != "present" -> drop
  * everything else flows into the canonical pipeline.

Change-detection (Matt's §5):
  * NEW external_id              -> 'New:'
  * magnitude steps up           -> 'Update:'
  * delay doubles (>=2x)         -> 'Update:'
  * icon_category changes        -> 'Update:'
  * 8h elapsed since last bcast  -> 'Update:'  (heartbeat)
  * otherwise                    -> drop silently

Same callback pattern as WFIGS: handler attaches `_on_broadcast_committed`
to data; dispatcher invokes it AFTER successful deliver(). Cold-start
suppression leaves last_broadcast_* NULL so the next successful broadcast
still labels itself New:.
"""

from __future__ import annotations
from meshai.adapter_config import adapter_config

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

from meshai.persistence import get_db

logger = logging.getLogger(__name__)


# v0.6-3b: freshness gate value lives in adapter_config.incident.freshness_seconds
# (default 1800). Read at handler call time. The module-level constant is
# kept as a backward-compat alias for downstream imports.
INCIDENT_FRESHNESS_MAX_S = 1800

# Heartbeat retained as a constant for backward-compatible imports, but
# the v0.5.9 REVISED handler no longer fires Update broadcasts. State
# tracking continues to UPSERT current_* columns; the dispatcher just
# stops getting wire strings after the first New: broadcast per
# external_id. See WFIGS handler if you want post-first-broadcast
# behavior (fires keep their 8h-rate-limited Update flow).
INCIDENT_BROADCAST_HEARTBEAT_S = 8 * 60 * 60   # 28800 (unused)


# ---- canonical sub_type vocabulary --------------------------------------

# Tomtom icon_category int -> canonical sub_type. Anything missing maps to
# the generic 'incident' bucket so the wire string is never empty.
_TOMTOM_ICON_TO_SUB = {
    0: "incident",       # unknown
    1: "accident",
    2: "fog",
    3: "danger",
    4: "rain",
    5: "ice",
    6: "jam",
    7: "lane_closed",
    8: "road_closed",
    9: "road_works",
    10: "wind",
    11: "flooding",
    12: "broken_down",
    14: "incident",      # cluster
}

# state_511 / itd_511 event_sub_type string -> canonical sub_type.
# Coverage in the 7-day Idaho sample shown after each entry.
_SUB_TYPE_511_MAP = {
    "crash":                                   "accident",          # 28/41
    "incident":                                "incident",          # 1/41
    "debrisOnRoadway":                         "debris",            # 1/41
    "disabledVehicle":                         "disabled_vehicle",  # 1/41
    "vehicleOnFire":                           "vehicle_on_fire",   # 2/41
    "wildfire":                                "incident",          # 1/41
    "wildfireInArea":                          "incident",          # 1/41
    "leftLaneBlocked":                         "lane_closed",       # 2/41
    "onRampBlocked":                           "ramp_closed",       # 2/41
    "roadwayBlocked":                          "road_closed",       # 2/41
    "roadConstruction":                        "road_works",
    "pavementMarkingOperations":               "road_works",
    "pavementMarkingOperations ":              "road_works",        # trailing-space variant
    "utilityWork":                             "road_works",
    "singleLineTraffic:AlternatingDirections": "lane_closed",
    "roadMaintenanceOperations":               "road_works",
    "pavingOperations":                        "road_works",
    "bridgeConstruction":                      "road_works",
    "bridgeMaintenanceOperations":             "road_works",
    "flaggingOperation":                       "lane_closed",
    "brushControl":                            "road_works",
    "constructionWork":                        "road_works",
    "guardrailRepairs":                        "road_works",
    "workOnTheShoulder":                       "road_works",
    "nightTimeConstructionWork":               "road_works",
    "bridgeInspectionWork":                    "road_works",
    "longTermRoadConstruction":                "road_works",
    "workOnUndergroundServices":               "road_works",
    "roadsideCleanupCrew":                     "road_works",
    "RampRestriction":                         "lane_closed",
    "parade":                                  "parade",
}

# Emoji per canonical sub_type.
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

# Human-readable noun phrase per canonical sub_type.
_SUB_TYPE_PHRASE = {
    "accident":         "crash",
    "jam":              "jam",
    "road_closed":      "road closed",
    "closure":          "closure",
    "road_works":       "road works",
    "lane_closed":      "lane closed",
    "ramp_closed":      "ramp closed",
    "debris":           "debris on roadway",
    "vehicle_on_fire":  "vehicle fire",
    "disabled_vehicle": "disabled vehicle",
    "ice":              "icy conditions",
    "fog":              "fog",
    "flooding":         "flooding",
    "wind":             "high wind",
    "broken_down":      "broken-down vehicle",
    "danger":           "dangerous conditions",
    "rain":             "heavy rain",
    "incident":         "incident",
    "special_event":    "special event",
    "parade":           "parade",
}


# ---- helpers -------------------------------------------------------------


def _now() -> int:
    return int(time.time())


def _direction_short(dir_str: Optional[str]) -> Optional[str]:
    if not dir_str: return None
    s = str(dir_str).strip().lower()
    if s.startswith("north"): return "N"
    if s.startswith("south"): return "S"
    if s.startswith("east"):  return "E"
    if s.startswith("west"):  return "W"
    if s in ("nb", "n"):      return "N"
    if s in ("sb", "s"):      return "S"
    if s in ("eb", "e"):      return "E"
    if s in ("wb", "w"):      return "W"
    if s in ("both", "both directions"): return "both"
    return None


def _parse_iso_epoch(s: Optional[str]) -> Optional[int]:
    if not s: return None
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def _parse_511_date_epoch(s: Optional[str]) -> Optional[int]:
    """state_511 uses '5/28/26, 10:45 PM' format."""
    if not s: return None
    try:
        return int(datetime.strptime(s, "%m/%d/%y, %I:%M %p").replace(
            tzinfo=timezone.utc).timestamp())
    except Exception:
        return None


_TTI_RE = re.compile(r"TTI-([0-9a-f-]{36})")


def _tomtom_tti(envelope_id: Optional[str]) -> Optional[str]:
    """Extract the stable TTI-<uuid> piece from the per-poll inner.id.

    TomTom IDs look like:
        ID:tomtom:TTI-<uuid>-TTR<numeric>
    The TTR<numeric> rotates each poll; the TTI-uuid stays stable across
    re-publishes of the same incident.
    """
    if not envelope_id: return None
    m = _TTI_RE.search(envelope_id)
    return m.group(1) if m else envelope_id


def _tomtom_direction_from_description(desc: Optional[str]) -> Optional[str]:
    if not desc: return None
    s = desc.lower()
    if "northbound" in s: return "N"
    if "southbound" in s: return "S"
    if "eastbound"  in s: return "E"
    if "westbound"  in s: return "W"
    return None


def _tomtom_road_label(d: dict) -> Optional[str]:
    """Compose 'I-84 W' style label from road_numbers + direction."""
    nums = d.get("road_numbers") or []
    if nums:
        return str(nums[0])
    return None  # caller falls back to from/to or street name


# ---- per-source parsers --------------------------------------------------


def _parse_tomtom_incident(envelope: dict, now: int) -> Optional[dict]:
    """Returns the canonical incident dict, or None if filtered."""
    inner = envelope.get("data") or {}
    d = inner.get("data") or {}

    # FILTER §6: magnitude_of_delay == 0 -> drop at handler entrance.
    # v0.6-3b: gated by adapter_config.tomtom_incidents.drop_zero_magnitude.
    magnitude = d.get("magnitude_of_delay")
    if magnitude == 0 and bool(adapter_config.tomtom_incidents.drop_zero_magnitude):
        return None

    # FILTER §4: time_validity != 'present' -> drop past/future.
    # v0.6-3b: gated by adapter_config.tomtom_incidents.drop_non_present.
    if (d.get("time_validity") != "present"
            and bool(adapter_config.tomtom_incidents.drop_non_present)):
        return None

    external_id = _tomtom_tti(inner.get("id"))
    if not external_id:
        return None

    icon = d.get("icon_category")
    sub_type = _TOMTOM_ICON_TO_SUB.get(icon, "incident")

    delay_s = d.get("delay")
    delay_minutes = None
    if isinstance(delay_s, (int, float)) and delay_s > 0:
        delay_minutes = max(1, int(round(delay_s / 60)))

    ge = (d.get("_enriched") or {}).get("geocoder") or {}

    return {
        "_kind":           "incident",
        "source":          "tomtom_incidents",
        "external_id":     external_id,
        "category_kind":   "incident",
        "road":            _tomtom_road_label(d) or (d.get("from") or "road"),
        "direction":       _tomtom_direction_from_description(d.get("description")),
        "mile_start":      None,
        "mile_end":        None,
        "county":          ge.get("county"),
        "state":           d.get("state_code"),
        "lat":             d.get("latitude"),
        "lon":             d.get("longitude"),
        "sub_type":        sub_type,
        "impact":          None,
        "delay_minutes":   delay_minutes,
        "delay_seconds":   int(delay_s) if isinstance(delay_s, (int, float)) else None,
        "magnitude":       magnitude,
        "icon_category":   sub_type,
        "from_loc":        d.get("from"),
        "to_loc":          d.get("to"),
        "start_at":        _parse_iso_epoch(d.get("start_time")),
        "end_at":          _parse_iso_epoch(d.get("end_time")),
        "geocoder_city":   ge.get("city"),
        "landclass":       ge.get("landclass"),
    }


def _parse_state_511_incident(envelope: dict, category_raw: str, now: int) -> Optional[dict]:
    """Handle state_511_atis with category in (incident, closure, special_event).
    Returns None for unsupported categories (work_zone is NOT handled here --
    that stays with the existing v0.5.8 _parse_state_511_atis path)."""
    inner = envelope.get("data") or {}
    d = inner.get("data") or {}

    if category_raw.startswith("incident."):        kind = "incident"
    elif category_raw.startswith("closure."):        kind = "closure"
    elif category_raw.startswith("special_event."): kind = "special_event"
    else:                                            return None

    external_id = inner.get("id")
    if not external_id:
        return None

    raw_sub = (d.get("event_sub_type") or "").strip()
    sub_type = _SUB_TYPE_511_MAP.get(raw_sub)
    if sub_type is None:
        if kind == "closure" or d.get("is_full_closure"):
            sub_type = "closure"
        elif kind == "special_event":
            sub_type = "special_event"
        else:
            sub_type = "incident"

    ge = (d.get("_enriched") or {}).get("geocoder") or {}

    return {
        "_kind":          "incident",
        "source":         "state_511_atis",
        "external_id":    external_id,
        "category_kind":  kind,
        "road":           d.get("roadway_name"),
        "direction":      _direction_short(d.get("direction")),
        "mile_start":     None,
        "mile_end":       None,
        "county":         d.get("county") or ge.get("county"),
        "state":          d.get("state_code"),
        "lat":            d.get("latitude"),
        "lon":            d.get("longitude"),
        "sub_type":       sub_type,
        "impact":         "all lanes closed" if d.get("is_full_closure") else None,
        "delay_minutes":  None,
        "delay_seconds":  None,
        "magnitude":      None,
        "icon_category":  sub_type,
        "from_loc":       None,
        "to_loc":         None,
        "start_at":       _parse_511_date_epoch(d.get("start_date")),
        "end_at":         None,
        "geocoder_city":  ge.get("city"),
        "landclass":      ge.get("landclass"),
    }


def _parse_itd_511_incident(envelope: dict, category_raw: str, now: int) -> Optional[dict]:
    inner = envelope.get("data") or {}
    d = inner.get("data") or {}

    if category_raw.startswith("incident."):        kind = "incident"
    elif category_raw.startswith("closure."):        kind = "closure"
    elif category_raw.startswith("special_event."): kind = "special_event"
    else:                                            return None

    external_id = inner.get("id")
    if not external_id:
        return None

    raw_sub = (d.get("event_sub_type") or "").strip()
    sub_type = _SUB_TYPE_511_MAP.get(raw_sub)
    if sub_type is None:
        # ITD has event_type_short ("closure", "incident", "work_zone",
        # "special_event"); fall through to that.
        sub_type = {
            "incident":      "incident",
            "closure":       "closure",
            "work_zone":     "road_works",
            "special_event": "special_event",
        }.get((d.get("event_type_short") or "").lower(), "incident")

    ge = (d.get("_enriched") or {}).get("geocoder") or {}

    return {
        "_kind":          "incident",
        "source":         "itd_511",
        "external_id":    external_id,
        "category_kind":  kind,
        "road":           d.get("roadway_name"),
        "direction":      _direction_short(d.get("direction")),
        "mile_start":     None,
        "mile_end":       None,
        "county":         ge.get("county"),
        "state":          "ID",
        "lat":            d.get("latitude"),
        "lon":            d.get("longitude"),
        "sub_type":       sub_type,
        "impact":         "all lanes closed" if d.get("is_full_closure") else None,
        "delay_minutes":  None,
        "delay_seconds":  None,
        "magnitude":      None,
        "icon_category":  sub_type,
        "from_loc":       None,
        "to_loc":         None,
        "start_at":       d.get("start_epoch"),
        "end_at":         d.get("planned_end_epoch"),
        "geocoder_city":  ge.get("city"),
        "landclass":      ge.get("landclass"),
    }



def _extract_start_time_epoch(envelope: dict, adapter: str) -> Optional[int]:
    """Per-source start-time -> epoch seconds. None when the field is
    missing or unparseable (caller treats None as 'do not gate')."""
    inner = envelope.get("data") or {}
    d = inner.get("data") or {}
    if adapter == "tomtom_incidents":
        # tomtom inner.data.start_time is ISO-8601 ("2026-06-01T21:08:11Z")
        return _parse_iso_epoch(d.get("start_time"))
    if adapter == "state_511_atis":
        # state_511 uses "5/28/26, 10:45 PM" in inner.data.start_date
        return _parse_511_date_epoch(d.get("start_date"))
    if adapter == "itd_511":
        # itd_511 carries start_epoch as a Unix epoch integer
        val = d.get("start_epoch")
        if isinstance(val, (int, float)) and val > 0:
            return int(val)
        return None
    return None


# ---- main entry point ----------------------------------------------------


def handle_incident(envelope: dict, subject: str,
                     data: Optional[dict] = None,
                     now: Optional[int] = None) -> Optional[str]:
    """Unified incident handler. Returns the wire string when a broadcast
    should fire, None otherwise."""
    if not isinstance(envelope, dict):
        return None

    inner = envelope.get("data") or {}
    adapter = inner.get("adapter") or ""
    category_raw = inner.get("category") or ""
    severity_word = _coerce_severity(inner.get("severity"))
    now = now if now is not None else _now()

    try:
        conn = get_db()
    except Exception:
        logger.exception("incident_handler: persistence unavailable")
        return None

    # v0.5.9 GAMMA: state_511_atis Idaho cutover -- itd_511 is the
    # authoritative ID source. state_511_atis remains active for non-ID
    # neighbor coverage (WA, OR, MT). Skip ID events at handler entrance
    # with event_log handled=0 reason='state_511_atis_id_replaced_by_itd_511'
    # so the accounting trail makes the cutover visible.
    if adapter == "state_511_atis":
        sd = (envelope.get("data") or {}).get("data") or {}
        sgeo = (envelope.get("data") or {}).get("geo") or {}
        # v0.6-3b: state allowlist from adapter_config.state_511_atis.skipped_states.
        skipped = {s.upper() for s in adapter_config.state_511_atis.skipped_states}
        primary_region_state = (sgeo.get("primary_region") or "").split("-")[-1].upper()
        if ((sd.get("state_code") or "").upper() in skipped
                or primary_region_state in skipped):
            _log_event(conn, now=now, source="state_511_atis",
                        category=category_raw + "|skip_id",
                        severity_word=severity_word,
                        event_id_external=inner.get("id"),
                        subject=subject, handled=0,
                        table_name=None, table_pk=None)
            return None

    # v0.5.9 REVISED gate (B): freshness check at handler entrance.
    # Computed BEFORE per-source parse + before the mag=0/past/future
    # filters, so stale envelopes never UPSERT into traffic_events.
    # Missing start_time -> default-allow (treat as fresh).
    start_epoch = _extract_start_time_epoch(envelope, adapter)
    if start_epoch is not None:
        age_s = now - start_epoch
        # v0.5.9 GAMMA: reject FUTURE-scheduled events (age < 0) as well
        # as stale events (age > window). itd_511 work_zone envelopes can
        # carry start_epoch many days in the future for scheduled
        # construction projects; under the previous one-sided check
        # those slipped through. Spec re-read: 'skip even New: broadcast
        # if the underlying event began more than 30 min ago' implies
        # the event must have BEGUN.
        fresh_max = int(adapter_config.incident.freshness_seconds)
        if age_s < 0 or age_s > fresh_max:
            logger.debug(
                "incident freshness gate: dropping source=%s subject=%s "
                "age=%ds (window=[0, %d])",
                adapter, subject, age_s, INCIDENT_FRESHNESS_MAX_S,
            )
            _log_event(conn, now=now, source=adapter, category=category_raw,
                        severity_word=severity_word,
                        event_id_external=inner.get("id"),
                        subject=subject, handled=0,
                        table_name=None, table_pk=None)
            return None

    # Per-source parse (returns None when filtered).
    if adapter == "tomtom_incidents":
        n = _parse_tomtom_incident(envelope, now)
    elif adapter == "state_511_atis":
        n = _parse_state_511_incident(envelope, category_raw, now)
    elif adapter == "itd_511":
        n = _parse_itd_511_incident(envelope, category_raw, now)
    else:
        return None

    if n is None:
        # Filtered envelope -- log to event_log handled=0 with no fires/
        # traffic_events row, no broadcast. Lets us account for upstream
        # noise without polluting the broadcast pipeline.
        _log_event(conn, now=now, source=adapter, category=category_raw,
                    severity_word=severity_word,
                    event_id_external=inner.get("id"),
                    subject=subject, handled=0,
                    table_name=None, table_pk=None)
        return None

    external_id = n["external_id"]
    source = n["source"]
    pk_combined = f"{source}|{external_id}"

    log_id = _log_event_returning_id(
        conn, now=now, source=source, category=category_raw,
        severity_word=severity_word,
        event_id_external=external_id,
        subject=subject, handled=0,
        table_name="traffic_events", table_pk=pk_combined)

    row = conn.execute(
        "SELECT first_seen_at, last_seen_at, last_broadcast_at, "
        "last_broadcast_magnitude, last_broadcast_delay_seconds, "
        "last_broadcast_icon_category FROM traffic_events "
        "WHERE source=? AND external_id=?",
        (source, external_id),
    ).fetchone()

    if row is None:
        # NEW external_id -- INSERT, return 'New:' wire, callback updates
        # last_broadcast_* on dispatcher commit.
        conn.execute(
            "INSERT INTO traffic_events(source, external_id, road, direction, "
            "mile_start, mile_end, county, state, lat, lon, sub_type, impact, "
            "start_at, end_at, first_seen_at, last_seen_at, last_broadcast_at, "
            "magnitude_of_delay, delay_seconds, icon_category, "
            "last_broadcast_magnitude, last_broadcast_delay_seconds, "
            "last_broadcast_icon_category) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (source, external_id, n["road"], n["direction"],
             n["mile_start"], n["mile_end"], n["county"], n["state"],
             n["lat"], n["lon"], n["sub_type"], n["impact"],
             n["start_at"], n["end_at"], now, now, None,
             n["magnitude"], n["delay_seconds"], n["icon_category"],
             None, None, None),
        )
        wire = _render(n, prefix="New")
        _attach_commit_handles(data, source=source, external_id=external_id,
                                 magnitude=n["magnitude"],
                                 delay_seconds=n["delay_seconds"],
                                 icon_category=n["icon_category"],
                                 event_log_row_id=log_id)
        return wire

    # EXISTING incident -- always UPSERT current fields + last_seen_at.
    conn.execute(
        "UPDATE traffic_events SET sub_type=?, impact=?, magnitude_of_delay=?, "
        "delay_seconds=?, icon_category=?, last_seen_at=?, "
        "lat=COALESCE(?, lat), lon=COALESCE(?, lon), "
        "direction=COALESCE(?, direction), road=COALESCE(?, road) "
        "WHERE source=? AND external_id=?",
        (n["sub_type"], n["impact"], n["magnitude"], n["delay_seconds"],
         n["icon_category"], now, n["lat"], n["lon"],
         n["direction"], n["road"], source, external_id),
    )

    last_bcast_at       = row["last_broadcast_at"]
    last_bcast_mag      = row["last_broadcast_magnitude"]
    last_bcast_delay    = row["last_broadcast_delay_seconds"]
    last_bcast_icon     = row["last_broadcast_icon_category"]

    # Cold-start race: row exists from a prior INSERT but the dispatcher
    # dropped the broadcast (grace, cooldown, etc.). last_broadcast_at is
    # still NULL -> the next successful broadcast still labels itself New:.
    if last_bcast_at is None:
        wire = _render(n, prefix="New")
        _attach_commit_handles(data, source=source, external_id=external_id,
                                 magnitude=n["magnitude"],
                                 delay_seconds=n["delay_seconds"],
                                 icon_category=n["icon_category"],
                                 event_log_row_id=log_id)
        return wire

    # v0.6-3b: post-first-broadcast Update gated by
    # adapter_config.incident.broadcast_on_update (default False --
    # preserves the v0.5.9 REVISED 'no Update' behavior). When True,
    # broadcast an Update on magnitude step-up, delay doubling, or
    # icon_category change. No heartbeat.
    if not bool(adapter_config.incident.broadcast_on_update):
        return None

    mag_stepped_up = (
        n["magnitude"] is not None
        and (last_bcast_mag is None or n["magnitude"] > last_bcast_mag)
    )
    delay_doubled = (
        n["delay_seconds"] is not None
        and last_bcast_delay is not None
        and last_bcast_delay > 0
        and n["delay_seconds"] >= 2 * last_bcast_delay
    )
    icon_changed = (
        n["icon_category"] is not None
        and last_bcast_icon is not None
        and n["icon_category"] != last_bcast_icon
    )
    if not (mag_stepped_up or delay_doubled or icon_changed):
        return None

    wire = _render(n, prefix="Update")
    _attach_commit_handles(data, source=source, external_id=external_id,
                             magnitude=n["magnitude"],
                             delay_seconds=n["delay_seconds"],
                             icon_category=n["icon_category"],
                             event_log_row_id=log_id)
    return wire


# ---- commit-callback factory --------------------------------------------


def _attach_commit_handles(data: Optional[dict], *, source: str,
                            external_id: str,
                            magnitude: Optional[int],
                            delay_seconds: Optional[int],
                            icon_category: Optional[str],
                            event_log_row_id: Optional[int]) -> None:
    if not isinstance(data, dict):
        return

    def _on_commit(committed_at: float) -> None:
        try:
            conn = get_db()
        except Exception:
            logger.exception("incident commit callback: persistence unavailable")
            return
        conn.execute(
            "UPDATE traffic_events SET last_broadcast_at=?, "
            "last_broadcast_magnitude=?, last_broadcast_delay_seconds=?, "
            "last_broadcast_icon_category=? "
            "WHERE source=? AND external_id=?",
            (int(committed_at), magnitude, delay_seconds, icon_category,
             source, external_id),
        )
        if event_log_row_id is not None:
            conn.execute("UPDATE event_log SET handled=1 WHERE id=?",
                          (int(event_log_row_id),))

    data["_on_broadcast_committed"] = _on_commit
    data["_broadcast_audit"] = {"table": "traffic_events",
                                  "pk": f"{source}|{external_id}"}


# ---- event_log helpers ---------------------------------------------------


def _coerce_severity(sev: Any) -> Optional[str]:
    if sev is None: return None
    if isinstance(sev, str): return sev or None
    try: return str(int(sev))
    except (TypeError, ValueError): return str(sev)


def _log_event(conn, *, now, source, category, severity_word,
                event_id_external, subject, handled,
                table_name, table_pk) -> None:
    conn.execute(
        "INSERT INTO event_log(received_at, source, category, severity_word, "
        "event_id_external, nats_subject, handled, table_name, table_pk) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (now, source, category, severity_word, event_id_external, subject,
         int(bool(handled)), table_name, table_pk),
    )


def _log_event_returning_id(conn, *, now, source, category, severity_word,
                              event_id_external, subject, handled,
                              table_name, table_pk) -> int:
    cur = conn.execute(
        "INSERT INTO event_log(received_at, source, category, severity_word, "
        "event_id_external, nats_subject, handled, table_name, table_pk) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (now, source, category, severity_word, event_id_external, subject,
         int(bool(handled)), table_name, table_pk),
    )
    return int(cur.lastrowid)


# ---- renderer ------------------------------------------------------------


def _render(n: dict, *, prefix: str = "") -> str:
    """MEDIUM-style wire string. Pattern:

        {emoji} {prefix}: {road} near {anchor}: {phrase}{impact}{delay}{coords}

    `delay` segment is OMITTED when delay_minutes is None (Matt's §3).
    `coords` segment is omitted when lat/lon are None (state_511 closures
    sometimes lack coords).
    """
    sub_type = n.get("sub_type") or "incident"
    emoji = _SUB_TYPE_EMOJI.get(sub_type, "⚠️")
    phrase = _SUB_TYPE_PHRASE.get(sub_type, "incident")

    road = n.get("road") or "road"
    direction = n.get("direction")
    if direction and direction != "both" and direction not in str(road):
        road_label = f"{road} {direction}"
    else:
        road_label = road

    anchor = _location_anchor(n)

    delay_minutes = n.get("delay_minutes")
    delay_seg = f", {delay_minutes} min delay" if delay_minutes else ""

    impact = n.get("impact")
    impact_seg = f", {impact}" if impact else ""

    lat = n.get("lat")
    lon = n.get("lon")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        coords = f", @ {lat:.3f},{lon:.3f}"
    else:
        coords = ""

    prefix_str = f"{prefix}: " if prefix else ""
    return f"{emoji} {prefix_str}{road_label} near {anchor}: {phrase}{impact_seg}{delay_seg}{coords}"


def _location_anchor(n: dict) -> str:
    """Anchor priority: geocoder.city > nearest_town > landclass > county."""
    city = n.get("geocoder_city")
    if city:
        return str(city)
    lat = n.get("lat")
    lon = n.get("lon")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        try:
            from meshai.central_normalizer import nearest_town
            nt = nearest_town(lat, lon, max_distance_mi=100.0)
        except Exception:
            nt = None
        if nt and nt.get("name"):
            town = nt["name"]
            d = nt.get("distance_mi")
            if isinstance(d, (int, float)):
                if d < 1: return f"near {town}"
                bearing = nt.get("bearing") or ""
                return f"{int(round(d))} mi {bearing} of {town}".strip()
            return str(town)
    landclass = n.get("landclass")
    if landclass:
        return str(landclass)
    county = n.get("county")
    state = n.get("state")
    if county and state: return f"{county} Co {state}"
    if state: return str(state)
    return "(location unknown)"
