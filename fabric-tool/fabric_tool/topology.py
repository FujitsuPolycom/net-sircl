"""The topology file of ``net-sircl``, parsed into the tool's one layout.

The file is the plugin's only route input
(``spark_transport/transport-core/src/topology.c`` 11-98); this parser
accepts the same keys and defaults:

- ``hosts <name> ...``: host names in position order;
- ``cabling ring`` / ``path`` / ``explicit``: ``ring`` cables host ``i``'s
  port 0 to host ``i+1``'s port 1 and the last host to the first; ``path``
  leaves the closing cable out; ``explicit`` takes only ``cable`` lines;
- ``cable <i>.port<p> <j>.port<q>``: one cable (repeatable, ``explicit``);
- ``port0 <fn> <fn>`` / ``port1 <fn> <fn>``: the RDMA functions of each port
  in lane order (lane 0 first). The defaults are the DGX OS names
  ``rocep1s0f0 roceP2p1s0f0`` and ``rocep1s0f1 roceP2p1s0f1``;
- ``relay_budget <bytes>``: the plugin's window toward a relayed peer; the
  tool carries it for reference (its own queue bound is the measured
  512 KiB hairpin queue);
- ``net-sircl-topology``: the file's optional first key, as the plugin reads it.

:func:`load` parses a file, installs the role table it names
(:func:`fabric_tool.model.set_roles`) and returns the :class:`Topology`. Its
:attr:`Topology.group` is the one group the tool plans: the fabric of the
topology's cables over positions 0..N-1, whole cycle or path, with the
relay-egress default of SparkRing's layouts (``same`` for a whole cycle,
``sibling`` otherwise).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from . import model
from .model import Cable, Fabric, FabricError, Layout, Role

DEFAULT_BUDGET = 393216          # 75 % of a ConnectX-7 hairpin queue of 512 KiB
DEFAULT_FUNCTIONS = ("rocep1s0f0", "roceP2p1s0f0", "rocep1s0f1", "roceP2p1s0f1")
DEFAULT_NETDEVS = ("enp1s0f0np0", "enP2p1s0f0np0", "enp1s0f1np1", "enP2p1s0f1np1")
CABLING_KINDS = ("ring", "path", "explicit")


@dataclasses.dataclass(frozen=True)
class GroupSpec:
    """The one group a topology file describes.

    ``ring_size`` follows SparkRing's convention for a group on a ring: the
    whole cycle is a ring of its own size, a path sits on a ring one Spark
    longer (the wrap position has no facts, so a frame that left the path's
    end could never be delivered). The attribute names match the plan
    derivation's group interface.
    """

    layout: Layout
    relay_egress: str
    name: str = "topology"

    @property
    def members(self) -> tuple[int, ...]:
        return tuple(self.layout.positions)

    @property
    def whole_ring(self) -> bool:
        return self.layout.fabric.kind == "cycle"

    @property
    def ring_size(self) -> int:
        return len(self.members) if self.whole_ring else len(self.members) + 1

    @property
    def order(self) -> tuple[int, ...]:
        """Members in fabric order: a cycle from position 0 out of port 0, a path from its first end."""
        fabric = self.layout.fabric
        if self.whole_ring:
            start, port = 0, 0
        else:
            degree: dict[int, int] = {}
            for cable in fabric.cables:
                for position, _ in cable.ends():
                    degree[position] = degree.get(position, 0) + 1
            start = min(position for position in fabric.positions if degree.get(position, 0) <= 1)
            step = fabric.cross(start, 0) or fabric.cross(start, 1)
            port = 0 if step is not None and step.src_port == 0 else 1
        order = [start]
        while len(order) < len(fabric.positions):
            step = fabric.cross(order[-1], port)
            if step is None:
                break
            order.append(step.dst)
            port = 1 - step.dst_port
        return tuple(order)

    @property
    def kind(self) -> str:
        if self.whole_ring:
            return "cycle"
        return "pair" if len(self.members) == 2 else "path"

    @property
    def label(self) -> str:
        """The group's identity, for example ``cycle:0-1-2-3`` or ``path:0-1-2``."""
        return f"{self.kind}:{'-'.join(str(position) for position in self.order)}"

    @property
    def cables(self) -> tuple[str, ...]:
        return tuple(cable.text for cable in self.layout.fabric.cables)

    def layout_of(self) -> Layout:
        return self.layout


@dataclasses.dataclass(frozen=True)
class Topology:
    """A parsed topology file: hosts, cabling, functions and the one group."""

    path: str
    hosts: tuple[str, ...]
    cabling: str
    cables: tuple[Cable, ...]
    functions: tuple[str, ...]          # port 0 lane 0/1, port 1 lane 0/1
    budget: int

    @property
    def size(self) -> int:
        return len(self.hosts)

    def position_of(self, name: str) -> int:
        """The position of a host name, or -1 when the topology does not name it."""
        try:
            return self.hosts.index(name)
        except ValueError:
            return -1

    @property
    def group(self) -> GroupSpec:
        return _group_of(self)


