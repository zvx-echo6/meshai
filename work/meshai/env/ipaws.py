"""FEMA IPAWS-OPEN EAS civil-alert adapter.

Two-stage public CAP pipeline (keyless, no auth):

  Stage 1  GET {base_url}/feed        -> Atom index (~13 rolling national
           entries). Each <entry> carries a statefips <category> and a <link>
           to the full CAP document.
  Stage 2  GET {base_url}/eas/<id>    -> full CAP 1.2 <alert>, fetched ONLY for
           entries that pass the coarse statefips gate (load reduction).

Scope (approved behaviour):
  * Coarse region: keep only entries whose statefips term is in
    ``config.state_fips`` — applied BEFORE the stage-2 fetch.
  * Fine region: optional ``config.same_codes`` SAME county filter.
  * Non-weather only: DROP NWS/NOAA-originated CAP (``exclude_weather``) so we
    never double-broadcast what the ``nws`` adapter already carries.
  * Language: en-US <info> blocks only (skip es-US).
  * Status: only ``status=Actual`` when ``status_actual_only`` (default True).
  * Severity: mapped via the SHARED ``env.nws.map_cap_severity`` (Extreme ->
    immediate, Severe -> priority, else routine).

The adapter is duck-typed identically to ``NWSAlertsAdapter`` (tick/_fetch/
get_events/to_event/health_status) and emits the canonical CAP ``data`` dict so
the formatter+gater architecture (formatters/ipaws.py, gating/ipaws.py) operates
on it exactly like NWS events.  Its own dedup table is ``ipaws_alerts`` (NOT the
NWS ``nws_alerts`` table).
"""

import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import TYPE_CHECKING, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from meshai.county_centroids import centroid_for_same
from meshai.env.nws import _cfg_str, map_cap_severity
from meshai.notifications.events import Event, make_event

if TYPE_CHECKING:
    from ..config import IPAWSConfig

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://apps.fema.gov/IPAWSOPEN_EAS_SERVICE/rest"

_ATOM_NS = "http://www.w3.org/2005/Atom"
_CAP_NS = "urn:oasis:names:tc:emergency:cap:1.2"

# ── SAME eventCode -> meshai emergency category ──────────────────────────────
# Civil / public-safety EAS event codes we broadcast. Weather SAME codes are
# handled by the nws adapter and are additionally dropped by the weather-sender
# gate, so they are intentionally NOT mapped here.
_SAME_CATEGORY = {
    "EVI": "emergency_evacuation",   # Evacuation Immediate
    "EVA": "emergency_evacuation",   # Evacuation (Watch)
    "CEM": "emergency_civil",        # Civil Emergency Message
    "CDW": "emergency_civil",        # Civil Danger Warning
    "CAE": "emergency_amber",        # Child Abduction Emergency (AMBER)
    "LAE": "emergency_amber",        # Local Area Emergency (child/other) — treat as amber-ish
    "LEW": "emergency_law",          # Law Enforcement Warning
    "SPW": "emergency_law",          # Shelter in Place Warning
    "TOE": "emergency_911_outage",   # 911 Telephone Outage Emergency
    "HMW": "emergency_hazmat",       # Hazardous Materials Warning
    "NUW": "emergency_hazmat",       # Nuclear Power Plant Warning
    "RHW": "emergency_hazmat",       # Radiological Hazard Warning
    "FRW": "emergency_hazmat",       # Fire Warning
}

# Keyword fallback on the CAP <event> string when the SAME code is unknown.
_EVENT_KEYWORD_CATEGORY = [
    ("evacuat", "emergency_evacuation"),
    ("amber", "emergency_amber"),
    ("child abduction", "emergency_amber"),
    ("shelter in place", "emergency_law"),
    ("law enforcement", "emergency_law"),
    ("911", "emergency_911_outage"),
    ("telephone outage", "emergency_911_outage"),
    ("hazardous material", "emergency_hazmat"),
    ("hazmat", "emergency_hazmat"),
    ("radiolog", "emergency_hazmat"),
    ("nuclear", "emergency_hazmat"),
    ("civil danger", "emergency_civil"),
    ("civil emergency", "emergency_civil"),
]

