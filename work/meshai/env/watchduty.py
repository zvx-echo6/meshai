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

Groups B (evacuation alerts) and C (report alerts) build on the columns and
stub methods this module ships now; ``get_events()``/``to_event()`` are
intentionally no-ops until then.
"""

import html
import json
import logging
import re
import time
from typing import TYPE_CHECKING, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from meshai.adapter_config import adapter_config

if TYPE_CHECKING:
    from ..config import WatchDutyConfig

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

    # ── HTTP ─────────────────────────────────────────────────────────────

    def fetch_geo_events(self) -> list:
        """GET the Watch Duty geo_events endpoint. Returns the flat JSON
        list. Raises on a non-list / non-JSON (e.g. HTML) response."""
        app_version = adapter_config.watchduty.app_version  # read hot, every call
        params = {
            "geo_event_types": "wildfire,location",
            "ts": int(time.time() * 1000),
        }
        url = f"{self._geo_events_url}?{urlencode(params)}"
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en",
            "Origin": "https://app.watchduty.org",
            "Referer": "https://app.watchduty.org/",
            "User-Agent": _USER_AGENT,
            "X-App-Is-Native": "false",
            "X-App-Version": app_version,
            "X-Git-Tag": app_version,
        }
        req = Request(url, headers=headers)
        with urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
        data = json.loads(raw)
        if not isinstance(data, list):
            raise ValueError(
                f"watchduty: expected a JSON list from geo_events, got {type(data).__name__}")
        return data

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
            try:
                self._seed_existing_reports(cand_irwin, wd_id)
            except Exception:
                logger.exception(
                    "watchduty: _seed_existing_reports failed for %s", cand_irwin)
            new_count += 1

        return new_count

    def _seed_existing_reports(self, irwin_id: str, wd_event_id: str) -> None:
        """No-op stub. Group C fills this in: on first match, seed
        ``watchduty_reports_sent`` with every report Watch Duty has already
        published for this geo_event (seeded=1, sent_at=NULL) so historical
        reports never broadcast the moment a fire is matched."""
        return None

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
        success), and records the failure for health_status."""
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

        self._consecutive_errors = 0
        self._last_error = None
        self._backoff_seconds = 0.0
        self._backoff_until = 0.0
        self._is_loaded = True
        return True

    # ── Event pipeline (stubs; Groups B/C fill these in) ────────────────

    def get_events(self) -> list:
        return []

    def to_event(self, evt: dict):
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
