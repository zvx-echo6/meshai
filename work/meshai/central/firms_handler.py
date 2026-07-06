"""v0.7-fire-tracker-3 FIRMS handler -- storage + attribution + cluster + growth/halt/spotting.

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

import json
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

    # ---- persist + attribute + fuse (source-agnostic core) --------------

    satellite = d.get("satellite") or ""
    brightness_raw = d.get("bright_ti4") if d.get("bright_ti4") is not None \
        else d.get("brightness")
    try:
        brightness = float(brightness_raw) if brightness_raw is not None else None
    except (TypeError, ValueError):
        brightness = None

    # The INSERT-OR-IGNORE into firms_pixels + attribution + growth/spotting/
    # halt fusion now live in the shared _ingest_pixel_core so the native
    # env/firms.py adapter feeds the SAME engine. handle_firms keeps the
    # envelope-specific concerns (field extraction, filtering, event_log
    # accounting) here so the Central path stays byte-identical. `data` is the
    # consumer's mutable Event.data dict; core stamps it in place on a fusion
    # broadcast exactly as the old inline path did.
    stored, rowid, wire = _ingest_pixel_core(
        conn, lat=lat, lon=lon, acq_epoch=acq_epoch, frp=frp,
        confidence=conf, brightness=brightness, satellite=satellite,
        data=data, now=now,
    )

    # event_log row regardless of dedup outcome -- both "stored" and
    # "dedup-hit" count as "handled" for accounting; the suffix tells them
    # apart for ops grep. (Written after the core call: event_log is a
    # distinct table with its own rowid sequence, so ordering it after the
    # attribution INSERTs leaves the row content + relative order identical.)
    cat_tag = category_raw if stored else category_raw + "|dedup_hit"
    _log_event(conn, now=now, source="firms", category=cat_tag,
                severity_word=severity_word,
                event_id_external=event_id_external,
                subject=subject, handled=1,
                table_name="firms_pixels" if stored else None,
                table_pk=(str(rowid) if stored else None))

    # Dedup hits skip broadcast -- the original insert already had its chance.
    if not stored:
        return None
    return wire


# ============================================================================
# Source-agnostic pixel ingest (Central + native env/firms.py share this)
# ============================================================================


def _ingest_pixel_core(conn, *, lat, lon, acq_epoch, frp, confidence,
                        brightness, satellite, data, now, seed=False):
    """INSERT one canonical hotspot pixel + run attribution/fusion.

    This is the source-agnostic heart of the fire-fusion engine, extracted so
    the Central NATS handler (``handle_firms``) and the native FIRMS adapter
    (``env/firms.py``) drive IDENTICAL attribution/fusion from a bare pixel.

    Steps (exactly what the old inline ``handle_firms`` tail did per pixel):
      1. Compute the v7 meters-quantized ``dedup_key`` and ``INSERT OR IGNORE``
         into ``firms_pixels`` (unique on ``dedup_key, acq_time, satellite``).
         A dedup hit is a no-op -- the original insert already ran attribution.
      2. For a NEWLY-stored pixel, run ``_attribute_or_cluster`` -> writes
         ``fire_pixels`` / ``fire_passes`` / ``fires`` centroid+cursor and runs
         the growth / spotting / halt fusion (honoring the Phase-3c deferred
         latches). On an attribution MISS it runs the curated
         unattributed-cluster path (``_maybe_emit_cluster``); ``seed=True``
         suppresses those cluster broadcasts (cold-start silent-seed). A raw
         per-pixel hotspot is NEVER broadcast.

    Determinism: ``now`` is threaded explicitly; there is no hidden clock read.

    Returns ``(stored, rowid, wire)`` where ``wire`` is the fusion broadcast
    string (or ``None``). ``data`` is mutated in place with the Phase-3c
    broadcast stamps when a fusion wire is produced.
    """
    satellite = satellite or ""

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
         (str(confidence) if confidence is not None else None),
         satellite, brightness, dedup_key),
    )
    stored = cur.rowcount > 0
    rowid = int(cur.lastrowid)
    if not stored:
        return (False, rowid, None)

    wire = _attribute_or_cluster(
        conn,
        pixel_row_id=rowid,
        lat=lat, lon=lon,
        acq_epoch=acq_epoch,
        frp=frp, satellite=satellite,
        data=data, now=now, seed=seed,
    )
    return (True, rowid, wire)


def ingest_hotspot_pixel(pixel: dict, *, now, seed=False) -> list[tuple[str, dict]]:
    """Source-agnostic entrypoint: ingest ONE canonical FIRMS pixel into the
    fire-fusion engine and return the fusion broadcasts it produced.

    ``pixel`` is the canonical FIRMS schema::

        {lat, lon, frp, confidence, brightness, satellite, acq_epoch}

    (``lat``/``lon``/``acq_epoch`` required; the rest optional.) This INSERTs
    the pixel into ``firms_pixels`` (dedup-safe), runs attribution, and runs the
    growth / spotting / halt fusion -- the same pipeline the Central handler
    runs -- honoring the Phase-3c deferred latches.

    Returns a list of ``(wire, data)`` fusion broadcasts, where ``wire`` is the
    mesh text and ``data`` carries the Phase-3c category/severity stamps (and,
    under cutover, the deferred commit hook). A dedup hit or a pixel that
    triggers no fusion returns ``[]``. This NEVER returns a raw-hotspot,
    ``wildfire_hotspot``, or ``new_ignition`` broadcast -- the possible outputs
    are ``wildfire_growth`` / ``wildfire_spotting`` / ``wildfire_halted`` (an
    attributed pixel grew/moved a known fire) OR a curated
    ``unattributed_hotspot_cluster`` ("possible new fire") when ``cluster_min_pixels``
    unattributed pixels fall within ``cluster_max_radius_mi`` over
    ``cluster_time_window_minutes``. Callers must therefore NEVER broadcast the
    raw pixel itself -- only these returned fusion/cluster wires.

    ``seed`` (cold-start silent-seed): on the FIRST fetch after boot, pass
    ``seed=True`` so any cluster that forms from the day's pre-existing hotspots
    is stamped (never re-fires) but emits NOTHING -- no cold-start dump. Only
    clusters formed by pixels arriving on a LATER (``seed=False``) fetch
    broadcast. Persistence + attribution run regardless of ``seed``.

    Determinism: ``now`` (epoch) is required and threaded straight through.
    """
    try:
        conn = get_db()
    except Exception:
        logger.exception("ingest_hotspot_pixel: persistence unavailable; dropping")
        return []

    lat = pixel.get("lat")
    lon = pixel.get("lon")
    acq_epoch = pixel.get("acq_epoch")
    if not (isinstance(lat, (int, float)) and isinstance(lon, (int, float))):
        return []
    if acq_epoch is None:
        return []

    try:
        frp = float(pixel["frp"]) if pixel.get("frp") is not None else None
    except (TypeError, ValueError):
        frp = None
    try:
        brightness = float(pixel["brightness"]) \
            if pixel.get("brightness") is not None else None
    except (TypeError, ValueError):
        brightness = None

    data: dict = {}
    _stored, _rowid, wire = _ingest_pixel_core(
        conn,
        lat=float(lat), lon=float(lon), acq_epoch=int(acq_epoch),
        frp=frp, confidence=pixel.get("confidence"), brightness=brightness,
        satellite=pixel.get("satellite") or "", data=data, now=now, seed=seed,
    )
    if wire is None:
        return []
    return [(wire, data)]


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
                            frp, satellite, data, now, seed=False):
    """Try attribution; on miss, run cluster check. Returns wire str | None.

    Attribution ALWAYS runs first (a pixel inside a known WFIGS fire's spread
    radius grows that fire and never forms a "new" cluster). ``seed`` only
    affects the unattributed-cluster branch: see ``_maybe_emit_cluster``.
    """
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
        this_pass_id = _pass_id(satellite, acq_epoch)
        conn.execute(
            "INSERT INTO fire_pixels(irwin_id, acq_time, lat, lon, frp, "
            "satellite, pass_id, attributed_at) VALUES (?,?,?,?,?,?,?,?)",
            (chosen_irwin, float(acq_epoch), lat, lon, frp, satellite,
             this_pass_id, float(now)),
        )
        conn.execute(
            "UPDATE firms_pixels SET attributed_at=? WHERE id=?",
            (float(now), pixel_row_id),
        )
        # Phase 1 24h-median centroid stays as a fallback for fires that
        # don't yet have pass data (cold-start). The pass-boundary path
        # below overrides it with the per-pass centroid once a pass has
        # been observed.
        _recompute_centroid_and_stamp(
            conn, chosen_irwin, acq_epoch=acq_epoch,
        )
        # v0.7-fire-tracker-2: per-pass aggregation + drift + growth.
        wire = _handle_pass_boundary(
            conn, irwin_id=chosen_irwin, pass_id=this_pass_id,
            lat=lat, lon=lon, acq_epoch=acq_epoch, frp=frp,
            data=data, now=now,
        )
        if wire is not None:
            return wire
        # No growth broadcast; opportunistically run halt detector for
        # OTHER fires that may have gone idle.
        return _maybe_emit_halt(conn, data=data, now=now)

    # 0 matches -- run cluster detection.
    wire = _maybe_emit_cluster(
        conn, lat=lat, lon=lon, acq_epoch=acq_epoch, frp=frp,
        data=data, now=now, this_pixel_id=pixel_row_id, seed=seed,
    )
    if wire is not None:
        return wire
    return _maybe_emit_halt(conn, data=data, now=now)


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
                          this_pixel_id, seed=False):
    """Return wire string + set data["category"] when a cluster condition
    fires; otherwise return None and leave data alone.

    ``seed`` (cold-start silent-seed): when True, a cluster that meets the
    threshold is still STAMPED (``cluster_broadcast_at`` on every member) so it
    can never re-fire on a later fetch, but NO wire is returned and ``data`` is
    left untouched -- i.e. the day's pre-existing hotspots discovered on the
    first fetch after boot are absorbed silently. Only clusters that form from
    pixels arriving on a LATER (non-seed) fetch broadcast. Persistence +
    attribution are unaffected (they run in the caller before this point).
    """
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

    # Cold-start silent-seed: the cluster is stamped (above) so it can never
    # re-fire, but on the first fetch after boot we emit NOTHING and leave
    # `data` untouched -- the day's pre-existing hotspots are absorbed silently.
    if seed:
        logger.info(
            "firms cold-start silent-seed: absorbed %d-pixel cluster "
            "@ %.3f,%.3f (stamped, no broadcast)",
            len(members), centroid_lat, centroid_lon)
        return None

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



# ============================================================================
# v0.7-fire-tracker-2: per-pass tracking + drift + growth + halt
# ============================================================================
#
# _handle_pass_boundary is called from _attribute_or_cluster after the
# Phase 1 attribution path inserts a fire_pixels row. Its job is to:
#   (1) UPSERT the fire_passes row for (irwin_id, this_pass_id) with the
#       running median + count + frp + started/ended_at across every
#       fire_pixels row currently tagged with that pass_id.
#   (2) On boundary (this_pass_id != fires.last_pass_id), walk back to
#       the prior pass's centroid, compute Haversine drift + 8-way
#       direction + mi/h speed, stamp those onto the current pass's row,
#       update fires.last_pass_id / last_pass_at / current_centroid_*,
#       and fire wildfire_growth when drift >= the configured threshold.
#
# _maybe_emit_halt runs on every pixel arrival as a fallback when neither
# growth nor cluster has produced a wire. It SELECTs at most one fire
# meeting the halt criteria, latches halt_broadcast_at, and returns the
# wire. A subsequent attributed pixel will UPDATE fires.last_pass_at to
# a recent value, making the fire re-eligible for halt if it goes idle
# again (the detector filter is halt_broadcast_at IS NULL OR
# halt_broadcast_at < last_pass_at).


def _handle_pass_boundary(conn, *, irwin_id, pass_id, lat, lon,
                            acq_epoch, frp, data, now):
    """Maintain fire_passes row, detect boundary, fire growth on drift."""
    # (1) Recompute the pass aggregate from fire_pixels.
    pass_rows = conn.execute(
        "SELECT lat, lon, frp, acq_time FROM fire_pixels "
        "WHERE irwin_id=? AND pass_id=? ORDER BY acq_time",
        (irwin_id, pass_id),
    ).fetchall()
    if not pass_rows:
        # Should not happen -- the caller just inserted one. Defensive.
        return None
    lats = sorted(r["lat"] for r in pass_rows)
    lons = sorted(r["lon"] for r in pass_rows)
    n = len(lats)
    pass_centroid_lat = lats[n // 2]
    pass_centroid_lon = lons[n // 2]
    total_frp = sum((r["frp"] or 0.0) for r in pass_rows)
    pass_started_at = float(min(r["acq_time"] for r in pass_rows))
    pass_ended_at = float(max(r["acq_time"] for r in pass_rows))

    # Look up the prior fire_passes row (most recent before this pass)
    # BEFORE we upsert the current row so the lookup doesn't find itself.
    prev = conn.execute(
        "SELECT pass_id, pass_centroid_lat, pass_centroid_lon, "
        "pass_ended_at FROM fire_passes "
        "WHERE irwin_id=? AND pass_id != ? "
        "ORDER BY pass_ended_at DESC LIMIT 1",
        (irwin_id, pass_id),
    ).fetchone()

    # Compute drift now if we have a prior pass; we'll write it into
    # the upserted row.
    drift_mi = None
    drift_direction = None
    drift_mi_per_hour = None
    if prev is not None:
        drift_mi = _haversine_mi(
            prev["pass_centroid_lat"], prev["pass_centroid_lon"],
            pass_centroid_lat, pass_centroid_lon,
        )
        drift_direction = _direction_8(_bearing(
            prev["pass_centroid_lat"], prev["pass_centroid_lon"],
            pass_centroid_lat, pass_centroid_lon,
        ))
        wall_clock_hours = (pass_ended_at - prev["pass_ended_at"]) / 3600.0
        if wall_clock_hours > 0:
            drift_mi_per_hour = drift_mi / wall_clock_hours

    # (1b) UPSERT the current pass row.
    conn.execute(
        "INSERT INTO fire_passes(irwin_id, pass_id, pass_centroid_lat, "
        "pass_centroid_lon, pixel_count, total_frp, pass_started_at, "
        "pass_ended_at, drift_mi_from_prev, drift_direction, "
        "drift_mi_per_hour) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(irwin_id, pass_id) DO UPDATE SET "
        "pass_centroid_lat=excluded.pass_centroid_lat, "
        "pass_centroid_lon=excluded.pass_centroid_lon, "
        "pixel_count=excluded.pixel_count, "
        "total_frp=excluded.total_frp, "
        "pass_started_at=MIN(fire_passes.pass_started_at, "
        "                     excluded.pass_started_at), "
        "pass_ended_at=MAX(fire_passes.pass_ended_at, "
        "                   excluded.pass_ended_at), "
        "drift_mi_from_prev=COALESCE(fire_passes.drift_mi_from_prev, "
        "                            excluded.drift_mi_from_prev), "
        "drift_direction=COALESCE(fire_passes.drift_direction, "
        "                         excluded.drift_direction), "
        "drift_mi_per_hour=COALESCE(fire_passes.drift_mi_per_hour, "
        "                           excluded.drift_mi_per_hour)",
        (irwin_id, pass_id, pass_centroid_lat, pass_centroid_lon,
         n, total_frp if total_frp > 0 else None,
         pass_started_at, pass_ended_at,
         drift_mi, drift_direction, drift_mi_per_hour),
    )

    # (2) Detect boundary: compare to fires.last_pass_id.
    fires_row = conn.execute(
        "SELECT incident_name, last_pass_id FROM fires WHERE irwin_id=?",
        (irwin_id,),
    ).fetchone()
    if fires_row is None:
        return None

    last_pass_id = fires_row["last_pass_id"]
    # Always update fires cursor + centroid to point at the current
    # in-progress pass.  Subsequent pixels in the same pass become same-
    # bucket and just refine the pass row; the cursor is already there.
    conn.execute(
        "UPDATE fires SET last_pass_id=?, last_pass_at=?, "
        "current_centroid_lat=?, current_centroid_lon=? WHERE irwin_id=?",
        (pass_id, float(acq_epoch), pass_centroid_lat,
         pass_centroid_lon, irwin_id),
    )

    # v0.7-fire-tracker-3: on the boundary path, close the prior pass's
    # perimeter (convex hull of its pixels) so this and subsequent
    # in-pass pixels can spotting-check against it.
    boundary = (last_pass_id != pass_id) and (last_pass_id is not None)
    if boundary and prev is not None and not _prev_has_perimeter(conn, irwin_id, prev["pass_id"]):
        _close_prev_perimeter(conn, irwin_id, prev["pass_id"])

    # v0.7-fire-tracker-3: spotting check. Runs for every attributed
    # pixel (not just boundary) because pixels 2..N of the new pass may
    # also be far from the prior perimeter. Spotting has IMMEDIATE
    # severity and preempts growth in the priority order.
    spotting_wire = _check_spotting(
        conn, irwin_id=irwin_id, pixel_lat=lat, pixel_lon=lon,
        current_pass_id=pass_id,
        incident_name=fires_row["incident_name"] or "(unnamed fire)",
        data=data, now=now,
    )
    if spotting_wire is not None:
        return spotting_wire

    # Phase-3c: the growth broadcast DECISION (pass boundary + drift threshold)
    # is delegated to gating.firms.decide; the wire is still rendered inline via
    # the WFIGS _render for byte-identity. The boundary predicate reproduces the
    # legacy guard (`last_pass_id == pass_id or last_pass_id is None or prev is
    # None` -> suppress) exactly. Growth has no latch, so there is nothing to
    # defer -- only the data stamping differs between the cutover and legacy
    # paths.
    from meshai.notifications.cutover import is_cutover
    from meshai.notifications.gating.firms import decide as _firms_decide

    boundary_growth = ((last_pass_id != pass_id) and (last_pass_id is not None)
                       and (prev is not None))
    gate = _firms_decide(
        {"_kind": "firms_growth", "irwin_id": irwin_id,
         "boundary": boundary_growth, "drift_mi": drift_mi,
         "drift_direction": drift_direction,
         "drift_mi_per_hour": drift_mi_per_hour},
        source="firms", now=float(now))
    if not gate.broadcast:
        return None

    # Drift exceeded the threshold -- emit wildfire_growth via WFIGS renderer.
    fire = conn.execute(
        "SELECT incident_name, current_acres, current_contained_pct, "
        "declared_at, lat, lon "
        "FROM fires WHERE irwin_id=?", (irwin_id,)
    ).fetchone()
    if fire is None:
        return None

    from meshai.central.wfigs_handler import _render
    movement = {"direction": drift_direction, "speed_mph": drift_mi_per_hour or 0.0}
    normalized = dict(fire)
    normalized["irwin_id"] = irwin_id
    if isinstance(data, dict):
        if is_cutover("wildfire_growth"):
            # NEW PATH: fire formatter re-renders from these hints. Feed ONLY the
            # fields _render actually reads on the growth path (incident_name,
            # lat/lon for the anchor, movement, is_update) so the wire matches.
            data.update(gate.data_patch)
            data["incident_name"] = fire["incident_name"]
            data["lat"] = fire["lat"]
            data["lon"] = fire["lon"]
            data["_broadcast_audit"] = {"table": "fires", "pk": irwin_id}
            _raw_commit = gate.commit
            if _raw_commit is not None:
                def _on_commit(committed_at: float) -> None:
                    _raw_commit(committed_at)
                data["_on_broadcast_committed"] = _on_commit
        else:
            # LEGACY verbatim (byte-for-byte identical live output).
            data["category"] = "wildfire_growth"
            data["_severity_override"] = "immediate"
            data["_cooldown_suffix"] = irwin_id
    return _render(normalized, prefix="Update", movement=movement)


def _render_growth_wire(*, incident_name, direction, speed_mph,
                          lat, lon):
    """Per design doc section 4 + Phase 2 spec item 3."""
    near_part = ""
    try:
        from meshai.central_normalizer import nearest_town
        nt = nearest_town(lat, lon, max_distance_mi=100.0)
        if nt and nt.get("name"):
            town = nt["name"]
            d_mi = nt.get("distance_mi")
            if isinstance(d_mi, (int, float)):
                near_part = f", ~{float(d_mi):.1f} mi from {town}"
            else:
                near_part = f" near {town}"
    except Exception:
        logger.exception("growth wire: nearest_town lookup failed")
    return (
        f"🔥 {incident_name} moving {direction} "
        f"{speed_mph:.1f} mi/h{near_part}"
    )


def _maybe_emit_halt(conn, *, data, now):
    """Find one fire matching the halt criteria, latch + broadcast.

    Returns the wire string when a halt event fires; otherwise None.

    Phase-3c: the DECISION (the halt-eligibility SELECT) is delegated to
    ``gating.firms.decide``; the wire is still rendered inline here for
    byte-identity.  On the not-cutover live path the eager
    ``fires.halt_broadcast_at`` latch is kept VERBATIM (stamped with the handler
    ``now``) so the live broadcast stays byte-for-byte identical; on the cutover
    path the latch is deferred into ``gate.commit`` (tier-b change).  Halt
    re-eligibility is unchanged: a fire whose ``last_pass_at`` advances past
    ``halt_broadcast_at`` becomes eligible again.
    """
    from meshai.notifications.cutover import is_cutover
    from meshai.notifications.gating.firms import decide as _firms_decide

    gate = _firms_decide({"_kind": "firms_halt"}, source="firms", now=float(now))
    if not gate.broadcast:
        return None

    p = gate.data_patch
    irwin_id = p["irwin_id"]
    name = p["incident_name"]
    hours = p["hours"]
    wire = f"🔥 {name} no growth in {hours}h"

    if isinstance(data, dict):
        if is_cutover("wildfire_halted"):
            # NEW PATH: formatter re-renders from data_patch; latch deferred.
            data.update(gate.data_patch)
            data["_broadcast_audit"] = {"table": "fires", "pk": irwin_id}
            _raw_commit = gate.commit

            def _on_commit(committed_at: float) -> None:
                if _raw_commit is not None:
                    _raw_commit(committed_at)

            data["_on_broadcast_committed"] = _on_commit
        else:
            # LEGACY verbatim (byte-for-byte identical live output): eager latch
            # with the handler `now`, plain category/severity stamps.
            conn.execute(
                "UPDATE fires SET halt_broadcast_at=? WHERE irwin_id=?",
                (float(now), irwin_id),
            )
            data["category"] = "wildfire_halted"
            data["severity"] = "routine"
    return wire


def _bearing(lat1: float, lon1: float,
              lat2: float, lon2: float) -> float:
    """Initial bearing FROM point 1 TO point 2, in degrees clockwise
    from north. Used to assign a direction to centroid drift."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    y = math.sin(dlon) * math.cos(phi2)
    x = (math.cos(phi1) * math.sin(phi2)
         - math.sin(phi1) * math.cos(phi2) * math.cos(dlon))
    theta = math.atan2(y, x)
    return (math.degrees(theta) + 360.0) % 360.0


