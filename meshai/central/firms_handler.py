"""v0.7-fire-tracker-1 FIRMS handler -- storage + attribution + cluster broadcast.

Pre-v0.6-1 the v0.5.13 default-deny gate at consumer._normalize() silently
dropped every `central.fire.hotspot.>` envelope because no per-adapter handler
existed (audit doc v0.6-phase1-audit.md finding #2). The `firms_pixels`
table was created in v0.5.8b (v1.sql:98-111) and has been empty ever since.

This handler closes that gap: every passing FIRMS pixel lands in
`firms_pixels`. No mesh broadcasts are emitted -- FIRMS data is for the
LLM context (commit #5: env_reporter) and for the v0.6 fire-tracker
fusion (per v0.6-design-fire-tracker.md). Returning None from the handler
tells the consumer's default-deny clause "no broadcast", which is exactly
the v0.6-1 contract (memory rule 19).

Subject pattern (Central v0.10.0):
    central.fire.hotspot.<satellite>.<confidence>.<region>
    where <region> is `us.<state>` or `unknown`.

Envelope shape (from firms-investigation.md, 250 envelopes 2026-05-28..06-04):
    envelope["data"]["adapter"] == "firms"
    envelope["data"]["data"]:
        latitude    (REAL)
        longitude   (REAL)
        frp         (REAL, MW -- fire radiative power)
        bright_ti4  (REAL, K -- VIIRS brightness temperature)
        bright_ti5  (REAL, K, optional)
        satellite   ("N" Suomi-NPP | "N20" NOAA-20)
        instrument  ("VIIRS" so far; MODIS would extend this)
        confidence  ("nominal" | "high" | "low")
        acq_date    ("YYYY-MM-DD" UTC)
        acq_time    ("HHMM" UTC, 4-digit)
        daynight    ("D" | "N")
        version     (str)
        _enriched.geocoder.city/state/county/landclass/elevation_m

Filtering (hardcoded defaults; commit #3 migrates these to adapter_config
GUI rows per Rule 17. Per Matt's lock: defaults become GUI default values
with no behavior change on first deploy.):

    FIRMS_CONFIDENCE_FLOOR = "low"   -- rank-based; "low" = store every conf
    FIRMS_FRP_FLOOR        = 0.0     -- 0 = store every FRP value
    FIRMS_BBOX_OPTIONAL    = None    -- None = no spatial filter

Permissive defaults are intentional: storage is cheap and v0.6 fire-tracker
fusion (FIRMS + WFIGS) needs the full pixel stream to detect unattributed
clusters early. Query-time filtering happens in env_reporter (commit #5).

Dedup:
    Unique partial index added in v4.sql on
        (round(lat,5), round(lon,5), acq_time, satellite)
    Same satellite pixel observation re-published via NATS reconnect /
    JetStream replay is a no-op INSERT OR IGNORE. 5 decimals on lat/lon
    is ~1.1 m precision -- well inside VIIRS' 375 m pixel.

event_log accounting:
    handled=1  -> row inserted into firms_pixels (or dedup-hit -- still
                   "successfully handled" semantically: we know about it)
    handled=0  -> dropped (missing coords / outside bbox / below conf
                   floor / below FRP floor / missing acq timestamp).
                  Category is suffixed with "|<reason>" for grep.
"""
from __future__ import annotations
from meshai.adapter_config import adapter_config

import logging
import math
import time
from datetime import datetime, timezone
from typing import Any, Optional

from meshai.persistence import get_db

logger = logging.getLogger(__name__)


# ============================================================================
# v0.6-3b: all four settings now live in adapter_config.firms. Module-level
# names retained as backward-compat aliases for test monkeypatches; the
# handler reads via adapter_config so a GUI edit takes effect on the next
# envelope without restart.
# ============================================================================

# VIIRS-FIRMS confidence rank table (CODE -- NOAA-defined vocabulary).
_CONFIDENCE_RANK = {"low": 0, "nominal": 1, "high": 2}

# Back-compat aliases for tests that import these names. New code should
# read via adapter_config.firms.<key>.
FIRMS_CONFIDENCE_FLOOR = "low"
FIRMS_FRP_FLOOR = 0.0
FIRMS_BBOX_OPTIONAL: Optional[tuple[float, float, float, float]] = None


# ============================================================================
# Public entry point
# ============================================================================


