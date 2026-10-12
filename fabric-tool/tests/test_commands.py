"""The shell commands: forms, the guard, refusals of placeholders and of non-fabric devices."""

from __future__ import annotations

import pytest

from fabric_tool import commands, model, plan as plan_mod
from fabric_tool.model import FabricError

MARKER = commands.MarkerConfig()


def _filter(k: int = 1) -> plan_mod.RelayFilter:
    return plan_mod.RelayFilter("enp1s0f1np1", k, "enp1s0f0np0", "02:00:00:00:00:03", 2, "enp1s0f0np1",
                                "cw", "primary")


def test_command_forms():
    relay_filter = _filter()
    assert commands.filter_add(relay_filter) == (
        "sudo -n tc filter add dev enp1s0f1np1 ingress protocol 0x88b5 pref 11 handle 1 flower skip_sw "
        "action pedit ex munge eth dst set 02:00:00:00:00:03 pipe "
        "action pedit ex munge eth type set 0x0800 pipe "
        "action mirred egress redirect dev enp1s0f0np0")
    assert commands.route_replace("192.0.2.17", "enp1s0f1np1", "192.0.2.26") == (
        "sudo -n ip route replace 192.0.2.17/32 dev enp1s0f1np1 src 192.0.2.26 scope link proto 82")
    assert commands.neigh_replace("192.0.2.17", "02:00:00:00:00:03", "enp1s0f1np1") == (
        "sudo -n ip neigh replace 192.0.2.17 lladdr 02:00:00:00:00:03 dev enp1s0f1np1 nud permanent proto 82")
    assert commands.qdisc_add("enp1s0f0np0") == "sudo -n tc qdisc add dev enp1s0f0np0 ingress"
    assert commands.qdisc_del("enp1s0f0np0") == "sudo -n tc qdisc del dev enp1s0f0np0 ingress"
    assert commands.filter_del("enp1s0f0np0", 11) == "sudo -n tc filter del dev enp1s0f0np0 ingress pref 11"
    assert commands.filter_del("enp1s0f0np0", 11, protocol=0x88b5, handle=1) == (
        "sudo -n tc filter del dev enp1s0f0np0 ingress protocol 0x88b5 pref 11 handle 1 flower")
    assert commands.marker_start(MARKER, "rocep1s0f0", [("192.0.2.10", 0x88b5)]) == (
        "(sudo -n setsid /var/tmp/ring8-mesh-marker --device rocep1s0f0 --rule 192.0.2.10=0x88b5 --managed "
        "< /dev/null > /tmp/ring8-mesh-marker-rocep1s0f0.log 2>&1 &)")
    stop, wait = commands.marker_stop(MARKER, 4100)
    assert stop == '[ "$(cat /proc/4100/comm 2>/dev/null)" != ring8-mesh-mark ] || sudo -n kill 4100'
    assert wait.startswith("for i in $(seq 1 50)")
    assert commands.record_remove() == "sudo -n rm -f /run/fabric-table/relay-state.json"


def test_guard_and_script_header():
    script = commands.read_script(MARKER)
    assert script.startswith("set -u\n")
    assert "@@hostname" in script and "@@links" in script and "@@record" in script
    assert "@@sparkring-record" in script
    for netdev in model.FABRIC_NETDEVS:
        assert f"@@qdisc:{netdev}" in script and f"@@filters:{netdev}" in script
    lines = [commands.HEADER, commands.guard("node-0"), "sudo -n true"]
    assert lines[1] == ('test "$(hostname)" = node-0 || '
                        '{ echo \'refusing: this host is not node-0\' >&2; exit 3; }')
    with pytest.raises(FabricError):
        commands.guard("bad host; rm -rf /")


def test_refusals():
    with pytest.raises(FabricError, match="not a fabric network device"):
        commands.qdisc_add("eth0")
    with pytest.raises(FabricError, match="not a fabric RDMA device"):
        commands.marker_start(MARKER, "mlx5_99", [("192.0.2.10", 0x88b5)])
    with pytest.raises(FabricError, match="not an IPv4 address"):
        commands.route_replace("<h0:enp1s0f0np0>", "enp1s0f0np0", "192.0.2.1")
    with pytest.raises(FabricError, match="not a MAC address"):
        commands.neigh_replace("192.0.2.10", "placeholder", "enp1s0f0np0")
    with pytest.raises(FabricError, match="needs at least one rule"):
        commands.marker_start(MARKER, "rocep1s0f0", [])
    with pytest.raises(FabricError, match="absolute path"):
        commands.MarkerConfig("relative/marker")


def test_marker_build_script():
    source = "int main(void) { return 0; }\n"
    script = commands.marker_build_script("node-0", MARKER, source)
    assert script.startswith("set -euo pipefail")
    assert "cc -O2 -Wall -Wextra" in script and "install -m 0755" in script
    with pytest.raises(FabricError, match="here-document"):
        commands.marker_build_script("node-0", MARKER, source + commands.MARKER_SOURCE_END + "\n")
