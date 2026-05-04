"""MeshMonitor API data source."""

import json
import logging
import os
import time
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .. import __version__

logger = logging.getLogger(__name__)

USER_AGENT = f"MeshAI/{__version__}"


class MeshMonitorDataSource:
    """Fetches mesh data from a MeshMonitor instance."""

    def __init__(self, url: str, api_token: str, refresh_interval: int = 300):
        """Initialize MeshMonitor data source.

        Args:
            url: Base URL of MeshMonitor instance (e.g., http://192.168.1.100:3333)
            api_token: API token for authentication. Supports ${ENV_VAR} format.
            refresh_interval: Seconds between refresh checks (default 5 minutes)
        """
        self._url = url.rstrip("/")
        self._api_token = self._resolve_token(api_token)
        self._refresh_interval = refresh_interval

        # Cached data
        self._nodes: list[dict] = []
        self._channels: list[dict] = []
        self._telemetry: list[dict] = []
        self._traceroutes: list[dict] = []
        self._network_stats: Optional[dict] = None
        self._topology: Optional[dict] = None
        self._packets: list[dict] = []
        self._solar: list[dict] = []

        self._last_refresh: float = 0.0
        self._last_error: Optional[str] = None
        self._is_loaded: bool = False

    def _resolve_token(self, token: str) -> str:
        """Resolve token, supporting ${ENV_VAR} format.

        Args:
            token: API token or env var reference

        Returns:
            Resolved token value
        """
        if token.startswith("${") and token.endswith("}"):
            env_var = token[2:-1]
            return os.environ.get(env_var, "")
        return token

    @property
    def nodes(self) -> list[dict]:
        """Get cached nodes list."""
        return self._nodes

    @property
    def channels(self) -> list[dict]:
        """Get cached channels list."""
        return self._channels

    @property
    def telemetry(self) -> list[dict]:
        """Get cached telemetry list."""
        return self._telemetry

    @property
    def traceroutes(self) -> list[dict]:
        """Get cached traceroutes list."""
        return self._traceroutes

    @property
    def network_stats(self) -> Optional[dict]:
        """Get cached network stats."""
        return self._network_stats

    @property
    def topology(self) -> Optional[dict]:
        """Get cached topology."""
        return self._topology

    @property
    def packets(self) -> list[dict]:
        """Get cached packets list."""
        return self._packets

    @property
    def solar(self) -> list[dict]:
        """Get cached solar estimates list."""
        return self._solar

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
        """Fetch JSON from an endpoint with Bearer auth.

        Args:
            endpoint: API endpoint path (e.g., /api/v1/nodes)

        Returns:
            Parsed JSON data or None on error
        """
        url = f"{self._url}{endpoint}"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._api_token}",
            "User-Agent": USER_AGENT,
        }
        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            # MeshMonitor wraps responses in {"success": true, "data": [...]}
            # Extract the actual data if wrapped
            if isinstance(data, dict) and "data" in data:
                return data["data"]
            return data

        except HTTPError as e:
            logger.warning(f"MeshMonitor {endpoint}: HTTP {e.code} {e.reason}")
            return None
        except URLError as e:
            logger.warning(f"MeshMonitor {endpoint}: Connection error - {e.reason}")
            return None
        except json.JSONDecodeError as e:
            logger.warning(f"MeshMonitor {endpoint}: Invalid JSON - {e}")
            return None
        except Exception as e:
            logger.warning(f"MeshMonitor {endpoint}: {e}")
            return None

    def fetch_all(self) -> bool:
        """Fetch all data from MeshMonitor API.

        Fetches all endpoints independently. One failure doesn't block others.

        Returns:
            True if at least one endpoint succeeded
        """
        success_count = 0
        errors = []

        # Fetch nodes
        data = self._fetch_json("/api/v1/nodes")
        if data is not None:
            self._nodes = data if isinstance(data, list) else []
            success_count += 1
            logger.debug(f"MeshMonitor: fetched {len(self._nodes)} nodes")
        else:
            errors.append("nodes")

        # Fetch channels
        data = self._fetch_json("/api/v1/channels")
        if data is not None:
            self._channels = data if isinstance(data, list) else []
            success_count += 1
            logger.debug(f"MeshMonitor: fetched {len(self._channels)} channels")
        else:
            errors.append("channels")

        # Fetch telemetry
        data = self._fetch_json("/api/v1/telemetry")
        if data is not None:
            self._telemetry = data if isinstance(data, list) else []
            success_count += 1
            logger.debug(f"MeshMonitor: fetched {len(self._telemetry)} telemetry records")
        else:
            errors.append("telemetry")

        # Fetch traceroutes
        data = self._fetch_json("/api/v1/traceroutes")
        if data is not None:
            self._traceroutes = data if isinstance(data, list) else []
            success_count += 1
            logger.debug(f"MeshMonitor: fetched {len(self._traceroutes)} traceroutes")
        else:
            errors.append("traceroutes")

        # Fetch network stats
        data = self._fetch_json("/api/v1/network")
        if data is not None:
            self._network_stats = data if isinstance(data, dict) else None
            success_count += 1
            logger.debug("MeshMonitor: fetched network stats")
        else:
            errors.append("network")

        # Fetch topology
        data = self._fetch_json("/api/v1/network/topology")
        if data is not None:
            self._topology = data if isinstance(data, dict) else None
            success_count += 1
            logger.debug("MeshMonitor: fetched topology")
        else:
            errors.append("topology")

        # Fetch packets
        data = self._fetch_json("/api/v1/packets")
        if data is not None:
            self._packets = data if isinstance(data, list) else []
            success_count += 1
            logger.debug(f"MeshMonitor: fetched {len(self._packets)} packets")
        else:
            errors.append("packets")

        # Fetch solar estimates
        data = self._fetch_json("/api/v1/solar")
        if data is not None:
            self._solar = data if isinstance(data, list) else []
            success_count += 1
            logger.debug(f"MeshMonitor: fetched {len(self._solar)} solar estimates")
        else:
            errors.append("solar")

        # Update state
        self._last_refresh = time.time()

        if success_count > 0:
            self._is_loaded = True
            self._last_error = None
            logger.info(
                f"MeshMonitor refresh: {len(self._nodes)} nodes, "
                f"{len(self._telemetry)} telemetry, {len(self._traceroutes)} traceroutes"
            )
            return True
        else:
            self._last_error = f"All endpoints failed: {', '.join(errors)}"
            logger.error(f"MeshMonitor: {self._last_error}")
            return False

    def maybe_refresh(self) -> bool:
        """Refresh data if interval has elapsed.

        Returns:
            True if refresh was performed
        """
        if time.time() - self._last_refresh >= self._refresh_interval:
            return self.fetch_all()
        return False
