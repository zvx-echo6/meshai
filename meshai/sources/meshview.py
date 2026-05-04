"""Meshview API data source."""

import json
import logging
import time
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


class MeshviewSource:
    """Fetches mesh data from a Meshview instance."""

    def __init__(self, url: str, refresh_interval: int = 300):
        """Initialize Meshview source.

        Args:
            url: Base URL of Meshview instance (e.g., https://meshview.example.com)
            refresh_interval: Seconds between refresh checks (default 5 minutes)
        """
        self._url = url.rstrip("/")
        self._refresh_interval = refresh_interval
        self._nodes: list[dict] = []
        self._edges: list[dict] = []
        self._stats: Optional[dict | list] = None
        self._counts: Optional[dict] = None
        self._last_refresh: float = 0.0
        self._last_error: Optional[str] = None
        self._is_loaded: bool = False

    @property
    def nodes(self) -> list[dict]:
        """Get cached nodes list."""
        return self._nodes

    @property
    def edges(self) -> list[dict]:
        """Get cached edges list."""
        return self._edges

    @property
    def stats(self) -> Optional[dict | list]:
        """Get cached stats."""
        return self._stats

    @property
    def counts(self) -> Optional[dict]:
        """Get cached counts."""
        return self._counts

    @property
    def last_refresh(self) -> float:
        """Get last refresh timestamp (epoch)."""
        return self._last_refresh

    @property
    def last_error(self) -> Optional[str]:
        """Get last error message if any."""
        return self._last_error

    @property
    def is_loaded(self) -> bool:
        """Check if data has been successfully loaded."""
        return self._is_loaded

    def _fetch_json(self, endpoint: str) -> Optional[dict | list]:
        """Fetch JSON from an endpoint.

        Args:
            endpoint: API endpoint path (e.g., /api/nodes)

        Returns:
            Parsed JSON data or None on error
        """
        url = f"{self._url}{endpoint}"
        try:
            req = Request(url, headers={"Accept": "application/json"})
            with urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            logger.warning(f"Meshview {endpoint}: HTTP {e.code} {e.reason}")
            return None
        except URLError as e:
            logger.warning(f"Meshview {endpoint}: Connection error - {e.reason}")
            return None
        except json.JSONDecodeError as e:
            logger.warning(f"Meshview {endpoint}: Invalid JSON - {e}")
            return None
        except Exception as e:
            logger.warning(f"Meshview {endpoint}: {e}")
            return None

    def fetch_all(self) -> bool:
        """Fetch all data from Meshview API.

        Fetches nodes, edges, stats, and counts independently.
        One failure doesn't block others.

        Returns:
            True if at least one endpoint succeeded
        """
        success_count = 0
        errors = []

        # Fetch nodes
        data = self._fetch_json("/api/nodes")
        if data is not None:
            self._nodes = data if isinstance(data, list) else []
            success_count += 1
            logger.debug(f"Meshview: fetched {len(self._nodes)} nodes")
        else:
            errors.append("nodes")

        # Fetch edges
        data = self._fetch_json("/api/edges")
        if data is not None:
            self._edges = data if isinstance(data, list) else []
            success_count += 1
            logger.debug(f"Meshview: fetched {len(self._edges)} edges")
        else:
            errors.append("edges")

        # Fetch stats (24h hourly)
        data = self._fetch_json("/api/stats?period_type=hour&length=24")
        if data is not None:
            self._stats = data
            success_count += 1
            logger.debug("Meshview: fetched stats")
        else:
            errors.append("stats")

        # Fetch counts
        data = self._fetch_json("/api/stats/count")
        if data is not None:
            self._counts = data if isinstance(data, dict) else None
            success_count += 1
            logger.debug("Meshview: fetched counts")
        else:
            errors.append("counts")

        # Update state
        self._last_refresh = time.time()

        if success_count > 0:
            self._is_loaded = True
            self._last_error = None
            logger.info(
                f"Meshview refresh: {len(self._nodes)} nodes, {len(self._edges)} edges"
            )
            return True
        else:
            self._last_error = f"All endpoints failed: {', '.join(errors)}"
            logger.error(f"Meshview: {self._last_error}")
            return False

    def maybe_refresh(self) -> bool:
        """Refresh data if interval has elapsed.

        Returns:
            True if refresh was performed
        """
        if time.time() - self._last_refresh >= self._refresh_interval:
            return self.fetch_all()
        return False
