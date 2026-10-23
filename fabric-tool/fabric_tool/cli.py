"""``python3 -m fabric_tool <command>``: the standalone relay-table installer.

Commands and their safety classes:

- ``plan`` (OFFLINE, the default): a topology file's relay plan; with a facts
  file it prints the exact install commands per host;
- ``facts`` (READ-ONLY REMOTE): read hosts and print the facts document;
- ``status`` (READ-ONLY REMOTE): the relay objects every host holds;
- ``apply`` (READ-ONLY REMOTE; MUTATES HOST with ``--execute``): compare each
  host with its part of the plan and reconcile it; ``--boot`` installs missing
  parts only (boot restore, never removes);
- ``verify`` (READ-ONLY REMOTE): compare every host with the plan and resolve
  every lane destination with ``ip route get``;
- ``remove`` (READ-ONLY REMOTE; MUTATES HOST with ``--execute``): delete
  exactly the owned objects;
- ``install-marker`` (READ-ONLY REMOTE; MUTATES HOST with ``--execute``):
  compile and install the marker executable;
- ``install-boot`` / ``remove-boot`` (MUTATES HOST with ``--execute``): the
  systemd unit that restores the table at boot, and its inputs.

Every command that changes a host is a dry run unless ``--execute`` is given,
and a dry run prints the exact scripts it would run. A host is reached
locally (the default; the tool runs on the Spark) or over SSH
(``--ssh <name>=<target>``, repeatable; all hosts of the topology are then
driven from one machine). Exit codes: 0 done (``verify``: every host holds
its part); 1 a read failed or changes or failed checks remain; 2 invalid
input; 3 blockers stopped the run or ``--execute``.
"""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from . import boot, commands, diff as diff_mod, facts as facts_mod, model, ops, report, topology as topology_mod
from .model import FabricError
from .plan import Facts, build_plan, render_text

DEFAULT_TOPOLOGY = "/etc/net-sircl.topology"
COMMANDS = ("plan", "facts", "apply", "verify", "remove", "status", "install-marker", "install-boot",
            "remove-boot")


def _print_outcome(outcome: ops.Outcome, args: argparse.Namespace, *, scripts: bool) -> None:
    print(report.render_outcome(outcome, verbose=args.verbose, scripts=scripts))
    print(report.summary_line(outcome))


def _route_check(outcome: ops.Outcome, hosts: dict[int, ops.HostTarget], executor: ops.Executor, *,
                 installed_only: bool) -> int:
    """``ip route get`` of every lane destination on its origin host, as the plugin's users check it."""
    if outcome.mode == "down" or outcome.plan is None:
        return 0
    problems = ops.check_lane_routes(hosts, outcome, executor, installed_only=installed_only)
    checked = sum(len(found) for found in ops.lane_route_checks(outcome, installed_only=installed_only).values())
    for position, found in sorted(problems.items()):
        for problem in found:
            print(f"ROUTE CHECK {hosts[position].name}: {problem}")
    count = sum(len(found) for found in problems.values())
    print(f"route check: {checked - count} of {checked} lane destination(s) resolve over their lane's device"
          + (" (destinations whose origin route is still to be added are not checked)" if installed_only else ""))
    return count


def _install_scripts(plan, marker: commands.MarkerConfig) -> dict[int, str]:
    """The from-nothing install script of every host (what ``apply`` converges to on an empty host)."""
    scripts = {}
    for host in plan.hosts:
        observed = ops.state_mod.HostState(host.position, host.name, "", True,
                                           hostname=host.name, bindings=host.bindings)
        result = diff_mod.compare_up(observed, host, plan, layout_name=plan.group.name,
                                     marker=marker, adopt=False)
        if result.script:
            scripts[host.position] = result.script
    return scripts


