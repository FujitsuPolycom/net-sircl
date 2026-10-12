"""Derive relay paths, routes, marker rules and forwarding filters.

Derived from the SparkRing relay installer; see NOTICE.

Carried paths. For every ordered pair of members the plan carries every
shortest path of the fabric (:meth:`fabric_tool.model.Fabric.shortest_paths`)
on both function classes. The lanes (``model.derive_routes``) are a
subset: lane 0 is the primary function and lane 1 the secondary function of
one of these paths. On a path of Sparks both sets are equal. On a cycle of
even size the plan also carries the other function class of each opposite
pair's two paths, which makes the whole-cycle plan the universal relay table:
every Spark reaches all four addresses of every Spark it shares no cable
with. Every lane of ``model.derive_routes`` is checked to be carried, so this
plan and the route maps cannot disagree.

Objects, for a carried path with ``h >= 2`` hops from ``a`` to ``b`` on
function class ``c``:

- an origin route at ``a``: ``b``'s address on the arrival function as a /32
  route, scope link, over ``a``'s network device of class ``c`` on the path's
  first port, source ``a``'s address on that device, and a permanent
  neighbour entry holding the adjacent Spark's MAC on that cable;
- a marker rule at ``a``: the RDMA device of that function rewrites the
  EtherType of RoCE packets to that destination into ``TAG(h - 1)``;
- a relay filter at every intermediate Spark: on the ingress of the device
  that receives the path (class ``c``), match ``TAG(k)`` (``k`` relays left),
  set the next Spark's MAC of class ``c``, rewrite the tag to ``TAG(k - 1)``
  and redirect out of the other port (relay egress ``same`` or ``sibling``,
  the topology's default).

``TAG(0)`` is IPv4 (0x0800) and ``TAG(k) = 0x88b4 + k``. Relay filters do not
depend on the sender: one per (Spark, ingress device, tag).

Measured constraints that the plan states and applies:

- a relay's hardware hairpin queue holds 512 KiB, the device maximum, and
  cannot pause its sender, so frames beyond it are dropped. The plan lists
  the lanes through every queue, the relay load factor ``f`` and the
  per-peer op bound ``0.75 * 512 KiB / f``;
- queue pairs use flow label 0 and tags are chosen by destination address,
  never by UDP port: the sender NIC computes the ICRC after the RDMA-TX
  rewrite, so a rewritten UDP field would be covered by the ICRC and could
  not be restored by a relay, and with flow label 0 the UDP source port does
  not identify a lane. The EtherType lies outside the ICRC.

Isolation: routes and tags exist only for destinations inside the topology's
hosts, and a relay filter only on a member that lies strictly inside a
carried path, between two cables of the fabric. :func:`trace` follows every
origin route through the plan's objects over the physical cabling and must
reach the destination without leaving it; :func:`isolation_problems` checks
the rules.

Everything here is offline. Facts (addresses and MACs) come from the Sparks
(``fabric_tool.facts``), from a facts file, or as symbolic placeholders that
make a plan reviewable without them.
"""

from __future__ import annotations

import dataclasses
import hashlib
import ipaddress
import json
import re
from collections.abc import Iterable, Mapping, Sequence

from . import model
from .model import FabricError
from .topology import GroupSpec

