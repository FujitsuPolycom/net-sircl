"""Regressions for the review findings: spoofed or failed reads, record identity, explicit cabling,
restore policy, objects the command builders refuse, boot unit handling and qdisc ownership.

Hosts are synthetic. Bash tests run the read script against shell functions only; no host command,
SSH, sudo or systemctl runs.
"""
from __future__ import annotations

import dataclasses
import ipaddress
import json
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from fabric_tool import boot, cli, commands, diff, facts as facts_mod, model, ops, plan, report, state, topology
from fabric_tool.model import FabricError
from test_commands import READ_STUBS
from test_facts import RING4, _document
from test_marker_security import environment  # noqa: F401  (fixture)

MARKER = commands.MarkerConfig()
PATH4 = "hosts node-0 node-1 node-2 node-3\ncabling path\n"
NETDEVS = model.FABRIC_NETDEVS


def healthy(text: str = RING4, path: str = "/srv/site/ring4.topology"):
    """Hosts that hold exactly their part of the plan, as a read would report them."""
    topo = topology.parse(text, path=path)
    document = _document()
    bound = facts_mod.bind(document, topo)
    built = plan.build_plan(topo.group, bound)
    states = {}
    for host in built.hosts:
        s = state.HostState(host.position, host.name, host.name, True, hostname=host.name)
        for device, function in document["hosts"][host.position]["functions"].items():
            address = ipaddress.ip_interface(function["address"])
            s.rdma[device] = function["netdev"]
            s.links[function["netdev"]] = state.Link(function["netdev"], function["mac"],
                                                     ((str(address.ip), address.network.prefixlen),), "UP")
        for f in host.filters:
            s.ingress_qdisc[f.in_netdev] = "ingress"
            s.filters.setdefault(f.in_netdev, []).append(state.FilterEntry(
                f.in_netdev, f.pref, 0, "flower", f.protocol, f.handle, True, True, f.next_mac, f.new_type,
                f.out_netdev, None))
        for r in host.routes:
            s.routes.append(state.RouteEntry(r.destination, 32, r.netdev, r.source, "link", 82, "82", 0, None,
                                             "unicast"))
            s.neighbours.append(state.NeighbourEntry(r.destination, r.netdev, r.next_mac, ("PERMANENT",), 82, "82"))
        for device in model.RDMA_DEVICES:
            rules = host.marker(device)
            if rules:
                s.markers.append(state.MarkerProcess(100 + len(s.markers), (MARKER.path,), device,
                                                     frozenset(rules.rules), True, None))
        s.marker_binary = s.marker_recorded = "a" * 64
        s.marker_owners = {p: ("root", 0o755) for p in (*MARKER.checked_paths, *MARKER.log_dirs)}
        s.record = diff._record(host, built, topo.group.name, MARKER, [])
        states[host.position] = s
    return topo, bound, built, states


def evaluate(topo, states, bound, mode):
    return ops.evaluate(topo, states, bound, mode=mode, marker=MARKER, adopt=False)


def read_output(name: str = "node-0", **sections: str) -> str:
    """A complete read of a host that holds nothing; keyword ``exit_links="2"`` or ``tools="tc"`` overrides
    a section (``_`` stands for ``:``)."""
    blocks = {"hostname": name, "tools": "", "links": "[]", "exit:links": "0", "routes": "[]", "exit:routes": "0",
              "neighbours": "[]", "exit:neighbours": "0"}
    for netdev in NETDEVS:
        blocks.update({f"qdisc:{netdev}": "[]", f"exit:qdisc:{netdev}": "0",
                       f"filters:{netdev}": "", f"exit:filters:{netdev}": "0"})
    blocks.update({"markers": "", "exit:markers": "1"})
    for key, value in sections.items():
        blocks[key.replace("_", ":")] = value
    return "".join(f"@@{key}\n{value}\n" for key, value in blocks.items()) + "@@end\n"


def parsed(**sections: str) -> state.HostState:
    return state.parse(0, "node-0", "node-0", returncode=0, stdout=read_output(**sections), stderr="")


# -- 1. marker liveness and section headers ----------------------------------------------------------------


def test_only_processes_of_root_are_read_as_markers():
    script = commands.read_script(MARKER)
    assert "pgrep -a -x -U 0 -u 0 fabric-tool-mar" in script.splitlines()
    assert not any(line.startswith("pgrep") and "-U 0 -u 0" not in line for line in script.splitlines())