def _resolve_hosts(topology: topology_mod.Topology, args: argparse.Namespace) -> dict[int, ops.HostTarget]:
    """The hosts an operation acts on, identified by host name.

    With ``--ssh name=target`` entries, every host of the topology is driven
    from this machine. Without them, the tool runs on one host: the topology
    entry that matches this machine's host name (``--host`` names the entry
    when the machine's host name differs, which ``--skip-hostname-check``
    must then allow).
    """
    targets = _parse_ssh(args.ssh)
    if targets:
        unknown = sorted(set(targets) - set(topology.hosts))
        if unknown:
            raise FabricError(f"--ssh names {', '.join(unknown)}, which the topology does not list")
        missing = [name for name in topology.hosts if name not in targets]
        if missing:
            raise FabricError(f"--ssh misses {', '.join(missing)}; driving hosts from one machine needs a "
                              "target for every host of the topology")
        return {position: ops.HostTarget(name, targets[name]) for position, name in enumerate(topology.hosts)}
    name = args.host or socket.gethostname().split(".")[0]
    position = topology.position_of(name)
    if position < 0:
        raise FabricError(f"this host ({socket.gethostname()}) matches no entry of the topology "
                          f"({', '.join(topology.hosts)}); a host the tool cannot identify is refused "
                          "(--host names the entry to act as)")
    return {position: ops.HostTarget(name, None)}


def _parse_ssh(pairs: list[str] | None) -> dict[str, str]:
    """The ``--ssh name=target`` pairs as {topology host name: SSH target}, refusing duplicates."""
    targets: dict[str, str] = {}
    for pair in pairs or []:
        name, _, target = pair.partition("=")
        if not name.strip() or not target.strip():
            raise FabricError(f"--ssh {pair!r} must be <name>=<target>")
        name, target = name.strip(), target.strip()
        if name in targets:
            raise FabricError(f"--ssh names {name} twice")
        if target in targets.values():
            raise FabricError(f"--ssh gives {target} for two hosts; one machine cannot be two hosts")
        targets[name] = target
    return targets


def _file_facts(args: argparse.Namespace, topology: topology_mod.Topology) -> Facts | None:
    if not args.facts:
        return None
    return facts_mod.bind(facts_mod.load(args.facts), topology)


def _command_plan(args: argparse.Namespace) -> int:
    topology = topology_mod.load(args.topology)
    if args.facts:
        facts = facts_mod.bind(facts_mod.load(args.facts), topology)
    else:
        facts = Facts.symbolic_for(topology.hosts, bindings=topology.bindings)
    plan = build_plan(topology.group, facts, max_relays=args.max_relays)
    if args.json:
        print(json.dumps({"schema": "fabric-tool-plan/v2", "topology": args.topology,
                          "hosts": list(topology.hosts), "cabling": topology.cabling,
                          "relay_egress": topology.group.relay_egress, "plan": plan.to_json()}, indent=1))
        return 0
    print(render_text(plan, facts, details=not args.brief))
    if args.commands:
        if facts.symbolic:
            print("plan: --commands needs a facts file (placeholders are not installable)", file=sys.stderr)
            return 2
        for position, script in sorted(_install_scripts(plan, commands.MarkerConfig(args.marker)).items()):
            print(f"--- {topology.hosts[position]} install script ---")
            print(script, end="")
    return 0


def _command_facts(args: argparse.Namespace, topology: topology_mod.Topology,
                   executor: ops.Executor) -> int:
    hosts = _resolve_hosts(topology, args)
    marker = commands.MarkerConfig(args.marker)
    states = ops.gather(hosts, sorted(hosts), executor, marker, bindings=topology.bindings)
    document = facts_mod.from_states(states, topology)
    text = json.dumps(document, indent=1)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
        print(f"wrote {args.output}")
    else:
        print(text)
    problems = [problem for state in states.values() for problem in state.fact_problems(check_hostname=False)]
    for problem in problems:
        print(f"problem: {problem}", file=sys.stderr)
    return 3 if problems or not all(state.reachable for state in states.values()) else 0


