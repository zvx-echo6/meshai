"""v0.5.12 usgs_nwis stream-gauge handler.

Minimal Idaho curation -- 9 starter sites in idaho_gauge_sites.py. Non-
curated sites are dropped at handler entrance (event_log handled=0, no
gauge_readings UPSERT). v0.6.x will migrate the curation dict into a DB
table so non-engineers can edit via the GUI.

Per-parameter filtering:
    00060 = Discharge (cfs)      -- captured as flow_cfs, paired with stage
    00065 = Gage height (ft)     -- the canonical stage for threshold calc
    everything else              -- dropped (no precipitation handling this round)

Change-detection (mirrors WFIGS forward-only):
    Insert the new reading into gauge_readings (time-series).
    Compare current threshold_state to most recent prior reading\\'s
    threshold_state for the same site. If current > prior in the ranked
    scale {normal < action < flood_minor < flood_moderate < flood_major},
    fire 'New:' broadcast. Otherwise (unchanged or descending), no
    broadcast. The receding-water case is intentionally silent --
    operationally less urgent than rising water.

Wire format MEDIUM:
    🌊 New: {gauge_name}: {label} {value} ft, flow {flow_cfs:,} cfs, @ lat,lon

Where {label} is:
    action          -> "action stage"
    flood_minor     -> "minor flooding"
    flood_moderate  -> "moderate flooding"
    flood_major     -> "major flooding"

flow_cfs segment is dropped when parameter_code is 00065 only (no
companion discharge reading). lat/lon segment is dropped when coords are
missing (rare since curated sites have coords).

Operational status (2026-06-08):
  PARKED — Idaho USGS sites return discharge only (parameter_code=00060).
  Gage height (00065) is not present in any retained CENTRAL_HYDRO envelope
  (confirmed: 113-envelope JetStream replay, 0 stage readings). Without
  00065, compute_threshold_state() always returns "normal", the
  upward-crossing check never fires, and this handler broadcasts nothing.

  The threshold/flood-stage machinery and IDAHO_CURATED_SITES thresholds
  are correct and should be preserved. The adapter will become active if/when
  USGS sites serving Idaho gage height data are added to the curated list.

  Enrichment idea (parked same date): if NWS issues a flood warning (FFW/FLW)
  and a NWIS gauge in the same county is above action stage, annotating the
  NWS wire with the gauge reading was considered. Blocked by the same 00065
  data gap — revisit if stage data becomes available.
"""
from __future__ import annotations
from meshai.adapter_config import adapter_config

import json
import logging
import time
from datetime import datetime
from typing import Any, Optional

from meshai.central.idaho_gauge_sites import (
    compute_threshold_state,
    lookup_site,
    normalize_site_id,
)
from meshai.persistence import get_db

logger = logging.getLogger(__name__)


# v0.6-3b: handled parameter codes + recede toggle live in
# adapter_config.usgs_nwis. Default {"00060", "00065"}.

# Human-readable label per threshold_state.
_LABEL = {
    "action":         "action stage",
    "flood_minor":    "minor flooding",
    "flood_moderate": "moderate flooding",
    "flood_major":    "major flooding",
}


def _now() -> int: return int(time.time())


def _coerce_severity(sev: Any) -> Optional[str]:
    if sev is None: return None
    if isinstance(sev, str): return sev or None
    try: return str(int(sev))
    except (TypeError, ValueError): return str(sev)


def _parse_iso_epoch(s: Optional[str]) -> Optional[int]:
    if not s: return None
    try: return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception: return None