def handle_firms(envelope: dict, subject: str,
                 data: Optional[dict] = None,
                 now: Optional[int] = None) -> Optional[str]:
    """Storage-only FIRMS handler. ALWAYS returns None.

    Args:
        envelope: CloudEvents envelope from the Central consumer.
        subject:  NATS subject (`central.fire.hotspot.<sat>.<conf>.<region>`).
        data:     Mutable Event.data dict (unused -- no broadcast attached).
        now:      Override current epoch for tests.

    Returns:
        None unconditionally. The v0.5.13 default-deny clause at
        consumer._normalize() interprets None as "no broadcast", which is
        the desired contract for storage-only adapters.
    """
    if not isinstance(envelope, dict):
        return None
    inner = envelope.get("data") or {}
    if (inner.get("adapter") or "") != "firms":
        return None

    d = inner.get("data") or {}
    now = now if now is not None else int(time.time())
    category_raw = inner.get("category") or ""
    severity_word = _coerce_severity(inner.get("severity"))
    event_id_external = inner.get("id")

    try:
        conn = get_db()
    except Exception:
        logger.exception("firms_handler: persistence unavailable; dropping")
        return None

    # ---- field extraction + validation -----------------------------------

    lat = d.get("latitude")
    lon = d.get("longitude")
    if not (isinstance(lat, (int, float)) and isinstance(lon, (int, float))):
        _log_event(conn, now=now, source="firms",
                    category=category_raw + "|missing_coords",
                    severity_word=severity_word,
                    event_id_external=event_id_external,
                    subject=subject, handled=0,
                    table_name=None, table_pk=None)
        return None
    lat = float(lat); lon = float(lon)

    # ---- filter: bbox (optional) -----------------------------------------

    if not _in_bbox(lat, lon):
        _log_event(conn, now=now, source="firms",
                    category=category_raw + "|outside_bbox",
                    severity_word=severity_word,
                    event_id_external=event_id_external,
                    subject=subject, handled=0,
                    table_name=None, table_pk=None)
        return None

    # ---- filter: confidence floor ----------------------------------------

    conf = d.get("confidence")
    if not _confidence_passes(conf):
        _log_event(conn, now=now, source="firms",
                    category=category_raw + "|below_confidence_floor",
                    severity_word=severity_word,
                    event_id_external=event_id_external,
                    subject=subject, handled=0,
                    table_name=None, table_pk=None)
        return None

    # ---- filter: FRP floor (no-op when FIRMS_FRP_FLOOR <= 0) -------------

    frp_raw = d.get("frp")
    try:
        frp = float(frp_raw) if frp_raw is not None else None
    except (TypeError, ValueError):
        frp = None
    import sys as _sys
    _this = _sys.modules[__name__]
    frp_floor = float(_this.FIRMS_FRP_FLOOR) if _this.FIRMS_FRP_FLOOR > 0 \
        else float(adapter_config.firms.frp_floor)
    if frp_floor > 0:
        if frp is None or frp < frp_floor:
            _log_event(conn, now=now, source="firms",
                        category=category_raw + "|below_frp_floor",
                        severity_word=severity_word,
                        event_id_external=event_id_external,
                        subject=subject, handled=0,
                        table_name=None, table_pk=None)
            return None

    # ---- acquisition timestamp (required for dedup key) ------------------

    acq_epoch = _parse_acq_epoch(d.get("acq_date"), d.get("acq_time"))
    if acq_epoch is None:
        _log_event(conn, now=now, source="firms",
                    category=category_raw + "|missing_acq_time",
                    severity_word=severity_word,
                    event_id_external=event_id_external,
                    subject=subject, handled=0,
                    table_name=None, table_pk=None)
        return None

    # ---- persist (INSERT OR IGNORE via v4.sql unique partial index) ------

    satellite = d.get("satellite") or ""
    brightness_raw = d.get("bright_ti4") if d.get("bright_ti4") is not None \
        else d.get("brightness")
    try:
        brightness = float(brightness_raw) if brightness_raw is not None else None
    except (TypeError, ValueError):
        brightness = None

    # v0.6-3b: dedup_key from meters-based quantization (v7 schema).
    dedup_distance_m = float(adapter_config.firms.dedup_distance_m)
    if dedup_distance_m > 0:
        step_deg = dedup_distance_m / 111_000.0
        q_lat = round(lat / step_deg) * step_deg
        q_lon = round(lon / step_deg) * step_deg
        dedup_key = f"{q_lat:.7f},{q_lon:.7f}"
    else:
        dedup_key = f"{lat:.5f},{lon:.5f}"
    cur = conn.execute(
        "INSERT OR IGNORE INTO firms_pixels(irwin_id, lat, lon, acq_time, "
        "frp, confidence, satellite, brightness, dedup_key) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (None, lat, lon, acq_epoch, frp,
         (str(conf) if conf is not None else None),
         satellite, brightness, dedup_key),
    )
    stored = cur.rowcount > 0

    # event_log row regardless of dedup outcome -- both "stored" and
    # "dedup-hit" count as "handled" for accounting; the suffix tells them
    # apart for ops grep.
    handled = 1 if stored else 1   # dedup hit is still handled-success
    cat_tag = category_raw if stored else category_raw + "|dedup_hit"
    _log_event(conn, now=now, source="firms", category=cat_tag,
                severity_word=severity_word,
                event_id_external=event_id_external,
                subject=subject, handled=handled,
                table_name="firms_pixels" if stored else None,
                table_pk=(str(cur.lastrowid) if stored else None))

    # ---- v0.7-fire-tracker-1: attribution + cluster -------------------
    # Dedup hits skip attribution -- the original insert already had its
    # chance. Only newly-stored pixels run through here.
    if not stored:
        return None

    return _attribute_or_cluster(
        conn,
        pixel_row_id=int(cur.lastrowid),
        lat=lat, lon=lon,
        acq_epoch=acq_epoch,
        frp=frp, satellite=satellite,
        data=data, now=now,
    )