def _roles_of_functions(functions: tuple[str, ...]) -> tuple[Role, ...]:
    """The four roles of a topology's function names, with the DGX OS network devices as defaults.

    The facts file (or a host read) binds each function to its network
    device; until then the roles carry the default devices, which the fact
    checks replace when a host names others.
    """
    roles = []
    for index, device in enumerate(functions):
        port, secondary = divmod(index, 2)
        roles.append(Role(("cw_primary", "cw_secondary", "ccw_primary", "ccw_secondary")[index],
                          device, DEFAULT_NETDEVS[index], port, bool(secondary)))
    return tuple(roles)


def parse(text: str, *, path: str = "<topology>") -> Topology:
    """Parse topology file text; FabricError names the line and the rule."""
    hosts: list[str] = []
    cables: list[Cable] = []
    functions: list[str] = list(DEFAULT_FUNCTIONS)
    budget = DEFAULT_BUDGET
    cabling = ""
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0]
        words = line.split()
        if not words:
            continue
        key, rest = words[0], words[1:]
        if key == "hosts":
            hosts.extend(rest)
        elif key == "cabling":
            if len(rest) != 1:
                raise FabricError(f"{path}:{lineno}: cabling takes one of {', '.join(CABLING_KINDS)}")
            cabling = rest[0]
        elif key == "cable":
            if len(rest) != 2:
                raise FabricError(f"{path}:{lineno}: a cable is `cable <i>.port<0|1> <j>.port<0|1>`")
            try:
                cables.append(Cable.parse(f"{rest[0]}-{rest[1]}"))
            except FabricError:
                raise FabricError(f"{path}:{lineno}: a cable is `cable <i>.port<0|1> <j>.port<0|1>`") from None
        elif key in ("port0", "port1"):
            port = int(key[-1])
            for lane, name in enumerate(rest[:2]):
                functions[2 * port + lane] = name
        elif key == "relay_budget":
            if len(rest) != 1 or not rest[0].isdigit() or int(rest[0]) == 0:
                raise FabricError(f"{path}:{lineno}: relay_budget is a positive byte count")
            budget = int(rest[0])
        elif key == "net-sircl-topology":
            if rest:
                raise FabricError(f"{path}:{lineno}: unknown words after net-sircl-topology")
        else:
            raise FabricError(f"{path}:{lineno}: unknown key {key}")
    if len(hosts) < 2:
        raise FabricError(f"{path}: `hosts` names fewer than two hosts")
    if len(set(hosts)) != len(hosts):
        raise FabricError(f"{path}: `hosts` names a host twice")
    if cabling not in CABLING_KINDS:
        raise FabricError(f"{path}: cabling is {', '.join(CABLING_KINDS)}")
    if cabling in ("ring", "path"):
        last = len(hosts) if cabling == "ring" else len(hosts) - 1
        for i in range(last):
            cables.append(Cable(i, 0, (i + 1) % len(hosts), 1))
    if len(set(functions)) != 4:
        raise FabricError(f"{path}: port0 and port1 name one function twice")
    try:
        fabric = Fabric(tuple(cables))
    except FabricError as error:
        raise FabricError(f"{path}: {error}") from None
    outside = sorted({p for cable in cables for p in (cable.a, cable.b) if not 0 <= p < len(hosts)})
    if outside:
        raise FabricError(f"{path}: a cable names position {outside[0]}, outside the hosts 0-{len(hosts) - 1}")
    missing = sorted(set(range(len(hosts))) - set(fabric.positions))
    if missing:
        raise FabricError(f"{path}: the cables leave host {missing[0]} ({hosts[missing[0]]}) without a cable")
    if fabric.kind != "cycle" and not _is_connected_path(fabric):
        raise FabricError(f"{path}: the cables do not join the hosts into one path or cycle")
    topology = Topology(path, tuple(hosts), cabling, tuple(fabric.cables), tuple(functions), budget)
    return topology


def _is_connected_path(fabric: Fabric) -> bool:
    """Whether a non-cycle fabric is one connected path (every position reached from position 0)."""
    reached, frontier = {fabric.positions[0]}, [fabric.positions[0]]
    while frontier:
        here = frontier.pop()
        for port in (0, 1):
            step = fabric.cross(here, port)
            if step is not None and step.dst not in reached:
                reached.add(step.dst)
                frontier.append(step.dst)
    return len(reached) == len(fabric.positions)


def _group_of(topology: Topology) -> GroupSpec:
    """The one group of a topology: whole cycle or path, with SparkRing's relay-egress default."""
    fabric = Fabric(topology.cables)
    layout = Layout(fabric, tuple(range(topology.size)))
    egress = "same" if fabric.kind == "cycle" else "sibling"
    return GroupSpec(layout, egress, f"topology:{Path(topology.path).name}")


def load(path: str | Path) -> Topology:
    """Read, parse and install the role table of a topology file."""
    file = Path(path)
    try:
        text = file.read_text(encoding="utf-8")
    except OSError as error:
        raise FabricError(f"cannot read the topology file {file}: {error}") from None
    topology = parse(text, path=str(file))
    model.set_roles(_roles_of_functions(topology.functions))
    return topology
