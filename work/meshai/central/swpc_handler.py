"""v0.5.10 SWPC space-weather handler.

Aggressive filter -- broadcast ONLY when:
    (a) Geomagnetic storm Kp >= 7 (G3 strong or higher)
    (b) Solar flare X1+ (R3 strong radio blackout or higher)
    (c) Solar proton event >= 10 pfu @ >= 10 MeV (S1 minor radiation storm
        or higher)

All else (Kp < 7, M-class flares, S0 protons) -> swpc_events table for
history + event_log handled=0, NO broadcast.

Three Central sub-adapters all route here:
    swpc_kindex   -> check Kp threshold
    swpc_alerts   -> parse alert payload (flare class, geomag, proton scale)
    swpc_protons  -> check >=10 MeV proton flux threshold

Wire format (multi-line, matches Fire/Quake/Avalanche style):
    Line 1: {emoji} New: {scale} {type} — {key fact}
    Line 2: supporting detail (impact summary / message, truncated 120 chars)
    Line 3: SWPC · {time tag}

    Geomag:   🧲 New: G3 Geomagnetic Storm — Kp7
    Flare:    ☀️ New: X1.2 Solar Flare — R3
    Proton:   ☢️ New: S1 Radiation Storm — 10 pfu
"""
from __future__ import annotations
from meshai.adapter_config import adapter_config

import json
import logging
import re
import time
from typing import Any, Optional

from meshai.persistence import get_db

logger = logging.getLogger(__name__)


# Kp -> G-scale mapping (NOAA-defined; CODE).
_G_SCALE = {5: ("G1", "minor"), 6: ("G2", "moderate"), 7: ("G3", "strong"),
             8: ("G4", "severe"), 9: ("G5", "extreme")}

# v0.6-3b: broadcast floors live in adapter_config.swpc
# (geomag_kp_floor, flare_class_floor, proton_pfu_floor).

# Proton flux -> S-scale. >= 10 pfu @ >=10 MeV is S1.
_S_SCALE_THRESHOLDS = [
    (1e5, "S5", "extreme"),
    (1e4, "S4", "severe"),
    (1e3, "S3", "strong"),
    (1e2, "S2", "moderate"),
    (10,  "S1", "minor"),
]


# Geomag cross-sub-adapter dedup window constant — kept for documentation.
# The in-memory _geomag_recent dict has moved to meshai.notifications.gating.swpc
# (_geomag_window) where the commit closure defers the stamp.
GEOMAG_DEDUP_WINDOW_SECONDS = 600
# _geomag_recent removed: now owned by gating.swpc._geomag_window.


def _trunc(s: str, limit: int = 120) -> str:
    """Truncate *s* at the last word boundary at or before *limit* chars."""
    if len(s) <= limit:
        return s
    cut = s[:limit].rsplit(" ", 1)[0]
    if not cut:
        cut = s[:limit]
    return cut + "…"


def _now() -> int: return int(time.time())


def _coerce_float(v) -> Optional[float]:
    if v is None: return None
    if isinstance(v, (int, float)): return float(v)
    try: return float(v)
    except (TypeError, ValueError): return None


def _kp_g_scale(kp: float) -> Optional[tuple]:
    """Map Kp -> NOAA G-scale tuple. v0.6-3b: returns None when below
    adapter_config.swpc.geomag_kp_floor (default 7.0 = G3+). Extends down
    to Kp=5 (G1) when the floor is lowered."""
    floor = float(adapter_config.swpc.geomag_kp_floor)
    if kp < floor: return None
    if kp >= 9: return _G_SCALE[9]
    if kp >= 8: return _G_SCALE[8]
    if kp >= 7: return _G_SCALE[7]
    if kp >= 6: return _G_SCALE[6]
    if kp >= 5: return _G_SCALE[5]
    return None


_CLASS_RANK = {"A": 0, "B": 1, "C": 2, "M": 3, "X": 4}


def _class_score(class_str: Optional[str]) -> Optional[float]:
    """Comparable score for X-ray flare class: rank*100 + magnitude."""
    if not class_str: return None
    s = str(class_str).strip().upper()
    m = re.match(r"^([ABCMX])([0-9.]+)?", s)
    if not m: return None
    cls = m.group(1)
    try: mag = float(m.group(2)) if m.group(2) else 1.0
    except ValueError: mag = 1.0
    return _CLASS_RANK[cls] * 100 + min(mag, 99.9)