def test_a_repeated_section_header_never_replaces_the_first():
    spoofed_route = json.dumps([{"dst": "203.0.113.9", "dev": NETDEVS[0], "protocol": "82", "scope": "link"}])
    # Output inside the markers section imitates the script: an earlier section and its own section.
    host = parsed(markers=f"31337 x\n@@routes\n{spoofed_route}\n@@markers\n")
    assert host.routes == []
    assert [process.pid for process in host.markers] == [31337]
    assert any("'routes' more than once" in problem for problem in host.parse_problems)
    assert any("'markers' more than once" in problem for problem in host.parse_problems)
    repeated: list[str] = []
    sections = state.parse_sections("@@a\n1\n@@b\n2\n@@a\n3\n4\n@@c\n5\n", repeated)
    assert sections == {"a": "1", "b": "2", "c": "5"} and repeated == ["a"]
    # The repeated header blocks the host instead of passing as a clean read.
    blocked = diff.compare_down(host, group="cycle:0-1-2-3", marker=MARKER, adopt=False)
    assert any("more than once" in blocker for blocker in blocked.blockers)
    assert parsed().parse_problems == []


# -- 2. the record does not depend on the topology file's path -------------------------------------------


def test_workstation_and_boot_unit_agree_on_the_record():
    topo, bound, _, states = healthy(RING4, "/srv/site/ring4.topology")
    assert not evaluate(topo, states, bound, "up").pending
    unit = topology.parse(RING4, path=boot.TOPOLOGY_INPUT)
    for mode in ("up", "boot"):
        outcome = evaluate(unit, states, facts_mod.bind(_document(), unit), mode)
        assert not outcome.pending and not outcome.blocked
    assert states[0].record["layout"] == topo.layout_name == unit.layout_name


def test_a_record_of_the_old_file_name_layout_converges_in_one_apply():
    topo, bound, _, states = healthy()
    for host in states.values():
        host.record = dict(host.record, layout="topology:ring4.topology")
    outcome = evaluate(topo, states, bound, "up")
    assert not outcome.blocked and outcome.host_changes == 0
    for host_diff in outcome.diffs:
        assert [(c.kind, c.action) for c in host_diff.pending] == [("record", "write")]
        line = next(line for line in host_diff.commands if "tee " + commands.RECORD_PATH in line)
        states[host_diff.position].record = json.loads(shlex.split(line)[2])
    assert not evaluate(topo, states, bound, "up").pending


# -- 3. explicit cabling ---------------------------------------------------------------------------------

PERMUTED = "hosts a b c d\ncabling explicit\ncable 0.port0 2.port1\ncable 2.port0 1.port1\ncable 1.port0 3.port1\n"
MIRRORED = "hosts a b c\ncabling explicit\ncable 0.port1 1.port0\ncable 1.port1 2.port0\n"
WRAPPED = "hosts a b c d\ncabling explicit\ncable 1.port0 2.port1\ncable 2.port0 3.port1\ncable 3.port0 0.port1\n"
EXPLICIT_RING = ("hosts a b c d\ncabling explicit\ncable 0.port0 2.port1\ncable 2.port0 1.port0\n"
                 "cable 1.port1 3.port1\ncable 3.port0 0.port1\n")


def cable_facts(topo: topology.Topology):
    """One /30 per cable and function class; functions without a cable get subnets of their own."""
    addresses = {}
    for index, cable in enumerate(topo.cables):
        for secondary in (False, True):
            subnet = 4 * (2 * index + int(secondary))
            addresses[(cable.a, model.role_at(cable.a_port, secondary).netdev)] = f"192.0.2.{subnet + 1}/30"
            addresses[(cable.b, model.role_at(cable.b_port, secondary).netdev)] = f"192.0.2.{subnet + 2}/30"
    hosts, spare = [], 0
    for position, name in enumerate(topo.hosts):
        functions = {}
        for role in model.ROLES:
            address = addresses.get((position, role.netdev))
            if address is None:
                address, spare = f"198.51.100.{4 * spare + 1}/30", spare + 1
            functions[role.device] = {"netdev": role.netdev, "address": address,
                                      "mac": f"02:00:{position:02x}:{role.port:02x}:{int(role.secondary):02x}:01"}
        hosts.append({"name": name, "functions": functions})
    return facts_mod.bind({"schema": facts_mod.FACTS_SCHEMA, "hosts": hosts}, topo)


@pytest.mark.parametrize("text,label", [(PERMUTED, "path:0-2-1-3"), (MIRRORED, "path:0-1-2"),
                                        (WRAPPED, "path:0-3-2-1"), (EXPLICIT_RING, "cycle:0-2-1-3")])
