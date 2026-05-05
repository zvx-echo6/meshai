"""MeshMonitor API data source with tick-based staggered polling."""

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
    """Fetches mesh data from a MeshMonitor instance with staggered polling."""

    # Endpoint schedule: (endpoint, interval_ticks)
    # At 30s per tick: 2 ticks = 60s, 4 ticks = 2min, 10 ticks = 5min
    ENDPOINT_SCHEDULE = [
        ("packets",     2),   # Every 2 ticks (60s)
        ("nodes",       4),   # Every 4 ticks (2 min)
        ("telemetry",   4),   # Every 4 ticks (2 min)
        ("traceroutes", 10),  # Every 10 ticks (5 min)
        ("channels",    10),  # Every 10 ticks (5 min)
        ("network",     10),  # Every 10 ticks (5 min)
        ("topology",    10),  # Every 10 ticks (5 min)
        ("solar",       20),  # Every 20 ticks (10 min)
    ]

    def __init__(self, url: str, api_token: str, refresh_interval: int = 30, polite_mode: bool = False):
        """Initialize MeshMonitor data source.

        Args:
            url: Base URL of MeshMonitor instance (e.g., http://192.168.1.100:3333)
            api_token: API token for authentication. Supports ${ENV_VAR} format.
            refresh_interval: Seconds between ticks (default 30)
            polite_mode: If True, use longer intervals
        """
        self._url = url.rstrip("/")
        self._api_token = self._resolve_token(api_token)
        self._tick_interval = refresh_interval
        self._polite_mode = polite_mode

        # Cached data
        self._nodes: list[dict] = []
        self._channels: list[dict] = []
        self._telemetry: list[dict] = []
        self._traceroutes: list[dict] = []
        self._network_stats: Optional[dict] = None
        self._topology: Optional[dict] = None
        self._packets: list[dict] = []
        self._solar: list[dict] = []

        # Tick state
        self._tick_count: int = 0
        self._last_tick: float = 0.0
        self._last_packet_timestamp: float = 0.0
        self._last_telemetry_timestamp: float = 0.0

        # Rate limit / health tracking
        self._backoff_until: float = 0.0
        self._avg_response_ms: float = 0.0
        self._consecutive_errors: int = 0
        self._max_consecutive_errors: int = 5

        # Status
        self._last_error: Optional[str] = None
        self._is_loaded: bool = False
        self._data_changed: bool = False

    def _resolve_token(self, token: str) -> str:
        """Resolve token, supporting ${ENV_VAR} format."""
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
        """Get last tick timestamp (epoch)."""
        return self._last_tick

    @property
    def last_error(self) -> Optional[str]:
        """Get last error message if any."""
        return self._last_error

    @property
    def is_loaded(self) -> bool:
        """Check if data has been successfully loaded."""
        return self._is_loaded

    @property
    def data_changed(self) -> bool:
        """Check if data has changed since last check, then reset flag."""
        changed = self._data_changed
        self._data_changed = False
        return changed

    @property
    def health_status(self) -> dict:
        """Get source health status for monitoring."""
        return {
            "url": self._url,
            "is_loaded": self._is_loaded,
            "last_error": self._last_error,
            "avg_response_ms": round(self._avg_response_ms),
            "consecutive_errors": self._consecutive_errors,
            "backed_off": time.time() < self._backoff_until,
            "tick_count": self._tick_count,
            "cached_packets": len(self._packets),
            "cached_nodes": len(self._nodes),
            "cached_telemetry": len(self._telemetry),
            "polite_mode": self._polite_mode,
        }

    def tick(self) -> Optional[str]:
        """Execute one polling tick. Called every 30 seconds.

        Returns:
            Name of endpoint that was fetched, or None if skipped/rate-limited
        """
        now = time.time()

        # Rate limit backoff
        if now < self._backoff_until:
            return None

        # Consecutive error backoff (exponential)
        if self._consecutive_errors >= self._max_consecutive_errors:
            backoff = min(300, 30 * (2 ** (self._consecutive_errors - self._max_consecutive_errors)))
            if now - self._last_tick < backoff:
                return None

        self._tick_count += 1
        self._last_tick = now

        # Determine which endpoint to call this tick
        endpoint = self._select_endpoint()
        if not endpoint:
            return None

        # Execute the call
        success = False
        if endpoint == "packets":
            success = self._fetch_packets_incremental()
        elif endpoint == "nodes":
            success = self._fetch_nodes()
        elif endpoint == "telemetry":
            success = self._fetch_telemetry_incremental()
        elif endpoint == "traceroutes":
            success = self._fetch_traceroutes()
        elif endpoint == "channels":
            success = self._fetch_channels()
        elif endpoint == "network":
            success = self._fetch_network()
        elif endpoint == "topology":
            success = self._fetch_topology()
        elif endpoint == "solar":
            success = self._fetch_solar()

        if success:
            self._consecutive_errors = 0
            self._is_loaded = True
            self._last_error = None
        else:
            self._consecutive_errors += 1

        return endpoint if success else None

    def _select_endpoint(self) -> Optional[str]:
        """Select which endpoint to call on this tick based on schedule."""
        schedule = self.ENDPOINT_SCHEDULE

        # In polite mode, double the intervals
        if self._polite_mode:
            schedule = [(ep, interval * 2) for ep, interval in schedule]

        # Find the highest-priority endpoint that's due
        for endpoint, interval_ticks in schedule:
            if self._tick_count % interval_ticks == 0:
                return endpoint

        return None

    def _fetch_with_tracking(self, endpoint: str) -> Optional[dict | list]:
        """Fetch JSON with Bearer auth, response tracking, and rate limit detection."""
        url = f"{self._url}{endpoint}"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._api_token}",
            "User-Agent": USER_AGENT,
        }

        start = time.time()
        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=15) as resp:
                elapsed_ms = (time.time() - start) * 1000

                # Track response time (rolling average)
                self._avg_response_ms = (self._avg_response_ms * 0.8) + (elapsed_ms * 0.2)

                # Warn if slowing down
                if elapsed_ms > 5000:
                    logger.warning(
                        f"MeshMonitor {self._url} slow: {elapsed_ms:.0f}ms on {endpoint}"
                    )

                data = json.loads(resp.read().decode("utf-8"))

                # MeshMonitor wraps responses in {"success": true, "data": [...]}
                if isinstance(data, dict) and "data" in data:
                    return data["data"]
                return data

        except HTTPError as e:
            if e.code == 429:
                retry_after = int(e.headers.get('Retry-After', 60))
                self._backoff_until = time.time() + retry_after
                logger.warning(
                    f"Rate limited by MeshMonitor. Backing off {retry_after}s"
                )
                self._last_error = f"Rate limited (429), backing off {retry_after}s"
            elif e.code == 401:
                # Token may be expired
                self._backoff_until = time.time() + 300  # Back off 5 min
                logger.error("MeshMonitor: API token may be expired or invalid (401)")
                self._last_error = "API token expired or invalid (401)"
            elif e.code == 503:
                self._backoff_until = time.time() + 60
                logger.warning(f"MeshMonitor {endpoint}: Service unavailable (503)")
                self._last_error = "Service unavailable (503)"
            else:
                logger.warning(f"MeshMonitor {endpoint}: HTTP {e.code} {e.reason}")
                self._last_error = f"HTTP {e.code} on {endpoint}"
            return None
        except URLError as e:
            logger.warning(f"MeshMonitor {endpoint}: Connection error - {e.reason}")
            self._last_error = f"Connection error: {e.reason}"
            return None
        except json.JSONDecodeError as e:
            logger.warning(f"MeshMonitor {endpoint}: Invalid JSON - {e}")
            self._last_error = f"Invalid JSON from {endpoint}"
            return None
        except Exception as e:
            logger.warning(f"MeshMonitor {endpoint}: {e}")
            self._last_error = str(e)
            return None

    def _fetch_nodes(self) -> bool:
        """Full node refresh."""
        data = self._fetch_with_tracking("/api/v1/nodes")
        if data is not None:
            self._nodes = data if isinstance(data, list) else []
            self._data_changed = True
            logger.debug(f"MeshMonitor nodes: {len(self._nodes)}")
            return True
        return False

    def _fetch_channels(self) -> bool:
        """Channels refresh."""
        data = self._fetch_with_tracking("/api/v1/channels")
        if data is not None:
            self._channels = data if isinstance(data, list) else []
            self._data_changed = True
            logger.debug(f"MeshMonitor channels: {len(self._channels)}")
            return True
        return False

    def _fetch_telemetry_incremental(self) -> bool:
        """Incremental telemetry fetch."""
        if self._last_telemetry_timestamp > 0:
            data = self._fetch_with_tracking(
                f"/api/v1/telemetry?since={int(self._last_telemetry_timestamp)}&limit=500"
            )
            if data is None:
                data = self._fetch_with_tracking("/api/v1/telemetry?limit=500")
        else:
            data = self._fetch_with_tracking("/api/v1/telemetry?limit=5000")

        if data is None:
            return False

        new_telemetry = data if isinstance(data, list) else []
        if new_telemetry:
            existing_keys = set()
            for t in self._telemetry:
                key = (t.get("nodeNum"), t.get("telemetryType"), t.get("timestamp"))
                existing_keys.add(key)

            added = 0
            for t in new_telemetry:
                key = (t.get("nodeNum"), t.get("telemetryType"), t.get("timestamp"))
                if key not in existing_keys:
                    self._telemetry.append(t)
                    existing_keys.add(key)
                    added += 1
                    ts = t.get("timestamp", 0)
                    if isinstance(ts, (int, float)) and ts > self._last_telemetry_timestamp:
                        self._last_telemetry_timestamp = ts

            if added > 0:
                self._data_changed = True
                logger.debug(f"MeshMonitor telemetry: +{added} new ({len(self._telemetry)} total)")

            # Cap to prevent unbounded growth
            if len(self._telemetry) > 10000:
                self._telemetry = self._telemetry[-10000:]

        return True

    def _fetch_traceroutes(self) -> bool:
        """Traceroutes refresh."""
        data = self._fetch_with_tracking("/api/v1/traceroutes?limit=1000")
        if data is None:
            data = self._fetch_with_tracking("/api/v1/traceroutes")
        if data is not None:
            self._traceroutes = data if isinstance(data, list) else []
            self._data_changed = True
            logger.debug(f"MeshMonitor traceroutes: {len(self._traceroutes)}")
            return True
        return False

    def _fetch_network(self) -> bool:
        """Network stats refresh."""
        data = self._fetch_with_tracking("/api/v1/network")
        if data is not None:
            self._network_stats = data if isinstance(data, dict) else None
            self._data_changed = True
            logger.debug("MeshMonitor: fetched network stats")
            return True
        return False

    def _fetch_topology(self) -> bool:
        """Topology refresh."""
        data = self._fetch_with_tracking("/api/v1/network/topology")
        if data is not None:
            self._topology = data if isinstance(data, dict) else None
            self._data_changed = True
            logger.debug("MeshMonitor: fetched topology")
            return True
        return False

    def _fetch_packets_incremental(self) -> bool:
        """Incremental packet fetch."""
        if self._last_packet_timestamp > 0:
            data = self._fetch_with_tracking(
                f"/api/v1/packets?since={int(self._last_packet_timestamp)}&limit=500"
            )
            if data is None:
                data = self._fetch_with_tracking("/api/v1/packets?limit=500")
        else:
            data = self._fetch_with_tracking("/api/v1/packets?limit=5000")

        if data is None:
            return False

        new_packets = data if isinstance(data, list) else []
        if new_packets:
            existing_ids = {p.get("packet_id") or p.get("id") for p in self._packets}
            added = 0
            for pkt in new_packets:
                pkt_id = pkt.get("packet_id") or pkt.get("id")
                if pkt_id and pkt_id not in existing_ids:
                    self._packets.append(pkt)
                    existing_ids.add(pkt_id)
                    added += 1

                    pkt_time = pkt.get("timestamp") or pkt.get("createdAt") or 0
                    if isinstance(pkt_time, (int, float)):
                        if pkt_time > 1e12:
                            pkt_time = pkt_time / 1000
                        if pkt_time > self._last_packet_timestamp:
                            self._last_packet_timestamp = pkt_time

            if added > 0:
                self._data_changed = True
                logger.debug(f"MeshMonitor packets: +{added} new ({len(self._packets)} total)")

            # Cap to prevent unbounded growth
            if len(self._packets) > 5000:
                self._packets = self._packets[-5000:]

        return True

    def _fetch_solar(self) -> bool:
        """Solar estimates refresh."""
        data = self._fetch_with_tracking("/api/v1/solar")
        if data is not None:
            self._solar = data if isinstance(data, list) else []
            self._data_changed = True
            logger.debug(f"MeshMonitor solar: {len(self._solar)}")
            return True
        return False

    def fetch_all(self) -> bool:
        """Fetch all data at once. Used for initial load and force refresh."""
        success_count = 0
        errors = []

        # Fetch nodes
        if self._fetch_nodes():
            success_count += 1
        else:
            errors.append("nodes")

        # Fetch channels
        if self._fetch_channels():
            success_count += 1
        else:
            errors.append("channels")

        # Fetch telemetry
        if self._fetch_telemetry_incremental():
            success_count += 1
        else:
            errors.append("telemetry")

        # Fetch traceroutes
        if self._fetch_traceroutes():
            success_count += 1
        else:
            errors.append("traceroutes")

        # Fetch network stats
        if self._fetch_network():
            success_count += 1
        else:
            errors.append("network")

        # Fetch topology
        if self._fetch_topology():
            success_count += 1
        else:
            errors.append("topology")

        # Fetch packets
        if self._fetch_packets_incremental():
            success_count += 1
        else:
            errors.append("packets")

        # Fetch solar
        if self._fetch_solar():
            success_count += 1
        else:
            errors.append("solar")

        self._last_tick = time.time()

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
        """Backward compatible refresh check. Now delegates to tick()."""
        now = time.time()
        if now - self._last_tick >= self._tick_interval:
            result = self.tick()
            return result is not None
        return False
