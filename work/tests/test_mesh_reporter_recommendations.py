"""Tests for MeshReporter's recommendations engine.

recommendations_list() is the canonical source of recommendation text
(list[str]); build_recommendations() formats that same list into the
"OPTIMIZATION RECOMMENDATIONS:\n  - ..." string consumed by router.py's LLM
prompt injection (a live production path — see router.py ~line 1145). This
file locks in that build_recommendations() is exactly
recommendations_list() joined with the historical header/bullet format, so
future refactors can't silently change the LLM-facing string.
"""
from __future__ import annotations

import time

import pytest

from meshai.mesh_health import HealthScore, MeshHealth, RegionHealth
from meshai.mesh_models import UnifiedNode
from meshai.mesh_reporter import MeshReporter


def _node(node_num, **kw):
    defaults = dict(
        node_num=node_num,
        node_id_hex=f"!{node_num:08x}",
        short_name=f"N{node_num}",
        long_name=f"Node {node_num}",
        last_heard=time.time(),
        is_online=True,
    )
    defaults.update(kw)
    return UnifiedNode(**defaults)


class _FakeHealthEngine:
    def __init__(self, mesh_health, packet_threshold=500):
        self.mesh_health = mesh_health
        self.packet_threshold = packet_threshold
        self._nodes = mesh_health.nodes if mesh_health else {}

    def get_node(self, identifier):
        for n in self._nodes.values():
            if str(n.node_num) == str(identifier) or n.node_id_hex == identifier or n.short_name == identifier:
                return n
        return None


class _FakeDataStore:
    def __init__(self, avg_gateways=1.5):
        self._avg_gateways = avg_gateways

    def get_mesh_deliverability(self):
        return {"avg_gateways": self._avg_gateways}


@pytest.fixture
def reporter_with_data():
    """A MeshReporter wired to a small synthetic mesh with several
    recommendation-triggering conditions across node/region/mesh scopes."""
    n1 = _node(
        1,
        packets_by_type={"POSITION_APP": 500},  # aggressive interval trigger
        channel_utilization=40,
        air_util_tx=15,
        battery_percent=10,
        battery_trend="declining",
        predicted_depletion_hours=20,
        is_infrastructure=True,
        uplink_enabled=False,
    )
    n2 = _node(2, is_online=False, last_heard=time.time() - 7200, is_infrastructure=True, uplink_enabled=True)
    n3 = _node(3, avg_gateways=1.0, packets_sent_24h=1000, text_messages_24h=0)
    n4 = _node(4, avg_gateways=1.0, packets_sent_24h=1000, text_messages_24h=0)
    n5 = _node(5, avg_gateways=1.0, packets_sent_24h=1000, text_messages_24h=0)
    n6 = _node(6, battery_percent=5, battery_trend="declining")
    n7 = _node(7, channel_utilization=20)

    nodes = {n.node_num: n for n in [n1, n2, n3, n4, n5, n6, n7]}

    region = RegionHealth(
        name="TestRegion",
        node_ids=[str(i) for i in range(1, 8)],
        score=HealthScore(util_percent=30, infra_total=2, infra_online=1),
    )

    mesh_health = MeshHealth(regions=[region], nodes=nodes)
    engine = _FakeHealthEngine(mesh_health)
    return MeshReporter(engine, data_store=_FakeDataStore())


@pytest.mark.parametrize(
    "scope,scope_value",
    [
        ("mesh", None),
        ("region", "TestRegion"),
        ("node", "1"),
        ("node", "2"),
        ("node", "999"),  # missing node
        ("region", "Nowhere"),  # missing region
    ],
)
def test_build_recommendations_matches_recommendations_list(reporter_with_data, scope, scope_value):
    """build_recommendations() must be exactly recommendations_list() formatted
    with the historical header + bullet convention (byte-identical LLM prompt
    text is the whole point of this refactor)."""
    recs = reporter_with_data.recommendations_list(scope, scope_value)
    text = reporter_with_data.build_recommendations(scope, scope_value)

    if not recs:
        assert text == ""
    else:
        expected_lines = ["OPTIMIZATION RECOMMENDATIONS:"] + [f"  - {r}" for r in recs]
        assert text == "\n".join(expected_lines)