def test_explicit_cabling_is_planned_and_every_route_is_delivered(text, label):
    topo = topology.parse(text)
    assert topo.group.label == label
    bound = cable_facts(topo)
    assert ops.cable_problems(topo.group, bound) == []
    built = plan.build_plan(topo.group, bound)
    hosts = {host.position: host for host in built.hosts}
    fabric = topo.group.layout.fabric
    routes = [(host, route) for host in built.hosts for route in host.routes]
    assert routes and any(host.filters for host in built.hosts)
    for host, route in routes:
        result = plan.trace(hosts, bound, topo.group.ring_size, host.position, route.destination, fabric=fabric)
        assert result.outcome == "delivered", (host.name, route.destination, result)
        assert result.hops[-1] == (route.peer, route.peer_netdev)
        assert [position for position, _ in result.hops[:-1]] == list(route.relays)
        assert bound.port(route.peer, route.peer_netdev).address == route.destination
    # Every pair of hosts without a common cable is reachable on both function classes.
    for origin in range(topo.size):
        for target in range(topo.size):
            if origin != target and len(fabric.shortest_paths(origin, target)[0]) > 1:
                reached = {(r.peer, r.peer_netdev) for r in hosts[origin].routes if r.peer == target}
                assert {model.role_of_netdev(netdev).secondary for _, netdev in reached} == {False, True}


def test_trace_follows_the_cables_it_is_given():
    topo = topology.parse(PERMUTED)
    bound = cable_facts(topo)
    built = plan.build_plan(topo.group, bound)
    hosts = {host.position: host for host in built.hosts}
    route = built.host(0).routes[0]
    # Without the fabric the trace assumes ring order, which these cables do not have.
    assert plan.trace(hosts, bound, topo.group.ring_size, 0, route.destination).outcome == "misdelivered"
    # A relay filter that sends the frame out of a port without a cable is found by the trace.
    end = built.host(3)
    stray = plan.RelayFilter(model.role_at(1, False).netdev, 1, model.role_at(0, False).netdev,
                             "02:00:00:00:00:01", 0, model.role_at(1, False).netdev, "cw", "primary")
    lost = dict(hosts)
    lost[3] = dataclasses.replace(end, filters=(stray,))
    sender = dataclasses.replace(built.host(1), routes=(dataclasses.replace(
        built.host(1).routes[0], destination="192.0.2.250", netdev=model.role_at(0, False).netdev,
        next_mac=bound.port(3, model.role_at(1, False).netdev).mac, tag=plan.tag(1)),))
    lost[1] = sender
    result = plan.trace(lost, bound, topo.group.ring_size, 1, "192.0.2.250", fabric=topo.group.layout.fabric)
    assert result.outcome == "dropped" and "no cable" in result.detail


# -- 4. failed reads are not empty hosts -----------------------------------------------------------------


def test_read_script_reports_missing_tools_and_exit_statuses():
    lines = commands.read_script(MARKER).splitlines()
    assert ('for tool in ip tc pgrep sha256sum stat head; do command -v "$tool" >/dev/null 2>&1 || '
            'echo "$tool"; done') in lines
    for command, section in (("ip -j addr show", "links"), ("ip -j -d route show table main", "routes"),
                             ("ip -j neigh show nud permanent", "neighbours"),
                             (f"tc -j qdisc show dev {NETDEVS[0]} 2>/dev/null", f"qdisc:{NETDEVS[0]}"),
                             (f"tc -j filter show dev {NETDEVS[3]} ingress 2>/dev/null", f"filters:{NETDEVS[3]}"),
                             ("pgrep -a -x -U 0 -u 0 fabric-tool-mar", "markers")):
        at = lines.index(command)
        assert lines[at + 1:at + 6] == ["status=$?", "echo", f"echo @@exit:{section}", 'echo "$status"', "echo"]
    assert "sudo" not in "\n".join(lines)


@pytest.mark.parametrize("sections,expected", [
    ({"tools": "tc"}, "cannot run tc"),
    ({"tools": "pgrep\nsha256sum"}, "cannot run pgrep"),
    ({"exit_links": "1"}, "ip -j addr show failed with exit status 1"),
    ({"exit_routes": "2"}, "ip -j -d route show table main failed with exit status 2"),
    ({"exit_neighbours": "127"}, "ip -j neigh show nud permanent failed with exit status 127"),
    ({"exit_routes": ""}, "ip -j -d route show table main reported no exit status"),
    ({f"exit_qdisc_{NETDEVS[1]}": "127"}, f"tc -j qdisc show dev {NETDEVS[1]} could not be executed"),
    ({f"exit_filters_{NETDEVS[2]}": "126"}, f"tc -j filter show dev {NETDEVS[2]} ingress could not be executed"),
    ({"exit_markers": "2"}, "pgrep failed with exit status 2"),
    ({"exit_markers": "127"}, "pgrep failed with exit status 127"),
])
def test_a_failed_read_is_a_problem_that_blocks_the_host(sections, expected):
    host = parsed(**sections)
    assert any(expected in problem for problem in host.parse_problems), host.parse_problems
    removal = diff.compare_down(host, group="cycle:0-1-2-3", marker=MARKER, adopt=False)
    assert removal.blocked and any(expected in blocker for blocker in removal.blockers)
    assert any(expected in problem for problem in host.fact_problems(check_hostname=False))


