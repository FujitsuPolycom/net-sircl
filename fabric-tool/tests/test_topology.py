"""The topology file: parser forms, refusals, the layout mapping and the role defaults."""

from __future__ import annotations

import pytest

from fabric_tool import model, topology
from fabric_tool.model import FabricError

RING4 = "hosts node-0 node-1 node-2 node-3\ncabling ring\n"
PATH4 = "hosts a b c d\ncabling path\n"
EXPLICIT = "hosts a b c\ncable 0.port0 1.port1\ncable 1.port0 2.port1\ncabling explicit\n"


def test_ring_and_path_and_explicit_map_to_one_group():
    ring = topology.parse(RING4)
    assert ring.cabling == "ring" and ring.size == 4
    assert ring.group.label == "cycle:0-1-2-3" and ring.group.relay_egress == "same"
    assert ring.group.whole_ring and ring.group.ring_size == 4
    assert ring.group.cables == ("0.port0-1.port1", "1.port0-2.port1", "2.port0-3.port1", "3.port0-0.port1")

    path = topology.parse(PATH4)
    assert path.group.label == "path:0-1-2-3" and path.group.relay_egress == "sibling"
    assert not path.group.whole_ring and path.group.ring_size == 5
    assert path.group.cables == ("0.port0-1.port1", "1.port0-2.port1", "2.port0-3.port1")

    explicit = topology.parse(EXPLICIT)
    assert explicit.cabling == "explicit"
    assert explicit.group.label == "path:0-1-2" and explicit.group.relay_egress == "sibling"


def test_defaults_and_overrides():
    ring = topology.parse(RING4)
    assert ring.functions == ("rocep1s0f0", "roceP2p1s0f0", "rocep1s0f1", "roceP2p1s0f1")
    assert ring.budget == topology.DEFAULT_BUDGET
    custom = topology.parse(RING4 + "port0 mlx5_0 mlx5_2\nport1 mlx5_1 mlx5_3\nrelay_budget 262144\n")
    assert custom.functions == ("mlx5_0", "mlx5_2", "mlx5_1", "mlx5_3")
    assert custom.budget == 262144
    assert custom.bindings.devices == ("mlx5_0", "mlx5_2", "mlx5_1", "mlx5_3")
    assert ring.bindings.devices == model.DEFAULT_BINDINGS.devices


def test_plugin_key_and_comments():
    text = "net-sircl-topology\n# a comment\nhosts a b\ncabling ring  # trailing\n"
    ring = topology.parse(text)
    assert ring.hosts == ("a", "b") and ring.group.label == "pair:0-1"


def test_two_host_ring_is_one_cable_like_the_plugin_reads_it():
    """`transport-core/src/topology.c` gives a pair one cable under `ring`; two cables are `explicit`."""
    ring = topology.parse("hosts a b\ncabling ring\n")
    path = topology.parse("hosts a b\ncabling path\n")
    assert ring.group.cables == path.group.cables == ("0.port0-1.port1",)
    assert ring.group.label == "pair:0-1" and ring.group.relay_egress == "sibling"
    assert ring.layout_name == path.layout_name
    # Both lanes of a pair use the one cable's port, as the plugin derives them.
    routes = model.derive_routes(ring.group.layout, 2)
    assert routes.route_text(0) == "1=rocep1s0f0/roceP2p1s0f0"
    assert routes.route_text(1) == "0=rocep1s0f1/roceP2p1s0f1"
    double = topology.parse("hosts a b\ncabling explicit\ncable 0.port0 1.port1\ncable 1.port0 0.port1\n")
    assert double.group.label == "cycle:0-1" and len(double.group.cables) == 2
    assert topology.parse("hosts a b c\ncabling ring\n").group.label == "cycle:0-1-2"


