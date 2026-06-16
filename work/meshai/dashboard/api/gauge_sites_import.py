"""v0.6-tail item 3: gauge_sites bulk-import endpoint.

POST /api/gauge-sites/import
    body:
        {"format": "csv",       "data": "<CSV text with header row>"}
      OR {"format": "nws-ahps", "wfo":  ["BOI", "PIH", ...]}

CSV path:
    Header must include site_id, gauge_name, lat, lon. Optional:
    action_ft, flood_minor_ft, flood_moderate_ft, flood_major_ft, enabled.
    Each row is UPSERTed by site_id. Returns count of inserted, updated,
    skipped (parse-failure) rows.

NWS-AHPS path:
    Fetches https://water.weather.gov/ahps2/index.php?wfo=<WFO> for each
    requested WFO, extracts gauge links + names + thresholds via regex
    on the gauge-detail pages, UPSERTs each. Hard cap on detail-page
    fetches per call to keep the request bounded.

Both paths call invalidate_curation_cache() on success so the next
nwis_handler.lookup_site() call sees the new rows.
"""
from __future__ import annotations

import csv
import io
import logging
import re
import time
from typing import Any, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request

from meshai.persistence.curation import invalidate_curation_cache


logger = logging.getLogger(__name__)
router = APIRouter(tags=["curation"])


_AHPS_DETAIL_FETCH_CAP = 50           # hard cap on gauge detail pages per call
_AHPS_TIMEOUT_S = 8.0
_AHPS_BASE = "https://water.weather.gov/ahps2"


# ============================================================================
# CSV
# ============================================================================


_NUMERIC_FIELDS = ("lat", "lon", "action_ft", "flood_minor_ft",
                    "flood_moderate_ft", "flood_major_ft")
_REQUIRED = ("site_id", "gauge_name", "lat", "lon")


def _csv_upsert(conn, text: str) -> dict[str, int]:
    inserted = updated = skipped = 0
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise HTTPException(400, "CSV must have a header row")
    missing = [c for c in _REQUIRED if c not in reader.fieldnames]
    if missing:
        raise HTTPException(400, f"CSV missing required columns: {missing}")
    now = time.time()
    for row in reader:
        try:
            site_id = (row.get("site_id") or "").strip()
            gauge_name = (row.get("gauge_name") or "").strip()
            if not site_id or not gauge_name:
                skipped += 1; continue
            lat = float(row["lat"]); lon = float(row["lon"])
            kwargs = {"action_ft": None, "flood_minor_ft": None,
                       "flood_moderate_ft": None, "flood_major_ft": None}
            for f in ("action_ft", "flood_minor_ft", "flood_moderate_ft", "flood_major_ft"):
                v = row.get(f)
                if v is not None and str(v).strip() != "":
                    kwargs[f] = float(v)
            enabled_raw = row.get("enabled")
            enabled = 1
            if enabled_raw is not None and str(enabled_raw).strip().lower() in ("false", "0", "no"):
                enabled = 0
        except (TypeError, ValueError) as e:
            skipped += 1
            logger.debug("csv import: row skipped %s: %s", row, e)
            continue

        existing = conn.execute(
            "SELECT 1 FROM gauge_sites WHERE site_id=?", (site_id,)
        ).fetchone()
        conn.execute(
            "INSERT INTO gauge_sites(site_id, gauge_name, lat, lon, "
            "action_ft, flood_minor_ft, flood_moderate_ft, flood_major_ft, "
            "enabled, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(site_id) DO UPDATE SET "
            "gauge_name=excluded.gauge_name, lat=excluded.lat, lon=excluded.lon, "
            "action_ft=excluded.action_ft, flood_minor_ft=excluded.flood_minor_ft, "
            "flood_moderate_ft=excluded.flood_moderate_ft, flood_major_ft=excluded.flood_major_ft, "
            "enabled=excluded.enabled, updated_at=excluded.updated_at",
            (site_id, gauge_name, lat, lon,
             kwargs["action_ft"], kwargs["flood_minor_ft"],
             kwargs["flood_moderate_ft"], kwargs["flood_major_ft"],
             enabled, now),
        )
        if existing: updated += 1
        else:        inserted += 1

    return {"inserted": inserted, "updated": updated, "skipped": skipped}


# ============================================================================
# NWS-AHPS scrape (best-effort)
# ============================================================================


# Gauge id rows in the WFO index appear as
#   <a href="hydrograph.php?gage=GAUGE&...">GAUGE Name</a>
# Format varies by WFO; we use a forgiving pattern.
_GAUGE_LINK_RE = re.compile(
    r'href="hydrograph\.php\?gage=([a-z0-9]+)[^"]*"[^>]*>([^<]+)</a>',
    re.IGNORECASE,
)

# On the detail page (hydrograph.php?gage=X) thresholds appear in a tabular
# format like "Action Stage ... 5.5 ft". We grep on common patterns.
_THRESHOLD_PATTERNS = {
    "action_ft":         re.compile(r"Action(?:\s*Stage)?[^0-9]{1,60}([0-9.]+)\s*ft", re.IGNORECASE),
    "flood_minor_ft":    re.compile(r"Minor(?:\s*Flood(?:\s*Stage)?)?[^0-9]{1,60}([0-9.]+)\s*ft", re.IGNORECASE),
    "flood_moderate_ft": re.compile(r"Moderate(?:\s*Flood(?:\s*Stage)?)?[^0-9]{1,60}([0-9.]+)\s*ft", re.IGNORECASE),
    "flood_major_ft":    re.compile(r"Major(?:\s*Flood(?:\s*Stage)?)?[^0-9]{1,60}([0-9.]+)\s*ft", re.IGNORECASE),
}
_LATLON_RE = re.compile(
    r"Latitude[^0-9\-]{1,40}([0-9.\-]+)[\s\S]{1,200}?Longitude[^0-9\-]{1,40}([0-9.\-]+)",
    re.IGNORECASE,
)


