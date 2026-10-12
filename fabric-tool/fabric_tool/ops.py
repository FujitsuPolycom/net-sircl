"""Operations on the hosts: read, plan, compare, apply and verify.

Absorbed from ``sparkring_sircl.fabric.ops`` (SparkRing
``spark_transport/sircl/sparkring_sircl/fabric/ops.py``); see NOTICE. Every
host is reached through an *executor*: a function that runs one ``bash -s``
script and returns its exit code and output — over SSH
(:func:`ssh_executor`, batch mode) or on this machine (:func:`local_executor`,
the per-host run). The hosts of an operation are contacted in parallel.

``evaluate`` reads nothing itself: it takes the states :func:`gather` read,
derives the facts of the topology's hosts (addresses and MACs as the hosts
report them, or from ``--facts`` files when only a subset is read), checks
the cabling of each owned cable (both ends of each link on one subnet), builds
the plan and compares every host with it. ``apply`` runs the scripts of a
comparison; callers read the hosts again and evaluate once more to verify.
"""

from __future__ import annotations

import concurrent.futures
import dataclasses
import subprocess
from collections.abc import Callable, Iterable, Sequence
from typing import TypeVar

from . import commands, diff as diff_mod, facts as facts_mod, model, state as state_mod
from .model import FabricError
from .plan import Facts, GroupPlan, HostFacts, build_plan
from .state import HostState
from .topology import GroupSpec, Topology

READ_TIMEOUT = 60.0
APPLY_TIMEOUT = 180.0
SSH_OPTIONS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=10",
               "-o", "ServerAliveCountMax=3")


@dataclasses.dataclass(frozen=True)
class Result:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclasses.dataclass(frozen=True)
class HostTarget:
    """One host of an operation: its topology name and how to reach it (None: this machine)."""

    name: str
    ssh: str | None


Executor = Callable[[str, str, float], Result]


