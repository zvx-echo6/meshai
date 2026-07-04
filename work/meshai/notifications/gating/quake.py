"""Earthquake event gating decider — Phase-1 reference implementation.

Mirrors quake_handler._should_broadcast + first-sighting logic EXACTLY.
All thresholds are read from adapter_config.usgs_quake (same source as the
handler) so GUI edits take effect on the next event without restart.

decide(data, *, source, now) -> GateResult:
    Canonical data schema consumed:
        magnitude, depth_km, lat, lon, place, tsunami, pager,
        occurred_at, event_id

    Emitted data_patch keys:
        distance_km         float  — haversine km to Idaho centroid
        is_update           bool   — always False (v0.5.9 no-Update rule)
        _severity_override  str|None — "immediate" for tsunami/PAGER, else None
        _dedup_suffix       str    — empty (bare event.id serves as dedup key)

    commit(now: float) -> None:
        Idempotent UPSERT: sets last_broadcast_at + first_broadcast_at on the
        quake_events row.  Safe to call N times (ON CONFLICT DO UPDATE).

Gate logic (preserved verbatim from quake_handler._should_broadcast):
    (a) tsunami at any magnitude            → broadcast
    (b) PAGER alert in broadcast_pager_alerts → broadcast
    (c) magnitude >= global_mag_floor (3.0) → broadcast
    (d) magnitude >= regional_mag_floor (2.5) AND within regional_radius_mi
        of regional_centroid (Idaho centroid 44.36, -114.61) → broadcast
    else                                    → suppress

First-sighting rule (v0.5.9 no-Update rule preserved):
    Check quake_events.last_broadcast_at for the event_id.
    None row OR last_broadcast_at IS NULL → broadcast (lifecycle="new")
    last_broadcast_at IS NOT NULL          → suppress (already broadcast)
"""
from __future__ import annotations

import logging
import math
from typing import Optional

from meshai.adapter_config import adapter_config
from meshai.notifications.gating.base import GateResult
from meshai.persistence import get_db

logger = logging.getLogger(__name__)


# ── Haversine distance (miles) ──────────────────────────────────────────────

def _haversine_mi(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R_mi = 3958.8
    p1 = math.radians(lat1); p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1); dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R_mi * math.atan2(math.sqrt(a), math.sqrt(1 - a))


_MI_TO_KM = 1.60934


def _within_regional_radius(lat: float, lon: float) -> tuple[bool, float]:
    """Return (within_radius, distance_km).

    Reads centroid + radius from adapter_config so GUI edits apply live.
    Mirrors quake_handler.within_250mi_of_idaho exactly.
    """
    if not (isinstance(lat, (int, float)) and isinstance(lon, (int, float))):
        return False, 0.0
    try:
        cen = adapter_config.usgs_quake.regional_centroid
        radius_mi = float(adapter_config.usgs_quake.regional_radius_mi)
    except Exception:
        cen = [44.36, -114.61]
        radius_mi = 250.0
    dist_mi = _haversine_mi(lat, lon, float(cen[0]), float(cen[1]))
    return dist_mi <= radius_mi, dist_mi * _MI_TO_KM


def _should_broadcast(
    mag: Optional[float],
    lat: Optional[float],
    lon: Optional[float],
    tsunami: bool,
    pager: Optional[str],
) -> bool:
    """Broadcast gate — verbatim copy of quake_handler._should_broadcast."""
    if tsunami:
        return True
    try:
        pager_set = {s.lower() for s in adapter_config.usgs_quake.broadcast_pager_alerts}
    except Exception:
        pager_set = {"orange", "red"}
    if pager and pager.lower() in pager_set:
        return True
    if not isinstance(mag, (int, float)):
        return False
    try:
        global_floor = float(adapter_config.usgs_quake.global_mag_floor)
        regional_floor = float(adapter_config.usgs_quake.regional_mag_floor)
    except Exception:
        global_floor = 3.0
        regional_floor = 2.5
    if mag >= global_floor:
        return True
    if mag >= regional_floor:
        within, _ = _within_regional_radius(lat, lon)
        if within:
            return True
    return False


def _severity_override_for(tsunami: bool, pager: Optional[str]) -> Optional[str]:
    """Derive _severity_override from tsunami + PAGER flags.

    tsunami / PAGER → "immediate" (mass-casualty threshold).
    Routine quake   → None (uses Central envelope severity via map_severity).
    """
    if tsunami:
        return "immediate"
    try:
        pager_set = {s.lower() for s in adapter_config.usgs_quake.broadcast_pager_alerts}
    except Exception:
        pager_set = {"orange", "red"}
    if pager and pager.lower() in pager_set:
        return "immediate"
    return None


# ── Public API ───────────────────────────────────────────────────────────────

