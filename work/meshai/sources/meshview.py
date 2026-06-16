"""Meshview API data source with tick-based staggered polling."""

import json
import logging
import time
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .. import __version__

logger = logging.getLogger(__name__)

USER_AGENT = f"MeshAI/{__version__}"


class MeshviewSource:
    """Fetches mesh data from a Meshview instance with staggered polling."""

    # Endpoint schedule: (endpoint, interval_ticks)
    # At 30s per tick: 1 tick = 30s, 4 ticks = 2min, 6 ticks = 3min, 10 ticks = 5min
    ENDPOINT_SCHEDULE = [
        ("packets",     1),   # Every tick (30s) — near real-time
        ("nodes",       4),   # Every 4 ticks (2 min)
        ("stats",       6),   # Every 6 ticks (3 min)
        ("edges",       6),   # Every 6 ticks (3 min)
        ("counts",      8),   # Every 8 ticks (4 min)
        ("traceroutes", 10),  # Every 10 ticks (5 min)
        ("feeders",     20),  # Every 20 ticks (10 min) — sample packets_seen for gateway data
    ]

    def __init__(self, url: str, refresh_interval: int = 30, polite_mode: bool = False):
        """Initialize Meshview source.

        Args:
            url: Base URL of Meshview instance (e.g., https://meshview.example.com)
            refresh_interval: Seconds between ticks (default 30)
            polite_mode: If True, skip frequent packet polling for shared instances
        """
        self._url = url.rstrip("/")
        self._tick_interval = refresh_interval
        self._polite_mode = polite_mode

        # Cached data
        self._nodes: list[dict] = []
        self._edges: list[dict] = []
        self._stats: Optional[dict | list] = None
        self._counts: Optional[dict] = None
        self._packets: list[dict] = []
        self._traceroutes: list[dict] = []

        # Tick state
        self._tick_count: int = 0
        self._last_tick: float = 0.0
        self._last_packet_timestamp: float = 0.0  # For incremental packet fetch

        # Rate limit / health tracking
        self._backoff_until: float = 0.0
        self._avg_response_ms: float = 0.0
        self._consecutive_errors: int = 0
        self._max_consecutive_errors: int = 5

        # Status
        self._last_error: Optional[str] = None
        self._is_loaded: bool = False
        self._data_changed: bool = False

        # Feeder gateway data from packets_seen
        self._feeder_data: dict = {}  # {from_node: [{gateway_id, gateway_name, avg_rssi, avg_snr, packet_count}]}

        # Capabilities (discovered on first fetch)
        self._capabilities: dict = {
            "packets": True,
            "packets_since": False,
            "traceroutes": True,
            "packets_seen": True,
        }
        self._capabilities_probed: bool = False

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
    def packets(self) -> list[dict]:
        """Get cached packets."""
        return self._packets

    @property
    def traceroutes(self) -> list[dict]:
        """Get cached traceroutes."""
        return self._traceroutes

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
        elif endpoint == "edges":
            success = self._fetch_edges()
        elif endpoint == "stats":
            success = self._fetch_stats()
        elif endpoint == "counts":
            success = self._fetch_counts()
        elif endpoint == "traceroutes":
            success = self._fetch_traceroutes()
        elif endpoint == "feeders":
            success = self._fetch_feeders()

        if success:
            self._consecutive_errors = 0
            self._is_loaded = True
            self._last_error = None
        else:
            self._consecutive_errors += 1

        return endpoint if success else None

    def _select_endpoint(self) -> Optional[str]:
        """Select which endpoint to call on this tick based on schedule."""
        # In polite mode, skip frequent packet polling
        schedule = self.ENDPOINT_SCHEDULE
        if self._polite_mode:
            schedule = [(ep, interval) for ep, interval in schedule if ep != "packets"]

        # Find the highest-priority endpoint that's due
        for endpoint, interval_ticks in schedule:
            if self._tick_count % interval_ticks == 0:
                # Skip if capability check failed
                if endpoint in ("packets", "traceroutes") and not self._capabilities.get(endpoint, True):
                    continue
                return endpoint

        # If nothing is scheduled this tick, check packets (always welcome)
        if not self._polite_mode and self._capabilities.get("packets", True):
            return "packets"

        return None

    def _fetch_with_tracking(self, endpoint: str) -> Optional[dict | list]:
        """Fetch JSON with response time tracking and rate limit detection."""
        url = f"{self._url}{endpoint}"
        headers = {
            "Accept": "application/json",
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
                        f"Meshview {self._url} slow: {elapsed_ms:.0f}ms on {endpoint}"
                    )

                data = json.loads(resp.read().decode("utf-8"))
                return data

        except HTTPError as e:
            if e.code == 429:
                retry_after = int(e.headers.get('Retry-After', 60))
                self._backoff_until = time.time() + retry_after
                logger.warning(
                    f"Rate limited by {self._url}. Backing off {retry_after}s"
                )
                self._last_error = f"Rate limited (429), backing off {retry_after}s"
            elif e.code == 503:
                self._backoff_until = time.time() + 60
                logger.warning(f"Meshview {endpoint}: Service unavailable (503)")
                self._last_error = "Service unavailable (503)"
            elif e.code == 404:
                # Endpoint doesn't exist — disable it
                if "packets" in endpoint:
                    self._capabilities["packets"] = False
                elif "traceroutes" in endpoint:
                    self._capabilities["traceroutes"] = False
                logger.info(f"Meshview {endpoint}: Not available (404)")
                self._last_error = f"Endpoint not available: {endpoint}"
            else:
                logger.warning(f"Meshview {endpoint}: HTTP {e.code}")
                self._last_error = f"HTTP {e.code} on {endpoint}"
            return None
        except URLError as e:
            logger.warning(f"Meshview {endpoint}: {e.reason}")
            self._last_error = f"Connection error: {e.reason}"
            return None
        except Exception as e:
            logger.warning(f"Meshview {endpoint}: {e}")
            self._last_error = str(e)
            return None

    def _extract_list(self, data: dict | list | None, key: str) -> list[dict]:
        """Extract a list from API response, handling wrapper dicts."""
        if data is None:
            return []
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and key in data:
            inner = data[key]
            return inner if isinstance(inner, list) else []
        return []

    def _fetch_packets_incremental(self) -> bool:
        """Fetch only NEW packets since last call.

        Uses the ?since= parameter if the Meshview API supports it.
        Falls back to fetching recent packets and deduping locally.
        """
        # Try incremental first
        if self._last_packet_timestamp > 0 and self._capabilities.get("packets_since", False):
            since_us = int(self._last_packet_timestamp * 1_000_000)
            data = self._fetch_with_tracking(f"/api/packets?since={since_us}")

            if data is None:
                # Fall back to getting recent packets without since parameter
                data = self._fetch_with_tracking("/api/packets?limit=100")
        elif self._last_packet_timestamp > 0:
            # No since support, get recent packets
            data = self._fetch_with_tracking("/api/packets?limit=100")
        else:
            # First fetch — get recent packets and probe since capability
            data = self._fetch_with_tracking("/api/packets?limit=200")
            if data is not None:
                # Try probing since capability
                since_probe = self._fetch_with_tracking(f"/api/packets?since={int(time.time() * 1_000_000 - 60_000_000)}&limit=1")
                if since_probe is not None:
                    self._capabilities["packets_since"] = True

        if data is None:
            return False

        new_packets = self._extract_list(data, "packets")

        if new_packets:
            # Dedup against existing packets
            existing_ids = {p.get("packet_id") or p.get("id") for p in self._packets}
            added = 0
            for pkt in new_packets:
                pkt_id = pkt.get("packet_id") or pkt.get("id")
                if pkt_id and pkt_id not in existing_ids:
                    self._packets.append(pkt)
                    existing_ids.add(pkt_id)
                    added += 1

                    # Track latest timestamp for next incremental
                    pkt_time = pkt.get("timestamp") or pkt.get("import_time") or 0
                    if isinstance(pkt_time, (int, float)):
                        # Handle microsecond timestamps
                        if pkt_time > 1e15:
                            pkt_time = pkt_time / 1_000_000
                        if pkt_time > self._last_packet_timestamp:
                            self._last_packet_timestamp = pkt_time

            if added > 0:
                self._data_changed = True
                logger.debug(f"Meshview packets: +{added} new ({len(self._packets)} total)")

            # Cap cached packets to prevent unbounded growth (keep last 2000)
            if len(self._packets) > 2000:
                self._packets = self._packets[-2000:]

        return True

    def _fetch_nodes(self) -> bool:
        """Full node refresh (replaces cached nodes)."""
        data = self._fetch_with_tracking("/api/nodes")
        if data is not None:
            self._nodes = self._extract_list(data, "nodes")
            self._data_changed = True
            logger.debug(f"Meshview nodes: {len(self._nodes)}")
            return True
        return False

    def _fetch_edges(self) -> bool:
        """Full edge refresh."""
        data = self._fetch_with_tracking("/api/edges")
        if data is not None:
            self._edges = self._extract_list(data, "edges")
            self._data_changed = True
            logger.debug(f"Meshview edges: {len(self._edges)}")
            return True
        return False

    def _fetch_stats(self) -> bool:
        """Stats refresh."""
        data = self._fetch_with_tracking("/api/stats?period_type=hour&length=24")
        if data is not None:
            self._stats = data
            self._data_changed = True
            return True
        return False

    def _fetch_counts(self) -> bool:
        """Counts refresh."""
        data = self._fetch_with_tracking("/api/stats/count")
        if data is not None:
            self._counts = data if isinstance(data, dict) else None
            self._data_changed = True
            return True
        return False

    def _fetch_traceroutes(self) -> bool:
        """Traceroute refresh."""
        data = self._fetch_with_tracking("/api/traceroutes")
        if data is not None:
            self._traceroutes = self._extract_list(data, "traceroutes")
            self._data_changed = True
            return True
        return False


    def _fetch_feeders(self) -> bool:
        """Sample recent packets to discover which gateway hears them.

        Calls /api/packets_seen/{packet_id} for a sample of recent packets.
        Builds a per-node gateway map with signal quality.

        Note: Each Meshview source has ONE gateway feeding it. To get multiple
        gateways, the data store aggregates feeder data across all sources.
        """
        if not self._packets:
            return False

        if not self._capabilities.get("packets_seen", True):
            return False

        # Sample 20 recent packets spread across different senders
        by_sender = {}
        for pkt in reversed(self._packets):  # Most recent first
            sender = pkt.get("from_node") or pkt.get("from") or pkt.get("from_node_id")
            if sender and sender not in by_sender:
                by_sender[sender] = pkt
            if len(by_sender) >= 20:
                break

        sample_packets = list(by_sender.values())
        if not sample_packets:
            return False

        # Build: node_gateways[from_node] = {gateway_id: {"rssi_values": [], "snr_values": []}}
        node_gateways: dict = {}
        errors = 0

        for pkt in sample_packets:
            pkt_id = pkt.get("packet_id") or pkt.get("id")
            from_node = pkt.get("from_node") or pkt.get("from") or pkt.get("from_node_id")
            if not pkt_id or not from_node:
                continue

            seen_data = self._fetch_with_tracking(f"/api/packets_seen/{pkt_id}")
            if seen_data is None:
                errors += 1
                if errors >= 3:
                    # Endpoint probably doesn't exist
                    self._capabilities["packets_seen"] = False
                    logger.info(f"Meshview {self._url}: packets_seen not available, disabling")
                    return False
                continue

            # Extract gateway list from {"seen": [...]}
            gateways = []
            if isinstance(seen_data, dict):
                gateways = seen_data.get("seen", [])
            elif isinstance(seen_data, list):
                gateways = seen_data

            if not gateways:
                continue

            if from_node not in node_gateways:
                node_gateways[from_node] = {}

            for gw in gateways:
                # Fields from API: node_id, rx_snr, rx_rssi, topic
                gw_node_id = gw.get("node_id")
                if not gw_node_id:
                    continue

                gw_id = str(gw_node_id)
                rssi = gw.get("rx_rssi")
                snr = gw.get("rx_snr")

                # Extract hex ID from topic for gateway name lookup
                # topic format: "msh/US/2/e/Freq51/!27780c47"
                topic = gw.get("topic", "")
                gw_hex = ""
                if topic and "!" in topic:
                    gw_hex = topic.split("!")[-1] if "!" in topic else ""
                    gw_hex = "!" + gw_hex if gw_hex else ""

                if gw_id not in node_gateways[from_node]:
                    node_gateways[from_node][gw_id] = {
                        "gateway_id": gw_id,
                        "gateway_hex": gw_hex,
                        "rssi_values": [],
                        "snr_values": [],
                    }

                if rssi is not None:
                    node_gateways[from_node][gw_id]["rssi_values"].append(rssi)
                if snr is not None:
                    node_gateways[from_node][gw_id]["snr_values"].append(snr)

        # Aggregate into feeder_data
        self._feeder_data = {}
        for from_node, gateways in node_gateways.items():
            self._feeder_data[from_node] = []
            for gw_id, gw_info in gateways.items():
                avg_rssi = sum(gw_info["rssi_values"]) / len(gw_info["rssi_values"]) if gw_info["rssi_values"] else None
                avg_snr = sum(gw_info["snr_values"]) / len(gw_info["snr_values"]) if gw_info["snr_values"] else None
                self._feeder_data[from_node].append({
                    "gateway_id": gw_id,
                    "gateway_hex": gw_info["gateway_hex"],
                    "gateway_name": "",  # Will be filled in by data store from node lookup
                    "avg_rssi": avg_rssi,
                    "avg_snr": avg_snr,
                    "packet_count": len(gw_info["rssi_values"]) or len(gw_info["snr_values"]) or 1,
                })

        self._data_changed = True
        logger.info(f"Feeder sampling: {len(sample_packets)} packets, {len(self._feeder_data)} nodes with gateway data")
        return True

    @property
    def feeder_data(self) -> dict:
        """Get per-node feeder gateway data."""
        return self._feeder_data

    def fetch_all(self) -> bool:
        """Fetch all data at once. Used for initial load and force refresh."""
        success = 0

        # Nodes first
        if self._fetch_nodes():
            success += 1

        # Edges
        if self._fetch_edges():
            success += 1

        # Stats
        if self._fetch_stats():
            success += 1

        # Counts
        if self._fetch_counts():
            success += 1

        # Packets (if supported)
        if self._capabilities.get("packets", True):
            if self._fetch_packets_incremental():
                success += 1

        # Traceroutes (if supported)
        if self._capabilities.get("traceroutes", True):
            if self._fetch_traceroutes():
                success += 1

        self._last_tick = time.time()

        if success > 0:
            self._is_loaded = True
            self._last_error = None
            logger.info(
                f"Meshview refresh: {len(self._nodes)} nodes, {len(self._edges)} edges, {len(self._packets)} packets"
            )
            return True
        else:
            self._last_error = "All endpoints failed"
            logger.error(f"Meshview: {self._last_error}")
            return False

    def maybe_refresh(self) -> bool:
        """Backward compatible refresh check. Now delegates to tick()."""
        now = time.time()
        if now - self._last_tick >= self._tick_interval:
            result = self.tick()
            return result is not None
        return False