def _flare_r_scale(flare_class: Optional[str]) -> Optional[tuple]:
    """Parse 'X1.2', 'M5.5', 'C3.1' etc. Return (R-code, label, class_str).

    v0.6-3b: filters to class at-or-above adapter_config.swpc.flare_class_floor
    (default 'X1'). Default keeps prior X-only behavior. Lowered floors
    accept M-class -> R1/R2."""
    obs_score = _class_score(flare_class)
    if obs_score is None: return None
    floor_str = str(adapter_config.swpc.flare_class_floor)
    floor_score = _class_score(floor_str)
    if floor_score is None: floor_score = _CLASS_RANK["X"] * 100 + 1.0   # X1 default
    if obs_score < floor_score: return None

    s = str(flare_class).strip().upper()
    m = re.match(r"^([ABCMX])([0-9.]+)?", s)
    cls = m.group(1)
    try: mag = float(m.group(2)) if m.group(2) else 1.0
    except ValueError: mag = 1.0
    if cls == "X":
        if mag >= 20: return ("R5", "extreme", s)
        if mag >= 10: return ("R4", "severe", s)
        return ("R3", "strong", s)
    if cls == "M":
        if mag >= 5: return ("R2", "moderate", s)
        return ("R1", "minor", s)
    # B/C/A: no NOAA R-code defined -- skip even if floor allowed entry.
    return None


def _proton_s_scale(pfu: float) -> Optional[tuple]:
    """Return (S-code, label, pfu_value) for proton flux at-or-above the
    NOAA S-scale threshold.

    v0.6-3b: gated by adapter_config.swpc.proton_pfu_floor (default 10 = S1).
    The S-scale lookup itself is CODE."""
    if pfu < float(adapter_config.swpc.proton_pfu_floor):
        return None
    for thr, code, label in _S_SCALE_THRESHOLDS:
        if pfu >= thr:
            return (code, label, pfu)
    return None


def _extract_kp(d: dict) -> Optional[float]:
    for k in ("kp_index", "kp", "k_index", "kindex", "value", "estimated_kp"):
        v = d.get(k)
        f = _coerce_float(v)
        if f is not None: return f
    return None


# S1+ NOAA scale is calibrated for the >=10 MeV proton channel. Lower-
# energy channels (>=1 MeV, >=5 MeV) have much higher baseline flux and
# would trigger spurious 'storm' events. Only honor these energy labels.
_S_SCALE_RELEVANT_ENERGIES = ("10", ">=10", ">10", ">=10 MeV", ">=10MeV",
                                "30", ">=30", ">=30 MeV", ">=50 MeV",
                                ">=100 MeV", ">=100")


def _is_relevant_proton_energy(energy) -> bool:
    if energy is None:
        return False  # missing energy label -> can't validate; safer to skip
    if isinstance(energy, (int, float)):
        return energy >= 10
    s = str(energy).strip()
    return s in _S_SCALE_RELEVANT_ENERGIES


def _extract_proton_flux(d: dict) -> Optional[float]:
    """Match the 'flux at >=10 MeV' channel (or higher). Field names vary;
    explicit channel labels win. Envelopes with `energy='>=1 MeV'` or
    `'>=5 MeV'` are ALWAYS rejected -- different background floor."""
    # Explicit per-channel field names (already named after the energy).
    for k in ("p10mev", "proton_flux_10mev", "flux_10mev", "p_geq_10MeV"):
        v = d.get(k)
        f = _coerce_float(v)
        if f is not None: return f
    # Generic flux/value -- require the `energy` field to validate channel.
    energy = d.get("energy_mev") or d.get("energy")
    if _is_relevant_proton_energy(energy):
        for k in ("flux", "value", "proton_flux"):
            v = d.get(k)
            f = _coerce_float(v)
            if f is not None: return f
    return None


def _extract_flare_class(d: dict) -> Optional[str]:
    for k in ("flare_class", "class", "magnitude_class", "x_ray_class"):
        v = d.get(k)
        if v: return str(v)
    # The product_id sometimes encodes the class (e.g. "X1.2 FLARE").
    pid = d.get("product_id") or d.get("message") or ""
    m = re.search(r"\b([MX][0-9.]+)\b", str(pid).upper())
    return m.group(0) if m else None