def handle_nwis(envelope: dict, subject: str,
                 data: Optional[dict] = None,
                 now: Optional[int] = None) -> Optional[str]:
    if not isinstance(envelope, dict): return None
    inner = envelope.get("data") or {}
    if (inner.get("adapter") or "") != "nwis": return None

    d = inner.get("data") or {}
    now = now if now is not None else _now()
    category_raw = inner.get("category") or ""
    severity_word = _coerce_severity(inner.get("severity"))

    try:
        conn = get_db()
    except Exception:
        logger.exception("nwis_handler: persistence unavailable")
        return None

    # Normalize site_id + look up the curated entry.
    raw_site = d.get("monitoring_location_id") or d.get("site_id")
    site_id = normalize_site_id(raw_site)
    site_meta = lookup_site(raw_site) if raw_site else None

    # Drop non-curated sites at entrance.
    if site_meta is None:
        _log_event(conn, now=now, source="nwis", category=category_raw,
                    severity_word=severity_word,
                    event_id_external=raw_site or inner.get("id"),
                    subject=subject, handled=0,
                    table_name=None, table_pk=None)
        return None

    # Drop unsupported parameters (precip etc.).
    pc = d.get("parameter_code")
    if pc not in set(adapter_config.usgs_nwis.parameter_codes):
        _log_event(conn, now=now, source="nwis", category=category_raw,
                    severity_word=severity_word,
                    event_id_external=site_id,
                    subject=subject, handled=0,
                    table_name=None, table_pk=None)
        return None

    # Extract reading value + reading_time.
    value = d.get("value")
    if isinstance(value, str):
        try: value = float(value)
        except ValueError: value = None
    if not isinstance(value, (int, float)):
        _log_event(conn, now=now, source="nwis", category=category_raw,
                    severity_word=severity_word, event_id_external=site_id,
                    subject=subject, handled=0,
                    table_name=None, table_pk=None)
        return None
    value = float(value)

    reading_time = _parse_iso_epoch(d.get("time")) or now
    unit = d.get("unit_of_measure") or ("ft^3/s" if pc == "00060" else "ft")

    # Compute threshold_state. ONLY parameter_code=00065 (stage in ft) maps
    # to threshold_state -- discharge (cfs) lands as a companion field.
    stage_ft: Optional[float] = value if pc == "00065" else None
    flow_cfs: Optional[float] = value if pc == "00060" else None
    threshold_state = "normal"
    if pc == "00065":
        threshold_state = compute_threshold_state(stage_ft, site_meta)

    lat = d.get("latitude") if isinstance(d.get("latitude"), (int, float)) else site_meta.get("lat")
    lon = d.get("longitude") if isinstance(d.get("longitude"), (int, float)) else site_meta.get("lon")

    # Build the canonical dict the decider + formatter read. parameter_code is
    # carried so the decider can perform the 00060 discharge back-look; it is
    # not part of the formatter's wire schema.
    canonical: dict = {
        "site_id": site_id,
        "gauge_name": site_meta["gauge_name"],
        "stage_ft": stage_ft,
        "flow_cfs": flow_cfs,
        "unit": unit,
        "threshold_state": threshold_state,
        "reading_time": reading_time,
        "lat": lat,
        "lon": lon,
        "parameter_code": pc,
    }

    # Always log the envelope to event_log. Initial handled=0; commit
    # callback flips to 1 if we actually broadcast.
    log_id = _log_event_returning_id(
        conn, now=now, source="nwis", category=category_raw,
        severity_word=severity_word, event_id_external=site_id,
        subject=subject, handled=0,
        table_name="gauge_readings", table_pk=site_id)

    # Delegate the threshold-crossing decision (prior-reading SELECT, 00060
    # stage back-look, THRESHOLD_RANK upward-crossing check, broadcast_on_recede
    # toggle) to the gating module. It reads gauge_readings for prior state
    # BEFORE the inline INSERT below, preserving the original ordering. It
    # returns the resolved (back-looked) threshold_state + stage_ft in
    # data_patch so the INSERT + render use the same values on every path.
    from meshai.notifications.gating.hydro import decide as _gate_decide
    gate = _gate_decide(canonical, source="nwis", now=float(now))
    threshold_state = gate.data_patch.get("threshold_state", threshold_state)
    stage_ft = gate.data_patch.get("stage_ft", stage_ft)
    canonical["threshold_state"] = threshold_state
    canonical["stage_ft"] = stage_ft

    # INSERT the new reading row INLINE (append-only time-series). Always
    # persist, regardless of the broadcast decision, and AFTER the decider's
    # reads so they never see the current row. Per the refactor plan this
    # write stays handler-owned; the decider only READS gauge_readings.
    conn.execute(
        "INSERT INTO gauge_readings(site_id, gauge_name, reading_value, "
        "reading_unit, threshold_state, flow_cfs, reading_time, lat, lon) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (site_id, site_meta["gauge_name"], value, unit,
         threshold_state, flow_cfs, reading_time, lat, lon),
    )

    if not gate.broadcast:
        return None

    # Cutover gate: mirror quake_handler. When "stream_flow" is cut over, write
    # gate.data_patch and wrap gate.commit so it also flips event_log.handled.
    # Hydro has no per-event broadcast-state table, so gate.commit is None and
    # the wrapper carries only the event_log flip. Otherwise old-style
    # _attach_commit keeps the live broadcast byte-for-byte identical while the
    # new formatter+decider bake in shadow.
    from meshai.notifications.cutover import is_cutover
    if isinstance(data, dict):
        data.update(canonical)
        if is_cutover("stream_flow"):
            data.update(gate.data_patch)
            data["_broadcast_audit"] = {"table": "gauge_readings", "pk": site_id}

            _raw_commit = gate.commit
            _log_row_id = log_id

            def _on_commit(committed_at: float) -> None:
                if _raw_commit is not None:
                    _raw_commit(committed_at)
                if _log_row_id is not None:
                    try:
                        c = get_db()
                        c.execute("UPDATE event_log SET handled=1 WHERE id=?",
                                  (int(_log_row_id),))
                    except Exception:
                        logger.exception("nwis commit: event_log update failed")

            data["_on_broadcast_committed"] = _on_commit
        else:
            _attach_commit(data, site_id=site_id, event_log_row_id=log_id)

    # Return _render() wire for backward-compat (existing call-sites + tests).
    # At dispatch time compose_mesh_message() uses the registered formatter
    # (formatters/hydro.py) on cutover, re-rendering byte-identically from data.
    return _render(gauge_name=site_meta["gauge_name"],
                    threshold_state=threshold_state,
                    stage_ft=stage_ft, flow_cfs=flow_cfs,
                    unit=unit if pc == "00065" else "ft",
                    lat=lat, lon=lon)