@pytest.mark.parametrize("sections", [
    {},
    {"exit_markers": "0", "markers": f"4100 {MARKER.path} --device rocep1s0f0 --rule 192.0.2.10=0x88b5 --managed"},
    {f"exit_qdisc_{NETDEVS[0]}": "1", f"exit_filters_{NETDEVS[0]}": "2"},   # tc's own failures are not judged
    {f"exit_qdisc_{NETDEVS[0]}": "255"},
])
def test_reads_that_legitimately_find_nothing_are_not_problems(sections):
    assert parsed(**sections).parse_problems == []


@pytest.mark.parametrize("command", ["remove", "verify", "apply"])
def test_cli_refuses_hosts_whose_tc_cannot_run(tmp_path, capsys, command):
    path = tmp_path / "ring.topology"
    path.write_text(RING4)
    facts = tmp_path / "facts.json"
    facts.write_text(json.dumps(_document()))
    argv = [command, "--topology", str(path), "--facts", str(facts)]
    for position in range(4):
        argv += ["--ssh", f"node-{position}=node-{position}"]

    def executor(target, script, timeout):
        assert script.startswith("set -u\n"), "a host whose read failed was changed"
        if "@@hostname" not in script:
            return ops.Result(0, "@@end\n", "")
        return ops.Result(0, read_output(target, tools="tc", **{f"exit_qdisc_{n}": "127" for n in NETDEVS}), "")

    assert cli.main(argv + ([] if command == "verify" else ["--execute"]), executor=executor) == 3
    output = capsys.readouterr().out
    assert "the read account cannot run tc" in output
    assert "verified" not in output and "nothing to apply" not in output


def run_read_script(extra_stubs: str) -> state.HostState:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("no bash on this host")
    script = READ_STUBS + extra_stubs + commands.read_script(MARKER)
    result = subprocess.run([bash, "-s"], input=script.encode("utf-8"), capture_output=True, timeout=30)
    return state.parse(0, "node-0", "node-0", returncode=result.returncode, stdout=result.stdout.decode("utf-8"),
                       stderr="")


def test_the_read_script_under_bash_reports_a_clean_read_without_problems():
    host = run_read_script("")
    assert host.parse_problems == [] and host.reachable and host.hostname == "node-0"
    assert host.raw["exit:links"].strip() == "0" and host.raw["exit:markers"].strip() == "1"


@pytest.mark.parametrize("stub,expected", [
    ('command() { if [ "$1" = -v ] && [ "$2" = tc ]; then return 1; fi; builtin command "$@"; }\n'
     "tc() { return 127; }\n", ["the read account cannot run tc", "tc -j qdisc show dev enp1s0f0np0 could not"]),
    ('ip() { case "$*" in *route*) echo "RTNETLINK answers: failure" >&2; return 2;; esac; echo "[]"; }\n',
     ["ip -j -d route show table main failed with exit status 2"]),
    ('ip() { case "$*" in *neigh*) return 1;; esac; echo "[]"; }\n',
     ["ip -j neigh show nud permanent failed with exit status 1"]),
    ("pgrep() { return 3; }\n", ["pgrep failed with exit status 3"]),
])
def test_the_read_script_under_bash_reports_failed_reads(stub, expected):
    host = run_read_script(stub)
    for text in expected:
        assert any(text in problem for problem in host.parse_problems), host.parse_problems


def test_the_read_script_under_bash_does_not_judge_other_tc_failures():
    assert run_read_script("tc() { return 2; }\n").parse_problems == []


# -- 5. restore keeps marker processes the plan does not name ------------------------------------------


def stray_markers(states, position, unplanned_device):
    rule = frozenset({("192.0.2.200", 0x88b5)})
    states[position].markers += [
        state.MarkerProcess(4242, (MARKER.path, "--device", unplanned_device), unplanned_device, rule, True, None),
        state.MarkerProcess(4343, ("/tmp/fabric-tool-marker", "sleep"), None, frozenset(), False, None)]


