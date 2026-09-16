"""Watch Duty (WD) enrichment adapter.

Watch Duty is an ENRICHMENT source for WFIGS fires meshai already tracks in
the ``fires`` table -- it never creates a fire. This adapter matches an
already-broadcast WFIGS fire (a ``fires`` row) to a Watch Duty geo_event by
proximity (see ``match_fires``) and stamps the match onto the existing row:
``watchduty_event_id`` / ``watchduty_name`` / ``watchduty_matched_at`` /
``watchduty_is_active``. Once matched, the fire's own ``watchduty_name`` and
Watch Duty's incident link (``incident_url``) are surfaced on every fire
alert about that fire (see notifications/formatters/fire.py).

Candidate fires are deliberately restricted to ones meshai actually
broadcast about (``last_broadcast_at IS NOT NULL`` and recent) -- that is
what keeps matching inside meshai's own coverage area without a separate
geographic filter of its own.

Group B (evacuation alerts, ``_build_evac_readings``/``decide_evac``) and
Group C (report-message alerts, ``_poll_reports``/``decide_report``) both
build on the columns this module's migration (v31) ships. ``get_events()``
drains and clears the pending evac + report readings together; ``to_event()``
branches on each reading's ``"type"`` key ("evac" or "report").
"""

import hashlib
import html
import json
import logging
import re
import time
from datetime import datetime
from typing import TYPE_CHECKING, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from meshai.adapter_config import adapter_config

if TYPE_CHECKING:
    from ..config import WatchDutyConfig
    from ..notifications.events import Event

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/26.2 Safari/605.1.15"
)

_MAX_BACKOFF_SECONDS = 3600  # 1h cap


def _cfg_str(config, attr: str, default: str) -> str:
    """Read a string config field, falling back to `default` if absent,
    empty, or not a real string (e.g. an unconfigured test mock)."""
    value = getattr(config, attr, None)
    return value if isinstance(value, str) and value else default


# ── HTML / text helpers (pure, module-level) ────────────────────────────────

_BLOCK_CLOSE_RE = re.compile(r"</\s*(p|div|li|ul|ol|h[1-6])\s*>", re.IGNORECASE)
_BLOCK_OPEN_RE = re.compile(r"<\s*(p|div|li|ul|ol|h[1-6])\b[^>]*>", re.IGNORECASE)
_BR_RE = re.compile(r"<\s*br\s*/?\s*>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_HSPACE_RE = re.compile(r"[ \t\r\f\v]+")


def strip_html(s) -> str:
    """Unescape entities and turn HTML markup into plain text.

    Block-level tags (p/div/li/ul/ol/h1-6) and <br> become line breaks so
    paragraphs/list items don't run together; every other tag (e.g. <a>) is
    simply removed. Whitespace is collapsed, blank lines dropped.
    """
    if not s:
        return ""
    text = html.unescape(str(s))
    text = _BR_RE.sub("\n", text)
    text = _BLOCK_CLOSE_RE.sub("\n", text)
    text = _BLOCK_OPEN_RE.sub("", text)
    text = _TAG_RE.sub("", text)
    lines = [_HSPACE_RE.sub(" ", ln).strip() for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if ln)


_EVAC_DICT_KEYS = ("text", "message", "description", "title", "name", "body", "summary")


def _evac_part_from_dict(d: dict) -> str:
    for key in _EVAC_DICT_KEYS:
        value = d.get(key)
        if isinstance(value, str) and value.strip():
            return strip_html(value)
    return ""


def normalize_evac_field(raw) -> str:
    """Normalize a Watch Duty evacuation field into a single plain-text string.

    Handles the shapes Watch Duty is known to send: None, an HTML string
    (the live shape), a list of plain strings, a list of dicts (checked
    against ``_EVAC_DICT_KEYS`` in order), and a bare dict. List items are
    joined with "; ".
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return strip_html(raw)
    if isinstance(raw, dict):
        return _evac_part_from_dict(raw)
    if isinstance(raw, list):
        parts = []
        for item in raw:
            if isinstance(item, str):
                part = strip_html(item)
            elif isinstance(item, dict):
                part = _evac_part_from_dict(item)
            elif item is not None:
                part = strip_html(str(item))
            else:
                part = ""
            if part:
                parts.append(part)
        return "; ".join(parts)
    return strip_html(str(raw))


def incident_url(event_id) -> str:
    """Watch Duty's own app link for a geo_event."""
    return f"https://app.watchduty.org/i/{event_id}"


# ── Report helpers (Group C, pure, module-level) ────────────────────────────

_WILDCAD_TEXT_RE = re.compile(r"\breported by wildcad\b", re.IGNORECASE)


def is_automated_report(report) -> bool:
    """True if a Watch Duty report was posted by an automated reporter
    (WildCAD dispatch feed) rather than a human.

    Defensive against a missing/null ``user_created``: treats it as NOT
    automated (falls through to the message-text check only).
    """
    if not isinstance(report, dict):
        return False
    user = report.get("user_created")
    if isinstance(user, dict):
        if user.get("is_default_reporter"):
            return True
        display_name = user.get("display_name") or ""
        username = user.get("username") or ""
        if "wildcad" in str(display_name).lower() or "wildcad" in str(username).lower():
            return True
    message = strip_html(report.get("message"))
    return bool(_WILDCAD_TEXT_RE.search(message))


def is_filtered_report(text: str, patterns) -> bool:
    """True if ``text`` matches any of ``patterns`` (case-insensitive
    regexes). An invalid pattern is logged and skipped, never raised."""
    if not text:
        return False
    for pattern in patterns or ():
        try:
            if re.search(pattern, text, re.IGNORECASE):
                return True
        except re.error:
            logger.warning("watchduty: invalid report_filter_patterns regex: %r", pattern)
    return False


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _report_epoch(report: dict) -> float:
    """Best-effort epoch (seconds) from a report's ``date_created``, for
    newest-first ordering. Falls back to 0.0 (oldest) on anything
    unparseable or missing -- never raises."""
    raw = report.get("date_created") if isinstance(report, dict) else None
    if not raw:
        return 0.0
    try:
        s = str(raw).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).timestamp()
    except (ValueError, TypeError):
        return 0.0


