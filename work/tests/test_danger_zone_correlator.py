"""Step 2 DangerZoneCorrelator tests (Step 5 verification, correlator part).

The correlator subscribes to the notification EventBus and, for located
hazard events, flags infrastructure nodes inside the hazard's threat radius
(+ buffer) and optionally alerts the operator. Distances are MILES
end-to-end. It delivers via channels.create_channel + MeshDMChannel,
BYPASSING the Dispatcher -- so we stub the CONNECTOR (capture send_message),
NOT a dispatcher (per plan C12).

Most tests use a NON-fire family (avalanche/seismic) so we avoid the fires
DB lookup entirely -- a non-fire hazard is a point at the event lat/lon.

Async delivery is fire-and-forget (asyncio.create_task). Tests that assert a
real send run inside an event loop (pytest.mark.asyncio) and yield once so
the scheduled task runs. dry_run / miss / filter tests assert the provably
send-free paths and don't need the loop.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from meshai.config import Config, DangerZonesConfig, DangerZoneHazardConfig
from meshai.mesh_data_store import UnifiedNode
from meshai.notifications.danger_zone_correlator import DangerZoneCorrelator
from meshai.notifications.events import make_event


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class StubConnector:
    """Captures send_message calls (mirrors MeshConnector.send_message)."""

    def __init__(self):
        self.calls: list[dict] = []

    def send_message(self, text=None, destination=None, channel=0, **kw):
        self.calls.append(
            {"text": text, "destination": destination, "channel": channel})
        return True


class FakeDataStore:
    """Minimal stand-in exposing get_nodes_by_roles with the SAME filter
    semantics as the real MeshDataStore.get_nodes_by_roles (role membership +
    fresh last_heard + non-None position), so freshness/role tests exercise
    real filtering rather than a hand-fed list."""

    def __init__(self, nodes):
        self._nodes = list(nodes)

    def get_nodes_by_roles(self, roles, max_age_s):
        cutoff = time.time() - max_age_s
        return [
            n for n in list(self._nodes)
            if n.role in roles
            and n.latitude is not None
            and n.longitude is not None
            and n.last_heard
            and n.last_heard >= cutoff
        ]


def _node(*, node_num, role="ROUTER", lat=42.0, lon=-114.0,
          last_heard=None, short_name=None):
    n = UnifiedNode(node_num=node_num)
    n.role = role
    n.latitude = lat
    n.longitude = lon
    n.last_heard = last_heard if last_heard is not None else time.time()
    n.node_id_hex = f"!{node_num:08x}"
    n.short_name = short_name if short_name is not None else f"N{node_num}"
    return n


def _config(*, enabled=True, dry_run=False, delivery_type="mesh_dm",
            node_ids=("!aaaa0001",), cooldown_minutes=360,
            position_max_age_hours=72, default_buffer_mi=5.0,
            **family_overrides):
    """Build a Config whose danger_zones is configured per test.

    family_overrides: e.g. avalanche=DangerZoneHazardConfig(...) to tune a
    specific family. Unspecified families keep defaults.
    """
    dz = DangerZonesConfig(
        enabled=enabled,
        dry_run=dry_run,
        delivery_type=delivery_type,
        node_ids=list(node_ids),
        cooldown_minutes=cooldown_minutes,
        position_max_age_hours=position_max_age_hours,
        default_buffer_mi=default_buffer_mi,
        monitor_roles=["ROUTER", "ROUTER_LATE", "CLIENT_BASE"],
        **family_overrides,
    )
    cfg = Config()
    cfg.danger_zones = dz
    return cfg


def _avalanche_event(lat, lon, severity="priority", title="Avalanche Warning"):
    """An avalanche hazard -> family 'avalanche', point hazard (no DB)."""
    return make_event(
        source="avalanche", category="avalanche_warning", severity=severity,
        title=title, summary=title, lat=lat, lon=lon,
    )


# ---------------------------------------------------------------------------
# 1. Hit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hit_flags_colocated_infra_node():
    """A hazard ON a known infra node (well within buffer) -> flagged; a real
    (non-dry_run) send fires exactly once."""
    conn = StubConnector()
    node = _node(node_num=101, role="ROUTER", lat=42.0, lon=-114.0)
    ds = FakeDataStore([node])
    cfg = _config(dry_run=False, default_buffer_mi=5.0)
    corr = DangerZoneCorrelator(cfg, ds, conn)

    corr.handle(_avalanche_event(42.0, -114.0))   # right on the node
    await asyncio.sleep(0)   # let the create_task'd deliver run

    assert len(conn.calls) == 1, f"expected exactly one send, got {conn.calls}"
    assert conn.calls[0]["destination"] == "!aaaa0001"


@pytest.mark.asyncio
async def test_hit_within_buffer_edge():
    """A hazard a few miles away but inside default_buffer_mi -> still a hit."""
    conn = StubConnector()
    # ~3.4 mi north of the hazard (0.05 deg lat ~ 3.45 mi); buffer is 5 mi.
    node = _node(node_num=102, lat=42.05, lon=-114.0)
    ds = FakeDataStore([node])
    cfg = _config(dry_run=False, default_buffer_mi=5.0)
    corr = DangerZoneCorrelator(cfg, ds, conn)

    corr.handle(_avalanche_event(42.0, -114.0))
    await asyncio.sleep(0)
    assert len(conn.calls) == 1


# ---------------------------------------------------------------------------
# 2. Miss
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_miss_far_node_not_flagged():
    """Same hazard far beyond radius+buffer -> no flag/send."""
    conn = StubConnector()
    node = _node(node_num=103, lat=43.0, lon=-114.0)   # ~69 mi north
    ds = FakeDataStore([node])
    cfg = _config(dry_run=False, default_buffer_mi=5.0)
    corr = DangerZoneCorrelator(cfg, ds, conn)

    corr.handle(_avalanche_event(42.0, -114.0))
    await asyncio.sleep(0)
    assert conn.calls == []


# ---------------------------------------------------------------------------
# 3. Role filter (CLIENT out, CLIENT_BASE in)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_node_not_flagged_but_client_base_is():
    """A CLIENT node co-located with the hazard is NOT flagged (not in
    monitor_roles); a co-located CLIENT_BASE node IS flagged."""
    conn = StubConnector()
    client = _node(node_num=201, role="CLIENT", lat=42.0, lon=-114.0,
                   short_name="CLNT")
    client_base = _node(node_num=202, role="CLIENT_BASE", lat=42.0, lon=-114.0,
                        short_name="CBAS")
    ds = FakeDataStore([client, client_base])
    cfg = _config(dry_run=False, default_buffer_mi=5.0)
    corr = DangerZoneCorrelator(cfg, ds, conn)

    corr.handle(_avalanche_event(42.0, -114.0))
    await asyncio.sleep(0)
    # Exactly one send (CLIENT_BASE), CLIENT excluded by role filter.
    assert len(conn.calls) == 1


# ---------------------------------------------------------------------------
# 4. Freshness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_node_skipped():
    """A node whose last_heard is older than position_max_age_hours is
    skipped (not scanned)."""
    conn = StubConnector()
    stale = _node(node_num=301, lat=42.0, lon=-114.0,
                  last_heard=time.time() - 100 * 3600)  # 100h old
    ds = FakeDataStore([stale])
    cfg = _config(dry_run=False, position_max_age_hours=72)
    corr = DangerZoneCorrelator(cfg, ds, conn)

    corr.handle(_avalanche_event(42.0, -114.0))
    await asyncio.sleep(0)
    assert conn.calls == []


# ---------------------------------------------------------------------------
# 5. Cooldown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cooldown_suppresses_second_then_allows_after_window():
    """Two events for the same (node, family) within cooldown -> one send;
    after the cooldown window elapses -> sends again."""
    conn = StubConnector()
    node = _node(node_num=401, lat=42.0, lon=-114.0)
    ds = FakeDataStore([node])
    cfg = _config(dry_run=False, cooldown_minutes=60)
    corr = DangerZoneCorrelator(cfg, ds, conn)

    corr.handle(_avalanche_event(42.0, -114.0))
    await asyncio.sleep(0)
    corr.handle(_avalanche_event(42.0, -114.0))   # within cooldown
    await asyncio.sleep(0)
    assert len(conn.calls) == 1, "second event within cooldown must be suppressed"

    # Wind the recorded last-alert ts back beyond the cooldown window.
    key = (node.node_num, "avalanche")
    corr._last[key] = time.time() - (60 * 60 + 5)
    corr.handle(_avalanche_event(42.0, -114.0))
    await asyncio.sleep(0)
    assert len(conn.calls) == 2, "after cooldown window, a new send must fire"


# ---------------------------------------------------------------------------
# 6. dry_run / disabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_never_sends_even_on_hit():
    """dry_run=True -> connector.send_message NEVER called (zero RF), even on
    a definite hit."""
    conn = StubConnector()
    node = _node(node_num=501, lat=42.0, lon=-114.0)
    ds = FakeDataStore([node])
    cfg = _config(dry_run=True)
    corr = DangerZoneCorrelator(cfg, ds, conn)

    corr.handle(_avalanche_event(42.0, -114.0))
    await asyncio.sleep(0)
    assert conn.calls == []
    # dry_run still records cooldown (proves it proceeded, just send-free).
    assert (node.node_num, "avalanche") in corr._last


@pytest.mark.asyncio
async def test_disabled_does_nothing():
    """enabled=False -> nothing happens at all."""
    conn = StubConnector()
    node = _node(node_num=502, lat=42.0, lon=-114.0)
    ds = FakeDataStore([node])
    cfg = _config(enabled=False, dry_run=False)
    corr = DangerZoneCorrelator(cfg, ds, conn)

    corr.handle(_avalanche_event(42.0, -114.0))
    await asyncio.sleep(0)
    assert conn.calls == []
    assert corr._last == {}


# ---------------------------------------------------------------------------
# 7. min_severity / family disabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_below_min_severity_not_flagged():
    """An event below the family's min_severity -> no flag."""
    conn = StubConnector()
    node = _node(node_num=601, lat=42.0, lon=-114.0)
    ds = FakeDataStore([node])
    # avalanche min_severity=immediate; event is only 'priority' -> gated out.
    cfg = _config(dry_run=False,
                  avalanche=DangerZoneHazardConfig(min_severity="immediate"))
    corr = DangerZoneCorrelator(cfg, ds, conn)

    corr.handle(_avalanche_event(42.0, -114.0, severity="priority"))
    await asyncio.sleep(0)
    assert conn.calls == []


