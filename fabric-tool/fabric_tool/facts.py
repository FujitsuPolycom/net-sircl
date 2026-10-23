"""The facts document: addresses and MACs of the fabric functions, per host.

Schema ``fabric-tool-facts/v1``:

.. code-block:: json

    {"schema": "fabric-tool-facts/v1", "hosts": [
      {"name": "node-0", "position": 0, "functions": {
        "rocep1s0f0": {"netdev": "enp1s0f0np0", "mac": "0c:42:a1:00:00:01",
                       "address": "192.0.2.1/30"}, ...}}]}

``position`` is optional and cross-checked against the topology file; the
function names are the topology file's (its defaults or its ``port0``/
``port1`` lines). ``fabric-tool facts`` produces the document from host reads
(:func:`from_states`); ``--facts`` files are merged (:func:`load`) and
validated against the topology (:func:`bind`, which returns immutable device
bindings with the host facts).
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from . import model
from .model import FabricError
from .plan import Facts, HostFacts, Port, port_from_text
from .state import HostState
from .topology import Topology

FACTS_SCHEMA = "fabric-tool-facts/v1"


@dataclasses.dataclass(frozen=True)
class FunctionFacts:
    """One function's binding on one host: network device, MAC and IPv4 address."""

    netdev: str
    mac: str
    address: str
    prefixlen: int | None


def empty() -> dict[str, object]:
    return {"schema": FACTS_SCHEMA, "hosts": []}


def from_states(states: dict[int, HostState], topology: Topology) -> dict[str, object]:
    """The facts document of what the reads found (only reachable hosts enter it)."""
    hosts = []
    for position, state in sorted(states.items()):
        if not state.reachable or topology.position_of(state.name) < 0 and position != state.position:
            continue
        functions: dict[str, dict[str, object]] = {}
        for device in state.bindings.devices:
            netdev = state.rdma.get(device, "")
            link = state.links.get(netdev)
            if link is None or len(link.addresses) != 1 or not link.mac:
                continue
            address, prefixlen = link.addresses[0]
            functions[device] = {"netdev": netdev, "mac": link.mac, "address": f"{address}/{prefixlen}"}
        hosts.append({"name": state.name, "position": position, "functions": functions})
    return {"schema": FACTS_SCHEMA, "hosts": hosts}


def load(paths: list[str | Path]) -> dict[str, object]:
    """Merge facts documents; later files fill hosts earlier ones lack and must not contradict them."""
    merged = empty()
    seen: dict[str, dict] = {}
    for path in paths:
        try:
            document = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise FabricError(f"cannot read the facts file {path}: {error}") from None
        if not isinstance(document, dict) or document.get("schema") != FACTS_SCHEMA:
            raise FabricError(f"facts file {path} must have schema {FACTS_SCHEMA}")
        entries = document.get("hosts")
        if not isinstance(entries, list):
            raise FabricError(f"facts file {path} lists no hosts")
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) \
                    or not isinstance(entry.get("functions"), dict):
                raise FabricError(f"facts file {path} has a host entry without a name or functions")
            name = entry["name"]
            functions = _functions_of(entry["functions"], f"facts file {path}, host {name}")
            if name in seen:
                if seen[name] != functions:
                    raise FabricError(f"facts files disagree about host {name}")
                continue
            seen[name] = functions
            # Keep the wire schema on output so another load/bind preserves prefixes.
            row = {"name": name, "functions": {
                device: {"netdev": bound["netdev"], "mac": bound["mac"],
                         "address": (f"{bound['address']}/{bound['prefixlen']}"
                                     if bound["prefixlen"] is not None else bound["address"])}
                for device, bound in functions.items()}}
            if entry.get("position") is not None:
                row["position"] = entry["position"]
            merged["hosts"].append(row)  # type: ignore[union-attr]
    return merged


