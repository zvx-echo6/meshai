"""v0.6-5 env_reporter -- LLM-facing summaries of every adapter table.

Mirrors the mesh_reporter pattern: pre-renders text blocks that
router.generate_llm_response() appends to the system prompt. The LLM
consumes the text; there is no tool-use / MCP / SQL pass-through (per
audit doc Section C: keep the existing prompt-injection contract).

Every build_*_detail() method honors adapter_meta.include_in_llm_context.
When a user (via /api/adapter-meta PUT) turns that off for an adapter,
its data drops out of build_env_summary AND its own detail block
returns an empty string -- the LLM never sees rows from that adapter.

Read-only library; no writes to any table.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


# Length budget per block. The LLM has a finite context window; we cap each
# block at this many characters to keep the assembled prompt sane.
# v0.6-tail item 2: configurable via adapter_config.pipeline.env_reporter_block_chars.
_DEFAULT_BLOCK_MAX_CHARS = 3000


def _block_cap() -> int:
    """Read the per-block char cap from adapter_config; fall back to
    the conservative 3000-char default if the row isn't seeded yet."""
    try:
        from meshai.adapter_config import adapter_config
        v = adapter_config.pipeline.env_reporter_block_chars
        return int(v) if v else _DEFAULT_BLOCK_MAX_CHARS
    except Exception:
        return _DEFAULT_BLOCK_MAX_CHARS


# ============================================================================
# Reporter
# ============================================================================