@pytest.mark.asyncio
async def test_min_severity_met_is_flagged():
    """Sanity counterpart: an event AT min_severity passes the gate."""
    conn = StubConnector()
    node = _node(node_num=602, lat=42.0, lon=-114.0)
    ds = FakeDataStore([node])
    cfg = _config(dry_run=False,
                  avalanche=DangerZoneHazardConfig(min_severity="priority"))
    corr = DangerZoneCorrelator(cfg, ds, conn)

    corr.handle(_avalanche_event(42.0, -114.0, severity="priority"))
    await asyncio.sleep(0)
    assert len(conn.calls) == 1


@pytest.mark.asyncio
async def test_family_disabled_not_flagged():
    """The family sub-config disabled -> no flag even on a clear hit."""
    conn = StubConnector()
    node = _node(node_num=603, lat=42.0, lon=-114.0)
    ds = FakeDataStore([node])
    cfg = _config(dry_run=False,
                  avalanche=DangerZoneHazardConfig(enabled=False))
    corr = DangerZoneCorrelator(cfg, ds, conn)

    corr.handle(_avalanche_event(42.0, -114.0))
    await asyncio.sleep(0)
    assert conn.calls == []


# ---------------------------------------------------------------------------
# Extra: seismic family also works as a point hazard (proves family routing
# beyond avalanche).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seismic_event_routes_and_flags():
    conn = StubConnector()
    node = _node(node_num=701, lat=42.0, lon=-114.0)
    ds = FakeDataStore([node])
    cfg = _config(dry_run=False,
                  seismic=DangerZoneHazardConfig(min_severity="routine"))
    corr = DangerZoneCorrelator(cfg, ds, conn)

    ev = make_event(source="usgs", category="earthquake_event",
                    severity="priority", title="M5.1 quake",
                    summary="M5.1 quake", lat=42.0, lon=-114.0)
    corr.handle(ev)
    await asyncio.sleep(0)
    assert len(conn.calls) == 1
