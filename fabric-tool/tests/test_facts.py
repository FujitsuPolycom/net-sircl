"""The facts document: schema, merging, identity and the cabling check."""

from __future__ import annotations

import json

import pytest

from fabric_tool import facts as facts_mod, model, topology
from fabric_tool.model import FabricError

RING4 = "hosts node-0 node-1 node-2 node-3\ncabling ring\n"


def _document(*, size: int = 4) -> dict:
    """Synthetic ring addressing: one documentation /30 per cable and function class."""
    hosts = []
    for position in range(size):
        functions = {}
        for role in model.ROLES:
            offset = int(role.secondary)
            subnet = 2 * (position if role.port == 0 else (position - 1) % size) + offset
            host_octet = 1 if role.port == 0 else 2
            functions[role.device] = {"netdev": role.netdev,
                                      "mac": f"02:00:{position:02x}:{role.port:02x}:{offset:02x}:01",
                                      "address": f"192.0.2.{subnet * 4 + host_octet}/30"}
        hosts.append({"name": f"node-{position}", "functions": functions})
    hosts[0]["name"], hosts[1]["name"], hosts[2]["name"], hosts[3]["name"] = (
        "node-0", "node-1", "node-2", "node-3")
    return {"schema": facts_mod.FACTS_SCHEMA, "hosts": hosts}


def test_bind_and_merge(tmp_path):
    ring_topology = topology.parse(RING4)
    facts = facts_mod.bind(_document(), ring_topology)
    assert facts.positions == (0, 1, 2, 3)
    assert facts.name(2) == "node-2"
    # Two files merge; a contradiction is refused.
    first = tmp_path / "a.json"
    second = tmp_path / "b.json"
    document = _document()
    first.write_text(json.dumps({"schema": facts_mod.FACTS_SCHEMA,
                                 "hosts": document["hosts"][:2]}), encoding="utf-8")
    second.write_text(json.dumps({"schema": facts_mod.FACTS_SCHEMA,
                                  "hosts": document["hosts"][2:]}), encoding="utf-8")
    merged = facts_mod.bind(facts_mod.load([first, second]), ring_topology)
    assert merged.positions == (0, 1, 2, 3)
    conflicting = json.loads(json.dumps(document))
    conflicting["hosts"][0]["functions"][model.RDMA_DEVICES[0]]["mac"] = "02:00:00:09:09:09"
    third = tmp_path / "c.json"
    third.write_text(json.dumps({"schema": facts_mod.FACTS_SCHEMA, "hosts": conflicting["hosts"][:1]}),
                     encoding="utf-8")
    with pytest.raises(FabricError, match="disagree"):
        facts_mod.load([first, third])


def test_bind_refusals():
    ring_topology = topology.parse(RING4)
    document = _document()
    document["hosts"][3]["name"] = "node-unknown"
    with pytest.raises(FabricError, match="does not list"):
        facts_mod.bind(document, ring_topology)
    document = _document()
    del document["hosts"][2]
    with pytest.raises(FabricError, match="facts are missing"):
        facts_mod.bind(document, ring_topology)
    document = _document()
    document["hosts"][0]["functions"][model.RDMA_DEVICES[0]]["netdev"] = "other0"
    with pytest.raises(FabricError, match="name it alike"):
        facts_mod.bind(document, ring_topology)
    document = _document()
    document["hosts"][0]["position"] = 7
    with pytest.raises(FabricError, match="position 7"):
        facts_mod.bind(document, ring_topology)
    document = _document()
    for host in document["hosts"]:
        for role in model.ROLES:
            host["functions"][role.device]["mac"] = "02:00:00:00:00:01"
    with pytest.raises(FabricError, match="held by"):
        facts_mod.bind(document, ring_topology)


def test_cabling_check_refuses_wrong_order():
    ring_topology = topology.parse(RING4)
    facts = facts_mod.bind(_document(), ring_topology)
    from fabric_tool.ops import cable_problems
    assert cable_problems(ring_topology.group, facts) == []
    # Swap two cable ends' addresses: the subnets no longer match the topology's host order.
    swapped = _document()
    functions = swapped["hosts"][0]["functions"]
    other = swapped["hosts"][1]["functions"]
    functions[model.ROLES[0].device]["address"] = "192.0.2.201/30"
    other[model.ROLES[3].device]["address"] = "192.0.2.206/30"
    broken = facts_mod.bind(swapped, ring_topology)
    problems = cable_problems(ring_topology.group, broken)
    assert problems and "host order does not match the cabling" in problems[0]


def test_facts_file_round_trip_preserves_cable_prefixes(tmp_path):
    document = _document()
    path = tmp_path / "facts.json"
    path.write_text(json.dumps(document))
    merged = facts_mod.load([path])
    assert merged == document
    path.write_text(json.dumps(merged))
    facts = facts_mod.bind(facts_mod.load([path]), topology.parse(RING4))
    assert all(port.prefixlen == 30 for position in facts.positions for port in facts.host(position).ports)
