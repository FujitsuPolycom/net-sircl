"""Apply, verify, remove and boot restore against simulated hosts.

The tool's CLI runs against SparkRing's host-command simulator
(``sparkring_sircl.testing.relay_hosts``), which accepts exactly the command
forms the absorbed generator produces and refuses anything else. The
simulator's frame walk (:meth:`SimRing.deliver`) is independent of the plan
module, so a delivered frame proves what was installed. Skips outside the
repository.
"""

from __future__ import annotations

import json

import pytest

from fabric_tool import commands, facts as facts_mod, model

try:
    from sparkring_sircl import routes as sircl_routes
    from sparkring_sircl.testing.relay_hosts import SimRing
    SIRCL = True
except ImportError:                      # the standalone archive carries the tool alone
    SIRCL = False

pytestmark = pytest.mark.skipif(not SIRCL, reason="SparkRing's SIRCL package is not beside the tool")

from fabric_tool import cli  # noqa: E402

RING = SimRing(4) if SIRCL else None
TOPOLOGY_TEXT = ("hosts " + " ".join(spark.name for spark in RING.sparks) + "\ncabling ring\n") if SIRCL else ""
SSH = ([f"{spark.name}=op@{spark.lan_address}" for spark in RING.sparks]) if SIRCL else []


@pytest.fixture(autouse=True)
def fresh_ring():
    """Every test starts from a fresh simulated ring (the tests share this module's RING)."""
    global RING, TOPOLOGY_TEXT, SSH
    RING = SimRing(4)
    TOPOLOGY_TEXT = "hosts " + " ".join(spark.name for spark in RING.sparks) + "\ncabling ring\n"
    SSH = [f"{spark.name}=op@{spark.lan_address}" for spark in RING.sparks]
    yield


def _facts_document() -> dict:
    hosts = []
    for spark in RING.sparks:
        functions = {}
        for role in model.ROLES:
            netdev = role.netdev
            entry = spark.netdevs[netdev]
            functions[role.device] = {"netdev": netdev, "mac": entry.mac,
                                      "address": f"{entry.address}/{entry.prefixlen}"}
        hosts.append({"name": spark.name, "functions": functions})
    return {"schema": facts_mod.FACTS_SCHEMA, "hosts": hosts}


@pytest.fixture()
def env(tmp_path):
    topology_path = tmp_path / "ring4.topology"
    topology_path.write_text(TOPOLOGY_TEXT, encoding="utf-8")
    facts_path = tmp_path / "facts.json"
    facts_path.write_text(json.dumps(_facts_document()), encoding="utf-8")
    return topology_path, facts_path


def _run(argv, topology_path, facts_path=None):
    extra = ["--facts", str(facts_path)] if facts_path else []
    return cli.main([argv[0], "--topology", str(topology_path), *extra,
                     *sum((["--ssh", pair] for pair in SSH), []), *argv[1:]], executor=RING.executor)


def _lane_destinations():
    group = RING.site()
    layout = sircl_routes.Layout(sircl_routes.Fabric.ring(4), tuple(range(4)))
    derived = sircl_routes.derive_routes(layout, 2)
    found = []
    for rank, peers in enumerate(derived.ranks):
        for peer, lanes in peers.items():
            for lane in lanes:
                remote_position = layout.positions[peer]
                netdev = lane.remote.netdev
                found.append((layout.positions[rank], RING.sparks[remote_position].netdevs[netdev].address,
                              lane.hops))
    del group
    return found


def test_plan_and_facts_offline_and_over_ssh(env, capsys):
    topology_path, facts_path = env
    assert cli.main(["plan", "--topology", str(topology_path)], executor=RING.executor) == 0
    assert "cycle:0-1-2-3" in capsys.readouterr().out
    assert _run(["facts", "--output", str(facts_path.with_name("read.json"))], topology_path) == 0
    read = json.loads(facts_path.with_name("read.json").read_text())
    assert read["schema"] == facts_mod.FACTS_SCHEMA
    assert {host["name"] for host in read["hosts"]} == {spark.name for spark in RING.sparks}
    assert facts_mod.bind(read, _load(topology_path)) is not None


def _load(path):
    from fabric_tool import topology as topology_mod
    return topology_mod.load(path)


def test_apply_dry_run_changes_nothing_and_prints_scripts(env, capsys):
    topology_path, facts_path = env
    before = [RING.snapshot(i) for i in range(4)]
    assert _run(["apply"], topology_path, facts_path) == 0
    out = capsys.readouterr().out
    assert "dry run" in out and "sudo -n tc filter add" in out
    assert [RING.snapshot(i) for i in range(4)] == before


def test_apply_installs_verifies_delivers_and_converges(env, capsys):
    topology_path, facts_path = env
    assert _run(["apply", "--execute"], topology_path, facts_path) == 0
    out = capsys.readouterr().out
    assert "verified: 4 host(s)" in out
    assert _run(["verify"], topology_path, facts_path) == 0
    capsys.readouterr()
    for origin, destination, hops in _lane_destinations():
        outcome, path = RING.deliver(origin, destination)
        assert outcome == "delivered", (origin, destination, outcome, path)
        assert len(path) == hops
    assert _run(["apply", "--execute"], topology_path, facts_path) == 0
    assert "nothing to apply" in capsys.readouterr().out


