"""Generic, config-driven REST/GeoJSON source adapter (meshai native).

ONE class, MANY sources. Every source is a plain dict in
``config.generic_sources[]`` describing how to poll a public REST or GeoJSON
endpoint and map its fields onto meshai Events — no Python required to add a
new feed. This is the UNIVERSAL way to wire any public data feed; the first
real use case (Idaho Power outages) is CONFIGURED, not hardcoded.

Ported from Central's ``GenericHttpAdapter`` (src/central/adapters/generic_http.py):
  * ``_dig(obj, path)`` dotted-path walker — ported verbatim.
  * settings schema (url / items_path / id_path / lat_path / lon_path /
    geometry_path / title_path / time_path / severity / category /
    field_mappings) — ported concept.
  * ``_item_to_event`` mapping — id (required, dedup), geometry-first then
    lat/lon fallback, title + field_mappings → data.

Central-coupled bits REPLACED for native meshai:
  * aiohttp → stdlib ``urllib.request`` (mirrors env/usgs_quake.py).
  * pydantic settings model → plain dicts (the adapter validates them).
  * NATS ``domain`` / ``subject_for`` → a meshai ``category`` string.
  * Central Event / Geo → meshai ``make_event`` (lat/lon carried on the
    Event so the deployed Shapely CoverageFilter gates it automatically).
"""

import json
import logging
import re
import time
from typing import TYPE_CHECKING, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from meshai.notifications.events import Event, make_event

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Browser-like default User-Agent. The old short "MeshAI/1.0" UA intermittently
# trips WAFs (Idaho Power's Azure Front Door 403s it ~2/30 requests); a
# browser UA gets 200 every time. A source can still override this (or add
# auth) via its optional per-source `headers` dict.
_BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# Only substitute {word} tokens in summary_template so stray braces in data
# values can never blow up the formatter.
_TEMPLATE_TOKEN = re.compile(r"\{(\w+)\}")


def _dig(obj, path):
    """Walk a nested dict/list by a dotted path string.

    Each segment is tried as a dict key first; if the current node is a
    list/tuple the segment is coerced to an integer index. Returns None on
    any miss (missing key, out-of-range index, wrong node type).

    Ported verbatim from Central's generic_http._dig. Examples::

        _dig({"a": {"b": 1}}, "a.b")   # 1
        _dig({"a": [10, 20]}, "a.1")    # 20
        _dig({"a": 1}, "a.b")          # None
        _dig(None, "anything")          # None
    """
    if not path:
        return None
    parts = path.split(".")
    cur = obj
    for part in parts:
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, (list, tuple)):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