ETH_P_IP = 0x0800
TAG_BASE = 0x88B4
MAX_TAG = 7                                  # tags TAG(1)..TAG(7); relay filter preferences 11..17
FILTER_PREF_BASE = 10
RESERVED_PREFS = range(FILTER_PREF_BASE + 1, FILTER_PREF_BASE + MAX_TAG + 1)
HAIRPIN_QUEUE_BYTES = model.DEFAULT_HAIRPIN_QUEUE
RELAY_QUEUE_SHARE = model.RELAY_QUEUE_SHARE
PLAN_SCHEMA = "fabric-tool-plan/v2"
_MAC = re.compile(r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$")


def tag(k: int) -> int:
    """The EtherType of a frame with ``k`` relays left."""
    if not 0 <= k <= MAX_TAG:
        raise FabricError(f"relay count {k} is outside the tags 0 to {MAX_TAG}")
    return ETH_P_IP if k == 0 else TAG_BASE + k


def relays_left(ethertype: int) -> int | None:
    """``k`` for ``TAG(k)``, or None for an EtherType that is not a relay tag."""
    if ethertype == ETH_P_IP:
        return 0
    k = ethertype - TAG_BASE
    return k if 1 <= k <= MAX_TAG else None


def _address_key(text: str) -> tuple:
    try:
        return (0, int(ipaddress.IPv4Address(text)))
    except ValueError:
        return (1, text)


# -- facts --------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Port:
    """One fabric function's network device on one host: its MAC and IPv4 address."""

    netdev: str
    mac: str
    address: str
    prefixlen: int | None = None

    def network(self) -> ipaddress.IPv4Network | None:
        if self.prefixlen is None:
            return None
        try:
            return ipaddress.IPv4Interface(f"{self.address}/{self.prefixlen}").network
        except ValueError:
            return None

    def to_json(self) -> dict[str, object]:
        address = self.address if self.prefixlen is None else f"{self.address}/{self.prefixlen}"
        return {"mac": self.mac, "address": address}


@dataclasses.dataclass(frozen=True)
class HostFacts:
    position: int
    name: str
    ports: tuple[Port, ...]

    def port(self, netdev: str) -> Port | None:
        for port in self.ports:
            if port.netdev == netdev:
                return port
        return None


class Facts:
    """Addresses and MACs of the fabric functions of the hosts a plan uses."""

    def __init__(self, hosts: Iterable[HostFacts], *, symbolic: bool = False) -> None:
        self.symbolic = symbolic
        self._hosts = {host.position: host for host in hosts}
        self._addresses: dict[str, list[tuple[int, str]]] = {}
        self._macs: dict[str, list[tuple[int, str]]] = {}
        for host in self._hosts.values():
            for port in host.ports:
                self._addresses.setdefault(port.address, []).append((host.position, port.netdev))
                self._macs.setdefault(port.mac.lower(), []).append((host.position, port.netdev))

    @property
    def positions(self) -> tuple[int, ...]:
        return tuple(sorted(self._hosts))

    def has(self, position: int) -> bool:
        return position in self._hosts

    def host(self, position: int) -> HostFacts:
        if position not in self._hosts:
            raise FabricError(f"no facts for the host at position {position}")
        return self._hosts[position]

    def name(self, position: int) -> str:
        return self._hosts[position].name if position in self._hosts else f"position {position}"

    def port(self, position: int, netdev: str) -> Port:
        port = self.host(position).port(netdev)
        if port is None:
            raise FabricError(f"{self.name(position)} has no address or MAC for {netdev}")
        return port

    def find_address(self, address: str) -> tuple[int, str] | None:
        owners = self._addresses.get(address, [])
        return owners[0] if len(owners) == 1 else None

    def find_mac(self, mac: str) -> tuple[int, str] | None:
        owners = self._macs.get(mac.lower(), [])
        return owners[0] if len(owners) == 1 else None

    def problems(self) -> list[str]:
        """Addresses or MACs that more than one function holds."""
        found = []
        for label, table in (("address", self._addresses), ("MAC", self._macs)):
            for value, owners in sorted(table.items()):
                if len(owners) > 1:
                    holders = ", ".join(f"{self.name(position)} {netdev}" for position, netdev in owners)
                    found.append(f"{label} {value} is held by {holders}")
        return found

    @classmethod
    def symbolic_for(cls, names: Sequence[str]) -> "Facts":
        """Placeholders ``<name:netdev>`` and ``<name:netdev:mac>`` for every host of a layout."""
        return cls((HostFacts(position, name, tuple(Port(netdev, f"<{name}:{netdev}:mac>",
                                                    f"<{name}:{netdev}>")
                                                    for netdev in model.FABRIC_NETDEVS))
                    for position, name in enumerate(names)), symbolic=True)


def port_from_text(netdev: str, mac: str, address: str, *, owner: str) -> Port:
    """A validated :class:`Port` from a MAC and ``a.b.c.d`` or ``a.b.c.d/len``."""
    mac = mac.strip().lower()
    if not _MAC.match(mac):
        raise FabricError(f"{owner} {netdev}: {mac!r} is not a MAC address")
    try:
        if "/" in address:
            interface = ipaddress.IPv4Interface(address.strip())
            return Port(netdev, mac, str(interface.ip), interface.network.prefixlen)
        return Port(netdev, mac, str(ipaddress.IPv4Address(address.strip())))
    except ValueError:
        raise FabricError(f"{owner} {netdev}: {address!r} is not an IPv4 address") from None


# -- plan objects -----------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class OriginRoute:
    """A /32 route, its permanent neighbour and its marker rule at the host where a path starts."""

    destination: str
    netdev: str
    device: str
    source: str
    next_mac: str
    tag: int
    peer: int
    peer_netdev: str
    hops: int
    relays: tuple[int, ...]
    cables: tuple[str, ...]              # cables crossed, in the direction of travel
    owned_cables: tuple[str, ...]        # the same cables in canonical spelling
    lanes: tuple[str, ...] = ()          # lanes it carries: "rank r->p lane l"

    def to_json(self) -> dict[str, object]:
        return {"destination": self.destination, "netdev": self.netdev, "device": self.device,
                "source": self.source, "next_mac": self.next_mac, "tag": f"0x{self.tag:04x}", "peer": self.peer,
                "peer_netdev": self.peer_netdev, "hops": self.hops, "relays": list(self.relays),
                "cables": list(self.cables), "lanes": list(self.lanes)}


@dataclasses.dataclass(frozen=True)
class RelayFilter:
    """A hardware ingress filter of a relay: ``TAG(k)`` in, ``TAG(k - 1)`` out of the other port."""

    in_netdev: str
    k: int
    out_netdev: str
    next_mac: str
    next_position: int
    next_netdev: str
    direction: str                       # cw (out of port 0) or ccw (out of port 1)
    lane_class: str                      # primary or secondary: the class of the lanes it relays

    @property
    def pref(self) -> int:
        return FILTER_PREF_BASE + self.k

    @property
    def handle(self) -> int:
        return self.k

    @property
    def protocol(self) -> int:
        return tag(self.k)

    @property
    def new_type(self) -> int:
        return tag(self.k - 1)

    def to_json(self) -> dict[str, object]:
        return {"in_netdev": self.in_netdev, "k": self.k, "pref": self.pref, "protocol": f"0x{self.protocol:04x}",
                "new_type": f"0x{self.new_type:04x}", "out_netdev": self.out_netdev, "next_mac": self.next_mac,
                "next_position": self.next_position, "next_netdev": self.next_netdev, "direction": self.direction,
                "lane_class": self.lane_class}


@dataclasses.dataclass(frozen=True)
class MarkerRules:
    """The RDMA-TX tag rules of one RDMA device: (destination, EtherType) pairs."""

    device: str
    rules: tuple[tuple[str, int], ...]

    def to_json(self) -> dict[str, object]:
        return {"device": self.device, "rules": {destination: f"0x{value:04x}" for destination, value in self.rules}}


@dataclasses.dataclass(frozen=True)
class HostPlan:
    position: int
    name: str
    group: str
    routes: tuple[OriginRoute, ...]
    filters: tuple[RelayFilter, ...]

    @property
    def markers(self) -> tuple[MarkerRules, ...]:
        by_device: dict[str, list[tuple[str, int]]] = {}
        for route in self.routes:
            by_device.setdefault(route.device, []).append((route.destination, route.tag))
        return tuple(MarkerRules(device, tuple(sorted(rules, key=lambda rule: _address_key(rule[0]))))
                     for device, rules in sorted(by_device.items(), key=lambda item: model.RDMA_DEVICES.index(item[0])))

    def marker(self, device: str) -> MarkerRules | None:
        for rules in self.markers:
            if rules.device == device:
                return rules
        return None

    @property
    def filter_netdevs(self) -> tuple[str, ...]:
        return tuple(netdev for netdev in model.FABRIC_NETDEVS
                     if any(relay_filter.in_netdev == netdev for relay_filter in self.filters))

    def route_to(self, destination: str) -> OriginRoute | None:
        for route in self.routes:
            if route.destination == destination:
                return route
        return None

    def filter_for(self, netdev: str, k: int) -> RelayFilter | None:
        for relay_filter in self.filters:
            if relay_filter.in_netdev == netdev and relay_filter.k == k:
                return relay_filter
        return None

    def to_json(self) -> dict[str, object]:
        return {"position": self.position, "name": self.name, "group": self.group,
                "routes": [route.to_json() for route in self.routes],
                "filters": [relay_filter.to_json() for relay_filter in self.filters],
                "markers": [rules.to_json() for rules in self.markers]}


@dataclasses.dataclass(frozen=True)
class RelayQueue:
    """One hairpin queue of a relay (ingress device to egress device) and the lanes through it."""

    position: int
    in_netdev: str
    out_netdev: str
    lanes: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class GroupPlan:
    """The relay plan of the topology's one group."""

    group: GroupSpec
    layout: model.Layout
    sircl: model.GroupRoutes
    relay_egress: str
    max_relays: int
    hosts: tuple[HostPlan, ...]
    queues: tuple[RelayQueue, ...]
    busiest_queue: int
    load_factor: float
    digest: str

    @property
    def per_peer_bytes(self) -> int | None:
        """The largest per-peer op size that keeps the busiest relay queue at 75 %; None without relays."""
        if not self.load_factor:
            return None
        return int(RELAY_QUEUE_SHARE * HAIRPIN_QUEUE_BYTES / self.load_factor)

    def host(self, position: int) -> HostPlan | None:
        for host in self.hosts:
            if host.position == position:
                return host
        return None

    def to_json(self) -> dict[str, object]:
        return {
            "group": self.group.label, "members": list(self.group.members), "cables": list(self.group.cables),
            "relay_egress": self.relay_egress, "max_relays": self.max_relays, "digest": self.digest,
            "route_maps": [self.sircl.route_text(rank) for rank in range(self.sircl.world)],
            "relay_queues": [{"position": queue.position, "in": queue.in_netdev, "out": queue.out_netdev,
                              "lanes": list(queue.lanes)} for queue in self.queues],
            "busiest_queue_lanes": self.busiest_queue, "load_factor": self.load_factor,
            "per_peer_bytes": self.per_peer_bytes, "hosts": [host.to_json() for host in self.hosts],
        }


CONSTRAINTS = (
    "relay hairpin queue: 512 KiB per (relay, ingress device, egress device), the device maximum; it cannot "
    "pause its sender, so frames beyond it are dropped",
    "relay egress: sibling function on paths; same-function egress on a full ring",
    "queue pairs use flow label 0",
    "tags are EtherTypes chosen by destination address, never by UDP port (the sender NIC computes the ICRC "
    "after the RDMA-TX rewrite)",
)


# -- derivation ---------------------------------------------------------------------------------


def _carry(path: tuple[model.Step, ...], secondary: bool, facts: Facts, relay_egress: str,
           routes_at: dict[int, dict[str, OriginRoute]],
           filters_at: dict[int, dict[tuple[str, int], RelayFilter]], label: str) -> None:
    hops = len(path)
    origin, target = path[0].src, path[-1].dst
    local = model.role_at(path[0].src_port, secondary)
    remote = model.role_at(path[-1].dst_port, secondary)
    first = model.role_at(path[0].dst_port, secondary)
    route = OriginRoute(
        destination=facts.port(target, remote.netdev).address, netdev=local.netdev, device=local.device,
        source=facts.port(origin, local.netdev).address, next_mac=facts.port(path[0].dst, first.netdev).mac,
        tag=tag(hops - 1), peer=target, peer_netdev=remote.netdev, hops=hops,
        relays=tuple(step.dst for step in path[:-1]), cables=tuple(step.text for step in path),
        owned_cables=tuple(step.cable.text for step in path))
    existing = routes_at[origin].get(route.destination)
    if existing is not None and existing != route:
        raise FabricError(f"group {label}: two paths from host {origin} reach {route.destination} differently")
    routes_at[origin][route.destination] = route
    for index in range(hops - 1):
        arrive, leave = path[index], path[index + 1]
        relay = arrive.dst
        if leave.src != relay or leave.src_port != 1 - arrive.dst_port:
            raise FabricError(f"group {label}: the path {route.cables} does not pass through host {relay}")
        in_role = model.role_at(arrive.dst_port, secondary)
        out_role = model.role_at(leave.src_port, secondary if relay_egress == "same" else not secondary)
        next_role = model.role_at(leave.dst_port, secondary)
        relay_filter = RelayFilter(
            in_netdev=in_role.netdev, k=hops - 1 - index, out_netdev=out_role.netdev,
            next_mac=facts.port(leave.dst, next_role.netdev).mac, next_position=leave.dst,
            next_netdev=next_role.netdev, direction="cw" if leave.src_port == 0 else "ccw",
            lane_class=model.class_name(secondary))
        key = (relay_filter.in_netdev, relay_filter.k)
        known = filters_at[relay].get(key)
        if known is not None and known != relay_filter:
            raise FabricError(f"group {label}: host {relay} would need two relay filters for TAG({relay_filter.k}) "
                              f"on {relay_filter.in_netdev}")
        filters_at[relay][key] = relay_filter


def _digest(label: str, relay_egress: str, hosts: Sequence[HostPlan]) -> str:
    # Hash the canonical installed objects and group metadata.
    canonical = json.dumps({"group": label, "relay_egress": relay_egress,
                            "sparks": [host.to_json() for host in hosts]}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def build_group_plan(group: GroupSpec, facts: Facts, *, relay_egress: str,
                     max_relays: int = model.DEFAULT_MAX_RELAYS) -> GroupPlan:
    """The relay plan of one group (module docstring), checked by :func:`trace` on every origin route."""
    if relay_egress not in ("same", "sibling"):
        raise FabricError(f"relay egress must be same or sibling, got {relay_egress!r}")
    if not 0 <= max_relays <= MAX_TAG:
        raise FabricError(f"the relay limit must be between 0 and {MAX_TAG} (one tag per relay count)")
    layout = group.layout
    members = layout.positions
    routes_at: dict[int, dict[str, OriginRoute]] = {position: {} for position in members}
    filters_at: dict[int, dict[tuple[str, int], RelayFilter]] = {position: {} for position in members}
    for origin in members:
        for target in members:
            if origin == target:
                continue
            for path in layout.fabric.shortest_paths(origin, target):
                if len(path) < 2:
                    continue
                if len(path) - 1 > max_relays:
                    raise FabricError(
                        f"group {group.label}: host {target} is {len(path)} cables from host {origin}, "
                        f"{len(path) - 1} relays; the configured limit is {max_relays} ("
                        "--max-relays raises it, research-only)")
                for secondary in (False, True):
                    _carry(path, secondary, facts, relay_egress, routes_at, filters_at, group.label)
    sircl = model.derive_routes(layout, 2)
    lanes_of: dict[tuple[int, str], list[str]] = {}
    for rank, peers in enumerate(sircl.ranks):
        origin = layout.positions[rank]
        for peer, lanes in sorted(peers.items()):
            for lane in lanes:
                if lane.hops < 2:
                    continue
                destination = facts.port(layout.positions[peer], lane.remote.netdev).address
                route = routes_at[origin].get(destination)
                if (route is None or route.netdev != lane.local.netdev or route.cables != lane.cables
                        or route.tag != tag(len(lane.relays))):
                    raise FabricError(f"group {group.label}: lane {lane.lane} from rank {rank} to rank {peer} "
                                      f"({lane.local.device}, {lane.cables}) is not carried by the relay plan")
                lanes_of.setdefault((origin, destination), []).append(f"rank {rank}->{peer} lane {lane.lane}")
    hosts = []
    for position in group.order:
        planned = sorted(routes_at[position].values(),
                         key=lambda r: (model.FABRIC_NETDEVS.index(r.netdev), r.hops, _address_key(r.destination)))
        planned = [dataclasses.replace(route, lanes=tuple(lanes_of.get((position, route.destination), ())))
                   for route in planned]
        filters = sorted(filters_at[position].values(),
                         key=lambda f: (model.FABRIC_NETDEVS.index(f.in_netdev), f.k))
        hosts.append(HostPlan(position, facts.name(position), group.label, tuple(planned), tuple(filters)))
    by_position = {host.position: host for host in hosts}
    for host in hosts:
        for route in host.routes:
            result = trace(by_position, facts, group.ring_size, host.position, route.destination)
            if result.outcome != "delivered" or result.hops[-1] != (route.peer, route.peer_netdev):
                raise FabricError(f"group {group.label}: the route from {host.name} to {route.destination} does "
                                  f"not deliver through the plan's relays ({result.outcome}: {result.detail})")
            outside = [position for position, _ in result.hops if position not in members]
            if outside:
                raise FabricError(f"group {group.label}: the route from {host.name} to {route.destination} "
                                  f"crosses hosts {outside} outside the group")
    maps = [sircl.route_map(rank) for rank in range(layout.world)]
    queues = []
    for (relay, secondary, egress_port), members_of_queue in sorted(model.relay_queues(layout, maps).items()):
        in_role = model.role_at(1 - egress_port, secondary)
        out_role = model.role_at(egress_port, secondary if relay_egress == "same" else not secondary)
        queues.append(RelayQueue(relay, in_role.netdev, out_role.netdev,
                                 tuple(f"rank {r}->{p} lane {lane}" for r, p, lane in members_of_queue)))
    busiest, load = model.relay_load(sircl)
    return GroupPlan(group, layout, sircl, relay_egress, max_relays, tuple(hosts), tuple(queues), busiest, load,
                     _digest(group.label, relay_egress, hosts))


def build_plan(group: GroupSpec, facts: Facts, *, max_relays: int = model.DEFAULT_MAX_RELAYS) -> GroupPlan:
    """The plan of a topology's group, checked for isolation."""
    problems = model.isolation_problems([model.derive_routes(group.layout, 2)])
    plan = build_group_plan(group, facts, relay_egress=group.relay_egress, max_relays=max_relays)
    problems += isolation_problems([plan])
    if problems:
        raise FabricError("isolation: " + "; ".join(problems))
    return plan


# -- checks ----------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Trace:
    """Where a frame sent on an origin route went: ``delivered``, ``dropped``, ``misdelivered``,
    ``unrouted`` or ``looped``; ``hops`` lists the (position, receiving device) of every cable crossed."""

    outcome: str
    hops: tuple[tuple[int, str], ...]
    detail: str = ""


def trace(hosts: Mapping[int, HostPlan], facts: Facts, ring_size: int, origin: int, destination: str,
          *, limit: int = 2 * MAX_TAG) -> Trace:
    """Follow a frame from ``origin`` to ``destination`` through the plan's objects on the cabling.

    Port 0 of position ``i`` is cabled to port 1 of position ``i + 1``. A frame
    is received by the function whose MAC it carries on the far end of the
    cable; a tagged frame continues only through a relay filter for its tag.
    """
    host = hosts.get(origin)
    route = host.route_to(destination) if host is not None else None
    if route is None:
        return Trace("unrouted", (), f"no origin route to {destination} at position {origin}")
    mac, ethertype, here, out_netdev = route.next_mac, route.tag, origin, route.netdev
    hops: list[tuple[int, str]] = []
    for _ in range(limit):
        port = model.role_of_netdev(out_netdev).port
        there = (here + 1) % ring_size if port == 0 else (here - 1) % ring_size
        receiver = facts.find_mac(mac)
        if receiver is None or receiver[0] != there or model.role_of_netdev(receiver[1]).port != 1 - port:
            return Trace("misdelivered", tuple(hops), f"a frame for {mac} leaves position {here} port {port}; "
                         f"the cable ends at position {there} port {1 - port}")
        hops.append(receiver)
        netdev = receiver[1]
        k = relays_left(ethertype)
        if k is None:
            return Trace("dropped", tuple(hops), f"EtherType 0x{ethertype:04x} is not a relay tag")
        if k == 0:
            if facts.port(there, netdev).address == destination:
                return Trace("delivered", tuple(hops))
            return Trace("misdelivered", tuple(hops), f"IPv4 frame for {destination} reached {netdev} at {there}")
        relay = hosts.get(there)
        relay_filter = relay.filter_for(netdev, k) if relay is not None else None
        if relay_filter is None:
            return Trace("dropped", tuple(hops), f"no relay filter for EtherType 0x{ethertype:04x} on {netdev} "
                         f"at position {there}")
        mac, ethertype, here, out_netdev = relay_filter.next_mac, tag(k - 1), there, relay_filter.out_netdev
    return Trace("looped", tuple(hops), f"more than {limit} cables")


def isolation_problems(plans: Sequence[GroupPlan]) -> list[str]:
    """Breaks of the isolation rules of one or more group plans; empty when isolated."""
    problems = []
    for plan in plans:
        members = set(plan.group.members)
        owned = set(plan.group.cables)
        fabric = plan.layout.fabric
        for host in plan.hosts:
            if host.position not in members:
                problems.append(f"{plan.group.label}: {host.name} is not a member but has objects")
            for route in host.routes:
                if route.peer not in members:
                    problems.append(f"{plan.group.label}: {host.name} routes to {route.destination} of host "
                                    f"{route.peer}, outside the group")
                if not set(route.relays) <= members or not set(route.owned_cables) <= owned:
                    problems.append(f"{plan.group.label}: the route from {host.name} to {route.destination} "
                                    f"crosses {route.cables}, outside the group's cables {sorted(owned)}")
            for relay_filter in host.filters:
                for netdev in (relay_filter.in_netdev, relay_filter.out_netdev):
                    port = model.role_of_netdev(netdev).port
                    if fabric.cable_at(host.position, port) is None:
                        problems.append(f"{plan.group.label}: {host.name} relays through {netdev} (port {port}), "
                                        "whose cable the group does not own")
                if relay_filter.next_position not in members:
                    problems.append(f"{plan.group.label}: {host.name} relays toward host "
                                    f"{relay_filter.next_position}, outside the group")
    problems += model.isolation_problems([plan.sircl for plan in plans])
    return problems


# -- rendering ----------------------------------------------------------------------------------------


def render_text(plan: GroupPlan, facts: Facts, *, details: bool = True) -> str:
    group = plan.group
    lines = [f"layout {group.name} on {len(group.members)} host(s) cabled as "
             f"{'a ring' if group.whole_ring else 'a path'}: group {group.label}, relay egress "
             f"{plan.relay_egress}"
             + (" (symbolic facts: placeholders stand for addresses and MACs)" if facts.symbolic else "")]
    for note in CONSTRAINTS:
        lines.append(f"  constraint: {note}")
    counts = (sum(len(h.routes) for h in plan.hosts), sum(len(h.filters) for h in plan.hosts),
              sum(len(h.markers) for h in plan.hosts))
    lines.append(f"group {group.label}: hosts {list(group.members)}, cables "
                 f"{list(group.cables) or 'none'}; {counts[0]} routes, {counts[1]} relay filters, "
                 f"{counts[2]} markers; plan {plan.digest}")
    if plan.per_peer_bytes is None:
        lines.append("  relay queues: none (no lane crosses a relay)")
    else:
        lines.append(f"  relay queues: busiest {plan.busiest_queue} lanes, load factor "
                     f"{plan.load_factor:g}; keep per-peer op sizes at or below {plan.per_peer_bytes} bytes "
                     f"({RELAY_QUEUE_SHARE:.0%} of {HAIRPIN_QUEUE_BYTES >> 10} KiB / {plan.load_factor:g})")
    for rank in range(plan.sircl.world):
        lines.append(f"  rank {rank} (position {plan.layout.positions[rank]}): "
                     f"NET_SIRCL_ROUTES={plan.sircl.route_text(rank)}")
    if not details:
        return "\n".join(lines)
    for queue in plan.queues:
        lines.append(f"  queue {facts.name(queue.position)} {queue.in_netdev} -> {queue.out_netdev}: "
                     f"{len(queue.lanes)} lanes")
    for host in plan.hosts:
        lines.append(f"  {host.name} (position {host.position}): {len(host.routes)} routes, "
                     f"{len(host.filters)} relay filters, {len(host.markers)} markers")
        for route in host.routes:
            carried = f"; {', '.join(route.lanes)}" if route.lanes else "; not a lane"
            lines.append(f"    route {route.destination}/32 dev {route.netdev} src {route.source} neighbour "
                         f"{route.next_mac} -> {facts.name(route.peer)} {route.peer_netdev}, {route.hops} hops "
                         f"through {list(route.relays)}, tag 0x{route.tag:04x}{carried}")
        for rules in host.markers:
            lines.append(f"    marker {rules.device}: "
                         + " ".join(f"{destination}=0x{value:04x}" for destination, value in rules.rules))
        for relay_filter in host.filters:
            lines.append(f"    filter {relay_filter.in_netdev} pref {relay_filter.pref} "
                         f"0x{relay_filter.protocol:04x} -> 0x{relay_filter.new_type:04x}, dst {relay_filter.next_mac} "
                         f"({facts.name(relay_filter.next_position)} {relay_filter.next_netdev}), out "
                         f"{relay_filter.out_netdev} ({relay_filter.direction}, {relay_filter.lane_class} lanes)")
    return "\n".join(lines)