def handle_swpc(envelope: dict, subject: str,
                 data: Optional[dict] = None,
                 now: Optional[int] = None) -> Optional[str]:
    if not isinstance(envelope, dict): return None
    inner = envelope.get("data") or {}
    adapter = inner.get("adapter") or ""
    if adapter not in ("swpc_alerts", "swpc_kindex", "swpc_protons"):
        return None

    d = inner.get("data") or {}
    now = now if now is not None else _now()
    category_raw = inner.get("category") or ""
    severity_word = _coerce_severity(inner.get("severity"))

    try:
        conn = get_db()
    except Exception:
        logger.exception("swpc_handler: persistence unavailable")
        return None

    event_id = d.get("id") or inner.get("id") or d.get("product_id")
    if not event_id:
        return None

    # Classify the event + decide.
    event_kind = None   # "geomag" | "flare" | "proton"
    scale_code = None
    label = None
    scalar_str = None

    if adapter == "swpc_kindex":
        kp = _extract_kp(d)
        if kp is not None:
            g = _kp_g_scale(kp)
            if g:
                event_kind = "geomag"
                scale_code, label = g
                scalar_str = f"Kp{int(round(kp))}"

    elif adapter == "swpc_protons":
        pfu = _extract_proton_flux(d)
        if pfu is not None:
            s = _proton_s_scale(pfu)
            if s:
                event_kind = "proton"
                scale_code, label, val = s
                scalar_str = f"{int(val) if float(val) >= 1 else val:.0f} pfu" if val >= 1 else f"{val:.1f} pfu"

    elif adapter == "swpc_alerts":
        # swpc_alerts can carry any kind. Try Kp first, flare next, proton last.
        kp = _extract_kp(d)
        if kp is not None:
            g = _kp_g_scale(kp)
            if g:
                event_kind = "geomag"; scale_code, label = g
                scalar_str = f"Kp{int(round(kp))}"
        if event_kind is None:
            fcls = _extract_flare_class(d)
            r = _flare_r_scale(fcls)
            if r:
                event_kind = "flare"; scale_code, label, cls_str = r
                scalar_str = cls_str
        if event_kind is None:
            pfu = _extract_proton_flux(d)
            if pfu is not None:
                s = _proton_s_scale(pfu)
                if s:
                    event_kind = "proton"; scale_code, label, val = s
                    scalar_str = f"{int(val)} pfu" if val >= 1 else f"{val:.1f} pfu"

    # Persist + filter.
    payload_json = None
    try: payload_json = json.dumps(d, default=str)[:8000]
    except Exception: payload_json = None
    occurred_at = None
    t = d.get("time") or d.get("issued_at") or d.get("issue_time")
    if isinstance(t, str):
        try:
            from datetime import datetime as _dt
            occurred_at = int(_dt.fromisoformat(t.replace("Z", "+00:00")).timestamp())
        except Exception: pass
    elif isinstance(t, (int, float)):
        occurred_at = int(t / 1000) if t > 1e12 else int(t)

    if event_kind is None:
        # Below threshold (routine Kp, M-class flare, S0 protons, etc).
        # Persist for history; log handled=0; no broadcast.
        _upsert_swpc(conn, event_id=event_id, adapter=adapter,
                      payload_json=payload_json, occurred_at=occurred_at or now,
                      first_seen_at=now, set_last_broadcast=False)
        _log_event(conn, now=now, source="swpc", category=category_raw,
                    severity_word=severity_word, event_id_external=event_id,
                    subject=subject, handled=0,
                    table_name="swpc_events", table_pk=event_id)
        return None

    # ── Extract detail + time tag ─────────────────────────────────────────────
    _detail = d.get("message") or d.get("description") or ""
    if isinstance(_detail, str):
        _detail = _trunc(_detail.strip())
    else:
        _detail = ""
    _time_tag = ""
    _t_raw = d.get("time") or d.get("issued_at") or d.get("issue_time") or ""
    if isinstance(_t_raw, str) and _t_raw:
        _time_tag = _t_raw[:16].replace("T", " ")

    # ── NEW ARCH: geomag + flare delegate to gating.swpc.decide() ────────────
    if event_kind in ("geomag", "flare"):
        # Build canonical data dict for the decider + formatter.
        if event_kind == "geomag":
            _kp_val = _extract_kp(d)   # idempotent re-extract
            canonical: dict = {
                "event_id": event_id,
                "driver": "kp",
                "scalar": _kp_val,
                "scale_code": scale_code,
                "message": _detail,
                "issued_at": _t_raw if _t_raw else None,
            }
        else:  # flare — scalar_str is the class string set by classification
            canonical = {
                "event_id": event_id,
                "driver": "flare",
                "scalar": scalar_str,
                "scale_code": scale_code,
                "message": _detail,
                "issued_at": _t_raw if _t_raw else None,
            }

        from meshai.notifications.gating.swpc import decide as _swpc_decide
        gate = _swpc_decide(canonical, source="swpc", now=float(now))

        if not gate.broadcast:
            logger.debug(
                "swpc_handler: geomag/flare suppressed by gating.swpc: %s",
                gate.reason,
            )
            _upsert_swpc(conn, event_id=event_id, adapter=adapter,
                          payload_json=payload_json, occurred_at=occurred_at or now,
                          first_seen_at=now, set_last_broadcast=False)
            _log_event(conn, now=now, source="swpc", category=category_raw,
                        severity_word=severity_word, event_id_external=event_id,
                        subject=subject, handled=0,
                        table_name="swpc_events", table_pk=event_id)
            return None

        log_id = _log_event_returning_id(
            conn, now=now, source="swpc", category=category_raw,
            severity_word=severity_word, event_id_external=event_id,
            subject=subject, handled=0,
            table_name="swpc_events", table_pk=event_id)

        # _upsert_swpc fills in event_type=adapter + payload_json via the
        # UPDATE path (decide() already INSERT-OR-IGNOREd the row).
        _upsert_swpc(conn, event_id=event_id, adapter=adapter,
                      payload_json=payload_json, occurred_at=occurred_at or now,
                      first_seen_at=now, set_last_broadcast=False)

        wire = _render(event_kind, scale_code, label, scalar_str,
                       is_update=False, detail=_detail, time_tag=_time_tag)

        # Cutover gate: geomag → geomagnetic_storm; flare → rf_propagation_alert.
        # Per-derived-category so geomag and flare can be cut over independently.
        from meshai.notifications.cutover import is_cutover
        _derived_cat = "geomagnetic_storm" if event_kind == "geomag" else "rf_propagation_alert"

        if isinstance(data, dict):
            data.update(canonical)
            if is_cutover(_derived_cat):
                # NEW PATH: gate.data_patch provides _severity_override, _cooldown_suffix.
                data.update(gate.data_patch)
                data["_broadcast_audit"] = {"table": "swpc_events", "pk": event_id}
                _raw_commit = gate.commit
                _log_row_id = log_id

                def _on_commit(committed_at: float,
                               _rc=_raw_commit, _lr=_log_row_id) -> None:
                    if _rc is not None:
                        _rc(committed_at)
                    if _lr is not None:
                        try:
                            c = get_db()
                            c.execute("UPDATE event_log SET handled=1 WHERE id=?",
                                      (int(_lr),))
                        except Exception:
                            logger.exception("swpc commit: event_log update failed")

                data["_on_broadcast_committed"] = _on_commit
            else:
                # NOT cutover: old-style attach (canonical only, no data_patch).
                # gate.commit is intentionally not called; geomag window does not
                # tick in shadow-bake mode (acceptable — worst case one extra
                # broadcast per restart, caught by shadow_gate diff).
                _attach_commit(data, event_id=event_id, event_log_row_id=log_id)

        return wire

    # ── LEGACY path: proton events (solar_radiation_storm) ───────────────────
    # solar_radiation_storm is NOT registered in the gating/formatter
    # registries; it stays on this inline legacy path unchanged.
    if event_kind != "proton":
        logger.warning("swpc_handler: unexpected event_kind=%r; suppressing", event_kind)
        return None

    log_id = _log_event_returning_id(
        conn, now=now, source="swpc", category=category_raw,
        severity_word=severity_word, event_id_external=event_id,
        subject=subject, handled=0,
        table_name="swpc_events", table_pk=event_id)

    row = conn.execute(
        "SELECT last_broadcast_at FROM swpc_events WHERE event_id=?",
        (event_id,)).fetchone()

    if row is None:
        _upsert_swpc(conn, event_id=event_id, adapter=adapter,
                      payload_json=payload_json, occurred_at=occurred_at or now,
                      first_seen_at=now, set_last_broadcast=False)
        wire = _render(event_kind, scale_code, label, scalar_str,
                       is_update=False, detail=_detail, time_tag=_time_tag)
        _attach_commit(data, event_id=event_id, event_log_row_id=log_id)
        return wire

    if row["last_broadcast_at"] is None:
        wire = _render(event_kind, scale_code, label, scalar_str,
                       is_update=False, detail=_detail, time_tag=_time_tag)
        _attach_commit(data, event_id=event_id, event_log_row_id=log_id)
        return wire

    # Already broadcast — no Update re-broadcast for SWPC point-in-time events.
    return None