def _apply_flow(args: argparse.Namespace, topology: topology_mod.Topology, mode: str,
                executor: ops.Executor) -> int:
    hosts = _resolve_hosts(topology, args)
    marker = commands.MarkerConfig(args.marker)
    positions = sorted(hosts)
    file_facts = _file_facts(args, topology)
    bindings = file_facts.bindings if file_facts else topology.bindings
    states = ops.gather(hosts, positions, executor, marker, bindings=bindings)
    outcome = ops.evaluate(topology, states, file_facts, mode=mode, marker=marker, adopt=args.adopt,
                           check_hostname=not args.skip_hostname_check, max_relays=args.max_relays,
                           positions=positions)
    _print_outcome(outcome, args, scripts=not args.no_scripts)
    if mode != "down" and outcome.plan is not None:
        _route_check(outcome, hosts, executor, installed_only=True)
    if not args.execute:
        print("dry run: nothing was changed; --execute runs the scripts above"
              + (" (blockers must be resolved first)" if outcome.blocked else ""))
        return 3 if outcome.blocked else 0
    if outcome.blocked:
        print("apply refused: resolve the blockers above (nothing was changed)")
        return 3
    if not outcome.pending:
        print("nothing to apply: the hosts already hold the plan")
        return 0
    results = ops.apply(outcome, hosts, executor)
    failed = False
    for position, result in sorted(results.items()):
        text = (result.stdout + result.stderr).strip()
        print(f"{hosts[position].name}: exit {result.returncode}" + (f": {text[-1500:]}" if text else ""))
        failed |= not result.ok
    states = ops.gather(hosts, positions, executor, marker, bindings=bindings)
    verify = ops.evaluate(topology, states, file_facts, mode=mode, marker=marker, adopt=args.adopt,
                          check_hostname=not args.skip_hostname_check, max_relays=args.max_relays,
                          positions=positions)
    for diff in verify.diffs:
        for change in diff.pending:
            print(f"after apply, {diff.name} still differs: {change.text()}")
        for blocker in diff.blockers:
            print(f"after apply, {diff.name}: {blocker}")
    route_problems = _route_check(verify, hosts, executor, installed_only=False)
    if failed or verify.pending or verify.blocked or route_problems:
        print("verification failed: run `verify` (or `status`) and inspect the hosts named above")
        return 1
    print(f"verified: {len(verify.diffs)} host(s) hold the {mode} state of group {topology.group.label}")
    return 0


def _command_verify(args: argparse.Namespace, topology: topology_mod.Topology,
                    executor: ops.Executor) -> int:
    hosts = _resolve_hosts(topology, args)
    marker = commands.MarkerConfig(args.marker)
    file_facts = _file_facts(args, topology)
    states = ops.gather(hosts, sorted(hosts), executor, marker,
                        bindings=file_facts.bindings if file_facts else topology.bindings)
    outcome = ops.evaluate(topology, states, file_facts, mode="up", marker=marker,
                           adopt=False, check_hostname=not args.skip_hostname_check,
                           max_relays=args.max_relays, positions=sorted(hosts))
    _print_outcome(outcome, args, scripts=args.scripts)
    if outcome.blocked or not all(state.reachable for state in states.values()):
        return 3
    if _route_check(outcome, hosts, executor, installed_only=True):
        return 3
    return 1 if outcome.host_changes else 0


def _command_status(args: argparse.Namespace, topology: topology_mod.Topology,
                    executor: ops.Executor) -> int:
    hosts = _resolve_hosts(topology, args)
    marker = commands.MarkerConfig(args.marker)
    states = ops.gather(hosts, sorted(hosts), executor, marker, bindings=topology.bindings)
    print(report.render_show(states, marker))
    return 0 if all(state.reachable for state in states.values()) else 1