def test_host_count_name_length_and_budget_limits_match_the_plugin():
    names = [f"h{i}" for i in range(65)]
    assert topology.parse("hosts " + " ".join(names[:64]) + "\ncabling path\n").size == 64
    with pytest.raises(FabricError, match="at most 64"):
        topology.parse("hosts " + " ".join(names) + "\ncabling path\n")
    assert topology.parse(f"hosts {'a' * 63} b\ncabling path\n").hosts[0] == "a" * 63
    with pytest.raises(FabricError, match="longer than 63 bytes"):
        topology.parse(f"hosts {'a' * 64} b\ncabling path\n")
    with pytest.raises(FabricError, match="longer than 63 bytes"):
        topology.parse(f"hosts {'é' * 32} b\ncabling path\n")          # 32 characters, 64 bytes
    assert topology.parse("hosts a b\ncabling path\nrelay_budget 0123\n").budget == 123
    for budget in ("١٢٣", "²", "+5", "1_0", "0x10"):                      # digits to Python, not to the plugin
        with pytest.raises(FabricError, match="positive byte count"):
            topology.parse(f"hosts a b\ncabling path\nrelay_budget {budget}\n")


def test_layout_name_follows_the_content_not_the_file():
    ring = topology.parse(RING4, path="/srv/site/ring4.topology")
    assert ring.group.name == ring.layout_name and ring.layout_name.startswith("topology:")
    assert "ring4" not in ring.layout_name
    assert topology.parse(RING4, path="/etc/fabric-table/topology").layout_name == ring.layout_name
    spelled = topology.parse("hosts node-0 node-1 node-2 node-3\ncabling explicit\n# the same cables\n"
                             "cable 0.port1 3.port0\ncable 2.port0 3.port1\n"
                             "cable 1.port0 2.port1\ncable 0.port0 1.port1\nrelay_budget 4096\n")
    assert spelled.layout_name == ring.layout_name
    assert topology.parse(PATH4).layout_name != topology.parse(RING4.replace("node-3", "node-9")).layout_name
    assert topology.parse(RING4 + "port0 mlx5_0 mlx5_2\n").layout_name != ring.layout_name
    assert topology.parse("hosts node-0 node-1 node-2 node-3\ncabling path\n").layout_name != ring.layout_name


def test_refusals():
    for text, why in (
        ("hosts a\ncabling ring\n", "fewer than two hosts"),
        ("hosts a a\ncabling ring\n", "twice"),
        ("hosts a b\ncabling loop\n", "cabling is"),
        ("hosts a b\n", "cabling is"),
        ("hosts a b\ncabling explicit\n", "without a cable"),
        ("hosts a b c\ncable 0.port0 1.port1\ncabling explicit\n", "without a cable"),
        ("hosts a b\ncable 0.port0 5.port1\ncabling explicit\n", "outside the hosts"),
        ("hosts a b c\ncable 0.port0 1.port1\ncable 0.port0 2.port1\ncabling explicit\n", "two cables"),
        ("hosts a b\ncabling ring\nport0 x x\n", "one function twice"),
        ("hosts a b\ncabling ring\nfrobnicate 1\n", "unknown key"),
        ("hosts a b\ncabling ring\nrelay_budget 0\n", "positive byte count"),
    ):
        with pytest.raises(FabricError, match=why):
            topology.parse(text)


def test_group_order_walks_the_fabric():
    crossed = topology.parse("hosts a b c d\ncable 0.port0 2.port1\ncable 2.port0 1.port1\n"
                             "cable 1.port0 3.port1\ncabling explicit\n")
    assert crossed.group.order == (0, 2, 1, 3)
    assert crossed.group.label == "path:0-2-1-3"
    assert crossed.group.relay_egress == "sibling"
    assert crossed.group.ring_size == 5


def test_load_preserves_default_bindings(tmp_path):
    path = tmp_path / "ring4.topology"
    path.write_text(RING4, encoding="utf-8")
    ring = topology.load(path)
    assert ring.bindings.devices == ring.functions
    assert model.DEFAULT_BINDINGS.roles == model.DEFAULT_ROLES