# ---- renderer ------------------------------------------------------------


def _render(*, gauge_name: str, threshold_state: str,
             stage_ft: Optional[float], flow_cfs: Optional[float],
             unit: str, lat: Optional[float], lon: Optional[float]) -> str:
    label = _LABEL.get(threshold_state, threshold_state)

    # Stage segment.
    if isinstance(stage_ft, (int, float)):
        stage_seg = f"{label} {stage_ft:.1f} ft"
    else:
        stage_seg = label

    # Optional flow segment.
    flow_seg = ""
    if isinstance(flow_cfs, (int, float)):
        flow_seg = f", flow {int(round(flow_cfs)):,} cfs"

    # Optional coords segment.
    coords = ""
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        coords = f", @ {lat:.3f},{lon:.3f}"

    return f"🌊 New: {gauge_name}: {stage_seg}{flow_seg}{coords}"


# ---- commit callback -----------------------------------------------------


def _attach_commit(data: Optional[dict], *, site_id: str,
                    event_log_row_id: Optional[int]) -> None:
    if not isinstance(data, dict): return

    def _on_commit(committed_at: float) -> None:
        try: conn = get_db()
        except Exception:
            logger.exception("nwis commit: persistence unavailable"); return
        if event_log_row_id is not None:
            conn.execute("UPDATE event_log SET handled=1 WHERE id=?",
                          (int(event_log_row_id),))

    data["_on_broadcast_committed"] = _on_commit
    data["_broadcast_audit"] = {"table": "gauge_readings", "pk": site_id}


# ---- event_log helpers ---------------------------------------------------


def _log_event(conn, *, now, source, category, severity_word,
                event_id_external, subject, handled, table_name, table_pk) -> None:
    conn.execute(
        "INSERT INTO event_log(received_at, source, category, severity_word, "
        "event_id_external, nats_subject, handled, table_name, table_pk) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (now, source, category, severity_word, event_id_external, subject,
         int(bool(handled)), table_name, table_pk))


def _log_event_returning_id(conn, *, now, source, category, severity_word,
                              event_id_external, subject, handled,
                              table_name, table_pk) -> int:
    cur = conn.execute(
        "INSERT INTO event_log(received_at, source, category, severity_word, "
        "event_id_external, nats_subject, handled, table_name, table_pk) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (now, source, category, severity_word, event_id_external, subject,
         int(bool(handled)), table_name, table_pk))
    return int(cur.lastrowid)