def _command_marker(args: argparse.Namespace, topology: topology_mod.Topology,
                    executor: ops.Executor) -> int:
    hosts = _resolve_hosts(topology, args)
    marker = commands.MarkerConfig(args.marker)
    states = ops.gather(hosts, sorted(hosts), executor, marker, bindings=topology.bindings)
    print(_marker_status(states, marker))
    # A missing executable, or one without the digest its install recorded, is (re)built.
    targets = [p for p in sorted(hosts) if states[p].reachable
               and (args.rebuild or states[p].marker_binary is None
                    or states[p].marker_recorded != states[p].marker_binary)]
    if not targets:
        print("every reachable host has the marker executable with the digest its install recorded")
        return 0 if all(states[p].reachable for p in hosts) else 1
    source = _marker_source()
    scripts = {p: commands.marker_build_script(hosts[p].name, marker, source) for p in targets}
    names = ", ".join(hosts[p].name for p in targets)
    if not args.execute:
        print(f"dry run: --execute compiles mesh_marker.c and installs {marker.path} on {names}")
        if args.scripts:
            print(scripts[targets[0]])
        return 0
    failed = False
    for position in targets:
        result = executor(hosts[position].ssh or "", scripts[position], ops.APPLY_TIMEOUT)
        text = (result.stdout + result.stderr).strip()
        print(f"{hosts[position].name}: exit {result.returncode}: {text[-800:]}")
        failed |= not result.ok
    return 1 if failed else 0


def _marker_status(states, marker: commands.MarkerConfig) -> str:
    lines = []
    for _position, state in sorted(states.items()):
        if not state.reachable:
            lines.append(f"{state.name}: unreachable: {state.error}")
            continue
        status = f"sha256 {state.marker_binary}" if state.marker_binary else "missing"
        if state.marker_binary and state.marker_recorded is None:
            status += f", no digest record {marker.record}"
        elif state.marker_binary and state.marker_recorded != state.marker_binary:
            status += f", differs from the sha256 {state.marker_recorded} recorded at install"
        running = ", ".join(f"{p.device} pid {p.pid}" for p in state.markers) or "no marker running"
        lines.append(f"{state.name}: {marker.path} {status}; {running}")
    return "\n".join(lines)


def _marker_source() -> str:
    path = Path(__file__).resolve().parents[1] / "mesh_marker.c"
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise FabricError(f"cannot read the marker source {path}: {error}") from None


def _command_install_boot(args: argparse.Namespace, topology: topology_mod.Topology,
                          executor: ops.Executor) -> int:
    if args.ssh:
        raise FabricError("install-boot runs on the host itself (the unit and its inputs are per-host); "
                          "run it on each Spark, e.g. `ssh <target> python3 -m fabric_tool install-boot ...`")
    file_facts = _file_facts(args, topology)
    if file_facts is None or file_facts.symbolic:
        raise FabricError("install-boot needs a complete facts file (--facts); placeholders are not installable")
    build_plan(topology.group, file_facts, max_relays=args.max_relays)  # refuses a plan that cannot be built
    restore = boot.RestoreInvocation(tool=args.tool, marker=args.marker, max_relays=args.max_relays,
                                     host=args.host, skip_hostname_check=args.skip_hostname_check)
    spec = boot.plan(args.topology, args.facts[0], invocation=restore,
                     topology_text=Path(args.topology).read_text(encoding="utf-8"),
                     facts_text=json.dumps(facts_mod.load(args.facts), indent=1) + "\n")
    print(f"unit {boot.UNIT_NAME}:")
    print(spec.files[2][1], end="")
    for destination, content in spec.files[3:]:
        print(f"unit {destination}:")
        print(content, end="")
    for destination, _ in spec.files[:2]:
        print(f"input: {destination}")
    if not args.execute:
        print("dry run: --execute copies this tool to /usr/local/lib, writes the files, enables the unit "
              "and installs the table now")
        return 0
    # Reject identity/ownership/facts blockers before persisting a boot service.
    hosts = _resolve_hosts(topology, args)
    marker = commands.MarkerConfig(args.marker)
    states = ops.gather(hosts, sorted(hosts), executor, marker, bindings=file_facts.bindings)
    outcome = ops.evaluate(topology, states, file_facts, mode="boot", marker=marker,
                           adopt=False, check_hostname=not args.skip_hostname_check,
                           max_relays=args.max_relays, positions=sorted(hosts))
    if outcome.blocked:
        _print_outcome(outcome, args, scripts=False)
        return 3
    if args.root == "/":
        source = Path(__file__).resolve().parents[1]
        target = Path(boot.LIB_DIR)
        if target.resolve() != source.resolve():
            shutil.copytree(source, target, dirs_exist_ok=True)
            print(f"copied {source} to {target}")
        wrapper = Path(boot.BIN_PATH)
        wrapper.write_text(boot.wrapper_text(), encoding="utf-8")
        wrapper.chmod(0o755)
        print(f"wrote {wrapper}")
    boot.install(spec, root=args.root, execute=True)
    print(f"enabled {boot.UNIT_NAME} and {boot.RECOVERY_TIMER}")
    code = _apply_flow(args, topology, "boot", executor)
    if code == 0 and args.root == "/":
        subprocess.run(["systemctl", "start", boot.RECOVERY_TIMER], check=True)
    return code