def test_single_function_per_port_ring_and_path_plan_and_facts():
    from fabric_tool import facts as facts_mod, ops, plan as plan_mod

    ring = topology.parse("hosts s0 s1 s2 s3\ncabling ring\nport0 mlx5_0\nport1 mlx5_1\n")
    assert ring.lanes == 1
    assert ring.functions == ("mlx5_0", "mlx5_1")
    assert ring.bindings.lanes == 1
    assert ring.bindings.devices == ("mlx5_0", "mlx5_1")
    assert ring.group.relay_egress == "same"

    sym = plan_mod.Facts.symbolic_for(ring.hosts, bindings=ring.bindings)
    ring_plan = plan_mod.build_plan(ring.group, sym)
    assert ring_plan.sircl.lanes == 1
    assert ring_plan.sircl.route_text(0) == "1=mlx5_0,2=mlx5_0,3=mlx5_1"
    assert ring_plan.sircl.route_text(2) == "0=mlx5_1,1=mlx5_1,3=mlx5_0"
    assert len(ring_plan.host(0).routes) == 2
    assert [r.lanes for r in ring_plan.host(0).routes if r.lanes] == [("rank 0->2 lane 0",)]
    assert len(ring_plan.host(1).filters) == 2

    # Facts binding + cable subnet check with /30 per-cable subnets
    doc = {
        "schema": facts_mod.FACTS_SCHEMA,
        "hosts": [
            {"name": "s0", "position": 0, "functions": {
                "mlx5_0": {"netdev": "eth0", "mac": "00:11:22:33:44:00", "address": "198.51.100.1/30"},
                "mlx5_1": {"netdev": "eth1", "mac": "00:11:22:33:44:01", "address": "198.51.100.14/30"},
            }},
            {"name": "s1", "position": 1, "functions": {
                "mlx5_0": {"netdev": "eth0", "mac": "00:11:22:33:44:10", "address": "198.51.100.5/30"},
                "mlx5_1": {"netdev": "eth1", "mac": "00:11:22:33:44:11", "address": "198.51.100.2/30"},
            }},
            {"name": "s2", "position": 2, "functions": {
                "mlx5_0": {"netdev": "eth0", "mac": "00:11:22:33:44:20", "address": "198.51.100.9/30"},
                "mlx5_1": {"netdev": "eth1", "mac": "00:11:22:33:44:21", "address": "198.51.100.6/30"},
            }},
            {"name": "s3", "position": 3, "functions": {
                "mlx5_0": {"netdev": "eth0", "mac": "00:11:22:33:44:30", "address": "198.51.100.13/30"},
                "mlx5_1": {"netdev": "eth1", "mac": "00:11:22:33:44:31", "address": "198.51.100.10/30"},
            }},
        ],
    }
    bound = facts_mod.bind(doc, ring)
    assert bound.bindings.lanes == 1
    assert ops.cable_problems(ring.group, bound) == []
    concrete = plan_mod.build_plan(ring.group, bound)
    assert concrete.host(0).routes[0].destination == "198.51.100.6"

    # Single-function path automatically uses same-function egress (no sibling exists)
    path = topology.parse("hosts s0 s1 s2 s3\ncabling path\nport0 mlx5_0\nport1 mlx5_1\n")
    assert path.group.relay_egress == "same"
    path_plan = plan_mod.build_plan(path.group, plan_mod.Facts.symbolic_for(path.hosts, bindings=path.bindings))
    assert path_plan.sircl.route_text(0) == "1=mlx5_0,2=mlx5_0,3=mlx5_0"
    assert path_plan.sircl.route_text(3) == "0=mlx5_1,1=mlx5_1,2=mlx5_1"

    with pytest.raises(model.FabricError, match="same number of functions"):
        topology.parse("hosts a b\ncabling path\nport0 mlx5_0\n")
    with pytest.raises(model.FabricError, match="same number of functions"):
        topology.parse("hosts a b\ncabling path\nport0 mlx5_0\nport1 mlx5_1 mlx5_2\n")
    with pytest.raises(model.FabricError, match="sibling relay egress"):
        plan_mod.build_group_plan(path.group, sym, relay_egress="sibling")