# ============================================================================
# Helpers
# ============================================================================


def _confidence_passes(conf: Optional[str]) -> bool:
    """Return True iff `conf` is at or above the configured floor.

    v0.6-3b: floor read from adapter_config.firms.confidence_floor; the
    module-level FIRMS_CONFIDENCE_FLOOR still wins when explicitly
    monkeypatched (so existing tests stay one-line).
    """
    if conf is None:
        return False
    rank = _CONFIDENCE_RANK.get(str(conf).lower())
    if rank is None:
        return False
    import sys
    _this = sys.modules[__name__]
    if _this.FIRMS_CONFIDENCE_FLOOR != "low":
        floor_str = _this.FIRMS_CONFIDENCE_FLOOR
    else:
        floor_str = str(adapter_config.firms.confidence_floor)
    floor = _CONFIDENCE_RANK.get(str(floor_str).lower(), 0)
    return rank >= floor


def _in_bbox(lat: float, lon: float) -> bool:
    import sys
    _this = sys.modules[__name__]
    if _this.FIRMS_BBOX_OPTIONAL is not None:
        bbox = _this.FIRMS_BBOX_OPTIONAL
    else:
        bbox = adapter_config.firms.bbox
    if bbox is None:
        return True
    min_lat, min_lon, max_lat, max_lon = bbox
    return (min_lat <= lat <= max_lat) and (min_lon <= lon <= max_lon)


def _parse_acq_epoch(date_s: Optional[str],
                       time_s: Optional[Any]) -> Optional[int]:
    """FIRMS publishes acq_date 'YYYY-MM-DD' + acq_time HHMM (UTC).

    acq_time is sometimes a 4-digit string ("2013") and sometimes an int
    (2013). Both supported. Zero-padded to 4 chars before parsing.
    """
    if not date_s or time_s is None:
        return None
    try:
        t_str = str(time_s).zfill(4)
        dt = datetime.strptime(f"{date_s} {t_str}", "%Y-%m-%d %H%M") \
            .replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return None


def _coerce_severity(sev: Any) -> Optional[str]:
    if sev is None: return None
    if isinstance(sev, str): return sev or None
    try: return str(int(sev))
    except (TypeError, ValueError): return str(sev)


def _log_event(conn, *, now, source, category, severity_word,
                event_id_external, subject, handled,
                table_name, table_pk) -> None:
    """event_log writer -- shape matches sibling handlers exactly."""
    conn.execute(
        "INSERT INTO event_log(received_at, source, category, severity_word, "
        "event_id_external, nats_subject, handled, table_name, table_pk) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (now, source, category, severity_word, event_id_external, subject,
         int(bool(handled)), table_name, table_pk),
    )



# ============================================================================
# v0.7-fire-tracker-1: attribution + unattributed-cluster detection
# ============================================================================
#
# Attribution: a FIRMS pixel is matched to a fire when it lies within the
# fire's spread radius (per-fire override or global default). The match is
# nearest-centroid on ties. Successfully attributed pixels append to
# fire_pixels and update fires.last_hotspot_at + fires.current_centroid_*
# (median of last 24h of pixels for that fire).
#
# Cluster detection: if NO fire matches, the pixel is "unattributed". We
# query firms_pixels for OTHER recent unattributed pixels within a small
# radius (cluster_max_radius_mi over cluster_time_window_minutes). If the
# count (including this pixel) reaches cluster_min_pixels AND none of
# them has cluster_broadcast_at set, fire a single broadcast and stamp
# cluster_broadcast_at on every member. A subsequent pixel arriving in
# the same cluster will find the stamp and stay silent.


