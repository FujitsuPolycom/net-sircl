"""Cables, fabrics, layouts and lane routes of directly cabled DGX Sparks.

Absorbed from SparkRing's ``sparkring_sircl.routes`` (SparkRing
``spark_transport/sircl/sparkring_sircl/routes.py``); see NOTICE. The parts
the relay table needs are kept: the four ConnectX-7 function roles, cables
and fabrics with their shortest paths, session layouts, lane derivation, the
relay queue load, and lane isolation. Validation of operator route maps, the
ring harness's window planners and the ``SIRCL_FABRIC_DOCUMENT`` loader stay
in SparkRing.

Physical model. A DGX Spark has one ConnectX-7 with ports 0 and 1; each port
appears as a primary and a secondary PCIe function, each with its own RDMA
device. The roles name the four functions; the defaults are the names DGX OS
gives them:

    port 0 primary    rocep1s0f0     enp1s0f0np0
    port 0 secondary  roceP2p1s0f0   enP2p1s0f0np0
    port 1 primary    rocep1s0f1     enp1s0f1np1
    port 1 secondary  roceP2p1s0f1   enP2p1s0f1np1

A topology file may name other functions (``port0 <fn> <fn>``,
``port1 <fn> <fn>``); the tool sets the role table from it once, before any
planning (:func:`set_roles`), and every module reads the table at call time.
A cable joins one port of one Spark to one port of another and carries two
links (the primary functions of its ends and the secondary functions). A
fabric is the set of cables a group owns, written
``"<position>.port<p>-<position>.port<q>"``; every Spark has two ports, so a
fabric is a path or a cycle. Ring cabling joins position ``i``'s port 0 to
position ``i+1``'s port 1.
"""

from __future__ import annotations

import dataclasses
import re
from collections import deque
from collections.abc import Iterable, Mapping, Sequence

DEFAULT_MAX_RELAYS = 3
DEFAULT_HAIRPIN_QUEUE = 524288
RELAY_QUEUE_SHARE = 0.75


class FabricError(ValueError):
    """A topology, facts, layout or relay plan breaks a rule of the tool."""


@dataclasses.dataclass(frozen=True)
class Role:
    """One PCIe function of a Spark's ConnectX-7."""

    name: str
    device: str
    netdev: str
    port: int
    secondary: bool

    @property
    def label(self) -> str:
        """The hyphenated spelling of the role, e.g. ``cw-primary``."""
        return self.name.replace("_", "-")


# The four functions under the names DGX OS gives a DGX Spark's ConnectX-7.
DEFAULT_ROLES = (
    Role("cw_primary", "rocep1s0f0", "enp1s0f0np0", 0, False),
    Role("cw_secondary", "roceP2p1s0f0", "enP2p1s0f0np0", 0, True),
    Role("ccw_primary", "rocep1s0f1", "enp1s0f1np1", 1, False),
    Role("ccw_secondary", "roceP2p1s0f1", "enP2p1s0f1np1", 1, True),
)

# The role table this process plans with: set once from the topology file
# (:func:`set_roles`) before any planning or command generation.
ROLES: tuple[Role, ...] = DEFAULT_ROLES
FABRIC_NETDEVS: tuple[str, ...] = tuple(role.netdev for role in ROLES)
RDMA_DEVICES: tuple[str, ...] = tuple(role.device for role in ROLES)


def set_roles(roles: Sequence[Role]) -> None:
    """Install ``roles`` as this process's role table (the four functions, port 0 and 1, both classes)."""
    global ROLES, FABRIC_NETDEVS, RDMA_DEVICES
    roles = tuple(roles)
    if len(roles) != 4:
        raise FabricError(f"a role table holds four functions, got {len(roles)}")
    if len({role.device for role in roles}) != 4 or len({role.netdev for role in roles}) != 4:
        raise FabricError("a role table names one device or interface for two functions")
    if sorted((role.port, role.secondary) for role in roles) != [(0, False), (0, True), (1, False), (1, True)]:
        raise FabricError("a role table needs the primary and the secondary function of each port")
    ROLES = roles
    FABRIC_NETDEVS = tuple(role.netdev for role in ROLES)
    RDMA_DEVICES = tuple(role.device for role in ROLES)


def role_at(port: int, secondary: bool) -> Role:
    for role in ROLES:
        if role.port == port and role.secondary == secondary:
            return role
    raise FabricError(f"no function on port {port}")


def role_of(device: str) -> Role | None:
    """The role of an RDMA device name, or None."""
    for role in ROLES:
        if role.device == device:
            return role
    return None


