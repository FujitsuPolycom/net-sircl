"""Drive the real verify CLI against a synthetic installed path, then lose a relay rule.

Only host reads are replaced: plan derivation, comparison, route verification,
diagnostics and CLI exit status run unchanged. No host commands execute.
"""
import dataclasses
import ipaddress

import pytest
from fabric_tool import cli, commands, diff, facts, model, ops, plan, state, topology
from test_facts import _document


@pytest.fixture
def installed(tmp_path, monkeypatch):
    text = "hosts node-0 node-1 node-2 node-3\ncabling path\n"
    path = tmp_path / "path.topology"
    path.write_text(text)
    topo = topology.parse(text, path=str(path))
    document = _document()
    bound = facts.bind(document, topo)
    built = plan.build_plan(topo.group, bound)
    marker = commands.MarkerConfig()
    observed = {}
    for host in built.hosts:
        s = state.HostState(host.position, host.name, host.name, True, hostname=host.name)
        for device, fn in document["hosts"][host.position]["functions"].items():
            addr = ipaddress.ip_interface(fn["address"])
            s.rdma[device] = fn["netdev"]
            s.links[fn["netdev"]] = state.Link(fn["netdev"], fn["mac"], ((str(addr.ip), addr.network.prefixlen),), "UP")
        for f in host.filters:
            s.ingress_qdisc[f.in_netdev] = "ingress"
            s.filters.setdefault(f.in_netdev, []).append(state.FilterEntry(
                f.in_netdev, f.pref, 0, "flower", f.protocol, f.handle, True, True,
                f.next_mac, f.new_type, f.out_netdev, None))
        for r in host.routes:
            s.routes.append(state.RouteEntry(r.destination, 32, r.netdev, r.source, "link", 82, "82", 0, None, "unicast"))
            s.neighbours.append(state.NeighbourEntry(r.destination, r.netdev, r.next_mac, ("PERMANENT",), 82, "82"))
        for device in model.RDMA_DEVICES:
            m = host.marker(device)
            if m:
                s.markers.append(state.MarkerProcess(100 + len(s.markers), (marker.path,), device,
                                                     frozenset(m.rules), True, None))
        s.record = diff._record(host, built, topo.group.name, marker, list(s.ingress_qdisc))
        observed[host.position] = s
    monkeypatch.setattr(ops, "gather", lambda *a, **kw: observed)
    healthy = ops.evaluate(topo, observed, None, mode="up", marker=marker, adopt=False)
    assert not healthy.blocked and not healthy.pending
    routes = ops.lane_route_checks(healthy, installed_only=True)

    def executor(target, script, timeout):
        # The CLI must never apply its suggested repair during verify.
        assert "ip -o route get " in script and "sudo" not in script
        position = int(target.split("-")[-1])
        return ops.Result(0, "".join(f"@@route:{dst}\n{dst} dev {dev}\n" for dst, dev in routes[position].items()), "")

    args = ["verify", "--topology", str(path)]
    for host in built.hosts:
        args += ["--ssh", f"{host.name}={host.name}"]
    return observed, lambda: cli.main(args, executor=executor)


def test_missing_middle_relay_rule_is_reported_and_recovery_verifies(installed, capsys):
    states, verify = installed
    assert verify() == 0
    capsys.readouterr()
    middle = states[1]
    netdev = next(iter(middle.filters))
    removed = middle.filters[netdev].pop()
    assert verify() == 1
    text = capsys.readouterr().out
    assert "node-1" in text and "add filter" in text and netdev in text
    middle.filters[netdev].append(removed)
    assert verify() == 0


def test_filter_present_but_no_longer_offloaded_fails_verify(installed):
    states, verify = installed
    netdev = next(iter(states[1].filters))
    entry = states[1].filters[netdev][0]
    states[1].filters[netdev][0] = dataclasses.replace(entry, in_hw=False)
    assert verify() == 1


def test_verify_refuses_ownership_blocker_even_with_matching_forwarding(installed):
    states, verify = installed
    states[1].record = dict(states[1].record, group="another-group")
    assert verify() == 3


def test_unreachable_relay_fails_verify(installed):
    states, verify = installed
    states[1].reachable = False
    states[1].error = "synthetic read timeout"
    assert verify() == 3