def _functions_of(functions: dict, where: str) -> dict[str, dict[str, object]]:
    bound: dict[str, dict[str, object]] = {}
    for device, values in functions.items():
        if not isinstance(values, dict) or not isinstance(values.get("netdev"), str) \
                or not isinstance(values.get("mac"), str) or not isinstance(values.get("address"), str):
            raise FabricError(f"{where}: function {device} needs a netdev, a MAC and an address")
        port = port_from_text(str(values["netdev"]), str(values["mac"]), str(values["address"]), owner=where)
        bound[str(device)] = {"netdev": port.netdev, "mac": port.mac,
                              "address": port.address, "prefixlen": port.prefixlen}
    return bound


def bind(document: dict[str, object], topology: Topology) -> Facts:
    """Validate a facts document against the topology and return the plan's facts.

    Every topology function must be bound on every host; a host must be a
    topology entry; an optional position must match; the same function must
    name the same network device on every host (SIRCL's rule for a fabric);
    the returned facts retain those device bindings.
    """
    rows = document.get("hosts")
    if not isinstance(rows, list) or not rows:
        raise FabricError("the facts document lists no hosts")
    by_name: dict[str, tuple[int | None, dict[str, dict[str, object]]]] = {}
    for entry in rows:
        name = str(entry["name"])
        position = entry.get("position")
        if position is not None and (not isinstance(position, int) or topology.position_of(name) != position):
            raise FabricError(f"facts name {name} at position {position}, the topology lists it at "
                              f"{topology.position_of(name)}")
        if topology.position_of(name) < 0:
            raise FabricError(f"facts name host {name!r}, which the topology does not list")
        by_name[name] = (position if isinstance(position, int) else None,
                         _functions_of(entry["functions"], f"facts, host {name}"))
    missing = [name for name in topology.hosts if name not in by_name]
    if missing:
        raise FabricError(f"facts are missing {', '.join(missing)}; read every host of the topology "
                          "(`facts`) or complete the facts files")
    for name, (_, functions) in by_name.items():
        missing_functions = set(topology.functions) - functions.keys()
        if missing_functions:
            raise FabricError(f"facts for {name} lack functions {sorted(missing_functions)}")
    netdevs: dict[str, str] = {}
    for device in topology.functions:
        named = {str(by_name[name][1][device]["netdev"]) for name in topology.hosts}
        if len(named) != 1:
            raise FabricError(f"the hosts name function {device} by different network devices "
                              f"{sorted(named)}; every host must name it alike")
        netdevs[device] = named.pop()
    roles = tuple(model.Role(("cw_primary", "cw_secondary", "ccw_primary", "ccw_secondary")[index],
                             device, netdevs[device], index // 2, bool(index % 2))
                  for index, device in enumerate(topology.functions))
    bindings = model.FabricBindings(roles)
    hosts = []
    for position, name in enumerate(topology.hosts):
        functions = by_name[name][1]
        ports = []
        for device in topology.functions:
            bound = functions[device]
            ports.append(Port(str(bound["netdev"]), str(bound["mac"]), str(bound["address"]),
                              bound["prefixlen"] if isinstance(bound["prefixlen"], int) else None))
        hosts.append(HostFacts(position, name, tuple(ports)))
    facts = Facts(hosts, bindings=bindings)
    problems = facts.problems()
    if problems:
        raise FabricError("; ".join(problems))
    return facts


def problems_against_reads(facts: Facts, states: dict[int, HostState], topology: Topology) -> list[str]:
    """Where a facts file disagrees with what the hosts report (both identify by host name)."""
    found = []
    for position, state in sorted(states.items()):
        if not state.reachable or topology.position_of(state.name) != position:
            continue
        for netdev in facts.bindings.netdevs:
            bound = facts.port(position, netdev) if facts.has(position) else None
            reported = state.port(netdev)
            if bound is None or reported is None:
                continue
            if bound.mac != reported.mac or bound.address != reported.address:
                found.append(f"{state.name}: facts say {netdev} is {bound.address} ({bound.mac}), the host "
                             f"reports {reported.address} ({reported.mac})")
    return found