def first_sentences(text: str, n: int = 2) -> str:
    """The first ``n`` sentences of ``text``, split on [.!?] followed by
    whitespace, punctuation kept. Falls back to the whole text if there is
    no such split (e.g. no terminal punctuation)."""
    if not text:
        return ""
    parts = _SENTENCE_SPLIT_RE.split(text.strip())
    parts = [p for p in parts if p]
    if not parts:
        return text
    return " ".join(parts[:n]).strip()


# ── Evacuation level (Group B, pure, module-level) ──────────────────────────

def evac_level(wd_event) -> tuple:
    """Evacuation level + zone text from a raw Watch Duty geo_event.

    Reads the evacuation fields from the SAME nested ``data`` sub-object
    ``_wd_eligible`` reads ``is_prescribed`` from. Advisories (Level 1) are
    ignored entirely -- only "order" (Level 3 / shelter-in-place) and
    "warning" (Level 2) are actionable evac tiers here.

    Returns:
        (level, zone_text) -- level is one of "order" / "warning" / "none".
        zone_text is "" for "none". Embedded newlines (multi-paragraph WD
        HTML) are collapsed to "; ".
    """
    data = wd_event.get("data") if isinstance(wd_event, dict) else None
    if not isinstance(data, dict):
        data = {}

    order_text = normalize_evac_field(data.get("evacuation_orders"))
    shelter_text = normalize_evac_field(data.get("evacuation_shelter_in_place"))
    warning_text = normalize_evac_field(data.get("evacuation_warnings"))

    if (order_text or shelter_text
            or data.get("has_custom_evacuation_orders")
            or data.get("has_custom_evacuation_shelter_in_place")):
        parts = []
        if order_text:
            parts.append(order_text)
        if shelter_text:
            parts.append(f"Shelter in place: {shelter_text}")
        zone_text = "; ".join(parts).replace("\n", "; ")
        return "order", zone_text

    if warning_text or data.get("has_custom_evacuation_warnings"):
        return "warning", warning_text.replace("\n", "; ")

    return "none", ""


# ── Matching (pure, module-level) ───────────────────────────────────────────