def role_of_netdev(netdev: str) -> Role:
    """The role of a network device name; FabricError for a device outside the fabric."""
    for role in ROLES:
        if role.netdev == netdev:
            return role
    raise FabricError(f"{netdev} is not one of the fabric network devices {', '.join(FABRIC_NETDEVS)}")


def class_name(secondary: bool) -> str:
    return "secondary" if secondary else "primary"


# -- fabric --------------------------------------------------------------------

_CABLE = re.compile(r"^\s*(\d+)\.port([01])\s*-\s*(\d+)\.port([01])\s*$")


@dataclasses.dataclass(frozen=True)
class Cable:
    a: int
    a_port: int
    b: int
    b_port: int

    def __post_init__(self) -> None:
        if self.a == self.b:
            raise FabricError(f"cable {self.text} joins a Spark to itself")

    @property
    def text(self) -> str:
        return f"{self.a}.port{self.a_port}-{self.b}.port{self.b_port}"

    @classmethod
    def parse(cls, text: str) -> "Cable":
        match = _CABLE.match(text)
        if match is None:
            raise FabricError(f"cable {text!r} is not written <position>.port<0|1>-<position>.port<0|1>")
        a, a_port, b, b_port = (int(group) for group in match.groups())
        return cls(a, a_port, b, b_port)

    def ends(self) -> tuple[tuple[int, int], tuple[int, int]]:
        return (self.a, self.a_port), (self.b, self.b_port)


@dataclasses.dataclass(frozen=True)
class Step:
    """One cable crossed from (``src``, ``src_port``) to (``dst``, ``dst_port``)."""

    cable: Cable
    src: int
    src_port: int
    dst: int
    dst_port: int

    @property
    def text(self) -> str:
        return f"{self.src}.port{self.src_port}-{self.dst}.port{self.dst_port}"


@dataclasses.dataclass(frozen=True)
class Fabric:
    """The cables a group owns."""

    cables: tuple[Cable, ...]

    def __post_init__(self) -> None:
        seen: dict[tuple[int, int], Cable] = {}
        for cable in self.cables:
            for end in cable.ends():
                if end in seen:
                    raise FabricError(f"port {end[1]} of position {end[0]} has two cables")
                seen[end] = cable

    @classmethod
    def ring(cls, size: int) -> "Fabric":
        """A ring of ``size`` Sparks: position ``i`` port 0 to position ``i+1`` port 1."""
        if size < 2:
            raise FabricError("a ring needs at least two Sparks")
        return cls(tuple(Cable(i, 0, (i + 1) % size, 1) for i in range(size)))

    @classmethod
    def path(cls, positions: Sequence[int]) -> "Fabric":
        """Consecutive Sparks, each cabled port 0 to the next one's port 1."""
        positions = tuple(positions)
        if len(positions) < 2:
            raise FabricError("a path needs at least two Sparks")
        return cls(tuple(Cable(a, 0, b, 1) for a, b in zip(positions, positions[1:])))

    @classmethod
    def parse(cls, texts: Iterable[str]) -> "Fabric":
        return cls(tuple(Cable.parse(text) for text in texts))

    @property
    def positions(self) -> tuple[int, ...]:
        return tuple(sorted({end[0] for cable in self.cables for end in cable.ends()}))

    @property
    def kind(self) -> str:
        """``cycle`` when every Spark has two cables and they close one loop, else ``path``."""
        degree: dict[int, int] = {}
        for cable in self.cables:
            for position, _ in cable.ends():
                degree[position] = degree.get(position, 0) + 1
        if degree and all(count == 2 for count in degree.values()) and len(self.cables) == len(degree):
            return "cycle"
        return "path"

    def cable_at(self, position: int, port: int) -> Cable | None:
        for cable in self.cables:
            if (position, port) in cable.ends():
                return cable
        return None

    def cross(self, position: int, port: int) -> Step | None:
        """Leave ``position`` through ``port``; None when that port has no cable in this fabric."""
        cable = self.cable_at(position, port)
        if cable is None:
            return None
        if (cable.a, cable.a_port) == (position, port):
            return Step(cable, position, port, cable.b, cable.b_port)
        return Step(cable, position, port, cable.a, cable.a_port)

    def shortest_paths(self, a: int, b: int) -> tuple[tuple[Step, ...], ...]:
        """Every shortest cable path from ``a`` to ``b``."""
        if a == b:
            raise FabricError("a lane joins two different Sparks")
        distance = {b: 0}
        queue = deque([b])
        while queue:
            here = queue.popleft()
            for port in (0, 1):
                step = self.cross(here, port)
                if step is not None and step.dst not in distance:
                    distance[step.dst] = distance[here] + 1
                    queue.append(step.dst)
        if a not in distance:
            return ()
        paths: list[tuple[Step, ...]] = []

        def extend(here: int, steps: list[Step]) -> None:
            if here == b:
                paths.append(tuple(steps))
                return
            for port in (0, 1):
                step = self.cross(here, port)
                if step is not None and distance.get(step.dst) == distance[here] - 1:
                    steps.append(step)
                    extend(step.dst, steps)
                    steps.pop()

        extend(a, [])
        return tuple(paths)

    def walk(self, start: int, port: int, target: int, limit: int) -> tuple[Step, ...] | None:
        """Follow the fabric from ``start`` out of ``port`` (relays leave through their other port).

        Returns the steps up to ``target``, or None when the walk leaves the
        fabric or exceeds ``limit`` cables first.
        """
        steps: list[Step] = []
        here, out = start, port
        while len(steps) < limit:
            step = self.cross(here, out)
            if step is None:
                return None
            steps.append(step)
            if step.dst == target:
                return tuple(steps)
            here, out = step.dst, 1 - step.dst_port
        return None