def _ahps_parse_index(html: str) -> list[tuple[str, str]]:
    """Return [(gauge_id, gauge_name), ...] from the WFO index page."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in _GAUGE_LINK_RE.finditer(html or ""):
        gid = m.group(1).strip().lower()
        name = re.sub(r"\s+", " ", m.group(2)).strip()
        if gid and gid not in seen:
            seen.add(gid)
            out.append((gid, name))
    return out


def _ahps_parse_detail(html: str) -> dict[str, Any]:
    """Pull lat/lon + thresholds from the gauge detail HTML."""
    out: dict[str, Any] = {
        "lat": None, "lon": None,
        "action_ft": None, "flood_minor_ft": None,
        "flood_moderate_ft": None, "flood_major_ft": None,
    }
    if not html: return out
    m = _LATLON_RE.search(html)
    if m:
        try:
            out["lat"] = float(m.group(1)); out["lon"] = float(m.group(2))
        except ValueError:
            pass
    for key, pat in _THRESHOLD_PATTERNS.items():
        m = pat.search(html)
        if m:
            try: out[key] = float(m.group(1))
            except ValueError: pass
    return out


def _ahps_upsert(conn, wfos: list[str]) -> dict[str, Any]:
    """Best-effort: fetch each WFO index, then up to _AHPS_DETAIL_FETCH_CAP
    gauge detail pages per call across all WFOs. Returns a summary."""
    inserted = updated = skipped = 0
    fetched_detail = 0
    errors: list[str] = []

    with httpx.Client(timeout=_AHPS_TIMEOUT_S, follow_redirects=True) as cli:
        for wfo in wfos:
            wfo = (wfo or "").strip().upper()
            if not wfo or not wfo.isalnum():
                errors.append(f"WFO {wfo!r}: invalid"); continue
            url = f"{_AHPS_BASE}/index.php?wfo={wfo}"
            try:
                r = cli.get(url)
                if r.status_code != 200:
                    errors.append(f"WFO {wfo}: index status {r.status_code}")
                    continue
                gauges = _ahps_parse_index(r.text)
            except Exception as e:
                errors.append(f"WFO {wfo}: index fetch {e}"); continue

            for gid, gname in gauges:
                if fetched_detail >= _AHPS_DETAIL_FETCH_CAP:
                    errors.append(
                        f"detail-fetch cap ({_AHPS_DETAIL_FETCH_CAP}) "
                        f"reached; remaining gauges skipped"
                    )
                    break
                fetched_detail += 1
                detail_url = f"{_AHPS_BASE}/hydrograph.php?gage={gid}"
                try:
                    r2 = cli.get(detail_url)
                    if r2.status_code != 200:
                        skipped += 1; continue
                    parsed = _ahps_parse_detail(r2.text)
                except Exception:
                    skipped += 1; continue

                if parsed["lat"] is None or parsed["lon"] is None:
                    skipped += 1; continue

                # AHPS gauge ids are the NWS id; we store as 'AHPS-<id>' so
                # they don't collide with USGS-* ids managed elsewhere.
                site_id = f"AHPS-{gid.upper()}"
                now = time.time()
                existing = conn.execute(
                    "SELECT 1 FROM gauge_sites WHERE site_id=?", (site_id,)
                ).fetchone()
                conn.execute(
                    "INSERT INTO gauge_sites(site_id, gauge_name, lat, lon, "
                    "action_ft, flood_minor_ft, flood_moderate_ft, flood_major_ft, "
                    "enabled, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(site_id) DO UPDATE SET "
                    "gauge_name=excluded.gauge_name, lat=excluded.lat, lon=excluded.lon, "
                    "action_ft=COALESCE(excluded.action_ft, action_ft), "
                    "flood_minor_ft=COALESCE(excluded.flood_minor_ft, flood_minor_ft), "
                    "flood_moderate_ft=COALESCE(excluded.flood_moderate_ft, flood_moderate_ft), "
                    "flood_major_ft=COALESCE(excluded.flood_major_ft, flood_major_ft), "
                    "updated_at=excluded.updated_at",
                    (site_id, gname, parsed["lat"], parsed["lon"],
                     parsed["action_ft"], parsed["flood_minor_ft"],
                     parsed["flood_moderate_ft"], parsed["flood_major_ft"],
                     1, now),
                )
                if existing: updated += 1
                else:        inserted += 1

    return {
        "inserted": inserted,
        "updated":  updated,
        "skipped":  skipped,
        "detail_fetched": fetched_detail,
        "errors":   errors,
    }


# ============================================================================
# Route
# ============================================================================


@router.post("/gauge-sites/import")
async def gauge_sites_import(request: Request) -> dict:
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "body must be a JSON object")
    fmt = (body.get("format") or "").lower().strip()
    if fmt not in ("csv", "nws-ahps"):
        raise HTTPException(400, "format must be 'csv' or 'nws-ahps'")

    from meshai.persistence import get_db
    conn = get_db()

    if fmt == "csv":
        text = body.get("data")
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(400, "csv body must include 'data' (CSV text)")
        result = _csv_upsert(conn, text)
    else:  # nws-ahps
        wfos = body.get("wfo") or []
        if isinstance(wfos, str):
            wfos = [wfos]
        if not isinstance(wfos, list) or not wfos:
            raise HTTPException(400, "nws-ahps body must include 'wfo' (list or string)")
        result = _ahps_upsert(conn, wfos)

    if result.get("inserted", 0) or result.get("updated", 0):
        invalidate_curation_cache()
    logger.info("gauge_sites import (%s): %s", fmt, result)
    return result
