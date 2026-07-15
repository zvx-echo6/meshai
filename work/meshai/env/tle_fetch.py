"""Celestrak TLE fetcher — native, keyless population of sat_tles.

Storage-only native adapter (like central.tle_handler): it does NOT emit
mesh Events. Its sole job is to keep the shared `sat_tles` table populated
with fresh two-line element sets so the native SGP4 pass-predictor (built
next) has current orbital data when satpass runs with feed_source="native".

Source: Celestrak GP API (https://celestrak.org/NORAD/elements/gp.php),
FORMAT=tle (classic 3-line: name / line1 / line2). Two selector styles:
    - GROUP  (e.g. ?GROUP=weather&FORMAT=tle) — a curated catalog group
    - CATNR  (e.g. ?CATNR=25544&FORMAT=tle)   — a single NORAD id

Config (SatpassConfig): `tle_groups` (list of group names), `norad_ids`
(list of NORAD catalog ids), `tle_refresh_seconds` (poll interval; TLEs
update ~daily so the default is 6h). Gated on feed_source=="native" via
the normal EnvironmentalStore registration.

Upserts flow through `central.tle_handler.upsert_tle` so native and
Central ingestion share identical latest-epoch-wins semantics and the same
`sat_tles` columns. The TLE line-1 epoch field (columns 19-32) is parsed
into an ISO-8601 string so it is directly comparable with the ISO epochs
Central stores.
"""

from __future__ import annotations

import datetime
import logging
import time
from typing import TYPE_CHECKING, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

if TYPE_CHECKING:
    from ..config import SatpassConfig

logger = logging.getLogger(__name__)

GP_BASE_URL = "https://celestrak.org/NORAD/elements/gp.php"


def _cfg_str(config, attr: str, default: str) -> str:
    """Read a string config field, falling back to `default` if absent,
    empty, or not a real string (e.g. an unconfigured test mock)."""
    value = getattr(config, attr, None)
    return value if isinstance(value, str) and value else default


def parse_tle_epoch(line1: str) -> str:
    """Parse the epoch from TLE line 1 into an ISO-8601 UTC string.

    The epoch occupies columns 19-32 (1-indexed) of line 1 in the form
    ``YYDDD.DDDDDDDD``: a two-digit year, day-of-year, and fractional day.
    Two-digit years 57-99 map to 1957-1999; 00-56 map to 2000-2056 (the
    standard NORAD windowing).

    Returns an ISO-8601 string (e.g. "2026-07-01T12:00:00+00:00") so the
    value is lexicographically comparable with the ISO epochs the Central
    handler stores. Raises ValueError on a malformed field.
    """
    raw = line1[18:32].strip()
    yy = int(raw[0:2])
    year = 2000 + yy if yy < 57 else 1900 + yy
    doy = float(raw[2:])
    if doy < 1:
        raise ValueError(f"bad day-of-year in TLE epoch: {raw!r}")
    dt = (datetime.datetime(year, 1, 1, tzinfo=datetime.timezone.utc)
          + datetime.timedelta(days=doy - 1.0))
    return dt.isoformat()


def parse_tle_block(text: str) -> list[dict]:
    """Parse a 3-line-per-satellite TLE block into records.

    Accepts the Celestrak FORMAT=tle payload: repeating triples of
    (name, line1, line2). Tolerant of blank lines and trailing whitespace.
    A malformed triple (missing/short lines, non-integer NORAD id, or an
    unparseable epoch) is skipped with a debug log rather than aborting the
    whole block.

    Returns a list of dicts: {norad_id, name, line1, line2, epoch}.
    """
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    out: list[dict] = []
    i = 0
    while i + 2 <= len(lines) - 1:
        # Three lines (name, line1, line2) available at i, i+1, i+2.
        name = lines[i].strip()
        line1 = lines[i + 1]
        line2 = lines[i + 2]

        # Validate structural markers before consuming the triple.
        if not (line1.startswith("1 ") and line2.startswith("2 ")):
            # Not aligned to a triple boundary — skip one line and resync.
            i += 1
            continue

        try:
            norad_id = int(line1[2:7])
            epoch = parse_tle_epoch(line1)
        except (ValueError, IndexError) as e:
            logger.debug("tle_fetch: skipping malformed TLE for %r: %s", name, e)
            i += 3
            continue

        out.append({
            "norad_id": norad_id,
            "name": name or f"SAT-{norad_id}",
            "line1": line1,
            "line2": line2,
            "epoch": epoch,
        })
        i += 3

    return out


