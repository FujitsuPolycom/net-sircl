"""The plan derivation: objects, tags, preferences, traces, isolation and the queue bound."""

from __future__ import annotations

import pytest

from fabric_tool import model, plan as plan_mod, topology
from fabric_tool.model import FabricError


def _facts(names, addresses):
    """Facts from {(position, netdev): (address, prefixlen)}; functions without a cable are left out."""
    hosts = []
    for position, name in enumerate(names):
        ports = []
        for role in model.ROLES:
            if (position, role.netdev) not in addresses:
                continue
            address, prefixlen = addresses[(position, role.netdev)]
            mac = f"02:{position:02x}:{role.port:02x}:{int(role.secondary):02x}:00:{role.port * 2 + role.secondary:02x}"
            ports.append(plan_mod.Port(role.netdev, mac, address, prefixlen))
        hosts.append(plan_mod.HostFacts(position, name, tuple(ports)))
    return plan_mod.Facts(hosts)


def _ring_addresses(size):
    """The simulator's addressing: port 0 of rank r is 198.18.(2r+f).1/24, port 1 is 198.18.(2(r-1)+f).2/24."""
    addresses = {}
    for rank in range(size):
        for secondary in (False, True):
            offset = int(secondary)
            addresses[(rank, model.role_at(0, secondary).netdev)] = (f"198.18.{2 * rank + offset}.1", 24)
            addresses[(rank, model.role_at(1, secondary).netdev)] = (f"198.18.{2 * ((rank - 1) % size) + offset}.2", 24)
    return addresses


@pytest.mark.parametrize("size", (4, 8))
def test_whole_cycle_universal_table(size):
    topology_text = "hosts " + " ".join(f"h{i}" for i in range(size)) + "\ncabling ring\n"
    group = topology.parse(topology_text).group
    facts = _facts(group.members and [f"h{i}" for i in range(size)], _ring_addresses(size))
    built = plan_mod.build_plan(group, facts)
    assert built.group.label == f"cycle:{'-'.join(str(i) for i in range(size))}"
    assert built.relay_egress == "same"
    routes = sum(len(host.routes) for host in built.hosts)
    filters = sum(len(host.filters) for host in built.hosts)
    # Every rank routes both addresses of every peer at distance 2..size/2 on both classes: the universal
    # table of the cycle (16 routes and filters per rank on a cycle of eight).
    assert routes == size * 2 * (size // 2 - 1) * 2
    assert filters == size * 2 * (size // 2 - 1) * 2
    for host in built.hosts:
        assert {rule.k for rule in host.filters} == set(range(1, size // 2))
        for route in host.routes:
            assert route.tag == plan_mod.tag(route.hops - 1)
            assert route.hops - 1 == len(route.relays)


def test_path3_and_path4_relay_inside_the_path():
    for size in (3, 4):
        topology_text = "hosts " + " ".join(f"h{i}" for i in range(size)) + "\ncabling path\n"
        group = topology.parse(topology_text).group
        facts = _facts([f"h{i}" for i in range(size)], _path_addresses(size))
        built = plan_mod.build_plan(group, facts)
        assert built.relay_egress == "sibling"
        counts = {host.position: (len(host.routes), len(host.filters)) for host in built.hosts}
        assert counts[0][1] == 0 and counts[size - 1][1] == 0          # the ends relay nothing
        assert counts[0][0] == 2 * (size - 2) and counts[size - 1][0] == 2 * (size - 2)
        for middle in range(1, size - 1):
            assert counts[middle][1] > 0


def _path_addresses(size):
    """Addresses for a path of ``size`` hosts: each cable's ends share one /24 (a-end .1, b-end .2)."""
    addresses = {}
    for cable_index in range(size - 1):
        for secondary in (False, True):
            subnet = 10 + cable_index * 2 + int(secondary)
            addresses[(cable_index, model.role_at(0, secondary).netdev)] = (f"198.18.{subnet}.1", 24)
            addresses[(cable_index + 1, model.role_at(1, secondary).netdev)] = (f"198.18.{subnet}.2", 24)
    return addresses


def test_tags_preferences_and_trace():
    group = topology.parse("hosts h0 h1 h2 h3\ncabling ring\n").group
    facts = _facts([f"h{i}" for i in range(4)], _ring_addresses(4))
    built = plan_mod.build_plan(group, facts)
    assert plan_mod.tag(0) == 0x0800 and plan_mod.tag(1) == 0x88b5 and plan_mod.tag(7) == 0x88bb
    assert plan_mod.relays_left(0x88b7) == 3 and plan_mod.relays_left(0x0800) == 0
    assert plan_mod.relays_left(0x88bc) is None
    with pytest.raises(FabricError):
        plan_mod.tag(8)
    host0 = built.host(0)
    assert host0 is not None
    for route in host0.routes:
        result = plan_mod.trace({h.position: h for h in built.hosts}, facts, 4, 0, route.destination)
        assert result.outcome == "delivered", (route.destination, result)


def test_isolation_and_relay_limit():
    group = topology.parse("hosts h0 h1 h2 h3\ncabling ring\n").group
    facts = _facts([f"h{i}" for i in range(4)], _ring_addresses(4))
    with pytest.raises(FabricError, match="qualified limit"):
        plan_mod.build_plan(group, facts, max_relays=0)
    built = plan_mod.build_plan(group, facts)
    assert plan_mod.isolation_problems([built]) == []
    # Symbolic facts make a reviewable plan without addresses.
    symbolic = plan_mod.Facts.symbolic_for([f"h{i}" for i in range(4)])
    symbolic_plan = plan_mod.build_plan(group, symbolic)
    assert symbolic_plan.host(0) is not None
    assert "<h0:enp1s0f0np0>" in plan_mod.render_text(symbolic_plan, symbolic)


def test_queue_bound():
    group = topology.parse("hosts h0 h1 h2 h3\ncabling ring\n").group
    facts = _facts([f"h{i}" for i in range(4)], _ring_addresses(4))
    built = plan_mod.build_plan(group, facts)
    assert built.load_factor == 0.5 and built.per_peer_bytes == int(0.75 * 524288 / 0.5)
    path_group = topology.parse("hosts h0 h1 h2 h3\ncabling path\n").group
    path_plan = plan_mod.build_plan(path_group, _facts([f"h{i}" for i in range(4)], _path_addresses(4)))
    assert path_plan.load_factor == 1 and path_plan.per_peer_bytes == int(0.75 * 524288)