def _render(event_kind, scale_code, label, scalar_str,
            *, is_update: bool = False, detail: str = "",
            time_tag: str = "") -> str:
    prefix = "Update:" if is_update else "New:"

    if event_kind == "geomag":
        line1 = f"🧲 {prefix} {scale_code} Geomagnetic Storm — {scalar_str}"
        line2 = _trunc(detail) if detail else "HF degraded, aurora possible"
        line3 = f"SWPC · {time_tag}" if time_tag else "SWPC"
    elif event_kind == "flare":
        line1 = f"☀️ {prefix} {scalar_str} Solar Flare — {scale_code}"
        line2 = _trunc(detail) if detail else "HF radio fading, GPS may glitch"
        line3 = f"SWPC · {time_tag}" if time_tag else "SWPC"
    elif event_kind == "proton":
        line1 = f"☢️ {prefix} {scale_code} Radiation Storm — {scalar_str}"
        line2 = _trunc(detail) if detail else "Polar HF radio affected"
        line3 = f"SWPC · {time_tag}" if time_tag else "SWPC"
    else:
        line1 = f"⚠️ {prefix} Space Weather Event — {scale_code or '?'}"
        line2 = _trunc(detail) if detail else None
        line3 = f"SWPC · {time_tag}" if time_tag else "SWPC"

    return "\n".join(l for l in [line1, line2, line3] if l)