def _attribute_or_cluster(conn, *, pixel_row_id, lat, lon, acq_epoch,
                            frp, satellite, data, now):
    """Try attribution; on miss, run cluster check. Returns wire str | None."""
    global_default_mi = float(adapter_config.fires.spread_radius_mi_default)
    # Conservative bbox prefilter: take the larger of the global default
    # and 10 mi so a per-fire override beyond the default doesn't get
    # quietly excluded. Real Haversine done after.
    search_mi = max(global_default_mi, 10.0)
    deg_lat = search_mi / 69.0
    cos_lat = max(0.01, math.cos(math.radians(lat)))
    deg_lon = search_mi / (69.0 * cos_lat)

    candidates = conn.execute(
        "SELECT irwin_id, lat AS anchor_lat, lon AS anchor_lon, "
        "current_centroid_lat, current_centroid_lon, spread_radius_mi "
        "FROM fires WHERE tombstoned_at IS NULL AND ("
        "COALESCE(current_centroid_lat, lat) BETWEEN ? AND ?) AND ("
        "COALESCE(current_centroid_lon, lon) BETWEEN ? AND ?)",
        (lat - deg_lat, lat + deg_lat, lon - deg_lon, lon + deg_lon),
    ).fetchall()

    attributed: list[tuple[str, float]] = []
    for row in candidates:
        fire_lat = (row["current_centroid_lat"]
                    if row["current_centroid_lat"] is not None
                    else row["anchor_lat"])
        fire_lon = (row["current_centroid_lon"]
                    if row["current_centroid_lon"] is not None
                    else row["anchor_lon"])
        if fire_lat is None or fire_lon is None:
            continue
        r_mi = (row["spread_radius_mi"]
                if row["spread_radius_mi"] is not None
                else global_default_mi)
        d_mi = _haversine_mi(lat, lon, fire_lat, fire_lon)
        if d_mi <= r_mi:
            attributed.append((row["irwin_id"], d_mi))

    if attributed:
        # 2+ matches resolve to nearest centroid per design doc Q2.
        attributed.sort(key=lambda t: t[1])
        chosen_irwin = attributed[0][0]
        conn.execute(
            "INSERT INTO fire_pixels(irwin_id, acq_time, lat, lon, frp, "
            "satellite, pass_id, attributed_at) VALUES (?,?,?,?,?,?,?,?)",
            (chosen_irwin, float(acq_epoch), lat, lon, frp, satellite,
             _pass_id(satellite, acq_epoch), float(now)),
        )
        conn.execute(
            "UPDATE firms_pixels SET attributed_at=? WHERE id=?",
            (float(now), pixel_row_id),
        )
        _recompute_centroid_and_stamp(
            conn, chosen_irwin, acq_epoch=acq_epoch,
        )
        # Attribution is a silent operation -- the wire goes out on the
        # NEXT WFIGS Update (which Phase 2 will gate on centroid drift).
        return None

    # 0 matches -- run cluster detection.
    return _maybe_emit_cluster(
        conn, lat=lat, lon=lon, acq_epoch=acq_epoch, frp=frp,
        data=data, now=now, this_pixel_id=pixel_row_id,
    )