_COMPASS_8 = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def _direction_8(bearing_deg: float) -> str:
    """Map a 0..360 bearing to the nearest 8-way compass label.

    Each sector spans 45 deg centered on a cardinal/intercardinal.
    """
    idx = int(((float(bearing_deg) + 22.5) % 360.0) // 45)
    return _COMPASS_8[idx]



# ============================================================================
# v0.7-fire-tracker-3: spotting detection (convex-hull perimeter + cooldown)
# ============================================================================
#
# Pass close: when a boundary is detected (the first pixel of a new
# pass arrives), the prior pass's perimeter is computed as the convex
# hull of its fire_pixels rows and stored as GeoJSON in fire_passes.
# Hulls with <3 distinct vertices are stored verbatim (point or line)
# and skipped during spotting checks -- a "perimeter" needs area.
#
# Spotting check: every attributed pixel looks up the most recent
# fire_passes row for this fire with a non-NULL perimeter (i.e. the
# previous closed pass). If the pixel is outside that hull AND the
# vertex-nearest distance is >= spotting_distance_threshold_mi AND
# the per-fire cooldown is clear, fire wildfire_spotting and stamp
# fires.last_spotting_broadcast_at.


def _prev_has_perimeter(conn, irwin_id: str, prev_pass_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM fire_passes WHERE irwin_id=? AND pass_id=? "
        "AND perimeter_geojson IS NOT NULL",
        (irwin_id, prev_pass_id),
    ).fetchone()
    return row is not None


def _close_prev_perimeter(conn, irwin_id: str, prev_pass_id: str) -> None:
    """Compute the convex hull of fire_pixels for (irwin_id, prev_pass_id)
    and write it into fire_passes.perimeter_geojson. A no-op if the pass
    has zero pixels (defensive; should not happen)."""
    pts = conn.execute(
        "SELECT lat, lon FROM fire_pixels WHERE irwin_id=? AND pass_id=?",
        (irwin_id, prev_pass_id),
    ).fetchall()
    coords = [(float(r["lat"]), float(r["lon"])) for r in pts]
    if not coords:
        return
    hull = _convex_hull(coords)
    geojson = _hull_to_geojson(hull)
    conn.execute(
        "UPDATE fire_passes SET perimeter_geojson=? "
        "WHERE irwin_id=? AND pass_id=?",
        (geojson, irwin_id, prev_pass_id),
    )


def _check_spotting(conn, *, irwin_id, pixel_lat, pixel_lon,
                      current_pass_id, incident_name, data, now):
    """Return spotting wire if criteria met, else None."""
    threshold_mi = float(adapter_config.fires.spotting_distance_threshold_mi)
    # Phase-3c: the cooldown gate (and its latch) moved into gating.firms.decide;
    # the cooldown seconds are read there. The geometry below stays inline.

    # Most recent CLOSED pass with a perimeter (i.e. not the current pass).
    prev = conn.execute(
        "SELECT pass_id, pass_centroid_lat, pass_centroid_lon, "
        "perimeter_geojson FROM fire_passes "
        "WHERE irwin_id=? AND pass_id != ? "
        "AND perimeter_geojson IS NOT NULL "
        "ORDER BY pass_ended_at DESC LIMIT 1",
        (irwin_id, current_pass_id),
    ).fetchone()
    if prev is None:
        return None

    try:
        poly = json.loads(prev["perimeter_geojson"])
        ring = poly["coordinates"][0]   # GeoJSON: outer ring, (lon,lat)
    except (KeyError, ValueError, TypeError):
        logger.exception("spotting: malformed perimeter geojson for %s",
                          irwin_id)
        return None

    # Need at least 3 distinct vertices for a real perimeter. Hulls
    # with <3 vertices represent degenerate point / line passes and
    # don't support a meaningful inside/outside test.
    # A closed GeoJSON ring repeats its first vertex at the end, so a
    # 3-vertex triangle has 4 entries; treat <4 as degenerate.
    if len(ring) < 4:
        return None

    # ring is [[lon, lat], ...]; convert to [(lat, lon), ...] for our
    # local Haversine + point-in-polygon math.
    vertices_lat_lon = [(c[1], c[0]) for c in ring[:-1]]

    inside = _point_in_polygon((pixel_lat, pixel_lon), vertices_lat_lon)
    if inside:
        return None

    # Vertex-distance approximation: closest vertex haversine distance.
    # Design doc accepts this -- exact edge projection is overkill at
    # VIIRS's 375 m pixel resolution.
    dist_mi = min(
        _haversine_mi(pixel_lat, pixel_lon, v_lat, v_lon)
        for v_lat, v_lon in vertices_lat_lon
    )
    if dist_mi < threshold_mi:
        return None

    # Direction is FROM the perimeter centroid (the previous pass's
    # pass_centroid_lat/lon, already on the row) TO this pixel.
    direction = _direction_8(_bearing(
        prev["pass_centroid_lat"], prev["pass_centroid_lon"],
        pixel_lat, pixel_lon,
    ))

    # Phase-3c: the cooldown DECISION (and its latch) is delegated to
    # gating.firms.decide. The wire is still rendered inline for byte-identity.
    # On the not-cutover live path the eager fires.last_spotting_broadcast_at
    # latch is kept VERBATIM (stamped with the handler `now`); on the cutover
    # path the latch is deferred into gate.commit (tier-b change).
    from meshai.notifications.cutover import is_cutover
    from meshai.notifications.gating.firms import decide as _firms_decide

    gate = _firms_decide(
        {"_kind": "firms_spotting", "irwin_id": irwin_id, "dist_mi": dist_mi,
         "direction": direction, "incident_name": incident_name},
        source="firms", now=float(now))
    if not gate.broadcast:
        return None

    wire = (
        f"🔥 Possible spotting {dist_mi:.1f} mi {direction} of "
        f"{incident_name} perimeter"
    )

    if isinstance(data, dict):
        if is_cutover("wildfire_spotting"):
            # NEW PATH: formatter re-renders from data_patch; latch deferred.
            data.update(gate.data_patch)
            data["_broadcast_audit"] = {"table": "fires", "pk": irwin_id}
            _raw_commit = gate.commit

            def _on_commit(committed_at: float) -> None:
                if _raw_commit is not None:
                    _raw_commit(committed_at)

            data["_on_broadcast_committed"] = _on_commit
        else:
            # LEGACY verbatim (byte-for-byte identical live output): eager latch
            # with the handler `now`, plain category/severity stamps.
            conn.execute(
                "UPDATE fires SET last_spotting_broadcast_at=? WHERE irwin_id=?",
                (float(now), irwin_id),
            )
            data["category"] = "wildfire_spotting"
            data["severity"] = "immediate"

    return wire


def _convex_hull(points):
    """Andrew's monotone-chain convex hull. Returns the hull as a list of
    (lat, lon) tuples in CCW order. Treats lat/lon as planar (small-area
    approximation -- adequate for fires <10 mi diameter).

    Pass through with <3 unique points: returned as-is (caller decides
    whether the perimeter is meaningful)."""
    pts = sorted(set(points))
    if len(pts) <= 1:
        return list(pts)

    def cross(o, a, b):
        return ((a[0] - o[0]) * (b[1] - o[1])
                - (a[1] - o[1]) * (b[0] - o[0]))

    lower = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _hull_to_geojson(hull) -> str:
    """Encode a list of (lat, lon) as a GeoJSON Polygon string. RFC 7946
    requires (lon, lat) order and a closed outer ring (first == last)."""
    if not hull:
        return json.dumps({"type": "Polygon", "coordinates": [[]]})
    ring = [[lon, lat] for lat, lon in hull]
    # Close the ring per spec.
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    return json.dumps({"type": "Polygon", "coordinates": [ring]})


def _point_in_polygon(point, polygon_lat_lon) -> bool:
    """Ray-casting point-in-polygon. polygon_lat_lon is a list of
    (lat, lon) tuples (the ring; do NOT repeat the first vertex).
    Returns True for points strictly inside the polygon; the boundary
    case is implementation-defined and is fine for our needs (a pixel
    sitting exactly on a vertex isn't a spotting candidate either way).
    """
    n = len(polygon_lat_lon)
    if n < 3:
        return False
    px_lat, px_lon = point
    inside = False
    j = n - 1
    for i in range(n):
        lat_i, lon_i = polygon_lat_lon[i]
        lat_j, lon_j = polygon_lat_lon[j]
        if ((lat_i > px_lat) != (lat_j > px_lat)):
            # x = (lon_j - lon_i) * (px_lat - lat_i) / (lat_j - lat_i) + lon_i
            try:
                x_intersect = (lon_j - lon_i) * (px_lat - lat_i) / (lat_j - lat_i) + lon_i
            except ZeroDivisionError:
                x_intersect = lon_i
            if px_lon < x_intersect:
                inside = not inside
        j = i
    return inside