class EnvReporter:
    """Build pre-rendered LLM context blocks from the adapter tables."""

    def __init__(self, conn_factory=None):
        """Args:
            conn_factory: callable returning a sqlite3.Connection. Defaults
                to meshai.persistence.get_db. Tests can pass an in-memory
                connection.
        """
        if conn_factory is None:
            from meshai.persistence import get_db
            self._conn_factory = get_db
        else:
            self._conn_factory = conn_factory

    # ---------- meta gate ---------------------------------------------------

    def _adapter_included(self, adapter: str) -> bool:
        """Read adapter_meta.include_in_llm_context. Missing meta defaults to True
        (defensive: don't silently exclude a new adapter)."""
        try:
            conn = self._conn_factory()
            r = conn.execute(
                "SELECT include_in_llm_context FROM adapter_meta WHERE adapter=?",
                (adapter,),
            ).fetchone()
            if r is None:
                return True
            return bool(r["include_in_llm_context"])
        except Exception:
            logger.exception("env_reporter: meta lookup failed for %s", adapter)
            return True

    # ---------- top-level summary -------------------------------------------

    def build_env_summary(self, *, now: Optional[int] = None) -> str:
        """One-line-per-adapter top-line: counts + the most-active item per
        included adapter. Skips adapters with include_in_llm_context=False
        AND skips adapters whose tables are empty.
        """
        now = now if now is not None else int(time.time())
        try:
            conn = self._conn_factory()
        except Exception:
            logger.exception("env_reporter: db unavailable")
            return ""

        lines: list[str] = ["ENVIRONMENTAL CONTEXT (compiled from local SQLite tables):"]

        if self._adapter_included("wfigs"):
            n_fires = self._scalar(conn,
                "SELECT COUNT(*) FROM fires WHERE last_event_at >= ? "
                "AND tombstoned_at IS NULL "
                "AND (current_contained_pct IS NULL OR current_contained_pct < 100)",
                (now - 7 * 86400,))
            if n_fires:
                lines.append(f"  Active fires (WFIGS, last 7d): {n_fires}")

        if self._adapter_included("firms"):
            n_pixels = self._scalar(conn,
                "SELECT COUNT(*) FROM firms_pixels WHERE acq_time >= ?",
                (now - 24 * 3600,))
            if n_pixels:
                lines.append(f"  FIRMS hotspots (last 24h): {n_pixels}")

        if self._adapter_included("nws"):
            n_alerts = self._scalar(conn,
                "SELECT COUNT(*) FROM nws_alerts WHERE expires_at IS NULL OR expires_at >= ?",
                (now,))
            if n_alerts:
                lines.append(f"  NWS active alerts: {n_alerts}")

        if self._adapter_included("usgs_quake"):
            n_q = self._scalar(conn,
                "SELECT COUNT(*) FROM quake_events WHERE occurred_at >= ?",
                (now - 24 * 3600,))
            if n_q:
                lines.append(f"  USGS earthquakes (last 24h): {n_q}")

        if self._adapter_included("usgs_nwis"):
            n_g = self._scalar(conn,
                "SELECT COUNT(DISTINCT site_id) FROM gauge_readings "
                "WHERE reading_time >= ? AND threshold_state != 'normal'",
                (now - 24 * 3600,))
            if n_g:
                lines.append(f"  Stream gauges above action stage (24h): {n_g}")

        if self._adapter_included("swpc"):
            r = conn.execute(
                "SELECT event_type, occurred_at FROM swpc_events "
                "WHERE occurred_at >= ? ORDER BY occurred_at DESC LIMIT 1",
                (now - 24 * 3600,)
            ).fetchone()
            if r:
                lines.append(f"  Latest SWPC event (24h): {r['event_type']}")
            # Band conditions latest broadcast
            r2 = conn.execute(
                "SELECT scheduled_for FROM band_conditions_broadcasts "
                "WHERE sent_at IS NOT NULL ORDER BY scheduled_for DESC LIMIT 1"
            ).fetchone()
            if r2 and self._adapter_included("band_conditions"):
                ts = _fmt_epoch(r2["scheduled_for"])
                lines.append(f"  Last band-conditions broadcast: {ts}")

        if self._adapter_included("tomtom_incidents") or \
                self._adapter_included("itd_511") or \
                self._adapter_included("state_511_atis"):
            n_t = self._scalar(conn,
                "SELECT COUNT(*) FROM traffic_events WHERE last_seen_at >= ?",
                (now - 2 * 3600,))
            if n_t:
                lines.append(f"  Active traffic incidents (last 2h): {n_t}")

        if len(lines) == 1:
            # Only the header; nothing to report.
            return ""
        return "\n".join(lines)[:_block_cap()]

    # ---------- per-adapter detail blocks ----------------------------------

    def build_fires_detail(self, *, near: Optional[tuple[float, float]] = None,
                            radius_mi: float = 100.0,
                            limit: int = 10,
                            now: Optional[int] = None) -> str:
        if not (self._adapter_included("wfigs") or self._adapter_included("firms")):
            return ""
        now = now if now is not None else int(time.time())
        try: conn = self._conn_factory()
        except Exception: return ""

        lines: list[str] = []

        if self._adapter_included("wfigs"):
            _cutoff = now - 7 * 86400
            _cols = ("irwin_id, incident_name, current_acres, "
                     "current_contained_pct, lat, lon, county, state, "
                     "declared_at, last_event_at")
            _where = ("last_event_at >= ? AND tombstoned_at IS NULL "
                      "AND (current_contained_pct IS NULL OR current_contained_pct < 100)")
            rows = conn.execute(
                f"SELECT {_cols} FROM fires WHERE {_where} "
                "ORDER BY last_event_at DESC LIMIT ?",
                (_cutoff, limit),
            ).fetchall()

            if rows:
                lines.append("ACTIVE WILDFIRES (WFIGS, last 7d, most recent first):")
                for r in rows:
                    name = r["incident_name"] or "(unnamed)"
                    acres = "?" if r["current_acres"] is None else f"{int(r['current_acres']):,} ac"
                    cont = "?" if r["current_contained_pct"] is None else f"{r['current_contained_pct']}%"
                    loc_parts = [p for p in (r["county"], r["state"]) if p]
                    loc = " / ".join(loc_parts) if loc_parts else (
                        f"{r['lat']:.3f},{r['lon']:.3f}" if r["lat"] is not None else "loc?")
                    declared = _fmt_epoch(r["declared_at"]) if r["declared_at"] else "?"
                    lines.append(f"  - {name}: {acres}, {cont} contained, {loc}, declared {declared}")

        if self._adapter_included("firms"):
            n_pixels = self._scalar(conn,
                "SELECT COUNT(*) FROM firms_pixels WHERE acq_time >= ?",
                (now - 24 * 3600,))
            n_high = self._scalar(conn,
                "SELECT COUNT(*) FROM firms_pixels WHERE acq_time >= ? AND confidence='high'",
                (now - 24 * 3600,))
            if n_pixels:
                lines.append(
                    f"FIRMS HOTSPOTS (NASA satellite, last 24h): "
                    f"{n_pixels} pixels total, {n_high} high-confidence"
                )

        return ("\n".join(lines) if lines else "")[:_block_cap()]

    def build_alerts_detail(self, *, region: Optional[str] = None,
                              limit: int = 10,
                              now: Optional[int] = None) -> str:
        if not self._adapter_included("nws"):
            return ""
        now = now if now is not None else int(time.time())
        try: conn = self._conn_factory()
        except Exception: return ""

        q = ("SELECT alert_type, severity, headline, county, state, expires_at "
             "FROM nws_alerts WHERE expires_at IS NULL OR expires_at >= ?")
        args: list = [now]
        if region:
            q += " AND state = ?"
            args.append(region.upper().lstrip("US-"))
        q += " ORDER BY expires_at DESC LIMIT ?"
        args.append(limit)

        rows = conn.execute(q, tuple(args)).fetchall()
        if not rows:
            return ""
        lines = ["ACTIVE NWS ALERTS:"]
        for r in rows:
            kind = r["alert_type"] or "Alert"
            sev = r["severity"] or "?"
            loc = " / ".join(p for p in (r["county"], r["state"]) if p) or "?"
            expires = _fmt_epoch(r["expires_at"]) if r["expires_at"] else "no expiry"
            head = (r["headline"] or "")[:90]
            lines.append(f"  - [{sev}] {kind} ({loc}): {head} -- until {expires}")
        return "\n".join(lines)[:_block_cap()]

    def build_quakes_detail(self, *, hours: int = 24,
                              limit: int = 10,
                              now: Optional[int] = None) -> str:
        if not self._adapter_included("usgs_quake"):
            return ""
        now = now if now is not None else int(time.time())
        try: conn = self._conn_factory()
        except Exception: return ""

        rows = conn.execute(
            "SELECT magnitude, depth_km, place, lat, lon, occurred_at, tsunami_warning "
            "FROM quake_events WHERE occurred_at >= ? "
            "ORDER BY occurred_at DESC LIMIT ?",
            (now - hours * 3600, limit),
        ).fetchall()
        if not rows:
            return ""
        lines = [f"RECENT EARTHQUAKES (last {hours}h):"]
        for r in rows:
            mag = f"M{r['magnitude']:.1f}" if r["magnitude"] is not None else "M?"
            depth = f"{int(r['depth_km'])}km" if r["depth_km"] is not None else "?"
            place = r["place"] or f"{r['lat']:.2f},{r['lon']:.2f}" if r["lat"] else "?"
            when = _fmt_epoch(r["occurred_at"]) if r["occurred_at"] else "?"
            ts = " TSUNAMI" if r["tsunami_warning"] else ""
            lines.append(f"  - {mag} {place}, {depth} depth, {when}{ts}")
        return "\n".join(lines)[:_block_cap()]

    def build_generic_detail(self, *, hours: int = 24,
                               limit: int = 20,
                               now: Optional[int] = None) -> str:
        """LLM block for config-driven generic sources (env/generic_http.py).

        Groups active generic_events by source, showing recent items (title +
        a couple of key mapped data fields) so a mesh user can ask the LLM
        about ANY configured feed. Honors the ``generic_http`` adapter's
        include_in_llm_context gate.
        """
        if not self._adapter_included("generic_http"):
            return ""
        now = now if now is not None else int(time.time())
        try: conn = self._conn_factory()
        except Exception: return ""

        try:
            rows = conn.execute(
                "SELECT source, event_id, category, title, lat, lon, severity, "
                "data_json, last_seen FROM generic_events "
                "WHERE last_seen >= ? ORDER BY source, last_seen DESC",
                (now - hours * 3600,),
            ).fetchall()
        except Exception:
            return ""
        if not rows:
            return ""

        # Bucket by source, capping items per source so no single feed hogs
        # the block budget.
        by_source: dict = {}
        for r in rows:
            by_source.setdefault(r["source"], []).append(r)

        lines = [f"CONFIGURED DATA SOURCES (last {hours}h):"]
        for src, items in by_source.items():
            cat = items[0]["category"] or "generic"
            lines.append(f"{src} ({cat}): {len(items)} item(s)")
            for r in items[:limit]:
                title = r["title"] or r["event_id"]
                extras = []
                try:
                    data = json.loads(r["data_json"]) if r["data_json"] else {}
                except Exception:
                    data = {}
                for k, v in data.items():
                    if k.startswith("_") or k in ("title", "latitude",
                                                  "longitude", "geometry"):
                        continue
                    if v is None:
                        continue
                    extras.append(f"{k}: {v}")
                    if len(extras) >= 3:
                        break
                tail = (" — " + ", ".join(extras)) if extras else ""
                lines.append(f"  - {title}{tail}")
        return "\n".join(lines)[:_block_cap()]

    def build_traffic_detail(self, *, state: Optional[str] = "ID",
                               hours: int = 2,
                               limit: int = 10,
                               now: Optional[int] = None) -> str:
        if not any(self._adapter_included(a) for a in
                    ("tomtom_incidents", "itd_511", "state_511_atis")):
            return ""
        now = now if now is not None else int(time.time())
        try: conn = self._conn_factory()
        except Exception: return ""

        q = ("SELECT source, road, direction, sub_type, impact, county, "
             "delay_seconds, last_seen_at FROM traffic_events "
             "WHERE last_seen_at >= ?")
        args: list = [now - hours * 3600]
        if state:
            q += " AND state = ?"
            args.append(state)
        q += " ORDER BY last_seen_at DESC LIMIT ?"
        args.append(limit)

        rows = conn.execute(q, tuple(args)).fetchall()
        if not rows:
            return ""
        lines = [f"ACTIVE TRAFFIC INCIDENTS (last {hours}h):"]
        for r in rows:
            road = r["road"] or "road?"
            direction = r["direction"] or ""
            sub = r["sub_type"] or "incident"
            county = r["county"] or "?"
            impact = f", {r['impact']}" if r["impact"] else ""
            delay = ""
            if r["delay_seconds"] and r["delay_seconds"] > 0:
                delay = f", {int(r['delay_seconds']/60)} min delay"
            when = _fmt_epoch(r["last_seen_at"])
            lines.append(f"  - {road} {direction} ({county}): {sub}{impact}{delay}, seen {when}")
        return "\n".join(lines)[:_block_cap()]

    def build_work_zones_detail(self, *, region: Optional[str] = None,
                                  limit: int = 10,
                                  now: Optional[int] = None) -> str:
        """Active WZDx work zones (road, location, impact, end date).

        build_traffic_detail() deliberately excludes these -- it only sources
        tomtom_incidents/itd_511/state_511_atis AND filters ``state = 'ID'``,
        but native wzdx rows carry ``source='wzdx'`` and ``state=NULL`` (see
        env/wzdx.py::to_event canonical_data), so they never surfaced on the
        DM path. This is the dedicated wzdx reader, region-scoped when a
        region name is passed (matches a configured coverage area's .name via
        event_region_names -- same derivation the wzdx_summary scheduler and
        Dispatcher.dispatch_scheduled_roads_broadcast use).
        """
        if not self._adapter_included("wzdx"):
            return ""
        now = now if now is not None else int(time.time())
        try: conn = self._conn_factory()
        except Exception: return ""

        try:
            rows = conn.execute(
                "SELECT road, direction, sub_type, impact, lat, lon, end_at "
                "FROM traffic_events "
                "WHERE source='wzdx' AND (end_at IS NULL OR end_at >= ?) "
                "ORDER BY end_at ASC LIMIT ?",
                (now, limit),
            ).fetchall()
        except Exception:
            return ""
        if not rows:
            return ""

        if region:
            # Region scoping needs the live coverage areas + a synthetic event
            # per row; guarded so a lookup failure degrades to unscoped output
            # rather than dropping the whole block.
            try:
                from meshai.coverage_area import areas_from_config, event_region_names
                from meshai.notifications.events import make_event
                from meshai.config import load_config as _load_config
                cfg = _load_config()
                areas = areas_from_config(getattr(cfg, "coverage", None))
                scoped = []
                for r in rows:
                    lat, lon = r["lat"], r["lon"]
                    if lat is None or lon is None:
                        continue
                    ev = make_event(source="wzdx", category="work_zone",
                                      lat=float(lat), lon=float(lon))
                    if region in (event_region_names(ev, areas) or []):
                        scoped.append(r)
                rows = scoped
            except Exception:
                logger.exception(
                    "env_reporter: work_zones region scoping failed for %r; "
                    "returning unscoped block", region)
            if not rows:
                return ""

        header = (f"ACTIVE WORK ZONES ({region}):" if region
                  else "ACTIVE WORK ZONES (WZDx):")
        lines = [header]
        for r in rows:
            road = r["road"] or "road?"
            direction = f" {r['direction']}" if r["direction"] else ""
            impact = r["impact"] or (r["sub_type"] or "work zone")
            ends = _fmt_epoch(r["end_at"]) if r["end_at"] else "no end date"
            lines.append(f"  - {road}{direction}: {impact}, ends {ends}")
        return "\n".join(lines)[:_block_cap()]

    def build_gauges_detail(self, *, limit: int = 10,
                              now: Optional[int] = None) -> str:
        if not self._adapter_included("usgs_nwis"):
            return ""
        now = now if now is not None else int(time.time())
        try: conn = self._conn_factory()
        except Exception: return ""

        # Most-recent reading per site, then filter to above-action sites.
        rows = conn.execute(
            "SELECT site_id, gauge_name, reading_value, reading_unit, "
            "threshold_state, flow_cfs, reading_time, lat, lon "
            "FROM gauge_readings "
            "WHERE reading_time >= ? "
            "GROUP BY site_id "
            "HAVING MAX(reading_time) "
            "ORDER BY reading_time DESC LIMIT ?",
            (now - 24 * 3600, limit),
        ).fetchall()
        if not rows:
            return ""
        lines = ["LATEST STREAM GAUGE READINGS (24h):"]
        for r in rows:
            ts_state = r["threshold_state"] or "?"
            value = (f"{r['reading_value']:.1f} {r['reading_unit']}"
                      if r['reading_value'] is not None else "?")
            flow = (f", flow {int(r['flow_cfs']):,} cfs"
                     if r['flow_cfs'] is not None else "")
            name = r["gauge_name"] or r["site_id"]
            lines.append(f"  - {name}: {value} ({ts_state}){flow}")
        return "\n".join(lines)[:_block_cap()]

    def build_swpc_detail(self, *, hours: int = 24,
                            now: Optional[int] = None) -> str:
        included_swpc = self._adapter_included("swpc")
        included_bc = self._adapter_included("band_conditions")
        if not (included_swpc or included_bc):
            return ""
        now = now if now is not None else int(time.time())
        try: conn = self._conn_factory()
        except Exception: return ""

        lines: list[str] = []
        if included_swpc:
            rows = conn.execute(
                "SELECT event_type, severity_int, occurred_at FROM swpc_events "
                "WHERE occurred_at >= ? "
                "ORDER BY occurred_at DESC LIMIT 5",
                (now - hours * 3600,),
            ).fetchall()
            if rows:
                lines.append(f"RECENT SPACE WEATHER (last {hours}h):")
                for r in rows:
                    when = _fmt_epoch(r["occurred_at"])
                    sev = f", severity {r['severity_int']}" if r["severity_int"] is not None else ""
                    lines.append(f"  - {r['event_type']}{sev}, {when}")

        if included_bc:
            r = conn.execute(
                "SELECT scheduled_for, ratings_json, source FROM band_conditions_broadcasts "
                "WHERE sent_at IS NOT NULL ORDER BY scheduled_for DESC LIMIT 1"
            ).fetchone()
            if r:
                lines.append(f"LATEST BAND CONDITIONS:")
                lines.append(f"  Scheduled for {_fmt_epoch(r['scheduled_for'])} "
                              f"(source: {r['source']})")
                if r["ratings_json"]:
                    lines.append(f"  Ratings: {r['ratings_json']}")

        return ("\n".join(lines) if lines else "")[:_block_cap()]

    def build_satpass_detail(self, *, hours: int = 24,
                              limit: int = 8,
                              now: Optional[int] = None) -> str:
        """Upcoming satellite passes from satpass_events (AOS in the near
        future). Data is written by the native satpass path; this is the
        missing LLM reader so the bot can answer satellite-pass questions.
        """
        if not self._adapter_included("satpass"):
            return ""
        now = now if now is not None else int(time.time())
        try: conn = self._conn_factory()
        except Exception: return ""

        rows = conn.execute(
            "SELECT sat_name, norad_id, observer, max_elevation, aos_at, los_at "
            "FROM satpass_events WHERE aos_at IS NOT NULL "
            "AND aos_at >= ? AND aos_at <= ? "
            "ORDER BY aos_at ASC LIMIT ?",
            (now, now + hours * 3600, limit),
        ).fetchall()
        if not rows:
            return ""
        lines = [f"UPCOMING SATELLITE PASSES (next {hours}h):"]
        for r in rows:
            name = r["sat_name"] or (
                f"NORAD {r['norad_id']}" if r["norad_id"] is not None else "sat?")
            aos = _fmt_epoch(r["aos_at"]) if r["aos_at"] else "?"
            el = (f"max el {int(r['max_elevation'])}°"
                   if r["max_elevation"] is not None else "el ?")
            obs = f" @ {r['observer']}" if r["observer"] else ""
            lines.append(f"  - {name}: AOS {aos}, {el}{obs}")
        return "\n".join(lines)[:_block_cap()]

    def build_avalanche_detail(self, *, limit: int = 10,
                                now: Optional[int] = None) -> str:
        """Current (non-expired) avalanche advisories from avalanche_events.
        NAADS danger scale 1-5. Off-season the table is empty -> empty block.
        """
        if not self._adapter_included("avalanche"):
            return ""
        now = now if now is not None else int(time.time())
        try: conn = self._conn_factory()
        except Exception: return ""

        rows = conn.execute(
            "SELECT zone_name, center_id, danger_level, danger_name, "
            "travel_advice, expires_at FROM avalanche_events "
            "WHERE expires_at IS NULL OR expires_at >= ? "
            "ORDER BY danger_level DESC, zone_name ASC LIMIT ?",
            (now, limit),
        ).fetchall()
        if not rows:
            return ""
        lines = ["AVALANCHE ADVISORIES (avalanche.org, NAADS 1-5):"]
        for r in rows:
            zone = r["zone_name"] or "(zone?)"
            dl = r["danger_level"]
            level = r["danger_name"] or "?"
            dl_str = f"{level} ({dl})" if dl is not None else level
            center = f" [{r['center_id']}]" if r["center_id"] else ""
            advice = (r["travel_advice"] or "")[:90]
            advice_str = f" -- {advice}" if advice else ""
            lines.append(f"  - {zone}{center}: {dl_str}{advice_str}")
        return "\n".join(lines)[:_block_cap()]

    def build_ducting_detail(self, *, now: Optional[int] = None) -> str:
        """Latest tropospheric ducting / RF-propagation assessment from
        ducting_events (single current row per location, most recent first).
        Lets the LLM answer band-opening / extended-UHF-range questions.
        """
        if not self._adapter_included("ducting"):
            return ""
        try: conn = self._conn_factory()
        except Exception: return ""

        rows = conn.execute(
            "SELECT lat, lon, condition, tier, min_gradient, duct_base_m, "
            "duct_thickness_m, assessment, assessed_at FROM ducting_events "
            "ORDER BY assessed_at DESC LIMIT 3",
        ).fetchall()
        if not rows:
            return ""
        lines = ["RF PROPAGATION (tropospheric ducting assessment):"]
        for r in rows:
            tier = r["tier"] or r["condition"] or "?"
            assessment = r["assessment"] or ""
            mg = (f", min M-gradient {r['min_gradient']}/km"
                   if r["min_gradient"] is not None else "")
            base = r["duct_base_m"]
            thick = r["duct_thickness_m"]
            duct = ""
            if base is not None and thick:
                duct = f", duct base {int(base)} m ~{int(thick)} m thick"
            loc = (f"{r['lat']:.2f},{r['lon']:.2f}"
                    if r["lat"] is not None else "loc?")
            when = _fmt_epoch(r["assessed_at"])
            assess_str = f": {assessment}" if assessment else ""
            lines.append(f"  - {loc} [{tier}]{assess_str}{mg}{duct} (as of {when})")
        return "\n".join(lines)[:_block_cap()]

    def build_drop_audit(self, *, hours: int = 1) -> str:
        """Why-was-X-dropped: event_log handled=0 grouped by source+reason
        + the dispatcher_state cumulative counters."""
        now = int(time.time())
        try: conn = self._conn_factory()
        except Exception: return ""

        lines = []
        try:
            rows = conn.execute(
                "SELECT source, category, COUNT(*) AS n FROM event_log "
                "WHERE handled=0 AND received_at >= ? "
                "GROUP BY source, category ORDER BY n DESC LIMIT 15",
                (now - hours * 3600,),
            ).fetchall()
            if rows:
                lines.append(f"FILTERED ENVELOPES (event_log handled=0, last {hours}h):")
                for r in rows:
                    lines.append(f"  - {r['source']:<18s} {r['category']:<40s} n={r['n']}")
        except Exception:
            pass

        # Dispatcher cumulative counters.
        try:
            r = conn.execute(
                "SELECT stale_dropped, cooldown_dropped, dedup_dropped, "
                "cold_start_dropped, cold_start_anchor FROM dispatcher_state WHERE id=1"
            ).fetchone()
            if r:
                lines.append(
                    f"DISPATCHER COUNTERS (cumulative since install): "
                    f"stale={r['stale_dropped']} cooldown={r['cooldown_dropped']} "
                    f"dedup={r['dedup_dropped']} cold_start={r['cold_start_dropped']}"
                )
        except Exception:
            pass

        return ("\n".join(lines) if lines else "")[:_block_cap()]

    def build_all(self, *, now: Optional[int] = None) -> str:
        """Convenience: summary + every detail block. Used by router when the
        env scope is detected. Empty blocks are dropped."""
        parts = [
            self.build_env_summary(now=now),
            self.build_fires_detail(now=now),
            self.build_alerts_detail(now=now),
            self.build_quakes_detail(now=now),
            self.build_traffic_detail(now=now),
            self.build_work_zones_detail(now=now),
            self.build_gauges_detail(now=now),
            self.build_swpc_detail(now=now),
            self.build_satpass_detail(now=now),
            self.build_avalanche_detail(now=now),
            self.build_ducting_detail(now=now),
            self.build_generic_detail(now=now),
        ]
        return "\n\n".join(p for p in parts if p)

    # ---------- helpers -----------------------------------------------------

    @staticmethod
    def _scalar(conn, sql, args=()) -> int:
        try:
            r = conn.execute(sql, args).fetchone()
            if r is None: return 0
            val = r[0]
            return int(val) if val is not None else 0
        except Exception:
            return 0


# ---------- module-level helpers -----------------------------------------


def _fmt_epoch(epoch) -> str:
    """Format an epoch second as ISO-ish UTC for the LLM prompt."""
    if not epoch:
        return "?"
    try:
        dt = datetime.fromtimestamp(int(epoch), tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return "?"


# Module-level singleton for the router to use.
env_reporter = EnvReporter()