_DEFAULT_CATEGORY = "emergency_civil"


def _norm_fips(value) -> str:
    """Normalise a state FIPS token to a zero-padded 2-char string.

    '16' -> '16', '6' -> '06', 6 -> '06'. Non-numeric tokens are returned
    stripped so an odd upstream value still compares by identity.
    """
    s = str(value).strip()
    try:
        return str(int(s)).zfill(2)
    except (TypeError, ValueError):
        return s


class IPAWSAlertsAdapter:
    """FEMA IPAWS-OPEN EAS civil alerts — two-stage CAP poller."""

    def __init__(self, config: "IPAWSConfig", coverage: dict = None):
        self._base_url = _cfg_str(config, "base_url", DEFAULT_BASE_URL).rstrip("/")
        self._user_agent = getattr(config, "user_agent", "") or "meshai-ipaws/1.0"

        # Coarse region scope: prefer the universal-coverage-derived state_fips
        # (bbox -> states), else the adapter's own config list.
        derived = None
        if coverage is not None:
            derived = coverage.get("state_fips")
        cfg_states = list(getattr(config, "state_fips", None) or [])
        states = derived if derived else cfg_states
        self._state_fips = {_norm_fips(s) for s in (states or [])}

        self._same_codes = {str(c).strip() for c in (getattr(config, "same_codes", None) or [])}
        self._exclude_weather = bool(getattr(config, "exclude_weather", True))
        self._drop_senders = [
            str(s).strip().lower()
            for s in (getattr(config, "drop_senders", None) or [])
            if str(s).strip()
        ]
        self._status_actual_only = bool(getattr(config, "status_actual_only", True))

        self._tick_interval = getattr(config, "tick_seconds", None) or 60
        self._last_tick = 0.0
        self._events = []
        self._consecutive_errors = 0
        self._last_error = None
        self._backoff_until = 0.0
        self._is_loaded = False

    # ── Polling ──────────────────────────────────────────────────────────────

    def tick(self) -> bool:
        """Execute one polling tick (self-throttled by tick_seconds)."""
        now = time.time()
        if now < self._backoff_until:
            return False
        if now - self._last_tick < self._tick_interval:
            return False
        self._last_tick = now
        return self._fetch()

    def _get(self, url: str) -> bytes:
        headers = {"User-Agent": self._user_agent, "Accept": "application/xml"}
        req = Request(url, headers=headers)
        with urlopen(req, timeout=15) as resp:
            return resp.read()

    def _stage2_url(self, link_href: str) -> Optional[str]:
        """Rebuild a base_url-relative stage-2 URL from an absolute FEMA link.

        The Atom <link href> is an ABSOLUTE FEMA URL; we extract the trailing
        ``eas/<id>`` and rebuild ``{base_url}/eas/<id>`` so the fetch routes
        through whatever base_url points at (direct FEMA or the Conduit proxy).
        Returns None if the href carries no ``eas/<id>`` segment.
        """
        if not link_href:
            return None
        marker = "/eas/"
        idx = link_href.rfind(marker)
        if idx == -1:
            # Some feeds use a bare "eas/<id>" without a leading slash.
            if link_href.startswith("eas/"):
                return f"{self._base_url}/{link_href}"
            return None
        tail = link_href[idx + 1:]  # "eas/<id>"
        return f"{self._base_url}/{tail}"

    def _fetch(self) -> bool:
        """Fetch + filter the IPAWS feed. Returns True if the active set changed."""
        # ── Stage 1: Atom index ──────────────────────────────────────────────
        try:
            raw = self._get(f"{self._base_url}/feed")
        except HTTPError as e:
            if e.code == 429:
                self._backoff_until = time.time() + 5
                logger.warning("IPAWS rate limited, backing off 5s")
            else:
                logger.warning("IPAWS HTTP error: %s", e.code)
            self._last_error = f"HTTP {e.code}"
            self._consecutive_errors += 1
            return False
        except URLError as e:
            logger.warning("IPAWS connection error: %s", e.reason)
            self._last_error = str(e.reason)
            self._consecutive_errors += 1
            return False
        except Exception as e:  # noqa: BLE001
            logger.warning("IPAWS feed fetch error: %s", e)
            self._last_error = str(e)
            self._consecutive_errors += 1
            return False

        try:
            feed = ET.fromstring(raw)
        except ET.ParseError as e:
            logger.warning("IPAWS feed parse error: %s", e)
            self._last_error = f"feed parse: {e}"
            self._consecutive_errors += 1
            return False

        new_events = []
        for entry in feed.findall(f"{{{_ATOM_NS}}}entry"):
            statefips = None
            for cat in entry.findall(f"{{{_ATOM_NS}}}category"):
                if cat.get("label") == "statefips":
                    statefips = cat.get("term")
                    break
            # ── Coarse region gate (BEFORE stage-2 fetch) ────────────────────
            if self._state_fips and _norm_fips(statefips) not in self._state_fips:
                continue

            link_el = entry.find(f"{{{_ATOM_NS}}}link")
            href = link_el.get("href") if link_el is not None else None
            cap_url = self._stage2_url(href)
            if not cap_url:
                continue

            # ── Stage 2: full CAP document ───────────────────────────────────
            try:
                cap_raw = self._get(cap_url)
            except Exception as e:  # noqa: BLE001
                logger.debug("IPAWS stage-2 fetch failed for %s: %s", cap_url, e)
                continue

            parsed = self._parse_cap(cap_raw, statefips)
            if parsed:
                new_events.append(parsed)

        # Change detection on the active identifier set.
        old_ids = {e["event_id"] for e in self._events}
        new_ids = {e["event_id"] for e in new_events}
        changed = old_ids != new_ids

        self._events = new_events
        self._consecutive_errors = 0
        self._last_error = None
        self._is_loaded = True
        if changed:
            logger.info("IPAWS alerts updated: %d active", len(new_events))
        return changed

    # ── CAP parsing / filtering ──────────────────────────────────────────────

    def _parse_cap(self, cap_raw: bytes, statefips: Optional[str]) -> Optional[dict]:
        """Parse a CAP 1.2 <alert> into an internal event dict, applying the
        status / weather-sender / language / SAME filters. Returns None when the
        alert is filtered out or unparseable."""
        try:
            alert = ET.fromstring(cap_raw)
        except ET.ParseError as e:
            logger.debug("IPAWS CAP parse error: %s", e)
            return None

        def _t(parent, tag):
            el = parent.find(f"{{{_CAP_NS}}}{tag}")
            return el.text.strip() if el is not None and el.text else ""

        identifier = _t(alert, "identifier")
        sender = _t(alert, "sender")
        sent = _t(alert, "sent")
        status = _t(alert, "status")
        msg_type = _t(alert, "msgType") or "Alert"

        # ── Status gate ──────────────────────────────────────────────────────
        if self._status_actual_only and status and status.lower() != "actual":
            return None

        # ── Weather-sender exclusion (drop NWS/NOAA) ─────────────────────────
        if self._exclude_weather and sender:
            s = sender.lower()
            if any(bad in s for bad in self._drop_senders):
                return None

        # ── en-US <info> block (skip es-US) ──────────────────────────────────
        infos = alert.findall(f"{{{_CAP_NS}}}info")
        info = None
        for cand in infos:
            lang_el = cand.find(f"{{{_CAP_NS}}}language")
            lang = (lang_el.text.strip().lower() if lang_el is not None and lang_el.text
                    else "en-us")
            if lang.startswith("en"):
                info = cand
                break
        if info is None:
            return None

        event_type = _t(info, "event") or "Emergency Alert"
        urgency = _t(info, "urgency")
        cap_severity = _t(info, "severity") or "Unknown"
        certainty = _t(info, "certainty")
        headline = _t(info, "headline")
        description = _t(info, "description")

        # eventCode SAME value
        same_value = ""
        for ec in info.findall(f"{{{_CAP_NS}}}eventCode"):
            vn = ec.find(f"{{{_CAP_NS}}}valueName")
            vv = ec.find(f"{{{_CAP_NS}}}value")
            if vn is not None and vn.text and vn.text.strip().upper() == "SAME":
                same_value = (vv.text.strip() if vv is not None and vv.text else "")
                break

        # ── Area: areaDesc + SAME geocodes + optional geometry ───────────────
        area_descs = []
        area_same_codes = []
        geometry = None
        for area in info.findall(f"{{{_CAP_NS}}}area"):
            ad = _t(area, "areaDesc")
            if ad:
                area_descs.append(ad)
            for gc in area.findall(f"{{{_CAP_NS}}}geocode"):
                vn = gc.find(f"{{{_CAP_NS}}}valueName")
                vv = gc.find(f"{{{_CAP_NS}}}value")
                if (vn is not None and vn.text and vn.text.strip().upper() == "SAME"
                        and vv is not None and vv.text):
                    area_same_codes.append(vv.text.strip())
            if geometry is None:
                geometry = self._polygon_geometry(area)

        area_desc = "; ".join(area_descs)

        # ── Fine region gate (optional SAME county filter) ───────────────────
        if self._same_codes:
            if not any(c in self._same_codes for c in area_same_codes):
                return None

        # ── County-centroid fallback for county-only (no-polygon) alerts ─────
        # A civil CAP alert that carries only SAME county geocode(s) and NO
        # <polygon> has no geometry, so the geometry-based coverage/region
        # tagger (CoverageFilter -> event_region_names -> matching_area_names)
        # cannot place it. With no region it never matches the region-routing
        # matrix and is SILENTLY not broadcast — the exact gap this closes.
        # Resolve each SAME county to its Census internal point so the EXISTING
        # geometry tagger locates it. Multi-county alerts become a MultiPoint so
        # EVERY county's region is tagged (Shapely intersects any point). A real
        # <polygon> always wins and is never overridden.
        if geometry is None and area_same_codes:
            county_points = []          # [(lat, lon), ...] in resolution order
            seen_points = set()
            for code in area_same_codes:
                pt = centroid_for_same(code)
                if pt and pt not in seen_points:
                    seen_points.add(pt)
                    county_points.append(pt)
            if len(county_points) == 1:
                lat, lon = county_points[0]
                geometry = {"type": "Point", "coordinates": [lon, lat]}
            elif len(county_points) > 1:
                geometry = {
                    "type": "MultiPoint",
                    "coordinates": [[lon, lat] for (lat, lon) in county_points],
                }

        # Stable per-alert id (also the dedup / group key).
        event_id = identifier or f"ipaws:{same_value}:{sent}"

        category = self._derive_category(same_value, event_type)
        centroid = self._centroid(geometry)

        return {
            "source": "ipaws",
            "event_id": event_id,
            "cap_id": event_id,
            "event_type": event_type,
            "urgency": urgency,
            "cap_severity": cap_severity,
            "certainty": certainty,
            "sender": sender,
            "status": status,
            "msgType": msg_type,
            "headline": headline,
            "description": description,
            "same_code": same_value,
            "area_desc": area_desc,
            "area_same_codes": area_same_codes,
            "statefips": statefips,
            "category": category,
            "sent": self._parse_iso(sent),
            "expires": self._parse_iso(_t(info, "expires")),
            "geometry": geometry,
            "lat": centroid[0] if centroid else None,
            "lon": centroid[1] if centroid else None,
            "fetched_at": time.time(),
        }

    def _derive_category(self, same_value: str, event_type: str) -> str:
        """Map a CAP SAME eventCode (or the event string) to an emergency_* category."""
        code = (same_value or "").strip().upper()
        if code in _SAME_CATEGORY:
            return _SAME_CATEGORY[code]
        ev = (event_type or "").lower()
        for kw, cat in _EVENT_KEYWORD_CATEGORY:
            if kw in ev:
                return cat
        return _DEFAULT_CATEGORY

    @staticmethod
    def _polygon_geometry(area) -> Optional[dict]:
        """Build a GeoJSON Polygon from a CAP <polygon> (space-separated
        'lat,lon' pairs). Returns None when the area has no polygon."""
        poly_el = area.find(f"{{{_CAP_NS}}}polygon")
        if poly_el is None or not (poly_el.text and poly_el.text.strip()):
            return None
        ring = []
        for pair in poly_el.text.strip().split():
            try:
                lat_s, lon_s = pair.split(",")
                ring.append([float(lon_s), float(lat_s)])  # GeoJSON is [lon,lat]
            except (ValueError, IndexError):
                continue
        if len(ring) < 4:
            return None
        if ring[0] != ring[-1]:
            ring.append(ring[0])
        return {"type": "Polygon", "coordinates": [ring]}

    @staticmethod
    def _centroid(geometry: Optional[dict]) -> Optional[tuple]:
        """Best-effort (lat, lon) centroid of a GeoJSON Polygon / Point /
        MultiPoint (or None). Point/MultiPoint arise from the county-centroid
        fallback for county-only alerts; MultiPoint returns the mean point (a
        reasonable display centroid — region tagging uses the full geometry, not
        this value)."""
        if not geometry:
            return None
        gtype = geometry.get("type")
        coords = geometry.get("coordinates") or []
        if gtype == "Point":
            # GeoJSON Point coordinates are [lon, lat].
            if len(coords) >= 2:
                return (coords[1], coords[0])
            return None
        if gtype == "MultiPoint":
            pts = [c for c in coords if len(c) >= 2]
            if not pts:
                return None
            lats = [c[1] for c in pts]
            lons = [c[0] for c in pts]
            return (sum(lats) / len(lats), sum(lons) / len(lons))
        if gtype != "Polygon":
            return None
        if not coords or not coords[0]:
            return None
        ring = coords[0]
        lats = [c[1] for c in ring]
        lons = [c[0] for c in ring]
        if not lats:
            return None
        return (sum(lats) / len(lats), sum(lons) / len(lons))

    @staticmethod
    def _parse_iso(iso_str: str) -> float:
        """Parse a CAP/ISO-8601 timestamp to epoch float (0.0 on failure)."""
        if not iso_str:
            return 0.0
        try:
            s = iso_str.strip()
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            return datetime.fromisoformat(s).timestamp()
        except Exception:  # noqa: BLE001
            return 0.0

    # ── Event emission ───────────────────────────────────────────────────────

    def to_event(self, raw: dict) -> Event:
        """Convert an internal event dict to a pipeline Event carrying the
        canonical CAP ``data`` dict (same shape the NWS path emits), so
        formatters/ipaws.py + gating/ipaws.py render/gate it identically."""
        event_type = raw.get("event_type", "Emergency Alert")
        category = raw.get("category") or _DEFAULT_CATEGORY
        severity = map_cap_severity(raw.get("cap_severity", "Unknown"))
        area_desc = raw.get("area_desc", "")

        canonical = {
            "cap_id": raw.get("cap_id") or raw.get("event_id", ""),
            "event": event_type,
            "same_code": raw.get("same_code", ""),
            "cap_severity": raw.get("cap_severity", "Unknown"),
            "certainty": raw.get("certainty", ""),
            "urgency": raw.get("urgency", ""),
            "sender": raw.get("sender", ""),
            "expires_at": raw.get("expires") or None,
            "area_desc": area_desc,
            "geocoder": {"city": None, "county": area_desc, "state": None},
            "description": raw.get("description", ""),
            "parameters": {},
            "msgType": raw.get("msgType", "Alert"),
            "references": [],
            "category": category,
            "headline": raw.get("headline", ""),
            "geometry": raw.get("geometry"),
        }

        return make_event(
            source="ipaws",
            category=category,
            severity=severity,
            title=raw.get("headline") or event_type,
            summary=raw.get("headline", ""),
            body=raw.get("description", ""),
            effective=raw.get("sent") or None,
            expires=raw.get("expires") or None,
            lat=raw.get("lat"),
            lon=raw.get("lon"),
            group_key=raw.get("event_id", ""),
            inhibit_keys=[],
            data=canonical,
        )

    def get_events(self) -> list:
        """Return the current active events."""
        return self._events

    @property
    def health_status(self) -> dict:
        """Adapter health snapshot (same shape as the NWS adapter)."""
        return {
            "source": "ipaws",
            "is_loaded": self._is_loaded,
            "last_error": str(self._last_error) if self._last_error else None,
            "consecutive_errors": self._consecutive_errors,
            "event_count": len(self._events),
            "last_fetch": self._last_tick,
        }