def _command_remove_boot(args: argparse.Namespace) -> int:
    removed = [f"/etc/systemd/system/{name}" for name in
               (boot.UNIT_NAME, boot.RECOVERY_SERVICE, boot.RECOVERY_TIMER)]
    removed += [boot.TOPOLOGY_INPUT, boot.FACTS_INPUT]
    print("remove-boot deletes: " + ", ".join(removed) + "; the relay table itself stays until `remove`")
    if not args.execute:
        print("dry run: --execute removes them")
        return 0
    for path in boot.remove(root=args.root, execute=True):
        print(f"removed {path}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fabric-tool", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands_parser = parser.add_subparsers(dest="command", required=True)

    def common(sub: argparse.ArgumentParser, *, topology: bool = True) -> None:
        if topology:
            sub.add_argument("--topology", default=DEFAULT_TOPOLOGY,
                             help=f"topology file of net-sircl (default {DEFAULT_TOPOLOGY})")
        sub.add_argument("--marker", default=commands.DEFAULT_MARKER,
                         help=f"marker executable on the hosts (default {commands.DEFAULT_MARKER})")
        sub.add_argument("--ssh", action="append", metavar="NAME=TARGET",
                         help="drive host NAME over SSH at TARGET (repeatable; all topology hosts)")
        sub.add_argument("--host", help="the topology entry this machine acts as (per-host run)")
        sub.add_argument("--skip-hostname-check", action="store_true",
                         help="accept a host whose name differs from its topology entry")

    def plan_facts(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--facts", action="append",
                         help="facts file (fabric-tool-facts/v1); repeatable, merged")

    def relay_options(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--max-relays", type=int, default=model.DEFAULT_MAX_RELAYS,
                         help=f"relay limit per lane (default {model.DEFAULT_MAX_RELAYS}, not hardware-qualified; "
                              "higher values are research-only)")

    plan_parser = commands_parser.add_parser("plan", help="print a topology's relay plan (OFFLINE, default)")
    common(plan_parser)
    plan_facts(plan_parser)
    relay_options(plan_parser)
    plan_parser.add_argument("--json", action="store_true")
    plan_parser.add_argument("--brief", action="store_true", help="the plan without per-object detail")
    plan_parser.add_argument("--commands", action="store_true",
                             help="also print every host's from-nothing install script (needs --facts)")

    facts_parser = commands_parser.add_parser("facts", help="read hosts and print the facts document")
    common(facts_parser)
    facts_parser.add_argument("--output", help="write the facts document here instead of printing it")

    for name, text in (("apply", "install the plan: dry run unless --execute (MUTATES HOST)"),
                       ("verify", "compare the hosts with the plan and resolve every lane (READ-ONLY)"),
                       ("remove", "remove the owned objects: dry run unless --execute (MUTATES HOST)"),
                       ("status", "show every host's relay objects (READ-ONLY REMOTE)"),
                       ("install-marker", "build the marker executable where it is missing (MUTATES HOST with "
                                          "--execute)")):
        sub = commands_parser.add_parser(name, help=text)
        common(sub)
        if name in ("apply", "verify", "remove"):
            plan_facts(sub)
            relay_options(sub)
            sub.add_argument("--adopt", action="store_true",
                             help="take over relay routes and neighbours that lack the ownership mark")
            sub.add_argument("--verbose", action="store_true", help="also list unchanged objects")
        if name == "apply":
            sub.add_argument("--boot", action="store_true",
                             help="restore semantics: install missing parts, never remove (the boot unit's mode)")
            sub.add_argument("--execute", action="store_true", help="change the hosts (MUTATES HOST)")
            sub.add_argument("--no-scripts", action="store_true", help="omit the per-host scripts")
        elif name == "verify":
            sub.add_argument("--scripts", action="store_true", help="print the scripts apply would run")
        elif name == "remove":
            sub.add_argument("--execute", action="store_true", help="change the hosts (MUTATES HOST)")
            sub.add_argument("--no-scripts", action="store_true", help="omit the per-host scripts")
        elif name == "install-marker":
            sub.add_argument("--execute", action="store_true", help="compile and install it (MUTATES HOST)")
            sub.add_argument("--rebuild", action="store_true", help="rebuild where it exists too")
            sub.add_argument("--scripts", action="store_true", help="print the build script")

    install_boot = commands_parser.add_parser("install-boot",
                                              help="install the boot unit and its inputs (MUTATES HOST with "
                                                   "--execute)")
    common(install_boot)
    plan_facts(install_boot)
    relay_options(install_boot)
    install_boot.add_argument("--execute", action="store_true", help="write the files and enable the unit")
    install_boot.add_argument("--root", default="/", help="install under this directory instead of / (tests)")
    install_boot.set_defaults(adopt=False, verbose=False, no_scripts=False)
    install_boot.add_argument("--tool", default=boot.DEFAULT_TOOL,
                              help=f"the ExecStart command of the unit (default {boot.DEFAULT_TOOL!r})")

    remove_boot = commands_parser.add_parser("remove-boot", help="remove the boot unit and its inputs "
                                               "(MUTATES HOST with --execute)")
    remove_boot.add_argument("--execute", action="store_true", help="remove them")
    remove_boot.add_argument("--root", default="/", help="remove under this directory instead of / (tests)")
    return parser


def main(argv: list[str] | None = None, *, executor: ops.Executor | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if not args_list or args_list[0] not in COMMANDS:
        args_list = ["plan", *args_list]
    args = _parser().parse_args(args_list)
    # Without --ssh the tool runs on this host (local scripts); with it, over SSH per target.
    run = executor or (ops.ssh_executor if getattr(args, "ssh", None) else ops.local_executor)
    try:
        if args.command == "plan":
            return _command_plan(args)
        if args.command == "remove-boot":
            return _command_remove_boot(args)
        topology = topology_mod.load(args.topology)
        if args.command == "facts":
            return _command_facts(args, topology, run)
        if args.command == "apply":
            return _apply_flow(args, topology, "boot" if args.boot else "up", run)
        if args.command == "verify":
            return _command_verify(args, topology, run)
        if args.command == "remove":
            return _apply_flow(args, topology, "down", run)
        if args.command == "status":
            return _command_status(args, topology, run)
        if args.command == "install-marker":
            return _command_marker(args, topology, run)
        if args.command == "install-boot":
            return _command_install_boot(args, topology, run)
    except FabricError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