def test_build_recommendations_llm_string_pinned(reporter_with_data):
    """Pins the exact LLM-facing string router.py:1145 injects into the
    system prompt (a live production path) against a literal, hardcoded
    expectation — independent of the implementation, unlike the
    recommendations_list()-derived check above. Captured against
    origin/main before the recommendations_list() refactor via a synthetic
    fixture identical to reporter_with_data's, and confirmed byte-identical
    after. If this test ever needs to change, the LLM prompt text changed
    and that must be a deliberate, reviewed decision — not a refactor
    side-effect.
    """
    assert reporter_with_data.build_recommendations("mesh") == (
        "OPTIMIZATION RECOMMENDATIONS:\n"
        "  - Coverage gap in TestRegion: 3 nodes only reach 1 gateway. "
        "A new MQTT feeder in this area would add monitoring redundancy.\n"
        "  - Node 6 (N6) at 5% battery and declining. Likely offline soon.\n"
        "  - High channel utilization on Node 1 (N1), Node 7 (N7). "
        "Check for aggressive broadcast intervals or nearby interference.\n"
        "  - Mesh-wide average is 1.5 gateways per packet. "
        "Adding MQTT feeders would improve monitoring reliability across the mesh."
    )

    assert reporter_with_data.build_recommendations("region", "TestRegion") == (
        "OPTIMIZATION RECOMMENDATIONS:\n"
        "  - Channel utilization at 30%. Consider spreading nodes across "
        "frequencies or reducing telemetry intervals.\n"
        "  - 1 infrastructure node(s) offline. Check power and connectivity.\n"
        "  - High-traffic nodes (Node 3 (N3), Node 4 (N4), Node 5 (N5)) "
        "impacting channel. Review their telemetry settings.\n"
        "  - Nodes with frequent position broadcasts (Node 1 (N1)). "
        "Recommend 900s interval."
    )

    assert reporter_with_data.build_recommendations("node", "2") == (
        "OPTIMIZATION RECOMMENDATIONS:\n"
        "  - Node offline since 2h ago. Check power and connectivity."
    )

    assert reporter_with_data.build_recommendations("node", "999") == ""
    assert reporter_with_data.build_recommendations("region", "Nowhere") == ""


def test_recommendations_list_caps_at_ten(reporter_with_data):
    recs = reporter_with_data.recommendations_list("node", "1")
    assert len(recs) <= 10


def test_recommendations_list_empty_when_no_health_data():
    engine = _FakeHealthEngine(None)
    reporter = MeshReporter(engine, data_store=_FakeDataStore())

    assert reporter.recommendations_list("mesh") == []
    assert reporter.build_recommendations("mesh") == ""


def test_recommendations_list_empty_mesh_no_triggers():
    """An empty mesh with no nodes/regions produces no recommendations."""
    mesh_health = MeshHealth()
    engine = _FakeHealthEngine(mesh_health)
    reporter = MeshReporter(engine, data_store=_FakeDataStore(avg_gateways=3.0))

    assert reporter.recommendations_list("mesh") == []
    assert reporter.build_recommendations("mesh") == ""


def test_recommendations_list_returns_plain_strings(reporter_with_data):
    recs = reporter_with_data.recommendations_list("mesh")
    assert isinstance(recs, list)
    assert all(isinstance(r, str) for r in recs)
    # Plain recommendation text, not pre-formatted with the LLM-prompt header
    # or bullet markers — that formatting belongs to build_recommendations().
    assert all(not r.startswith("OPTIMIZATION RECOMMENDATIONS") for r in recs)
    assert all(not r.startswith("  - ") for r in recs)