@dataclasses.dataclass(frozen=True)
class Layout:
    """A session's layout identity: its fabric and the fabric position of every rank."""

    fabric: Fabric
    positions: tuple[int, ...]

    def __post_init__(self) -> None:
        if len(set(self.positions)) != len(self.positions):
            raise FabricError("ranks need distinct fabric positions")
        known = set(self.fabric.positions)
        missing = [p for p in self.positions if p not in known]
        if missing:
            raise FabricError(f"positions {missing} are not on the fabric")

    @property
    def world(self) -> int:
        return len(self.positions)

    def identity(self) -> dict[str, object]:
        return {
            "kind": self.fabric.kind,
            "size": len(self.fabric.positions),
            "positions": list(self.positions),
            "cables": [cable.text for cable in self.fabric.cables],
        }


# -- derivation -------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class LaneRoute:
    """One lane from a rank toward a peer."""

    lane: int
    local: Role
    remote: Role
    steps: tuple[Step, ...]

    @property
    def hops(self) -> int:
        return len(self.steps)

    @property
    def relays(self) -> tuple[int, ...]:
        return tuple(step.dst for step in self.steps[:-1])

    @property
    def cables(self) -> tuple[str, ...]:
        return tuple(step.text for step in self.steps)

    def to_json(self) -> dict[str, object]:
        return {
            "lane": self.lane,
            "local": self.local.device,
            "remote": self.remote.device,
            "local_role": self.local.label,
            "remote_role": self.remote.label,
            "hops": self.hops,
            "relays": list(self.relays),
            "cables": list(self.cables),
        }


def derive_lanes(fabric: Fabric, a: int, b: int, lanes: int = 2) -> tuple[LaneRoute, ...]:
    """The lanes from position ``a`` to position ``b``.

    The shortest paths of the fabric (one, or two when the two ends are
    opposite on an even cycle or joined by two cables). With one path, lane 0
    uses the primary and lane 1 the secondary function of the path's first
    cable at ``a``. With two paths, path A is the one that leaves the end
    with the smaller position through its port 0; lane 0 uses the primary
    function on path A and lane 1 the secondary function on path B. A lane's
    remote device is the function of the same class at the far end of its
    last cable.
    """
    if lanes not in (1, 2):
        raise FabricError("a session has one or two lanes per peer")
    paths = fabric.shortest_paths(a, b)
    if not paths:
        raise FabricError(f"position {b} cannot be reached from position {a} on this fabric")
    if len(paths) == 1:
        chosen = (paths[0], paths[0])
    elif len(paths) == 2:
        smaller = min(a, b)

        def leaves_smaller_through_port0(path: tuple[Step, ...]) -> bool:
            if smaller == a:
                return path[0].src_port == 0
            return path[-1].dst_port == 0

        path_a = [path for path in paths if leaves_smaller_through_port0(path)]
        if len(path_a) != 1:
            raise FabricError(f"cannot tell path A from path B between positions {a} and {b}")
        path_b = next(path for path in paths if path is not path_a[0])
        chosen = (path_a[0], path_b)
    else:
        raise FabricError(f"{len(paths)} shortest paths between positions {a} and {b}")
    result = []
    for lane in range(lanes):
        path = chosen[lane]
        secondary = lane == 1
        result.append(LaneRoute(lane, role_at(path[0].src_port, secondary),
                                role_at(path[-1].dst_port, secondary), path))
    return tuple(result)


