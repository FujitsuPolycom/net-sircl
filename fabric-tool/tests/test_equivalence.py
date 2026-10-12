"""Equivalence: the tool's planned commands equal sparkring_sircl.fabric's for the same layouts.

For a ring of four, a ring of eight, a path of four and a path of three, both
tools plan the same layout from identical facts (addresses and MACs). The
test compares the route sets, the relay-filter sets, the marker rules and the
exact shell command strings of both command generators. Skips where
SparkRing's SIRCL package is not beside the tool (the standalone archive).
"""

from __future__ import annotations

import pytest

from fabric_tool import commands as tool_commands, model, plan as tool_plan, topology as tool_topology

try:
    from sparkring_sircl.fabric import commands as sircl_commands, layouts as sircl_layouts
    from sparkring_sircl.fabric import plan as sircl_plan
    SIRCL = True
except ImportError:                      # the standalone archive carries the tool alone
    SIRCL = False

pytestmark = pytest.mark.skipif(not SIRCL, reason="SparkRing's SIRCL package is not beside the tool")


def _facts_for(cables, size):
    """Identical facts for both tools: each cable's a-end .1 and b-end .2 on one /30 per (cable, class)."""
    netdev_of = {}
    addresses: dict[tuple[int, int, bool], tuple[str, int]] = {}
    for index, cable in enumerate(cables):
        for port, position, other in ((cable.a_port, cable.a, cable.b), (cable.b_port, cable.b, cable.a)):
            netdev_of[(position, port)] = model.role_at(port, False).netdev
            netdev_of[(position, port)] = model.role_at(port, False).netdev
        for secondary in (False, True):
            subnet = 40 + index * 2 + int(secondary)
            a = model.role_at(cable.a_port, secondary)
            b = model.role_at(cable.b_port, secondary)
            addresses[(cable.a, cable.a_port, secondary)] = (f"192.0.2.{subnet * 4 + 1}", 30)
            addresses[(cable.b, cable.b_port, secondary)] = (f"192.0.2.{subnet * 4 + 2}", 30)
            del a, b
    hosts = []
    for position in range(size):
        ports = []
        for secondary in (False, True):
            for port in (0, 1):
                if (position, port) not in netdev_of:
                    continue
                netdev = model.role_at(port, secondary).netdev
                address, prefixlen = addresses[(position, port, secondary)]
                mac = f"02:{position:02x}:{port:02x}:{int(secondary):02x}:00:{position * 4 + port * 2 + secondary:02x}"
                ports.append(tool_plan.Port(netdev, mac, address, prefixlen))
        hosts.append(tool_plan.HostFacts(position, f"host{position}", tuple(ports)))
    return tool_plan.Facts(hosts)


def _tool_group(spec: str) -> tool_topology.GroupSpec:
    return tool_topology.parse(spec).group


def _sircl_group(spec: str) -> sircl_layouts.Group:
    """SparkRing's group of the same layout: ring<N>, or a path on a ring one Spark longer."""
    if spec.startswith("ring"):
        size = int(spec[4:])
        return sircl_layouts.resolve(ring_size=size, layout=f"ring{size}").groups[0]
    size = int(spec[4:])
    return sircl_layouts.resolve(ring_size=size + 1, groups=f"0-{size - 1}").groups[0]


SPECS = ("ring4", "ring8", "path4", "path3")


def _topology_text(spec: str) -> str:
    size = int(spec[4:])
    cabling = "ring" if spec.startswith("ring") else "path"
    return "hosts " + " ".join(f"host{i}" for i in range(size)) + f"\ncabling {cabling}\n"


def _both(spec: str):
    topology = tool_topology.parse(_topology_text(spec), path=f"{spec}.topology")
    model.set_roles(model.DEFAULT_ROLES)
    facts = _facts_for(topology.group.layout.fabric.cables, topology.size)
    tool = tool_plan.build_plan(topology.group, facts)
    sircl = sircl_plan.build_group_plan(_sircl_group(spec), facts, relay_egress=topology.group.relay_egress)
    return tool, sircl, facts


def _route_set(spark):
    return {(route.destination, route.netdev, route.source, route.next_mac, route.tag)
            for route in spark.routes}


def _filter_set(spark):
    return {(f.in_netdev, f.k, f.out_netdev, f.next_mac, f.protocol, f.new_type) for f in spark.filters}


def _marker_rules(spark):
    return {rules.device: frozenset(rules.rules) for rules in spark.markers}


@pytest.mark.parametrize("spec", SPECS)
def test_planned_rules_and_routes_are_identical(spec):
    tool, sircl, _ = _both(spec)
    assert tool.group.label == sircl.group.label
    assert tool.relay_egress == sircl.relay_egress
    for position in tool.group.members:
        mine, theirs = tool.host(position), sircl.spark(position)
        assert mine is not None and theirs is not None
        assert _route_set(mine) == _route_set(theirs), (spec, position, "routes")
        assert _filter_set(mine) == _filter_set(theirs), (spec, position, "filters")
        assert _marker_rules(mine) == _marker_rules(theirs), (spec, position, "markers")


@pytest.mark.parametrize("spec", SPECS)
def test_planned_command_strings_are_identical(spec):
    tool, sircl, _ = _both(spec)
    for position in tool.group.members:
        mine, theirs = tool.host(position), sircl.spark(position)
        my_filters = {f.pref: f for f in mine.filters}
        their_filters = {f.pref: f for f in theirs.filters}
        assert set(my_filters) == set(their_filters)
        for pref, relay_filter in my_filters.items():
            assert tool_commands.filter_add(relay_filter) == sircl_commands.filter_add(their_filters[pref])
        my_routes = {route.destination: route for route in mine.routes}
        their_routes = {route.destination: route for route in theirs.routes}
        for destination, route in my_routes.items():
            theirs_route = their_routes[destination]
            assert tool_commands.route_replace(destination, route.netdev, route.source) == \
                sircl_commands.route_replace(destination, theirs_route.netdev, theirs_route.source)
            assert tool_commands.neigh_replace(destination, route.next_mac, route.netdev) == \
                sircl_commands.neigh_replace(destination, theirs_route.next_mac, theirs_route.netdev)
        my_markers = _marker_rules(mine)
        their_markers = _marker_rules(theirs)
        for device, rules in my_markers.items():
            assert tool_commands.marker_start(tool_commands.MarkerConfig(), device, sorted(rules)) == \
                sircl_commands.marker_start(sircl_commands.MarkerConfig(), device, sorted(their_markers[device]))


@pytest.mark.parametrize("spec", SPECS)
def test_sircl_lanes_are_carried_and_the_plan_digests_the_group(spec):
    tool, sircl, _ = _both(spec)
    assert tool.digest == sircl.digest
    assert [tool.sircl.route_text(rank) for rank in range(tool.sircl.world)] == \
        [sircl.sircl.route_text(rank) for rank in range(sircl.sircl.world)]
    assert tool.per_peer_bytes == sircl.per_peer_bytes
    assert tool.busiest_queue == sircl.busiest_queue and tool.load_factor == sircl.load_factor


def test_ring8_is_the_universal_table_of_ninety_six_rules_and_routes():
    tool, sircl, _ = _both("ring8")
    routes = sum(len(host.routes) for host in tool.hosts)
    filters = sum(len(host.filters) for host in tool.hosts)
    assert (routes, filters) == (96, 96) == (sum(len(s.routes) for s in sircl.sparks),
                                             sum(len(s.filters) for s in sircl.sparks))