def test_restore_retains_stray_marker_processes_and_apply_stops_them():
    topo, bound, built, states = healthy(PATH4, "/srv/site/path4.topology")
    unplanned = next(device for device in model.RDMA_DEVICES if built.host(0).marker(device) is None)
    stray_markers(states, 0, unplanned)
    restored = evaluate(topo, states, bound, "boot").diffs[0]
    assert not restored.pending and restored.commands == [] and not restored.blocked
    assert any("pid 4242" in note and "retained" in note for note in restored.notes)
    assert any("pid 4343" in note and "retained" in note for note in restored.notes)
    applied = evaluate(topo, states, bound, "up").diffs[0]
    assert sorted(c.key for c in applied.changes if c.kind == "marker" and c.action == "remove") == [
        "pid 4343", unplanned]
    assert commands.marker_stop(MARKER, 4242)[0] in applied.commands
    assert commands.marker_stop(MARKER, 4343)[0] in applied.commands
    removed = evaluate(topo, states, bound, "down").diffs[0]
    assert commands.marker_stop(MARKER, 4242)[0] in removed.commands


def test_restore_still_replaces_a_wrong_marker_of_a_planned_device():
    topo, bound, built, states = healthy(PATH4, "/srv/site/path4.topology")
    planned = built.host(0).markers[0]
    states[0].markers = [m if m.device != planned.device else dataclasses.replace(
        m, rules=frozenset({("192.0.2.200", 0x88b5)})) for m in states[0].markers]
    wrong = next(m for m in states[0].markers if m.device == planned.device)
    restored = evaluate(topo, states, bound, "boot").diffs[0]
    assert [(c.kind, c.action, c.key) for c in restored.pending] == [("marker", "replace", planned.device)]
    assert commands.marker_stop(MARKER, wrong.pid)[0] in restored.commands
    assert any(line.startswith("sudo -n setsid -f ") and f"--device {planned.device} " in line
               for line in restored.commands)


# -- 6. observed objects the command builders refuse ---------------------------------------------------


def test_permanent_ipv6_neighbours_are_not_relay_neighbours():
    text = json.dumps([
        {"dst": "fe80::1", "dev": NETDEVS[0], "lladdr": "02:aa:bb:cc:dd:ee", "state": ["PERMANENT"]},
        {"dst": "2001:db8::7", "dev": NETDEVS[1], "lladdr": "02:aa:bb:cc:dd:ef", "state": ["PERMANENT"],
         "protocol": "82"},
        {"dst": "198.51.100.77", "dev": NETDEVS[0], "lladdr": "02:aa:bb:cc:dd:f0", "state": ["PERMANENT"]}])
    problems: list[str] = []
    assert [n.destination for n in state.parse_neighbours(text, problems)] == ["198.51.100.77"] and not problems
    topo, _, _, _ = healthy()
    host = state.parse(0, "node-0", "node-0", returncode=0, stderr="", stdout=read_output(neighbours=json.dumps(
        [{"dst": "fe80::1", "dev": NETDEVS[0], "lladdr": "02:aa:bb:cc:dd:ee", "state": ["PERMANENT"]}])))
    assert host.parse_problems == [] and host.relay_neighbours() == []
    removed = diff.compare_down(host, group=topo.group.label, marker=MARKER, adopt=False)
    assert not removed.blocked and not removed.pending


@pytest.mark.parametrize("mode", ["up", "down", "boot"])
def test_marked_objects_on_other_devices_are_noted_and_left(mode):
    topo, bound, _, states = healthy()
    states[0].routes.append(state.RouteEntry("203.0.113.9", 32, "eth0", None, "link", 82, "82", 0, None, "unicast"))
    states[0].routes.append(state.RouteEntry("203.0.113.128", 16, "", None, "global", 82, "82", 0, None, "blackhole"))
    states[0].neighbours.append(state.NeighbourEntry("203.0.113.9", "eth0", "02:aa:bb:cc:dd:ee", ("PERMANENT",),
                                                     82, "82"))
    result = evaluate(topo, states, bound, mode).diffs[0]
    assert not result.blocked
    assert not any("eth0" in line or "203.0.113.128" in line for line in result.commands)
    assert not any(c.key.startswith(("203.0.113.9", "203.0.113.128")) for c in result.changes)
    noted = [note for note in result.notes if "not a fabric network device" in note]
    assert len(noted) == (0 if mode == "boot" else 3)


