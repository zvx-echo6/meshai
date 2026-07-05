"""FHWA WZDx (Work Zone Data Exchange) native adapter.

Gives meshai a local source for the ``work_zone`` event category, so it no
longer depends on Central for work-zone events. Mirrors the native-adapter
shape used by ``roads511`` / ``traffic``: ``tick()`` polls on an interval and
populates ``self._events``; ``get_events()`` returns them; ``to_event()``
builds a pipeline ``Event`` with ``category="work_zone"`` and a canonical
``data`` dict; ``health_status`` reports adapter health.

Discovery + fetch
-----------------
1. Fetch the FHWA WZDx Feed Registry (keyless Socrata JSON) — each row
   describes a state DOT feed (``feedname``/``url``/``apiurl``/``state``/
   ``version``/``format``). The registry rarely changes, so it is cached for
   ``registry_ttl`` seconds (default 6h).
2. Filter registry rows to the configured ``states`` that publish a GeoJSON
   WZDx feed, collect their feed URLs.
3. On each tick, fetch every matching feed (WZDx v4 GeoJSON
   ``FeatureCollection``) and parse each ``work-zone`` road_event feature.

A single ``base_url`` override short-circuits registry discovery (poll that
one feed directly). A down state feed is logged + skipped (never crashes the
adapter). Keyless — no ``api_key`` required. stdlib HTTP only (urllib), same
as the other native adapters.

Canonical ``work_zone`` data shape
----------------------------------
Field mapping reuses ``central_normalizer._parse_wzdx_federal`` verbatim so
the wire is identical to what the Central-side parser would have produced
(road, direction, mile posts, folded sub_type, impact, ends_at). ``to_event``
then converts ``ends_at`` → ``ends_at_epoch`` exactly as the Phase-2 bridge
does (``calendar.timegm`` of the naive datetime) and augments with lat/lon +
a stable ``external_id`` (WZDx ``data_source_id`` + feature id) so the
incident gating decider dedups correctly.
"""

import calendar
import json
import logging
import os
import time
from datetime import datetime
from typing import TYPE_CHECKING, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from meshai.notifications.events import Event, make_event

# Reuse the exact WZDx field-mapping logic from the Central normalizer so the
# native path and the Central path produce an identical canonical shape.
from meshai.central_normalizer import _parse_wzdx_federal

if TYPE_CHECKING:
    from ..config import WZDxConfig

logger = logging.getLogger(__name__)


# US state abbreviation ↔ full-name map, so a configured "ID" matches a
# registry row whose ``state`` reads "Idaho" (and vice-versa).
_US_STATES = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas",
    "CA": "california", "CO": "colorado", "CT": "connecticut", "DE": "delaware",
    "FL": "florida", "GA": "georgia", "HI": "hawaii", "ID": "idaho",
    "IL": "illinois", "IN": "indiana", "IA": "iowa", "KS": "kansas",
    "KY": "kentucky", "LA": "louisiana", "ME": "maine", "MD": "maryland",
    "MA": "massachusetts", "MI": "michigan", "MN": "minnesota", "MS": "mississippi",
    "MO": "missouri", "MT": "montana", "NE": "nebraska", "NV": "nevada",
    "NH": "new hampshire", "NJ": "new jersey", "NM": "new mexico", "NY": "new york",
    "NC": "north carolina", "ND": "north dakota", "OH": "ohio", "OK": "oklahoma",
    "OR": "oregon", "PA": "pennsylvania", "RI": "rhode island", "SC": "south carolina",
    "SD": "south dakota", "TN": "tennessee", "TX": "texas", "UT": "utah",
    "VT": "vermont", "VA": "virginia", "WA": "washington", "WV": "west virginia",
    "WI": "wisconsin", "WY": "wyoming", "DC": "district of columbia",
}