def decide(data: dict, *, source: str, now: float) -> GateResult:
    """Gate + first-sighting decision for earthquake_event.

    Parameters
    ----------
    data:
        Canonical Event.data dict (see module docstring for schema).
        Reads both canonical keys (magnitude, depth_km, lat, lon, pager) and
        USGS-raw fallback keys (mag, depth, latitude, longitude, alert) so the
        function works for both the Central path (where handle_quake writes
        canonical keys into the shared data dict) and the native path (where
        to_event() supplies the canonical dict directly).
    source:
        Adapter source name, e.g. "usgs_quake".
    now:
        Current epoch (from clock.now()) — determinism seam, no time.time().

    Returns
    -------
    GateResult with:
        broadcast=True   lifecycle="new"      data_patch+commit populated
        broadcast=False  lifecycle="suppress"  data_patch={} commit=None
    """
    # ── Extract canonical fields (with raw-key fallbacks) ───────────────────
    mag = data.get("magnitude") if data.get("magnitude") is not None else data.get("mag")
    if isinstance(mag, str):
        try:
            mag = float(mag)
        except ValueError:
            mag = None
    elif isinstance(mag, (int, float)):
        mag = float(mag)

    depth_km = data.get("depth_km") if data.get("depth_km") is not None else data.get("depth")
    lat = data.get("lat") if data.get("lat") is not None else data.get("latitude")
    lon = data.get("lon") if data.get("lon") is not None else data.get("longitude")
    place = data.get("place")
    tsunami = bool(data.get("tsunami") or data.get("tsunami_warning"))
    pager = data.get("pager") if data.get("pager") is not None else data.get("alert")
    occurred_at = data.get("occurred_at")
    event_id = data.get("event_id")

    if not event_id:
        return GateResult(
            broadcast=False, lifecycle="suppress",
            reason="no event_id in canonical data",
        )

    # ── Magnitude gate ───────────────────────────────────────────────────────
    if not _should_broadcast(mag, lat, lon, tsunami, pager):
        return GateResult(
            broadcast=False, lifecycle="suppress",
            reason=f"below threshold mag={mag} lat={lat} lon={lon} "
                   f"tsunami={tsunami} pager={pager}",
        )

    # ── Compute distance_km for data_patch ──────────────────────────────────
    within_region, distance_km = _within_regional_radius(lat, lon)

    # ── First-sighting check via quake_events ────────────────────────────────
    try:
        conn = get_db()
    except Exception:
        logger.exception("quake decide: persistence unavailable")
        return GateResult(
            broadcast=False, lifecycle="suppress",
            reason="persistence unavailable",
        )

    row = conn.execute(
        "SELECT last_broadcast_at FROM quake_events WHERE event_id=?",
        (event_id,),
    ).fetchone()

    # Already broadcast → no-Update rule (v0.5.9): suppress revision
    if row is not None and row["last_broadcast_at"] is not None:
        return GateResult(
            broadcast=False, lifecycle="suppress",
            reason=f"already broadcast at {row['last_broadcast_at']}",
        )

    # First sighting: INSERT OR IGNORE to create the row.  Idempotent —
    # a concurrent arrival for the same event_id is safe (ON CONFLICT IGNORE).
    conn.execute(
        "INSERT OR IGNORE INTO quake_events"
        "(event_id, magnitude, depth_km, place, lat, lon, "
        "occurred_at, tsunami_warning, first_seen_at, last_broadcast_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            event_id,
            mag,
            depth_km,
            place,
            lat if isinstance(lat, (int, float)) else None,
            lon if isinstance(lon, (int, float)) else None,
            int(occurred_at) if occurred_at is not None else None,
            1 if tsunami else 0,
            int(now),
            None,  # armed by commit() on confirmed delivery
        ),
    )

    # ── Build data_patch and commit closure ──────────────────────────────────
    sev_override = _severity_override_for(tsunami, pager)

    patch: dict = {
        "distance_km": distance_km,
        "is_update": False,           # v0.5.9 no-Update rule preserved
        "_severity_override": sev_override,
        "_dedup_suffix": "",          # bare event.id suffices for quake dedup
    }

    def _commit(committed_at: float) -> None:
        """Idempotent UPSERT: arm last_broadcast_at on confirmed delivery."""
        try:
            c = get_db()
            c.execute(
                "UPDATE quake_events "
                "SET last_broadcast_at=?, "
                "first_broadcast_at=COALESCE(first_broadcast_at, ?) "
                "WHERE event_id=?",
                (int(committed_at), int(committed_at), event_id),
            )
        except Exception:
            logger.exception("quake commit: persistence update failed for %s", event_id)

    return GateResult(
        broadcast=True,
        lifecycle="new",
        reason=f"first sighting M{mag} event_id={event_id}",
        data_patch=patch,
        commit=_commit,
    )