def test_an_owned_route_is_deleted_with_its_own_prefix_length():
    topo, bound, _, states = healthy()
    states[0].routes.append(state.RouteEntry("203.0.113.0", 24, NETDEVS[0], None, "link", 82, "82", 0, None, "unicast"))
    states[0].routes.append(state.RouteEntry("0.0.0.0", 0, NETDEVS[1], None, "link", 82, "82", 50, None, "unicast"))
    result = evaluate(topo, states, bound, "up").diffs[0]
    assert f"sudo -n ip route del 203.0.113.0/24 dev {NETDEVS[0]} proto 82" in result.commands
    assert f"sudo -n ip route del 0.0.0.0/0 dev {NETDEVS[1]} proto 82 metric 50" in result.commands
    assert sorted(c.key for c in result.changes if c.kind == "route" and c.action == "remove") == [
        "0.0.0.0/0", "203.0.113.0/24"]
    assert commands.route_del("192.0.2.17", NETDEVS[0], protocol=82) == (
        f"sudo -n ip route del 192.0.2.17/32 dev {NETDEVS[0]} proto 82")
    with pytest.raises(FabricError, match="prefix length"):
        commands.route_del("192.0.2.17", NETDEVS[0], protocol=82, prefixlen=33)
    problems: list[str] = []
    routes = state.parse_routes(json.dumps([{"dst": "203.0.113.16/24", "dev": "x"}, {"dst": "203.0.113.32/99", "dev": "x"},
                                            {"dst": "203.0.113.48/x", "dev": "x"}]), problems)
    assert [(r.destination, r.prefixlen) for r in routes] == [("203.0.113.16", 24)]


# -- 7. install-boot starts the timer whatever the immediate apply returns --------------------------------


@pytest.fixture
def boot_install(environment, tmp_path, monkeypatch):  # noqa: F811
    _, _, _, arguments = environment
    facts = tmp_path / "facts.json"
    facts.write_text(json.dumps(_document()))
    calls = []
    monkeypatch.setattr(cli.subprocess, "run", lambda argv, **kwargs: calls.append((argv, kwargs)))
    monkeypatch.setattr(cli.shutil, "copytree", lambda *args, **kwargs: None)
    monkeypatch.setattr(boot, "install", lambda spec, root, execute: [])
    monkeypatch.setattr(boot, "stop_reconciliation", lambda: None)
    monkeypatch.setattr(boot, "LIB_DIR", str(Path(cli.__file__).resolve().parents[1]))
    monkeypatch.setattr(boot, "BIN_PATH", str(tmp_path / "fabric-tool"))
    argv = ["install-boot", "--topology", arguments[arguments.index("--topology") + 1], "--facts", str(facts),
            "--host", "node-0", "--execute"]

    def forbid(target, script, timeout):
        raise AssertionError("the apply flow is replaced in this test")

    return calls, lambda: cli.main(argv, executor=forbid)


@pytest.mark.parametrize("code", [0, 1, 3])
def test_install_boot_starts_the_timer_whatever_the_apply_returns(boot_install, monkeypatch, capsys, code):
    calls, install = boot_install
    monkeypatch.setattr(cli, "_apply_flow", lambda *args, **kwargs: code)
    assert install() == code
    assert [argv for argv, _ in calls] == [["systemctl", "start", boot.RECOVERY_TIMER]]
    assert calls[0][1] == {"check": True}
    output = capsys.readouterr().out
    assert f"started {boot.RECOVERY_TIMER}" in output
    assert ("did not succeed" in output and f"(exit {code})" in output) == (code != 0)


def test_install_boot_starts_the_timer_when_the_apply_raises(boot_install, monkeypatch, capsys):
    calls, install = boot_install

    def fail(*args, **kwargs):
        raise FabricError("synthetic failure")

    monkeypatch.setattr(cli, "_apply_flow", fail)
    assert install() == 2
    assert [argv for argv, _ in calls] == [["systemctl", "start", boot.RECOVERY_TIMER]]
    assert "did not succeed" in capsys.readouterr().out


# -- 8. remove-boot is idempotent ------------------------------------------------------------------------