class TLEFetchAdapter:
    """Native Celestrak TLE fetcher — populates sat_tles, emits no events."""

    def __init__(self, config: "SatpassConfig"):
        self._config = config
        self._tle_base_url = _cfg_str(config, "tle_base_url", GP_BASE_URL)
        self._last_tick = 0.0
        self._last_error: Optional[str] = None
        self._consecutive_errors = 0
        self._is_loaded = False
        self._upserted = 0  # rows written on the most recent successful tick
        self._interval = int(getattr(config, "tle_refresh_seconds", 21600) or 21600)

    # -- fetch targets --------------------------------------------------------

    def _targets(self) -> list[tuple[str, str]]:
        """Build the (label, url) fetch list from config groups + norad_ids."""
        targets: list[tuple[str, str]] = []
        for group in (getattr(self._config, "tle_groups", None) or []):
            targets.append(
                (f"GROUP={group}", f"{self._tle_base_url}?GROUP={group}&FORMAT=tle"))
        for norad in (getattr(self._config, "norad_ids", None) or []):
            targets.append(
                (f"CATNR={norad}", f"{self._tle_base_url}?CATNR={norad}&FORMAT=tle"))
        return targets

    # -- polling --------------------------------------------------------------

    def tick(self, now: Optional[float] = None) -> bool:
        """One slow poll. Returns True if any TLE row changed.

        Resilient: a failed target logs + is skipped and updates health; a
        thrown exception never propagates out of tick().
        """
        now = now if now is not None else time.time()
        if now - self._last_tick < self._interval:
            return False
        self._last_tick = now

        targets = self._targets()
        if not targets:
            logger.debug("tle_fetch: no groups/norad_ids configured; nothing to fetch")
            self._is_loaded = True
            self._upserted = 0
            return False

        written = 0
        errors = 0
        for label, url in targets:
            try:
                text = self._fetch(url)
            except Exception as e:
                errors += 1
                self._last_error = f"{label}: {e}"
                logger.warning("tle_fetch: fetch failed for %s: %s", label, e)
                continue
            try:
                written += self._store(parse_tle_block(text))
            except Exception as e:
                errors += 1
                self._last_error = f"{label}: store {e}"
                logger.warning("tle_fetch: store failed for %s: %s", label, e)

        self._upserted = written
        if errors == 0:
            self._last_error = None
            self._consecutive_errors = 0
        else:
            self._consecutive_errors += 1
        self._is_loaded = True
        return written > 0

    def _fetch(self, url: str) -> str:
        """HTTP GET a Celestrak GP endpoint, returning the decoded body."""
        headers = {"User-Agent": "MeshAI/1.0", "Accept": "text/plain"}
        req = Request(url, headers=headers)
        try:
            with urlopen(req, timeout=20) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except HTTPError as e:
            raise RuntimeError(f"HTTP {e.code}") from e
        except URLError as e:
            raise RuntimeError(str(e.reason)) from e

    def _store(self, records: list[dict]) -> int:
        """Upsert parsed TLE records into sat_tles. Returns rows written."""
        if not records:
            return 0
        from meshai.persistence import get_db
        from meshai.central.tle_handler import upsert_tle

        conn = get_db()
        now = int(time.time())
        written = 0
        for rec in records:
            if upsert_tle(conn, rec["norad_id"], rec["name"],
                          rec["line1"], rec["line2"], rec["epoch"], now=now):
                written += 1
        return written

    # -- store integration ----------------------------------------------------

    def get_events(self) -> list:
        """Storage-only adapter: never produces mesh events."""
        return []

    def to_event(self, evt: dict):
        """Storage-only adapter: no event translation."""
        return None

    @property
    def health_status(self) -> dict:
        return {
            "source": "satpass_tle",
            "is_loaded": self._is_loaded,
            "last_error": self._last_error,
            "consecutive_errors": self._consecutive_errors,
            "event_count": 0,
            "last_fetch": self._last_tick,
            "tles_upserted": self._upserted,
        }