def _parse_coord(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _wd_eligible(evt: dict, claimed_ids: set) -> bool:
    """WD eligibility: active, not prescribed, has parseable coordinates,
    has a stable id, and isn't already claimed by another fires row."""
    if not evt.get("is_active"):
        return False
    data = evt.get("data")
    if isinstance(data, dict) and data.get("is_prescribed"):
        return False
    if _parse_coord(evt.get("lat")) is None or _parse_coord(evt.get("lng")) is None:
        return False
    wd_id = evt.get("id")
    if wd_id is None:
        return False
    if wd_id in claimed_ids:
        return False
    return True


def match_fires(wd_events: list, candidates: list, radius_km: float,
                 claimed_ids=None) -> list:
    """Greedy nearest-first, one-to-one match of ``candidates`` (fires rows,
    each ``{"irwin_id", "lat", "lon"}``) against ``wd_events`` (raw Watch
    Duty geo_event dicts).

    Computes every (distance, irwin_id, wd_event) pair within ``radius_km``,
    sorts ascending by distance, then assigns greedily so neither side is
    ever used twice. A WD event id already present in ``claimed_ids`` (WD
    ids already stored on some OTHER fires row) is never (re)assigned.

    Returns a list of ``(irwin_id, wd_event)`` tuples -- new matches only.
    """
    from meshai.geo import haversine_distance  # returns MILES

    claimed = set(claimed_ids or ())
    eligible = [evt for evt in (wd_events or []) if _wd_eligible(evt, claimed)]

    pairs = []
    for cand in candidates or []:
        irwin_id = cand.get("irwin_id")
        c_lat = _parse_coord(cand.get("lat"))
        c_lon = _parse_coord(cand.get("lon"))
        if irwin_id is None or c_lat is None or c_lon is None:
            continue
        for evt in eligible:
            lat = _parse_coord(evt.get("lat"))
            lng = _parse_coord(evt.get("lng"))
            dist_km = haversine_distance(c_lat, c_lon, lat, lng) * 1.60934
            if dist_km <= radius_km:
                pairs.append((dist_km, irwin_id, evt))

    pairs.sort(key=lambda p: p[0])

    assigned_irwin: set = set()
    assigned_wd: set = set()
    result = []
    for _dist, irwin_id, evt in pairs:
        if irwin_id in assigned_irwin:
            continue
        wd_id = evt.get("id")
        if wd_id in assigned_wd:
            continue
        assigned_irwin.add(irwin_id)
        assigned_wd.add(wd_id)
        result.append((irwin_id, evt))
    return result


# ── Adapter ──────────────────────────────────────────────────────────────

class WatchDutyAdapter:
    """Polls Watch Duty's geo_events endpoint and matches it against
    meshai's already-broadcast WFIGS fires. Never emits its own Event
    (Groups B/C fill in get_events()/to_event())."""

    GEO_EVENTS_URL = "https://api.watchduty.org/api/v1/geo_events/"
    REPORTS_URL = "https://api.watchduty.org/api/v1/reports/"

    def __init__(self, config: "WatchDutyConfig"):
        self._config = config
        self._geo_events_url = _cfg_str(config, "geo_events_url", self.GEO_EVENTS_URL)
        self._reports_url = _cfg_str(config, "reports_url", self.REPORTS_URL)
        self._tick_interval = config.tick_seconds or 900
        self._last_tick = 0.0
        self._consecutive_errors = 0
        self._last_error = None
        self._backoff_seconds = 0.0
        self._backoff_until = 0.0
        self._is_loaded = False
        # Group B: evacuation readings built by match_and_store(), drained
        # (and cleared) by get_events(). Each match_and_store() call
        # OVERWRITES this with the complete current snapshot (see
        # _build_evac_readings).
        self._readings: list = []
        # Group C: report readings built by _poll_reports() (called from
        # tick(), after a successful match_and_store()), drained (and
        # cleared) by get_events() alongside _readings. Kept as a SEPARATE
        # list (appended to, not overwritten) because a report poll only
        # evaluates the reports fetched THAT tick -- there is no complete
        # current-snapshot to recompute the way evac readings have, so an
        # overwrite here would drop readings if get_events() were ever
        # drained less often than a report poll runs.
        self._report_readings: list = []

    # ── HTTP ─────────────────────────────────────────────────────────────

    def _headers(self) -> dict:
        """Shared request headers -- same spoofed-client headers for every
        Watch Duty endpoint (geo_events, reports)."""
        app_version = adapter_config.watchduty.app_version  # read hot, every call
        return {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en",
            "Origin": "https://app.watchduty.org",
            "Referer": "https://app.watchduty.org/",
            "User-Agent": _USER_AGENT,
            "X-App-Is-Native": "false",
            "X-App-Version": app_version,
            "X-Git-Tag": app_version,
        }

    def fetch_geo_events(self) -> list:
        """GET the Watch Duty geo_events endpoint. Returns the flat JSON
        list. Raises on a non-list / non-JSON (e.g. HTML) response."""
        params = {
            "geo_event_types": "wildfire,location",
            "ts": int(time.time() * 1000),
        }
        url = f"{self._geo_events_url}?{urlencode(params)}"
        req = Request(url, headers=self._headers())
        with urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
        data = json.loads(raw)
        if not isinstance(data, list):
            raise ValueError(
                f"watchduty: expected a JSON list from geo_events, got {type(data).__name__}")
        return data

    def fetch_reports(self, wd_event_id: str, limit: int) -> list:
        """GET the Watch Duty reports endpoint for one geo_event. Accepts a
        dict with a "results" list (the live paginated shape) or a bare
        list; raises on anything else (e.g. an HTML error page)."""
        params = {
            "geo_event_id": wd_event_id,
            "status": "approved",
            "limit": limit,
            "offset": 0,
            "ts": int(time.time() * 1000),
        }
        url = f"{self._reports_url}?{urlencode(params)}"
        req = Request(url, headers=self._headers())
        with urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            results = data.get("results")
            if isinstance(results, list):
                return results
            raise ValueError(
                "watchduty: expected a 'results' list from reports, got "
                f"{type(results).__name__}")
        if isinstance(data, list):
            return data
        raise ValueError(
            f"watchduty: expected a JSON list or dict from reports, got {type(data).__name__}")

    # ── Candidate query ──────────────────────────────────────────────────

    def _candidate_fires(self, conn, recency_window_seconds: float,
                          irwin_id: str = None) -> list:
        """Fires eligible to be newly matched: not tombstoned (closed --
        ``fires.status`` is never written anywhere in this codebase, so
        ``tombstoned_at`` is the only real "closed" signal; see
        notifications/gating/fire.py), and not already matched.

        Two modes:

        * Batch (``irwin_id`` is None -- the periodic ``tick()`` poll):
          ALSO requires ``last_broadcast_at IS NOT NULL`` and recent. This
          restricts matching to fires meshai actually broadcast about,
          which is what keeps matching inside meshai's own coverage area.

        * Single-fire (``irwin_id`` given -- the immediate post-emit lookup
          ``_ingest_fires`` schedules right after a WFIGS New/Update emit):
          the ``last_broadcast_at`` check is DROPPED. A first-sight fire is
          INSERTed with ``last_broadcast_at=NULL`` (env/store.py
          ``_ingest_fires``, the unconditional current-state write) --
          that column is only set later by the decider's deferred commit
          (notifications/gating/fire.py::_make_commit), which runs AFTER
          the broadcast is actually delivered, i.e. strictly after this
          immediate lookup runs. Requiring it here would make
          ``match_and_store(irwin_id=...)`` match nothing for every
          brand-new fire -- the exact case it exists to handle. The caller
          just emitted this fire and ingest already applied the coverage
          drop, so ``tombstoned_at IS NULL AND watchduty_event_id IS NULL``
          is sufficient on its own.
        """
        if irwin_id:
            rows = conn.execute(
                "SELECT irwin_id, lat, lon FROM fires WHERE irwin_id=? "
                "AND tombstoned_at IS NULL AND watchduty_event_id IS NULL",
                (irwin_id,),
            ).fetchall()
        else:
            cutoff = time.time() - recency_window_seconds
            rows = conn.execute(
                "SELECT irwin_id, lat, lon FROM fires WHERE tombstoned_at IS NULL "
                "AND watchduty_event_id IS NULL AND last_broadcast_at IS NOT NULL "
                "AND last_broadcast_at >= ?",
                (cutoff,),
            ).fetchall()
        return [
            {"irwin_id": r["irwin_id"], "lat": r["lat"], "lon": r["lon"]}
            for r in rows if r["lat"] is not None and r["lon"] is not None
        ]

    # ── Match + persist ──────────────────────────────────────────────────

    def match_and_store(self, irwin_id: str = None) -> int:
        """One list request, match, and write. For NEW matches, stamps
        watchduty_event_id/name/matched_at/is_active. For fires ALREADY
        matched: if the WD event is still present in this response,
        refreshes name/is_active; if it is ABSENT (WD's response is always
        the full current list, never filtered by id -- so absence means
        the incident is no longer listed as active), stamps
        watchduty_is_active=0 so ``_should_poll()`` stops treating it as a
        reason to keep polling. Applies in both batch and irwin_id mode.
        Returns the number of NEW matches."""
        from meshai.persistence import get_db
        conn = get_db()

        wd_events = self.fetch_geo_events()
        by_id = {evt.get("id"): evt for evt in wd_events if evt.get("id") is not None}

        if irwin_id:
            matched_rows = conn.execute(
                "SELECT irwin_id, watchduty_event_id FROM fires "
                "WHERE irwin_id=? AND watchduty_event_id IS NOT NULL",
                (irwin_id,),
            ).fetchall()
        else:
            matched_rows = conn.execute(
                "SELECT irwin_id, watchduty_event_id FROM fires "
                "WHERE watchduty_event_id IS NOT NULL",
            ).fetchall()

        claimed_ids = set()
        for row in matched_rows:
            claimed_ids.add(row["watchduty_event_id"])
            evt = by_id.get(row["watchduty_event_id"])
            if evt is None:
                # Absent from WD's (always-full) response list: no longer
                # active. Leave the name/event_id alone -- only is_active
                # reflects an absence.
                conn.execute(
                    "UPDATE fires SET watchduty_is_active=0 WHERE irwin_id=?",
                    (row["irwin_id"],),
                )
                continue
            conn.execute(
                "UPDATE fires SET watchduty_name=?, watchduty_is_active=? "
                "WHERE irwin_id=?",
                (evt.get("name"), 1 if evt.get("is_active") else 0, row["irwin_id"]),
            )

        radius_km = adapter_config.watchduty.match_radius_km
        recency_window_seconds = adapter_config.watchduty.recency_window_seconds
        candidates = self._candidate_fires(conn, recency_window_seconds, irwin_id)
        pairs = match_fires(wd_events, candidates, radius_km, claimed_ids)

        now = time.time()
        new_count = 0
        for cand_irwin, evt in pairs:
            wd_id = evt.get("id")
            conn.execute(
                "UPDATE fires SET watchduty_event_id=?, watchduty_name=?, "
                "watchduty_matched_at=?, watchduty_is_active=? WHERE irwin_id=?",
                (wd_id, evt.get("name"), now,
                 1 if evt.get("is_active") else 0, cand_irwin),
            )
            new_count += 1

        self._build_evac_readings(conn, by_id)

        if irwin_id and new_count > 0:
            # The post-emit single-fire lookup (env/store.py::_ingest_fires,
            # fire-and-forget right after a WFIGS New/Update emit) just
            # created a NEW match. That call never goes through tick() --
            # it calls match_and_store() directly -- so it does not touch
            # _last_tick, and the readings it just built above would
            # otherwise sit undrained (get_events()/_ingest only run when
            # tick() itself returns True) until the next regularly
            # scheduled poll, up to _tick_interval (15 min default) later.
            # Resetting _last_tick makes the very next refresh() cycle's
            # tick() call run immediately (batch match_and_store + drain),
            # so the new fire's evac state is evaluated within about a
            # second instead.
            self._last_tick = 0.0

        return new_count

    def _build_evac_readings(self, conn, by_id: dict) -> None:
        """(Re)build the pending evac-reading snapshot from ``by_id`` (the
        full Watch Duty geo_events response keyed by id, just fetched by
        this ``match_and_store`` call) for every currently-matched,
        non-tombstoned fire whose Watch Duty event is present in it.

        Overwrites (does not append to) ``self._readings`` -- each call
        recomputes the complete current snapshot from the SAME fresh
        response, so an overwrite from a later call can never lose data
        the way an accumulate-then-drain design could double-process a
        reading built by both the immediate single-fire lookup and the
        batch tick that follows it (see the ``_last_tick`` reset above).
        """
        self._readings = []
        if not adapter_config.watchduty.evac_alerts_enabled:
            return
        rows = conn.execute(
            "SELECT irwin_id, watchduty_event_id, lat, lon, county, state "
            "FROM fires WHERE watchduty_event_id IS NOT NULL "
            "AND tombstoned_at IS NULL"
        ).fetchall()
        for row in rows:
            wd_evt = by_id.get(row["watchduty_event_id"])
            if wd_evt is None:
                continue
            level, zone_text = evac_level(wd_evt)
            self._readings.append({
                "type": "evac",
                "irwin_id": row["irwin_id"],
                "wd_event_id": row["watchduty_event_id"],
                "name": wd_evt.get("name"),
                "level": level,
                "zone_text": zone_text,
                "wd_modified": wd_evt.get("date_modified"),
                "lat": row["lat"],
                "lon": row["lon"],
                "county": row["county"],
                "state": row["state"],
            })

    # ── Report polling (Group C) ────────────────────────────────────────

    _SEED_PREFIX = "seed:"

    def _report_seed_sentinel_present(self, conn, irwin_id: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM watchduty_reports_sent WHERE report_id=?",
            (f"{self._SEED_PREFIX}{irwin_id}",),
        ).fetchone()
        return row is not None

    def _seed_reports(self, conn, irwin_id: str, wd_event_id: str,
                       reports: list, now: float) -> None:
        """Lazy, sentinel-based first-poll seed (Group C, replaces the
        Group A ``_seed_existing_reports`` stub): mark every report Watch
        Duty currently has for this fire's geo_event as already-seen
        (``seeded=1``, ``sent_at`` NULL), plus a sentinel row
        ``report_id="seed:<irwin_id>"``, so a fire's pre-existing report
        backlog is never broadcast the moment report polling first reaches
        it. Runs lazily from ``_poll_reports`` (NOT from ``match_and_store``)
        so a seeding fetch failure just leaves the sentinel absent -- the
        very next poll retries the seed instead of dumping the backlog.
        """
        for report in reports:
            if not isinstance(report, dict):
                continue
            rid = report.get("id")
            if rid is None:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO watchduty_reports_sent"
                "(report_id, irwin_id, geo_event_id, sent_at, seeded, created_at) "
                "VALUES (?,?,?,?,1,?)",
                (str(rid), irwin_id, wd_event_id, None, now),
            )
        conn.execute(
            "INSERT OR IGNORE INTO watchduty_reports_sent"
            "(report_id, irwin_id, geo_event_id, sent_at, seeded, created_at) "
            "VALUES (?,?,?,?,1,?)",
            (f"{self._SEED_PREFIX}{irwin_id}", irwin_id, wd_event_id, None, now),
        )

    def _mark_report_seeded(self, conn, irwin_id: str, wd_event_id: str,
                             report_id: str, now: float) -> None:
        """INSERT OR IGNORE one report as seeded=1 (deliberately skipped:
        automated, filtered, empty text, or simply not this poll's pick) so
        it is never re-evaluated on a later poll."""
        conn.execute(
            "INSERT OR IGNORE INTO watchduty_reports_sent"
            "(report_id, irwin_id, geo_event_id, sent_at, seeded, created_at) "
            "VALUES (?,?,?,?,1,?)",
            (report_id, irwin_id, wd_event_id, None, now),
        )

    def _poll_one_fire_reports(self, conn, irwin_id: str, wd_event_id: str,
                                name: str, lat, lon, county, state,
                                limit: int, patterns: list) -> None:
        """Fetch + evaluate reports for ONE matched fire, appending at most
        one new reading to ``self._report_readings``. Raises on a fetch
        failure -- the caller (``_poll_reports``) logs and continues with
        the other fires so one bad fire never aborts the whole tick.
        """
        reports = self.fetch_reports(wd_event_id, limit)
        now = time.time()

        if not self._report_seed_sentinel_present(conn, irwin_id):
            self._seed_reports(conn, irwin_id, wd_event_id, reports, now)
            return

        existing_ids = {
            r["report_id"] for r in conn.execute(
                "SELECT report_id FROM watchduty_reports_sent WHERE irwin_id=?",
                (irwin_id,),
            ).fetchall()
        }

        qualifying = []
        for report in reports:
            if not isinstance(report, dict):
                continue
            rid = report.get("id")
            if rid is None:
                continue
            rid = str(rid)
            if rid in existing_ids:
                continue
            text = strip_html(report.get("message"))
            if is_automated_report(report) or is_filtered_report(text, patterns) or not text:
                self._mark_report_seeded(conn, irwin_id, wd_event_id, rid, now)
                continue
            qualifying.append((rid, report, text))

        if not qualifying:
            return

        # Newest first; the newest becomes the reading, every OTHER
        # qualifying report is marked seeded (deliberately skipped) -- one
        # report per fire per poll.
        qualifying.sort(key=lambda item: _report_epoch(item[1]), reverse=True)
        newest_id, newest_report, newest_text = qualifying[0]
        for rid, _report, _text in qualifying[1:]:
            self._mark_report_seeded(conn, irwin_id, wd_event_id, rid, now)

        self._report_readings.append({
            "type": "report",
            "irwin_id": irwin_id,
            "wd_event_id": wd_event_id,
            "name": name,
            "report_id": newest_id,
            "text": first_sentences(newest_text, 2),
            "date_created": newest_report.get("date_created"),
            "lat": lat,
            "lon": lon,
            "county": county,
            "state": state,
        })

    def _poll_reports(self) -> None:
        """Group C: for every matched, still-active, non-tombstoned fire,
        fetch its reports and turn at most one new qualifying report into a
        pending reading (see ``_poll_one_fire_reports``). Called from
        ``tick()`` after a successful ``match_and_store()``, only when
        ``report_alerts_enabled`` is true. A single fire's fetch error is
        logged and skipped -- it must not fail the whole tick or trigger
        the adapter's HTTP backoff (the geo_events list fetch already
        succeeded this tick).
        """
        from meshai.persistence import get_db
        conn = get_db()

        rows = conn.execute(
            "SELECT irwin_id, watchduty_event_id, watchduty_name, lat, lon, "
            "county, state FROM fires WHERE watchduty_event_id IS NOT NULL "
            "AND watchduty_is_active=1 AND tombstoned_at IS NULL"
        ).fetchall()

        limit = adapter_config.watchduty.reports_per_fire_limit
        patterns = adapter_config.watchduty.report_filter_patterns

        first = True
        for row in rows:
            if not first:
                time.sleep(1.0)
            first = False
            irwin_id = row["irwin_id"]
            wd_event_id = row["watchduty_event_id"]
            try:
                self._poll_one_fire_reports(
                    conn, irwin_id, wd_event_id, row["watchduty_name"],
                    row["lat"], row["lon"], row["county"], row["state"],
                    limit, patterns,
                )
            except Exception:
                logger.warning(
                    "watchduty: report poll failed for irwin=%s wd_event_id=%s",
                    irwin_id, wd_event_id, exc_info=True)
                continue

    # ── Polling gate ─────────────────────────────────────────────────────

    def _should_poll(self) -> bool:
        """Cheap DB-only check (no HTTP): is there anything worth polling
        for? True iff some fires row is either a still-active matched fire,
        or an eligible new candidate."""
        try:
            from meshai.persistence import get_db
            conn = get_db()
        except Exception:
            logger.exception("watchduty: _should_poll DB unavailable")
            return False
        recency_window_seconds = adapter_config.watchduty.recency_window_seconds
        cutoff = time.time() - recency_window_seconds
        row = conn.execute(
            "SELECT EXISTS("
            "  SELECT 1 FROM fires WHERE tombstoned_at IS NULL AND ("
            "    (watchduty_event_id IS NOT NULL AND watchduty_is_active=1)"
            "    OR"
            "    (watchduty_event_id IS NULL AND last_broadcast_at IS NOT NULL "
            "     AND last_broadcast_at >= ?)"
            "  )"
            ") AS has_candidate",
            (cutoff,),
        ).fetchone()
        return bool(row and row["has_candidate"])

    def tick(self) -> bool:
        """Interval-gated poll (same gate shape as NICFFiresAdapter.tick).
        Zero HTTP requests when disabled or nothing to poll for. On error:
        never raises -- logs, backs off (doubling up to 1h, reset on
        success), and records the failure for health_status.

        Group C: after a successful ``match_and_store()``, also polls Watch
        Duty's reports endpoint (``_poll_reports``) for every matched fire,
        when ``report_alerts_enabled`` is true. A report-poll failure for
        ONE fire is caught and logged inside ``_poll_reports`` itself -- it
        never reaches here, so it can't trigger this method's backoff (the
        list fetch above already succeeded this tick).
        """
        now = time.time()
        if now < self._backoff_until:
            return False
        if now - self._last_tick < self._tick_interval:
            return False
        self._last_tick = now

        if not self._config.enabled:
            return False
        if not self._should_poll():
            return False

        try:
            self.match_and_store()
        except Exception as e:
            self._consecutive_errors += 1
            self._last_error = str(e)
            if self._backoff_seconds <= 0:
                self._backoff_seconds = min(self._tick_interval, _MAX_BACKOFF_SECONDS)
            else:
                self._backoff_seconds = min(self._backoff_seconds * 2, _MAX_BACKOFF_SECONDS)
            self._backoff_until = now + self._backoff_seconds
            logger.warning("watchduty: tick failed: %s", e)
            return False

        if adapter_config.watchduty.report_alerts_enabled:
            self._poll_reports()

        self._consecutive_errors = 0
        self._last_error = None
        self._backoff_seconds = 0.0
        self._backoff_until = 0.0
        self._is_loaded = True
        return True

    # ── Event pipeline ───────────────────────────────────────────────────

    def get_events(self) -> list:
        """Drain the pending readings built by the most recent
        ``match_and_store`` (evac) and ``_poll_reports`` (report) calls.
        Returns BOTH the evac snapshot and the report readings, concatenated,
        and clears both -- a reading is handed to the store exactly once."""
        readings = self._readings + self._report_readings
        self._readings = []
        self._report_readings = []
        return readings

    def to_event(self, evt: dict) -> Optional["Event"]:
        """Translate a pending reading into a pipeline Event. Branches on
        ``evt["type"]`` -- "evac" (see ``_build_evac_readings``) or "report"
        (see ``_poll_one_fire_reports``)."""
        if evt.get("type") == "report":
            return self._report_to_event(evt)
        return self._evac_to_event(evt)

    def _evac_to_event(self, evt: dict) -> Optional["Event"]:
        """Translate an evacuation reading (see ``_build_evac_readings``)
        into a pipeline Event. Mirrors ``NICFFiresAdapter.to_event`` for
        lat/lon placement (so region routing / CoverageFilter treat this
        exactly like a WFIGS fire event); source is "watchduty", category
        "wildfire_evac", severity always "priority" (evacuation alerts are
        never routine). The event id is deterministic per
        (irwin_id, level, sha1(zone_text)[:10]) so the SAME evac state never
        mints a new id, while a genuine level or text change does.
        """
        try:
            from meshai.notifications.events import make_event

            irwin_id = evt.get("irwin_id")
            level = evt.get("level")
            if not irwin_id or not level:
                return None

            lat = evt.get("lat")
            lon = evt.get("lon")
            if lat is None or lon is None:
                return None  # no centroid -- can't make a useful Event

            name = evt.get("name") or "Wildfire"
            zone_text = evt.get("zone_text") or ""
            zone_hash = hashlib.sha1(zone_text.encode("utf-8")).hexdigest()[:10]
            event_id = f"watchduty_evac_{irwin_id}_{level}_{zone_hash}"

            return make_event(
                source="watchduty",
                category="wildfire_evac",
                severity="priority",
                title=name,
                summary=f"{name} evacuation {level}",
                lat=lat,
                lon=lon,
                group_key=event_id,
                inhibit_keys=[event_id],
                id=event_id,
                data=dict(evt),
            )
        except Exception:
            logger.exception(
                "watchduty evac to_event failed for irwin=%s", evt.get("irwin_id"))
            return None

    def _report_to_event(self, evt: dict) -> Optional["Event"]:
        """Translate a report reading (see ``_poll_one_fire_reports``) into
        a pipeline Event. Same lat/lon placement as evac; source is
        "watchduty", category "wildfire_report", severity always "routine"
        (a report message is informational, never an evacuation-grade
        alert). The event id is the report_id itself -- WD report ids are
        already unique and stable, no hashing needed.
        """
        try:
            from meshai.notifications.events import make_event

            irwin_id = evt.get("irwin_id")
            report_id = evt.get("report_id")
            if not irwin_id or not report_id:
                return None

            lat = evt.get("lat")
            lon = evt.get("lon")
            if lat is None or lon is None:
                return None  # no centroid -- can't make a useful Event

            name = evt.get("name") or "Wildfire"
            event_id = f"watchduty_report_{report_id}"

            return make_event(
                source="watchduty",
                category="wildfire_report",
                severity="routine",
                title=name,
                summary=f"{name} report",
                lat=lat,
                lon=lon,
                group_key=event_id,
                inhibit_keys=[event_id],
                id=event_id,
                data=dict(evt),
            )
        except Exception:
            logger.exception(
                "watchduty report to_event failed for irwin=%s", evt.get("irwin_id"))
            return None

    @property
    def health_status(self) -> dict:
        return {
            "source": "watchduty",
            "is_loaded": self._is_loaded,
            "last_error": str(self._last_error) if self._last_error else None,
            "consecutive_errors": self._consecutive_errors,
            "last_fetch": self._last_tick,
            "backoff_until": self._backoff_until,
        }