def test_remove_boot_twice_does_not_disable_units_that_are_gone(tmp_path, monkeypatch):
    calls = []

    def systemctl(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "disable" and not (tmp_path / "etc/systemd/system" / argv[2]).exists():
            raise subprocess.CalledProcessError(1, argv)
        return subprocess.CompletedProcess(argv, 0, stdout="ActiveState=inactive\nJob=\n")

    spec = boot.plan("topology", "facts", topology_text="hosts a b\n", facts_text="{}")
    boot.install(spec, root=str(tmp_path), execute=False)
    monkeypatch.setattr(boot, "Path", lambda value: tmp_path / str(value).lstrip("/")
                        if str(value).startswith("/") else Path(value))
    monkeypatch.setattr(boot.subprocess, "run", systemctl)
    assert len(boot.remove(execute=True)) == 5
    assert [argv for argv in calls if argv[1] == "disable"] == [
        ["systemctl", "disable", boot.RECOVERY_TIMER], ["systemctl", "disable", boot.UNIT_NAME]]
    calls.clear()
    assert boot.remove(execute=True) == []
    assert not [argv for argv in calls if argv[1] == "disable"]
    assert ["systemctl", "daemon-reload"] in calls


# -- 9. qdisc ownership survives a failed apply ------------------------------------------------------------


def record_writes(lines):
    return [(index, json.loads(shlex.split(line)[2])) for index, line in enumerate(lines)
            if "tee " + commands.RECORD_PATH in line]


def test_a_failed_apply_keeps_the_ownership_of_the_qdiscs_it_added():
    topo, _, built, _ = healthy()
    host = built.host(1)
    arguments = dict(layout_name=topo.group.name, marker=MARKER, adopt=False)
    fresh = state.HostState(1, host.name, "", True, hostname=host.name)
    first = diff.compare_up(fresh, host, built, **arguments)
    writes = record_writes(first.commands)
    adds = [index for index, line in enumerate(first.commands) if " tc qdisc add " in line]
    assert len(adds) == 4 and len(writes) == 1
    assert writes[0][0] < min(adds), "the record must be written before the first qdisc is added"
    assert writes[0][1]["qdiscs"] == sorted(host.filter_netdevs)
    assert first.commands[writes[0][0] - 1].startswith("sudo -n mkdir -p ")
    assert first.commands[1] == commands.guard(host.name)
    # The script stopped after the qdiscs: the rerun still owns them, and so does a removal.
    stopped = state.HostState(1, host.name, "", True, hostname=host.name, record=writes[0][1])
    for netdev in host.filter_netdevs:
        stopped.ingress_qdisc[netdev] = "ingress"
    rerun = diff.compare_up(stopped, host, built, **arguments)
    assert not any(c.kind in ("qdisc", "record") for c in rerun.pending)
    assert not record_writes(rerun.commands)
    removed = diff.compare_down(stopped, group=topo.group.label, marker=MARKER, adopt=False)
    assert sorted(c.key for c in removed.changes if c.kind == "qdisc" and c.action == "remove") == sorted(
        host.filter_netdevs)
    for boot_first in (diff.compare_boot(fresh, host, built, **arguments),):
        assert record_writes(boot_first.commands)[0][0] < min(
            index for index, line in enumerate(boot_first.commands) if " tc qdisc add " in line)


def test_write_ahead_record_keeps_a_qdisc_until_it_is_deleted():
    topo, _, built, _ = healthy()
    full = built.host(1)
    needed, stale = full.filter_netdevs[0], full.filter_netdevs[1]
    host = dataclasses.replace(full, filters=tuple(f for f in full.filters if f.in_netdev == needed))
    observed = state.HostState(1, host.name, "", True, hostname=host.name)
    observed.ingress_qdisc[stale] = "ingress"
    observed.record = diff._record(host, built, topo.group.name, MARKER, [stale])
    result = diff.compare_up(observed, host, built, layout_name=topo.group.name, marker=MARKER, adopt=False)
    (first_at, first), (last_at, last) = record_writes(result.commands)
    assert first["qdiscs"] == sorted([needed, stale]) and last["qdiscs"] == [needed]
    delete_at = result.commands.index(commands.qdisc_del(stale))
    assert first_at < result.commands.index(commands.qdisc_add(needed)) < delete_at < last_at
    # Without a qdisc to add, the record stays the script's last step.
    present = dataclasses.replace(observed, ingress_qdisc={needed: "ingress", stale: "ingress"})
    later = diff.compare_up(present, host, built, layout_name=topo.group.name, marker=MARKER, adopt=False)
    ((only_at, only),) = record_writes(later.commands)
    assert only["qdiscs"] == [] and only_at > later.commands.index(commands.qdisc_del(stale))


# -- 11. hosts whose interfaces are not the bound ones ----------------------------------------------------


def test_a_recorded_qdisc_outside_the_bindings_is_noted_not_a_crash():
    topo, bound, _, states = healthy()
    states[0].record = dict(states[0].record, qdiscs=["fabric0", 7])
    for mode in ("up", "down"):
        result = evaluate(topo, states, bound, mode).diffs[0]
        assert any("ingress qdisc on fabric0" in note for note in result.notes)
        assert not any("fabric0" in line and "qdisc" in line and "tee" not in line for line in result.commands)


def test_remove_without_facts_refuses_hosts_with_other_interface_names(tmp_path, monkeypatch, capsys):
    topo, _, _, states = healthy()
    for host in states.values():
        host.rdma = {device: f"fabric{index}" for index, device in enumerate(model.RDMA_DEVICES)}
        host.record = dict(host.record, qdiscs=["fabric0"])
        host.routes = [dataclasses.replace(route, dev="fabric0") for route in host.routes]
    monkeypatch.setattr(ops, "gather", lambda *args, **kwargs: states)
    path = tmp_path / "ring.topology"
    path.write_text(RING4)
    argv = ["remove", "--topology", str(path), "--execute"]
    for name in topo.hosts:
        argv += ["--ssh", f"{name}={name}"]

    def forbid(target, script, timeout):
        raise AssertionError("a refused removal ran a script")

    assert cli.main(argv, executor=forbid) == 3
    output = capsys.readouterr().out
    assert "RDMA device rocep1s0f0 is network device fabric0 on this host, not enp1s0f0np0" in output
    assert "--facts" in output
    # An RDMA device the host does not report (driver gone) does not stop a removal.
    topo, bound, _, states = healthy()
    states[0].rdma = {}
    assert not evaluate(topo, states, bound, "down").blocked


# -- 12. facts without prefix lengths ----------------------------------------------------------------------


def test_a_skipped_cabling_check_is_reported():
    topo, bound, _, states = healthy()
    assert evaluate(topo, states, bound, "up").notes == []
    document = _document()
    for host in document["hosts"]:
        for function in host["functions"].values():
            function["address"] = function["address"].split("/")[0]
    bare = facts_mod.bind(document, topo)
    outcome = evaluate(topo, states, bare, "up")
    assert not outcome.blocked and outcome.plan is not None
    assert len(outcome.notes) == 1 and "the cabling check was skipped for 8 link(s)" in outcome.notes[0]
    assert "NOTE: the cabling check was skipped" in report.render_outcome(outcome)
    assert ops.unchecked_cables(topo.group, bound) == []


# -- 13. remove and the marker the record names -----------------------------------------------------------


def test_remove_refuses_a_host_whose_record_names_another_marker():
    topo, bound, _, states = healthy()
    states[0].record = dict(states[0].record, marker="/opt/relay/bin/tagger")
    removed = evaluate(topo, states, bound, "down").diffs[0]
    assert any("--marker /opt/relay/bin/tagger" in blocker for blocker in removed.blockers)
    named = diff.compare_down(states[0], group=topo.group.label, marker=commands.MarkerConfig("/opt/relay/bin/tagger"),
                              adopt=False)
    assert not named.blockers
    # The same process name from another directory is found by pgrep and needs no refusal.
    states[0].record = dict(states[0].record, marker="/opt/other/fabric-tool-marker")
    assert not evaluate(topo, states, bound, "down").diffs[0].blockers
    # A recorded path that --marker could not name is reported without blocking the removal.
    states[0].record = dict(states[0].record, marker="/tmp/tagger")
    unnamed = evaluate(topo, states, bound, "down").diffs[0]
    assert not unnamed.blockers and any("/tmp/tagger" in note for note in unnamed.notes)
    # Apply and restore are not affected.
    states[0].record = dict(states[0].record, marker="/opt/relay/bin/tagger")
    assert not evaluate(topo, states, bound, "boot").blocked


# -- nits -------------------------------------------------------------------------------------------------


def test_validators_reject_a_trailing_newline():
    with pytest.raises(FabricError, match="cannot be checked safely"):
        commands.guard("node-0\n")
    with pytest.raises(FabricError, match="marker path"):
        commands.MarkerConfig("/opt/marker\n")
    with pytest.raises(FabricError, match="marker log directory"):
        commands.MarkerConfig(log_dir="/var/lib/logs\n")
    with pytest.raises(FabricError, match="not a MAC address"):
        commands.neigh_replace("192.0.2.10", "02:00:00:00:00:03\n", NETDEVS[0])
    assert commands.guard("node-0").startswith('test "$(hostname)" = node-0 ')


def test_ssh_target_cannot_be_read_as_an_option(monkeypatch):
    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(ops.subprocess, "run", run)
    assert ops.ssh_executor("-oProxyCommand=id", "true\n", 5).ok
    assert seen[0][-3:] == ["--", "-oProxyCommand=id", "bash -s"]
    assert seen[0][0] == "ssh" and all(word != "-oProxyCommand=id" for word in seen[0][:-3])


def test_verify_rejects_adopt(capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.main(["verify", "--adopt"])
    assert stopped.value.code == 2 and "--adopt" in capsys.readouterr().err
    for command in ("apply", "remove"):
        assert cli._parser().parse_args([command, "--adopt"]).adopt