class WZDxAdapter:
    """FHWA WZDx work-zone polling adapter (native ``work_zone`` source)."""

    def __init__(self, config: "WZDxConfig"):
        self._api_key = self._resolve_env(config.api_key or "")  # unused (keyless)
        self._base_url = (config.base_url or "").strip().rstrip("/")
        self._registry_url = (config.registry_url or "").strip()
        self._registry_ttl = config.registry_ttl or 21600
        self._tick_interval = config.tick_seconds or 300
        self._states = [str(s).strip() for s in (config.states or ["ID"]) if str(s).strip()]
        self._bbox = config.bbox or []  # [west, south, east, north]

        self._last_tick = 0.0
        self._events = []
        self._feed_urls = []            # discovered / overridden feed URLs
        self._registry_fetched_at = 0.0
        self._consecutive_errors = 0
        self._last_error = None
        self._is_loaded = False

        if not self._base_url and not self._registry_url:
            logger.info("WZDx: no base_url and no registry_url configured, adapter idle")

    # ── helpers ────────────────────────────────────────────────────────────

    def _resolve_env(self, value: str) -> str:
        """Resolve ${ENV_VAR} references in value."""
        if value and value.startswith("${") and value.endswith("}"):
            return os.environ.get(value[2:-1], "")
        return value

    def _wanted_states(self) -> set:
        """Lowercased set of accepted state tokens (abbrev + full name)."""
        wanted = set()
        for s in self._states:
            low = s.strip().lower()
            wanted.add(low)
            if s.upper() in _US_STATES:              # "ID" → "idaho"
                wanted.add(_US_STATES[s.upper()])
            for ab, name in _US_STATES.items():      # "Idaho" → "id"
                if name == low:
                    wanted.add(ab.lower())
        return wanted

    def _http_get_json(self, url: str, timeout: int = 30):
        """GET a URL and JSON-decode. Raises on HTTP/URL/parse error."""
        headers = {"User-Agent": "MeshAI/1.0", "Accept": "application/json"}
        req = Request(url, headers=headers)
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # ── tick / polling ───────────────────────────────────────────────────────

    def tick(self) -> bool:
        """Execute one polling tick. Returns True if the event set changed."""
        now = time.time()

        if not self._base_url and not self._registry_url:
            return False

        if now - self._last_tick < self._tick_interval:
            return False

        self._last_tick = now

        # Refresh the (cached) registry-derived feed URL list if stale.
        self._refresh_feed_urls(now)
        if not self._feed_urls:
            return False

        return self._fetch_all(now)

    def _refresh_feed_urls(self, now: float) -> None:
        """Populate ``self._feed_urls`` from base_url override or the registry.

        The registry lookup is cached for ``registry_ttl`` seconds. A registry
        fetch failure is logged + swallowed (keeps any previously-discovered
        feeds).
        """
        # Single-feed override always wins and needs no registry.
        if self._base_url:
            self._feed_urls = [self._base_url]
            return

        if self._feed_urls and (now - self._registry_fetched_at) < self._registry_ttl:
            return  # cache still warm

        if not self._registry_url:
            return

        try:
            rows = self._http_get_json(self._registry_url)
        except (HTTPError, URLError) as e:
            reason = getattr(e, "reason", None) or getattr(e, "code", None) or e
            logger.warning("WZDx registry fetch failed (%s); keeping cached feeds", reason)
            self._last_error = f"registry: {reason}"
            self._consecutive_errors += 1
            return
        except Exception as e:
            logger.warning("WZDx registry parse failed (%s); keeping cached feeds", e)
            self._last_error = f"registry parse: {e}"
            self._consecutive_errors += 1
            return

        self._feed_urls = self._select_feeds(rows)
        self._registry_fetched_at = now
        logger.info("WZDx registry: %d GeoJSON feed(s) for states=%s",
                    len(self._feed_urls), self._states)

    def _select_feeds(self, rows) -> list:
        """Filter registry rows to configured states' GeoJSON WZDx feeds."""
        if not isinstance(rows, list):
            return []
        wanted = self._wanted_states()
        urls = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            state = str(row.get("state") or row.get("state_full") or "").strip().lower()
            if wanted and state not in wanted:
                continue
            fmt = str(row.get("format") or "").strip().lower()
            if fmt and "geojson" not in fmt:
                continue  # only GeoJSON WZDx feeds; skip xml/other
            url = (row.get("url") or row.get("apiurl") or row.get("feed_url") or "").strip()
            if url and url not in urls:
                urls.append(url)
        return urls

    def _fetch_all(self, now: float) -> bool:
        """Fetch + parse every configured feed. Returns True if events changed."""
        new_events = []
        any_success = False

        for url in self._feed_urls:
            try:
                fc = self._http_get_json(url)
            except (HTTPError, URLError) as e:
                reason = getattr(e, "reason", None) or getattr(e, "code", None) or e
                logger.warning("WZDx feed fetch failed for %s (%s); skipping", url, reason)
                self._last_error = f"feed: {reason}"
                self._consecutive_errors += 1
                continue
            except Exception as e:
                logger.warning("WZDx feed parse failed for %s (%s); skipping", url, e)
                self._last_error = f"feed parse: {e}"
                self._consecutive_errors += 1
                continue

            any_success = True
            for feat in self._iter_features(fc):
                evt = self._parse_feature(feat, now)
                if evt:
                    new_events.append(evt)

        if not any_success:
            return False  # all feeds down — keep the last known good set

        # Optional bbox filter.
        if self._bbox and len(self._bbox) == 4:
            west, south, east, north = self._bbox
            new_events = [
                e for e in new_events
                if e.get("lat") is not None and e.get("lon") is not None
                and west <= e["lon"] <= east and south <= e["lat"] <= north
            ]

        old_ids = {e["event_id"] for e in self._events}
        new_ids = {e["event_id"] for e in new_events}
        changed = old_ids != new_ids

        self._events = new_events
        self._is_loaded = True
        self._consecutive_errors = 0
        self._last_error = None

        if changed:
            logger.info("WZDx work zones updated: %d active", len(new_events))
        return changed

    @staticmethod
    def _iter_features(fc) -> list:
        """Yield features from a WZDx v4 GeoJSON FeatureCollection."""
        if not isinstance(fc, dict):
            return []
        feats = fc.get("features")
        return feats if isinstance(feats, list) else []

    @staticmethod
    def _geometry_centroid(geometry) -> Optional[list]:
        """Return [lon, lat] centroid of a GeoJSON geometry, or None."""
        if not isinstance(geometry, dict):
            return None
        coords = geometry.get("coordinates")
        pts = []

        def _collect(c):
            if (isinstance(c, (list, tuple)) and len(c) >= 2
                    and all(isinstance(x, (int, float)) for x in c[:2])):
                pts.append((c[0], c[1]))
            elif isinstance(c, (list, tuple)):
                for sub in c:
                    _collect(sub)

        _collect(coords)
        if not pts:
            return None
        lon = sum(p[0] for p in pts) / len(pts)
        lat = sum(p[1] for p in pts) / len(pts)
        return [lon, lat]

    def _parse_feature(self, feat: dict, now: float) -> Optional[dict]:
        """Parse one WZDx v4 road_event feature into a stored event dict.

        Only ``work-zone`` road_events are kept. Field mapping delegates to
        ``central_normalizer._parse_wzdx_federal`` (which reads either the
        raw ``core_details.*`` nesting or a flattened envelope). Returns None
        for non-work-zone / malformed / id-less features (never raises).
        """
        try:
            if not isinstance(feat, dict):
                return None
            props = feat.get("properties")
            if not isinstance(props, dict):
                return None

            cd = props.get("core_details")
            cd = cd if isinstance(cd, dict) else {}

            # Only work-zone road_events (event_type may live nested or flat).
            event_type = str(cd.get("event_type") or props.get("event_type") or "").strip().lower()
            if event_type and event_type != "work-zone":
                return None

            # Stable external id: data_source_id + feature id.
            data_source_id = cd.get("data_source_id") or props.get("data_source_id")
            feat_id = feat.get("id") or cd.get("id") or props.get("id")
            if not feat_id and not data_source_id:
                return None  # no stable identity → cannot dedup
            external_id = ":".join(str(p) for p in (data_source_id, feat_id) if p)

            # Coordinates: prop lat/lon if present, else geometry centroid.
            lat = props.get("latitude")
            lon = props.get("longitude")
            centroid = self._geometry_centroid(feat.get("geometry"))
            if (lat is None or lon is None) and centroid:
                lon, lat = centroid[0], centroid[1]
            if lat is None or lon is None:
                return None  # unusable without coords

            # inner_data for _parse_wzdx_federal: pass properties verbatim (its
            # field() helper reads core_details first, then top-level).
            inner_data = dict(props)
            geo = {"centroid": centroid} if centroid else {}
            n = _parse_wzdx_federal(inner_data, geo)

            # start/end epochs for the incident-gating keys.
            start_at = self._iso_to_epoch(props.get("start_date") or cd.get("start_date"))
            end_at = self._iso_to_epoch(props.get("end_date") or cd.get("end_date"))

            return {
                "source": "wzdx",
                "event_id": f"wzdx_{external_id}",
                "event_type": "Work Zone",
                "severity": "priority" if n.get("impact") == "full_closure" else "routine",
                "lat": float(lat),
                "lon": float(lon),
                "expires": (end_at + 3600) if end_at else (now + 21600),
                "fetched_at": now,
                "external_id": external_id,
                "start_at": start_at,
                "end_at": end_at,
                "normalized": n,   # output of _parse_wzdx_federal
            }
        except Exception as e:
            logger.debug("WZDx feature parse error: %s", e)
            return None

    @staticmethod
    def _iso_to_epoch(iso) -> Optional[int]:
        """ISO-8601 → int epoch (UTC), or None."""
        if not iso:
            return None
        try:
            s = str(iso).replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is not None:
                return int(dt.timestamp())
            # Naive → treat as UTC (matches ends_at_epoch convention).
            return int(calendar.timegm(dt.timetuple()))
        except Exception:
            return None

    # ── pipeline Event construction ──────────────────────────────────────────

    def to_event(self, evt: dict) -> Optional["Event"]:
        """Translate a stored WZDx event dict into a ``work_zone`` Event.

        Emits ``category="work_zone"`` with the canonical work-zone ``data``
        (road, direction, mile_start/end, sub_type, impact, ends_at_epoch,
        town, distance_mi, bearing, lat, lon) plus dedup keys (external_id,
        source) and the incident-gating fields (county, state, start/end,
        lanes_affected, icon_category). Returns None if the dict lacks coords
        or a stable id.
        """
        try:
            lat = evt.get("lat")
            lon = evt.get("lon")
            if lat is None or lon is None:
                return None

            external_id = evt.get("external_id")
            event_id = evt.get("event_id")
            if not event_id:
                return None

            n = evt.get("normalized", {}) or {}
            severity = evt.get("severity", "routine")

            # ends_at (datetime) → ends_at_epoch (float), TZ-independent.
            # Stored as calendar.timegm(naive.timetuple()); the formatter
            # reconstructs via datetime.utcfromtimestamp().
            ends_at = n.get("ends_at")
            ends_at_epoch = None
            if ends_at is not None:
                try:
                    naive = (ends_at.replace(tzinfo=None)
                             if ends_at.tzinfo is not None else ends_at)
                    ends_at_epoch = float(calendar.timegm(naive.timetuple()))
                except Exception:
                    ends_at_epoch = None

            impact = n.get("impact")
            sub_type = n.get("sub_type")
            road = n.get("road")
            direction = n.get("direction")

            # Canonical work_zone data (the 12 keys the formatter reads) plus
            # the incident-gating keys (external_id/source/start_at/end_at/…).
            canonical_data = {
                # ── work_zone renderer keys ──────────────────────────────
                "road":          road,
                "direction":     direction,        # full form ("northbound"…)
                "mile_start":    n.get("mile_start"),
                "mile_end":      n.get("mile_end"),
                "sub_type":      sub_type,          # human-readable, impact-folded
                "impact":        impact,            # "full_closure" | "partial"
                "ends_at_epoch": ends_at_epoch,
                "town":          n.get("town"),
                "distance_mi":   n.get("distance_mi"),
                "bearing":       n.get("bearing"),
                "lat":           lat,
                "lon":           lon,
                # ── incident-gating / dedup keys ─────────────────────────
                "external_id":   external_id,       # stable WZDx id → gating dedup
                "source":        "wzdx",
                "county":        None,
                "state":         None,
                "from_loc":      None,
                "to_loc":        None,
                "mile_marker":   None,
                "lanes_affected": self._lanes_phrase(impact),
                "cause":         None,
                "comment":       n.get("description") or None,
                "geocoder_city": None,
                "landclass":     None,
                "start_at":      evt.get("start_at"),
                "end_at":        evt.get("end_at"),
                "magnitude":     None,
                "delay_seconds": None,
                "icon_category": "road_works",
            }

            # Title / summary for the LLM + dashboard.
            head_road = road or (n.get("town") or "Road event")
            title = f"Work Zone: {head_road}"
            if direction and direction != "unknown":
                title += f" {direction}"
            summary_parts = [title]
            if sub_type:
                summary_parts.append(sub_type)
            if impact == "full_closure":
                summary_parts.append("all lanes closed")
            desc = n.get("description")
            if desc and desc.strip():
                summary_parts.append(desc.strip())
            summary = " | ".join(summary_parts)[:300]

            return make_event(
                source="wzdx",
                category="work_zone",
                severity=severity,
                title=title,
                summary=summary,
                timestamp=evt.get("fetched_at"),
                expires=evt.get("expires"),
                lat=lat,
                lon=lon,
                group_key=event_id,
                inhibit_keys=[event_id],
                data=canonical_data,
            )
        except Exception:
            logger.exception("WZDx to_event failed for evt: %s", evt.get("event_id"))
            return None

    @staticmethod
    def _lanes_phrase(impact: Optional[str]) -> Optional[str]:
        """Human lane-status phrase from the folded impact value."""
        if impact == "full_closure":
            return "all lanes closed"
        if impact == "partial":
            return "lanes affected"
        return None

    def get_events(self) -> list:
        """Get current work-zone events."""
        return self._events

    @property
    def health_status(self) -> dict:
        """Get adapter health status."""
        return {
            "source": "wzdx",
            "is_loaded": self._is_loaded,
            "last_error": str(self._last_error) if self._last_error else None,
            "consecutive_errors": self._consecutive_errors,
            "event_count": len(self._events),
            "last_fetch": self._last_tick,
            "feed_count": len(self._feed_urls),
            "registry_age": (time.time() - self._registry_fetched_at)
                            if self._registry_fetched_at else None,
        }
