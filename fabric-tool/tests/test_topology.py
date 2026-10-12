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
    model.set_roles(topology._roles_of_functions(custom.functions))
    assert model.RDMA_DEVICES == ("mlx5_0", "mlx5_2", "mlx5_1", "mlx5_3")
    model.set_roles(model.DEFAULT_ROLES)


def test_plugin_key_and_comments():
    text = "net-sircl-topology\n# a comment\nhosts a b\ncabling ring  # trailing\n"
    ring = topology.parse(text)
    assert ring.hosts == ("a", "b") and ring.group.label == "cycle:0-1"


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


def test_load_installs_roles(tmp_path):
    path = tmp_path / "ring4.topology"
    path.write_text(RING4, encoding="utf-8")
    ring = topology.load(path)
    assert [role.device for role in model.ROLES] == list(ring.functions)
    model.set_roles(model.DEFAULT_ROLES)