def ssh_executor(target: str, script: str, timeout: float) -> Result:
    """Run ``script`` with ``bash -s`` on ``target`` (batch-mode SSH, deadline ``timeout``)."""
    argv = ["ssh", *SSH_OPTIONS, target, "bash -s"]
    try:
        process = subprocess.run(argv, input=script.encode(), capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return Result(124, "", f"ssh {target} timed out after {timeout:g} s")
    except FileNotFoundError:
        return Result(127, "", "ssh is not installed on this machine")
    return Result(process.returncode, process.stdout.decode(errors="replace"),
                  process.stderr.decode(errors="replace"))


def local_executor(target: str, script: str, timeout: float) -> Result:
    """Run ``script`` with ``bash -s`` on this machine (the per-host run)."""
    try:
        process = subprocess.run(["bash", "-s"], input=script.encode(), capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return Result(124, "", f"the local run timed out after {timeout:g} s")
    return Result(process.returncode, process.stdout.decode(errors="replace"),
                  process.stderr.decode(errors="replace"))


def executor_for(target: HostTarget) -> Executor:
    """The executor of a target: local when it names this machine, SSH otherwise."""
    if target.ssh is None:
        return local_executor
    remote = target.ssh

    def run(_target: str, script: str, timeout: float) -> Result:
        return ssh_executor(remote, script, timeout)

    return run


T = TypeVar("T")


def _parallel(jobs: Sequence[int], work: Callable[[int], T]) -> dict[int, T]:
    if not jobs:
        return {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(jobs))) as pool:
        return dict(zip(jobs, pool.map(work, jobs)))


def gather(hosts: dict[int, HostTarget], positions: Iterable[int], executor: Executor,
           marker: commands.MarkerConfig, *, timeout: float = READ_TIMEOUT) -> dict[int, HostState]:
    """Read every listed host (READ-ONLY REMOTE)."""
    script = commands.read_script(marker)

    def read(position: int) -> HostState:
        target = hosts[position]
        result = executor(target.ssh or "", script, timeout)
        return state_mod.parse(position, target.name, target.ssh or "", returncode=result.returncode,
                               stdout=result.stdout, stderr=result.stderr)

    return {position: state for position, state in _parallel(sorted(set(positions)), read).items()}


def facts_from(states: dict[int, HostState]) -> Facts:
    """The addresses and MACs the hosts report (only usable ports are included)."""
    return Facts(HostFacts(position, state.name, state.ports())
                 for position, state in sorted(states.items()) if state.reachable)


def cable_problems(group: GroupSpec, facts: Facts) -> list[str]:
    """Each owned cable's two links must join one subnet: otherwise the topology's host order is not
    the cabling."""
    problems = []
    for cable in group.layout.fabric.cables:
        for secondary in (False, True):
            here = facts.port(cable.a, model.role_at(cable.a_port, secondary).netdev)
            there = facts.port(cable.b, model.role_at(cable.b_port, secondary).netdev)
            networks = (here.network(), there.network())
            if None in networks:
                continue
            if networks[0] != networks[1] or here.address == there.address:
                problems.append(f"cable {cable.text} ({model.class_name(secondary)} link): {facts.name(cable.a)} "
                                f"{here.netdev} {here.address}/{here.prefixlen} and {facts.name(cable.b)} "
                                f"{there.netdev} {there.address}/{there.prefixlen} are not one subnet; the "
                                "topology's host order does not match the cabling")
    return problems


@dataclasses.dataclass
class Outcome:
    """A comparison of the topology's hosts with the plan (``up``/``boot``) or their removal (``down``)."""

    topology: Topology
    group: GroupSpec
    mode: str
    plan: GroupPlan | None
    diffs: list[diff_mod.HostDiff] = dataclasses.field(default_factory=list)
    problems: list[str] = dataclasses.field(default_factory=list)
    facts: Facts | None = None

    @property
    def blocked(self) -> bool:
        return bool(self.problems) or any(d.blocked for d in self.diffs)

    @property
    def pending(self) -> bool:
        return any(d.pending for d in self.diffs)

    @property
    def host_changes(self) -> int:
        return sum(len(d.host_changes) for d in self.diffs)


def evaluate(topology: Topology, states: dict[int, HostState], file_facts: Facts | None, *, mode: str,
             marker: commands.MarkerConfig, adopt: bool, check_hostname: bool = True,
             max_relays: int = model.DEFAULT_MAX_RELAYS, positions: Sequence[int] | None = None) -> Outcome:
    """Compare the read states with the topology's plan (or its removal).

    ``positions`` restricts the comparison to the hosts read (a per-host run);
    the plan itself is always built for the whole group, which needs the facts
    of every host (from the reads, or from ``--facts``).
    """
    if mode not in ("up", "down", "boot"):
        raise FabricError(f"mode must be up, boot or down, got {mode!r}")
    group = topology.group
    compared = sorted(set(positions if positions is not None else group.order))
    outcome = Outcome(topology, group, mode, None)
    problems = model.isolation_problems([model.derive_routes(group.layout, 2)])
    if problems:
        raise FabricError("isolation: " + "; ".join(problems))
    read_facts = facts_from(states)
    if len(read_facts.positions) == len(group.members) and file_facts is None:
        facts = read_facts
    elif file_facts is not None:
        facts = file_facts
        problems += facts_mod.problems_against_reads(facts, states, topology)
    else:
        facts = read_facts
        missing = [topology.hosts[p] for p in range(len(group.members)) if not read_facts.has(p)]
        problems.append("no facts for " + ", ".join(missing) + "; read every host (`facts`) or pass --facts")
    outcome.facts = facts
    if mode == "down":
        for position in compared:
            outcome.diffs.append(diff_mod.compare_down(states[position], group=group.label, marker=marker,
                                                       adopt=adopt, check_hostname=check_hostname))
        return outcome
    problems += [problem for position in compared
                 for problem in states[position].fact_problems(check_hostname=False)]
    member_facts = Facts([read_facts.host(p) for p in compared if read_facts.has(p)])
    problems += member_facts.problems()
    plan = None
    if not problems:
        try:
            problems += cable_problems(group, facts)
            if not problems:
                plan = build_plan(group, facts, max_relays=max_relays)
        except FabricError as error:
            problems.append(str(error))
    if plan is None:
        outcome.problems.extend(problems)
        for position in compared:
            blocked = diff_mod.HostDiff(position, states[position].name, group.label, mode)
            blocked.blockers.append(f"group {group.label} cannot be planned (see the problems above)")
            outcome.diffs.append(blocked)
        return outcome
    outcome.plan = plan
    for position in compared:
        compare = diff_mod.compare_boot if mode == "boot" else diff_mod.compare_up
        outcome.diffs.append(compare(states[position], plan.host(position), plan,
                                     layout_name=group.name, marker=marker, adopt=adopt,
                                     check_hostname=check_hostname))
    return outcome


def lane_route_checks(outcome: Outcome, *, installed_only: bool) -> dict[int, dict[str, str]]:
    """Destinations to resolve with ``ip route get`` per host, and the network device each must use.

    Every SIRCL lane originating at a compared host (direct lanes use the connected
    route of their cable) and every other carried path. With
    ``installed_only``, relayed destinations whose origin route the
    comparison still has to add or replace are left out.
    """
    checks: dict[int, dict[str, str]] = {}
    plan = outcome.plan
    if plan is None:
        return checks
    layout = plan.layout
    compared = {d.position for d in outcome.diffs}
    pending = {(d.position, change.key.split("/")[0]) for d in outcome.diffs
               for change in d.changes if change.kind == "route" and change.action in ("add", "replace")}
    for host in plan.hosts:
        if host.position not in compared:
            continue
        for route in host.routes:
            if not (installed_only and (host.position, route.destination) in pending):
                checks.setdefault(host.position, {})[route.destination] = route.netdev
    facts = outcome.facts
    for rank, peers in enumerate(plan.sircl.ranks):
        origin = layout.positions[rank]
        if origin not in compared:
            continue
        for peer, lanes in peers.items():
            for lane in lanes:
                if lane.hops == 1 and facts is not None and facts.has(layout.positions[peer]):
                    destination = facts.port(layout.positions[peer], lane.remote.netdev).address
                    checks.setdefault(origin, {})[destination] = lane.local.netdev
    return checks


def check_lane_routes(hosts: dict[int, HostTarget], outcome: Outcome, executor: Executor, *,
                      installed_only: bool, timeout: float = READ_TIMEOUT) -> dict[int, list[str]]:
    """Resolve every lane destination on its origin host (READ-ONLY REMOTE); problems per host."""
    checks = lane_route_checks(outcome, installed_only=installed_only)

    def resolve(position: int) -> list[str]:
        wanted = checks[position]
        result = executor(hosts[position].ssh or "", commands.route_get_script(wanted), timeout)
        answers = state_mod.parse_sections(result.stdout)
        problems = []
        for destination, netdev in sorted(wanted.items()):
            answer = answers.get(f"route:{destination}", "").strip()
            words = answer.split()
            device = words[words.index("dev") + 1] if "dev" in words[:-1] else None
            if device != netdev:
                problems.append(f"ip route get {destination} uses {device or 'no device'}, the lane needs {netdev} "
                                f"({answer.splitlines()[0] if answer else 'no answer'})")
        return problems

    return {position: problems for position, problems in _parallel(sorted(checks), resolve).items() if problems}


def apply(outcome: Outcome, hosts: dict[int, HostTarget], executor: Executor, *, timeout: float = APPLY_TIMEOUT
          ) -> dict[int, Result]:
    """Run every host's script of a comparison without blockers (MUTATES HOST)."""
    if outcome.blocked:
        raise FabricError("refusing to apply a comparison with blockers")
    scripts = {d.position: d.script for d in outcome.diffs if d.script}

    def run(position: int) -> Result:
        return executor(hosts[position].ssh or "", scripts[position], timeout)

    return {position: result for position, result in _parallel(sorted(scripts), run).items()}