class GenericHttpAdapter:
    """Config-driven REST/GeoJSON source adapter — one instance, many sources.

    meshai registers a SINGLE ``generic_http`` adapter; it internally polls
    every enabled source in ``config.generic_sources`` on that source's own
    cadence. Each source dict carries (mirrors Central's settings)::

        {
          name, enabled, url, items_path, id_path,
          lat_path, lon_path, geometry_path, title_path, time_path,
          category, poll_seconds, severity,
          field_mappings: [{source_path, dest_key}, ...],
          summary_template, emoji,
          headers: {header_name: value, ...},   # optional — UA/auth override
        }
    """

    def __init__(self, sources: list, coverage: dict = None):
        # Keep only enabled sources that carry the minimum needed to poll +
        # dedup: a url and an id_path. Everything else has sane fallbacks.
        self._sources = []
        for s in (sources or []):
            if not isinstance(s, dict):
                continue
            if not s.get("enabled", True):
                continue
            if not s.get("url") or not s.get("id_path"):
                logger.warning(
                    "generic_http: skipping source %r (needs url + id_path)",
                    s.get("name", "?"))
                continue
            self._sources.append(s)

        # Pipeline CoverageFilter (Shapely) gates lat/lon events; the adapter
        # does not hard-filter. Retained for parity / future use.
        self._coverage = coverage

        # Per-source last-poll epoch (keyed by source name) for cadence.
        self._last_poll: dict = {}
        # (source_name, event_id) -> internal event dict (current snapshot).
        self._events: dict = {}
        self._consecutive_errors = 0
        self._last_error = None
        self._is_loaded = False

    # ------------------------------------------------------------------
    # Poll cadence
    # ------------------------------------------------------------------

    def tick(self) -> bool:
        """Poll each source whose cadence has elapsed. Returns True if the set
        of (source, event_id) keys changed this tick.

        Errors are guarded PER SOURCE so one broken feed never kills the
        others — its exception is logged and the remaining sources still poll.
        """
        now = time.time()
        changed = False
        for source in self._sources:
            name = source.get("name") or source.get("url")
            cadence = source.get("poll_seconds") or 300
            last = self._last_poll.get(name, 0.0)
            if now - last < cadence:
                continue
            self._last_poll[name] = now
            try:
                if self._poll_source(source, now):
                    changed = True
            except Exception:
                logger.exception("generic_http: source %r poll failed", name)
                self._last_error = f"{name}: poll error"
                self._consecutive_errors += 1
        if changed:
            self._is_loaded = True
        return changed

    def _poll_source(self, source: dict, now: float) -> bool:
        """Fetch + map one source. Returns True if its id-set changed."""
        name = source.get("name") or source.get("url")
        raw = self._fetch(source["url"], extra_headers=source.get("headers"))
        if raw is None:
            return False

        items = _dig(raw, source.get("items_path") or "features")
        if not isinstance(items, list):
            logger.warning(
                "generic_http: source %r items_path %r did not resolve to a "
                "list (got %s)", name, source.get("items_path"),
                type(items).__name__)
            items = []

        mapped = {}
        for item in items:
            evt = self._item_to_event(item, source, now)
            if evt is not None:
                mapped[(evt["_source"], evt["event_id"])] = evt

        # Replace only THIS source's slice of the snapshot; detect change on
        # the id-set for this source.
        old_ids = {k for k in self._events if k[0] == name}
        new_ids = set(mapped.keys())
        changed = old_ids != new_ids
        for k in old_ids:
            self._events.pop(k, None)
        self._events.update(mapped)

        self._consecutive_errors = 0
        self._last_error = None
        if changed:
            logger.info("generic_http: source %r updated (%d item(s))",
                        name, len(mapped))
        return changed

    def _fetch(self, url: str, extra_headers: dict = None):
        """GET ``url`` and return parsed JSON, or None on any error.

        stdlib urllib (mirrors env/usgs_quake.py); 30s timeout. Defaults to a
        browser-like User-Agent (WAFs 403 the short "MeshAI/1.0" UA); a source's
        optional ``headers`` dict is layered on top so it can override the UA or
        add auth (e.g. ``Authorization``). Retries ONCE on a 403/429 (a WAF
        that intermittently blocks succeeds on the immediate retry).
        """
        headers = {"User-Agent": _BROWSER_UA,
                   "Accept": "application/json, text/plain, */*"}
        headers.update(extra_headers or {})
        for attempt in (1, 2):
            try:
                req = Request(url, headers=headers)
                with urlopen(req, timeout=30) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except HTTPError as e:
                # Retry once on a WAF-style block (403/429) — the live test
                # showed an immediate retry succeeds.
                if attempt == 1 and e.code in (403, 429):
                    logger.info("generic_http HTTP %s for %s — retrying once",
                                e.code, url)
                    time.sleep(1)
                    continue
                logger.warning("generic_http HTTP error %s for %s", e.code, url)
                self._last_error = f"HTTP {e.code}"
                self._consecutive_errors += 1
                return None
            except URLError as e:
                logger.warning("generic_http connection error for %s: %s",
                               url, e.reason)
                self._last_error = str(e.reason)
                self._consecutive_errors += 1
                return None
            except Exception as e:
                logger.warning("generic_http fetch error for %s: %s", url, e)
                self._last_error = str(e)
                self._consecutive_errors += 1
                return None
        return None

    # ------------------------------------------------------------------
    # Item -> internal event dict
    # ------------------------------------------------------------------

    def _item_to_event(self, item, source: dict, now: float = None):
        """Map one source item to an internal event dict, or None to skip.

        Mirrors Central's ``_item_to_event``: id is required (dedup);
        geometry-first (Point → centroid) then lat/lon fallback; title +
        field_mappings written into ``data``. The resolved geometry dict (when
        present) is carried in ``data['geometry']`` so the coverage gate can
        use it even for non-Point shapes.
        """
        now = now if now is not None else time.time()
        name = source.get("name") or source.get("url")

        # --- id (required, dedup) ---
        raw_id = _dig(item, source["id_path"])
        if raw_id is None:
            return None
        event_id = str(raw_id)

        category = source.get("category") or "generic"
        severity = source.get("severity") or "routine"

        # --- geo: geometry Point centroid first, else lat/lon paths ---
        lat = lon = None
        geometry = None
        geom_path = source.get("geometry_path")
        if geom_path:
            geom = _dig(item, geom_path)
            if isinstance(geom, dict):
                geometry = geom
                if geom.get("type") == "Point":
                    coords = geom.get("coordinates") or []
                    if len(coords) >= 2:
                        try:
                            lon = float(coords[0])
                            lat = float(coords[1])
                        except (TypeError, ValueError):
                            lat = lon = None
        if lat is None or lon is None:
            lat_path = source.get("lat_path")
            lon_path = source.get("lon_path")
            if lat_path and lon_path:
                raw_lat = _dig(item, lat_path)
                raw_lon = _dig(item, lon_path)
                if raw_lat is not None and raw_lon is not None:
                    try:
                        lat = float(raw_lat)
                        lon = float(raw_lon)
                    except (TypeError, ValueError):
                        lat = lon = None

        # --- title ---
        title = None
        if source.get("title_path"):
            t = _dig(item, source["title_path"])
            if t is not None:
                title = str(t)

        # --- data: title + coords + field_mappings + geometry + render hints ---
        data = {}
        if title is not None:
            data["title"] = title
        if lat is not None and lon is not None:
            data["latitude"] = lat
            data["longitude"] = lon
        for fm in (source.get("field_mappings") or []):
            if not isinstance(fm, dict):
                continue
            dest = fm.get("dest_key")
            src_path = fm.get("source_path")
            if not dest or not src_path:
                continue
            data[dest] = _dig(item, src_path)
        if geometry is not None:
            data["geometry"] = geometry
        data["_source"] = name
        data["_summary_template"] = source.get("summary_template")
        data["_emoji"] = source.get("emoji")

        # --- optional event time ---
        ts = now
        if source.get("time_path"):
            raw_time = _dig(item, source["time_path"])
            if raw_time is not None:
                try:
                    # epoch ms (int) or ISO-8601 string both accepted.
                    if isinstance(raw_time, (int, float)):
                        ts = float(raw_time)
                        if ts > 1e12:  # milliseconds
                            ts /= 1000.0
                except (TypeError, ValueError):
                    ts = now

        return {
            # store keys on evt["source"] + evt["event_id"]; namespace the
            # source per configured name so each feed dedups independently.
            "source": f"generic:{name}",
            "_source": name,          # bare configured name (table + cold-start)
            "event_id": event_id,
            "category": category,
            "severity": severity,
            "title": title,
            "latitude": lat,
            "longitude": lon,
            "data": data,
            "fetched_at": now,
            "quake_time": ts,          # generic event time
        }

    # ------------------------------------------------------------------
    # Internal event dict -> pipeline Event
    # ------------------------------------------------------------------

    def to_event(self, evt: dict) -> Optional["Event"]:
        """Build a meshai Event from an internal event dict.

        lat/lon (and ``data['geometry']`` when resolved) ride on the Event so
        the deployed Shapely CoverageFilter gates it with no extra wiring.
        """
        try:
            event_id = evt.get("event_id")
            if not event_id:
                return None
            summary = self._render(evt)
            return make_event(
                source="generic:" + (evt.get("_source") or "?"),
                category=evt.get("category") or "generic",
                severity=evt.get("severity") or "routine",
                title=evt.get("title") or summary,
                summary=summary,
                timestamp=evt.get("fetched_at"),
                expires=evt.get("expires"),
                lat=evt.get("latitude"),
                lon=evt.get("longitude"),
                group_key=event_id,
                inhibit_keys=[event_id],
                data=evt.get("data") or {},
            )
        except Exception:
            logger.exception("generic_http to_event failed for %s",
                             evt.get("event_id"))
            return None

    def _render(self, evt: dict) -> str:
        """Render the mesh wire string for one event.

        With ``summary_template`` set: substitute ``{key}`` tokens from the
        event's data dict (title included). Missing keys render BLANK — the
        formatter never crashes on an absent field.

        Without a template: ``"{emoji} {title}"`` plus a compact join of the
        mapped fields (``dest_key: value``). Kept short for the mesh budget.
        """
        data = evt.get("data") or {}
        template = data.get("_summary_template")
        emoji = data.get("_emoji") or ""
        title = evt.get("title") or data.get("title") or ""

        if template:
            mapping = {k: v for k, v in data.items() if not k.startswith("_")}
            if title and "title" not in mapping:
                mapping["title"] = title

            def _sub(m):
                v = mapping.get(m.group(1))
                return "" if v is None else str(v)

            return _TEMPLATE_TOKEN.sub(_sub, template).strip()

        # Default render: emoji + title + compact mapped fields.
        parts = []
        head = f"{emoji} {title}".strip()
        if head:
            parts.append(head)
        extras = []
        for k, v in data.items():
            if k.startswith("_") or k in ("title", "latitude", "longitude",
                                          "geometry"):
                continue
            if v is None:
                continue
            extras.append(f"{k}: {v}")
        if extras:
            parts.append(", ".join(extras))
        return " — ".join(parts) if parts else (evt.get("category") or "event")

    # ------------------------------------------------------------------
    # State / status
    # ------------------------------------------------------------------

    def get_active(self) -> list:
        """Current mapped events across all sources (store ingests these)."""
        return list(self._events.values())

    # Alias so the store's generic get_active(source=...) style also works.
    def get_events(self) -> list:
        return self.get_active()

    @property
    def health_status(self) -> dict:
        return {
            "source": "generic_http",
            "is_loaded": self._is_loaded,
            "last_error": str(self._last_error) if self._last_error else None,
            "consecutive_errors": self._consecutive_errors,
            "event_count": len(self._events),
            "source_count": len(self._sources),
            "last_fetch": max(self._last_poll.values()) if self._last_poll else 0.0,
        }