@dataclasses.dataclass(frozen=True)
class GroupRoutes:
    """Every rank's lanes for one session."""

    layout: Layout
    lanes: int
    ranks: tuple[Mapping[int, tuple[LaneRoute, ...]], ...]

    @property
    def world(self) -> int:
        return self.layout.world

    def route_map(self, rank: int) -> dict[int, tuple[str, ...]]:
        """Peer rank to lane device names."""
        return {peer: tuple(lane.local.device for lane in lanes)
                for peer, lanes in sorted(self.ranks[rank].items())}

    def route_text(self, rank: int) -> str:
        return format_peer_routes(self.route_map(rank))

    def lanes_to(self, rank: int, peer: int) -> tuple[LaneRoute, ...]:
        return self.ranks[rank][peer]

    def max_relays(self) -> int:
        return max((len(lane.relays) for rank in self.ranks for lanes in rank.values() for lane in lanes),
                   default=0)


def derive_routes(layout: Layout, lanes: int = 2) -> GroupRoutes:
    """Route maps of every rank of a session on ``layout``."""
    ranks = []
    for rank, here in enumerate(layout.positions):
        ranks.append({peer: derive_lanes(layout.fabric, here, there, lanes)
                      for peer, there in enumerate(layout.positions) if peer != rank})
    return GroupRoutes(layout, lanes, tuple(ranks))


def format_peer_routes(routes: Mapping[int, Sequence[str]]) -> str:
    return ",".join(f"{peer}={'/'.join(devices)}" for peer, devices in sorted(routes.items()))


def relay_load(routes: GroupRoutes) -> tuple[int, float]:
    """``(lanes through the busiest relay queue, load factor f)``.

    A relay queue is one (relay Spark, function class, direction). When every
    rank sends ``b`` bytes to every peer, split over ``L`` lanes, the busiest
    queue holds about ``f * b`` bytes with ``f`` = lanes / ``L``.
    """
    load: dict[tuple[int, bool, int], int] = {}
    for rank in routes.ranks:
        for lanes in rank.values():
            for lane in lanes:
                for step in lane.steps[:-1]:
                    # Arriving on port 1 continues clockwise (out of port 0).
                    key = (step.dst, lane.local.secondary, step.dst_port)
                    load[key] = load.get(key, 0) + 1
    busiest = max(load.values(), default=0)
    return busiest, busiest / routes.lanes


def _lane_steps(layout: Layout, rank: int, peer: int, device: str) -> tuple[Role, tuple[Step, ...]] | None:
    role = role_of(device)
    if role is None:
        return None
    steps = layout.fabric.walk(layout.positions[rank], role.port, layout.positions[peer],
                               limit=len(layout.fabric.positions))
    return (role, steps) if steps is not None else None


def relay_queues(layout: Layout,
                 route_maps: Sequence[Mapping[int, Sequence[str]]]) -> dict[tuple[int, bool, int],
                                                                           list[tuple[int, int, int]]]:
    """Lanes through every relay hairpin queue of a group.

    Key: (relay position, secondary function, egress port); value: the
    ``(rank, peer, lane)`` of every lane whose packets that queue forwards.
    """
    queues: dict[tuple[int, bool, int], list[tuple[int, int, int]]] = {}
    for rank, routes in enumerate(route_maps):
        for peer, devices in sorted(routes.items()):
            for lane, device in enumerate(devices):
                found = _lane_steps(layout, rank, peer, device)
                if found is None:
                    continue
                role, steps = found
                for step in steps[:-1]:
                    key = (step.dst, role.secondary, 1 - step.dst_port)
                    queues.setdefault(key, []).append((rank, peer, lane))
    return queues


def isolation_problems(groups: Sequence[GroupRoutes]) -> list[str]:
    """Cables or relays shared by independent groups on one ring (must be empty)."""
    problems = []
    for first in range(len(groups)):
        for second in range(first + 1, len(groups)):
            a, b = groups[first], groups[second]
            cables_a = {step.cable for rank in a.ranks for lanes in rank.values() for lane in lanes
                        for step in lane.steps}
            cables_b = {step.cable for rank in b.ranks for lanes in rank.values() for lane in lanes
                        for step in lane.steps}
            for cable in sorted(cables_a & cables_b, key=lambda c: c.text):
                problems.append(f"groups {first} and {second} both use cable {cable.text}")
            sparks_a = set(a.layout.positions) | {relay for rank in a.ranks for lanes in rank.values()
                                                  for lane in lanes for relay in lane.relays}
            sparks_b = set(b.layout.positions) | {relay for rank in b.ranks for lanes in rank.values()
                                                  for lane in lanes for relay in lane.relays}
            for spark in sorted(sparks_a & sparks_b):
                problems.append(f"groups {first} and {second} both use Spark {spark}")
    return problems