def test_boot_restore_reinstalls_after_a_reboot(env, capsys):
    topology_path, facts_path = env
    assert _run(["apply", "--execute"], topology_path, facts_path) == 0
    capsys.readouterr()
    # A reboot: /run is cleared, the markers are gone, and the table's routes, neighbours and filters go
    # with the interfaces; the addresses come back with their connected routes and the default route.
    for spark in RING.sparks:
        kept = [route for route in spark.routes if route["plen"] != 32]
        spark.routes.clear()
        spark.routes.extend(kept)
        spark.neighbours.clear()
        for netdev in list(spark.filters):
            spark.filters[netdev].clear()
        spark.processes.clear()
        spark.files.pop(commands.RECORD_PATH, None)
    assert _run(["apply", "--boot", "--execute"], topology_path, facts_path) == 0
    capsys.readouterr()
    assert _run(["verify"], topology_path, facts_path) == 0
    capsys.readouterr()
    for origin, destination, _ in _lane_destinations():
        outcome, _path = RING.deliver(origin, destination)
        assert outcome == "delivered", (origin, destination, outcome)


def test_remove_deletes_exactly_the_owned_objects(env, capsys):
    topology_path, facts_path = env
    assert _run(["apply", "--execute"], topology_path, facts_path) == 0
    capsys.readouterr()
    installed = [RING.snapshot(i) for i in range(4)]
    assert _run(["remove", "--execute"], topology_path, facts_path) == 0
    capsys.readouterr()
    for position in range(4):
        spark = RING.sparks[position]
        assert not [route for route in spark.routes if route["plen"] == 32]      # the table's routes
        assert not spark.neighbours                                             # and its permanent neighbours
        assert all(not entries for entries in spark.filters.values())
        assert not spark.processes
        assert spark.qdiscs == {} or all(kind != "ingress" for kind in spark.qdiscs.values())
    assert _run(["verify"], topology_path, facts_path) == 1  # the plan is absent again
    capsys.readouterr()
    del installed


def test_sparkring_record_blocks_apply_and_remove(env, capsys):
    topology_path, facts_path = env
    RING.sparks[0].files["/run/sparkring-fabric/relay-state.json"] = json.dumps(
        {"schema": "sparkring-fabric-relay-record/v1", "group": "cycle:0-1-2-3-4-5-6-7"})
    assert _run(["apply", "--execute"], topology_path, facts_path) == 3
    out = capsys.readouterr().out
    assert "SparkRing's relay table is installed" in out
    assert _run(["remove", "--execute"], topology_path, facts_path) == 3
    capsys.readouterr()


def test_refuses_a_host_the_topology_does_not_list(env):
    topology_path, facts_path = env
    assert cli.main(["apply", "--topology", str(topology_path), "--facts", str(facts_path),
                     "--host", "node-unknown"], executor=RING.executor) == 2


def test_status_labels_ownership(env, capsys):
    topology_path, facts_path = env
    assert _run(["status"], topology_path) == 0
    assert "no group record" in capsys.readouterr().out
    assert _run(["apply", "--execute"], topology_path, facts_path) == 0
    capsys.readouterr()
    assert _run(["status"], topology_path) == 0
    out = capsys.readouterr().out
    assert f"group {RING.sparks[0].name}" in out or "group cycle:0-1-2-3" in out
    assert "[unowned]" not in out


def test_boot_cli_persists_merged_facts_and_applies_locally(env, tmp_path, monkeypatch):
    from fabric_tool import boot

    topology_path, facts_path = env
    document = json.loads(facts_path.read_text())
    parts = []
    for index, rows in enumerate((document["hosts"][:2], document["hosts"][2:])):
        path = tmp_path / f"facts-{index}.json"
        path.write_text(json.dumps({"schema": facts_mod.FACTS_SCHEMA, "hosts": rows}))
        parts.extend(["--facts", str(path)])
    monkeypatch.setattr(cli.socket, "gethostname", lambda: RING.sparks[0].name)

    def local(script_target, script, timeout):
        assert script_target == ""
        return RING.executor(SSH[0].split("=", 1)[1], script, timeout)

    root = tmp_path / "root"
    argv = ["install-boot", "--topology", str(topology_path), *parts, "--root", str(root)]
    assert cli.main(argv, executor=local) == 0
    assert not root.exists()
    assert cli.main([*argv, "--execute"], executor=local) == 0
    saved = root / boot.FACTS_INPUT.lstrip("/")
    assert len(json.loads(saved.read_text())["hosts"]) == 4
    assert cli.main(["verify", "--topology", str(topology_path), "--facts", str(saved)], executor=local) == 0
    assert cli.main(["remove-boot", "--root", str(root), "--execute"], executor=local) == 0
    assert not saved.exists()


def test_boot_cli_refuses_foreign_owner_before_writing(env, tmp_path, monkeypatch):
    topology_path, facts_path = env
    RING.sparks[0].files["/run/sparkring-fabric/relay-state.json"] = json.dumps(
        {"schema": "sparkring-fabric-relay-record/v1", "group": "cycle:0-1-2-3"})
    monkeypatch.setattr(cli.socket, "gethostname", lambda: RING.sparks[0].name)
    root = tmp_path / "blocked-root"

    def local(target, script, timeout):
        return RING.executor(SSH[0].split("=", 1)[1], script, timeout)

    assert cli.main(["install-boot", "--topology", str(topology_path), "--facts", str(facts_path),
                     "--root", str(root), "--execute"], executor=local) == 3
    assert not root.exists()