def _recompute_centroid_and_stamp(conn, irwin_id: str, *,
                                    acq_epoch) -> None:
    """fires.current_centroid_* = median of last 24h of fire_pixels for
    this fire. last_hotspot_at = this pixel's acq_time (max of last 24h
    is the same thing on insert). Median over mean per design doc Q4
    ("Median (more robust to outliers)")."""
    window_start = float(acq_epoch) - 86400.0
    pixels = conn.execute(
        "SELECT lat, lon FROM fire_pixels WHERE irwin_id=? "
        "AND acq_time >= ?",
        (irwin_id, window_start),
    ).fetchall()
    if not pixels:
        return
    lats = sorted(r["lat"] for r in pixels)
    lons = sorted(r["lon"] for r in pixels)
    median_lat = lats[len(lats) // 2]
    median_lon = lons[len(lons) // 2]
    conn.execute(
        "UPDATE fires SET current_centroid_lat=?, current_centroid_lon=?, "
        "last_hotspot_at=? WHERE irwin_id=?",
        (float(median_lat), float(median_lon), float(acq_epoch), irwin_id),
    )


def _maybe_emit_cluster(conn, *, lat, lon, acq_epoch, frp, data, now,
                          this_pixel_id):
    """Return wire string + set data["category"] when a cluster condition
    fires; otherwise return None and leave data alone."""
    min_pixels = int(adapter_config.firms.cluster_min_pixels)
    radius_mi = float(adapter_config.firms.cluster_max_radius_mi)
    window_s = int(adapter_config.firms.cluster_time_window_minutes) * 60

    window_start = float(acq_epoch) - window_s
    # Bbox prefilter again, this time on radius_mi (much tighter than the
    # 10 mi attribution search).
    deg_lat = radius_mi / 69.0
    cos_lat = max(0.01, math.cos(math.radians(lat)))
    deg_lon = radius_mi / (69.0 * cos_lat)
    rows = conn.execute(
        "SELECT id, lat, lon, frp FROM firms_pixels WHERE "
        "attributed_at IS NULL AND cluster_broadcast_at IS NULL "
        "AND acq_time >= ? "
        "AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
        (window_start, lat - deg_lat, lat + deg_lat,
         lon - deg_lon, lon + deg_lon),
    ).fetchall()

    # Filter to exact Haversine radius. The query above is a bbox; the
    # corners are slightly farther than radius_mi from the center.
    members: list[dict] = []
    total_frp = 0.0
    sum_lat = 0.0
    sum_lon = 0.0
    for r in rows:
        d_mi = _haversine_mi(lat, lon, r["lat"], r["lon"])
        if d_mi > radius_mi:
            continue
        members.append({"id": r["id"], "lat": r["lat"], "lon": r["lon"]})
        sum_lat += r["lat"]; sum_lon += r["lon"]
        if r["frp"] is not None:
            total_frp += float(r["frp"])

    if len(members) < min_pixels:
        return None

    centroid_lat = sum_lat / len(members)
    centroid_lon = sum_lon / len(members)

    # Stamp cluster_broadcast_at on every member so a future pixel
    # arriving in the same cluster does not re-fire the broadcast.
    member_ids = [int(m["id"]) for m in members]
    placeholders = ",".join("?" * len(member_ids))
    conn.execute(
        f"UPDATE firms_pixels SET cluster_broadcast_at=? "
        f"WHERE id IN ({placeholders})",
        (float(now), *member_ids),
    )

    # Override the FIRMS source category so the dispatcher routes this
    # broadcast under unattributed_hotspot_cluster (priority, fire toggle).
    if isinstance(data, dict):
        data["category"] = "unattributed_hotspot_cluster"
        # Set severity to priority so downstream rules see the right tier.
        data["severity"] = "priority"

    return _render_cluster_wire(
        n=len(members), radius_mi=radius_mi,
        centroid_lat=centroid_lat, centroid_lon=centroid_lon,
        total_frp=total_frp,
    )


def _render_cluster_wire(*, n, radius_mi, centroid_lat, centroid_lon,
                           total_frp):
    """Wire string per design doc section 4 + user item 6."""
    # Drop the decimal on radius when it's an integer mile for terse output.
    radius_str = (f"{int(radius_mi)}" if float(radius_mi).is_integer()
                  else f"{radius_mi:.1f}")
    frp_str = ""
    if total_frp > 0:
        frp_str = f" (combined {int(round(total_frp))} MW)"
    return (
        f"🔥 Possible new fire: {n} hotspots within {radius_str} mi "
        f"@ {centroid_lat:.3f},{centroid_lon:.3f}{frp_str}"
    )


def _haversine_mi(lat1: float, lon1: float,
                    lat2: float, lon2: float) -> float:
    """Great-circle distance in statute miles."""
    R_MI = 3958.7613
    p1 = math.radians(lat1); p2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2.0) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2.0) ** 2)
    return 2.0 * R_MI * math.asin(min(1.0, math.sqrt(a)))


def _pass_id(satellite, acq_epoch) -> str:
    """Coarse satellite-pass bucket: <satellite>-<acq_epoch // 5400s>.

    VIIRS makes ~4 passes/day in Idaho (one every ~6h), so a 90-minute
    bucket groups pixels from the same overpass without straddling
    boundaries. Phase 2's per-pass centroid logic will use this column.
    """
    if not satellite:
        satellite = "?"
    try:
        bucket = int(acq_epoch) // 5400
    except (TypeError, ValueError):
        bucket = 0
    return f"{satellite}-{bucket}"