def _upsert_swpc(conn, *, event_id, adapter, payload_json, occurred_at,
                  first_seen_at, set_last_broadcast=False, broadcast_at=None) -> None:
    existing = conn.execute(
        "SELECT 1 FROM swpc_events WHERE event_id=?", (event_id,)).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO swpc_events(event_id, event_type, severity_int, "
            "payload_json, occurred_at, first_seen_at, last_broadcast_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (event_id, adapter, None, payload_json, occurred_at,
             first_seen_at, broadcast_at if set_last_broadcast else None))
    else:
        conn.execute(
            "UPDATE swpc_events SET event_type=?, payload_json=?, occurred_at=? "
            "WHERE event_id=?",
            (adapter, payload_json, occurred_at, event_id))


def _attach_commit(data: Optional[dict], *, event_id: str,
                    event_log_row_id: Optional[int]) -> None:
    if not isinstance(data, dict): return

    def _on_commit(committed_at: float) -> None:
        try: conn = get_db()
        except Exception:
            logger.exception("swpc commit: persistence unavailable"); return
        conn.execute(
            "UPDATE swpc_events SET last_broadcast_at=?, "
            "first_broadcast_at=COALESCE(first_broadcast_at, ?) WHERE event_id=?",
            (int(committed_at), int(committed_at), event_id))
        if event_log_row_id is not None:
            conn.execute("UPDATE event_log SET handled=1 WHERE id=?",
                          (int(event_log_row_id),))

    data["_on_broadcast_committed"] = _on_commit
    data["_broadcast_audit"] = {"table": "swpc_events", "pk": event_id}


def _coerce_severity(sev: Any) -> Optional[str]:
    if sev is None: return None
    if isinstance(sev, str): return sev or None
    try: return str(int(sev))
    except (TypeError, ValueError): return str(sev)


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
