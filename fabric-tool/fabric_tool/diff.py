"""Comparison of one host's objects with its plan, and the script that reconciles them.

Absorbed from ``sparkring_sircl.fabric.diff`` (SparkRing
``spark_transport/sircl/sparkring_sircl/fabric/diff.py``); see NOTICE.
:func:`compare_up` compares a host with its group's plan; :func:`compare_down`
lists everything ``remove`` deletes. Both return a :class:`HostDiff`: the
changes (``add``, ``remove``, ``replace``; ``mark`` puts the ownership mark on
an unowned object equal to the plan; ``keep``; ``write`` and ``remove`` of the
record), the blockers that stop ``--execute``, notes, and the apply script.

Rules:

- a host whose record names another group is left alone: ``apply`` and
  ``remove`` stop there, so one group's operation never changes another
  group's objects;
- a host that holds SparkRing's installer record
  (``/run/sparkring-fabric/relay-state.json``) is left alone: SparkRing's
  installer owns objects there; remove its table with SparkRing's own command;
- unowned relay routes and neighbours (no protocol 82) are changed or removed
  only with ``--adopt``; without it, ``apply`` and ``remove`` stop on a host
  that holds any, and a dry run reports what ``--adopt`` would do. A route
  that is not a relay route but has a planned destination is a conflict and is
  never touched;
- relay filters and marker processes are the tool's by their reserved
  identifiers and are replaced when they differ from the plan;
- an ingress qdisc is added where filters need one; ``remove`` deletes only a
  qdisc the record says the tool added, and only when no filter remains.

Script order: qdiscs, relay filters, neighbours, routes, marker restarts,
removal of stale routes and neighbours, qdisc removal, record. A changed
filter or marker is deleted and re-created, which interrupts the lanes that
use it for well under a second; an unchanged object is not touched.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence

from . import commands, model
from .plan import GroupPlan, HostPlan
from .state import FilterEntry, HostState, NeighbourEntry, RouteEntry

HOST_ACTIONS = ("add", "remove", "replace")


@dataclasses.dataclass(frozen=True)
class Change:
    kind: str                 # route, neighbour, filter, qdisc, marker, record
    action: str               # add, remove, replace, mark, keep, write
    key: str
    detail: str
    unowned: bool = False     # acts on an object without the ownership mark

    @property
    def host_change(self) -> bool:
        """Whether the change alters forwarding state (marks and the record do not)."""
        return self.kind != "record" and self.action in HOST_ACTIONS

    def text(self) -> str:
        symbol = {"add": "+", "remove": "-", "replace": "~", "mark": "*", "keep": "=", "write": "+"}[self.action]
        owner = " [unowned]" if self.unowned else ""
        return f"{symbol} {self.action} {self.kind} {self.key}: {self.detail}{owner}"


@dataclasses.dataclass
class HostDiff:
    position: int
    name: str
    group: str
    mode: str
    changes: list[Change] = dataclasses.field(default_factory=list)
    blockers: list[str] = dataclasses.field(default_factory=list)
    notes: list[str] = dataclasses.field(default_factory=list)
    commands: list[str] = dataclasses.field(default_factory=list)
    needs_adopt: str | None = None   # why --execute also needs --adopt (unowned objects would change)

    @property
    def blocked(self) -> bool:
        """Whether ``--execute`` must refuse this host."""
        return bool(self.blockers) or self.needs_adopt is not None

    def count(self, action: str) -> int:
        return sum(1 for change in self.changes if change.action == action)

    @property
    def host_changes(self) -> list[Change]:
        return [change for change in self.changes if change.host_change]

    @property
    def pending(self) -> list[Change]:
        """Every change apply would make (anything but ``keep``)."""
        return [change for change in self.changes if change.action != "keep"]

    @property
    def script(self) -> str | None:
        return "\n".join(self.commands) + "\n" if self.commands else None


@dataclasses.dataclass
class _Script:
    qdisc_add: list[str] = dataclasses.field(default_factory=list)
    filter_del: list[str] = dataclasses.field(default_factory=list)
    filter_add: list[str] = dataclasses.field(default_factory=list)
    neigh_set: list[str] = dataclasses.field(default_factory=list)
    route_set: list[str] = dataclasses.field(default_factory=list)
    marker_stop: list[str] = dataclasses.field(default_factory=list)
    marker_start: list[str] = dataclasses.field(default_factory=list)
    route_del: list[str] = dataclasses.field(default_factory=list)
    neigh_del: list[str] = dataclasses.field(default_factory=list)
    qdisc_del: list[str] = dataclasses.field(default_factory=list)
    record: list[str] = dataclasses.field(default_factory=list)

    def lines(self, hostname: str) -> list[str]:
        body = (self.qdisc_add + self.filter_del + self.filter_add + self.neigh_set + self.route_set
                + self.marker_stop + self.marker_start + self.route_del + self.neigh_del + self.qdisc_del
                + self.record)
        if not body:
            return []
        settle = [commands.SETTLE] if self.marker_start else []
        return [commands.HEADER, commands.guard(hostname), *body, *settle]


def _route_equal(entry: RouteEntry, destination: str, netdev: str, source: str) -> bool:
    return (entry.dev == netdev and entry.source == source and entry.scope == "link" and not entry.gateway
            and entry.metric == 0 and entry.kind == "unicast" and entry.destination == destination)


def _route_removal(entry: RouteEntry) -> str:
    return commands.route_del(entry.destination, entry.dev, protocol=entry.protocol, metric=entry.metric)


def _filter_removal(entries: Sequence[FilterEntry]) -> list[str]:
    if len(entries) == 1 and entries[0].protocol is not None and entries[0].handle is not None:
        entry = entries[0]
        return [commands.filter_del(entry.netdev, entry.pref, protocol=entry.protocol, handle=entry.handle)]
    return [commands.filter_del(entries[0].netdev, entries[0].pref)]


def _common_blockers(diff: HostDiff, state: HostState, *, check_hostname: bool) -> bool:
    """Blockers that make the comparison meaningless; True when the host cannot be compared."""
    if not state.reachable:
        diff.blockers.append(f"unreachable: {state.error}")
        return True
    if state.parse_problems:
        diff.blockers.extend(f"read: {problem}" for problem in state.parse_problems)
    if check_hostname and state.hostname != state.name:
        diff.blockers.append(f"the host at {state.ssh} is named {state.hostname or 'nothing'}, the topology "
                             f"lists {state.name}")
    if state.record_problem:
        diff.blockers.append(state.record_problem)
    if state.sparkring_record is not None:
        diff.blockers.append(f"SparkRing's relay table is installed (its record {commands.SPARKRING_RECORD_PATH} "
                             f"names group {state.sparkring_record.get('group')}); remove it with SparkRing's own "
                             "command first")
    if state.record is not None and state.record.get("group") != diff.group:
        diff.blockers.append(f"the host belongs to group {state.record.get('group')} (layout "
                             f"{state.record.get('layout')}); remove that group with `remove` first")
    return False


def _adopt_blocker(diff: HostDiff, adopt: bool) -> None:
    unowned = [change for change in diff.changes if change.unowned and change.action != "keep"]
    if unowned and not adopt:
        diff.needs_adopt = (f"{len(unowned)} relay route and neighbour change(s) act on objects without the "
                            "ownership mark (installed by other tooling); --adopt takes them over")


def _marker_changes(diff: HostDiff, script: _Script, state: HostState, host: HostPlan | None,
                    marker: commands.MarkerConfig) -> None:
    handled = set()
    for device in model.RDMA_DEVICES:
        planned = host.marker(device) if host is not None else None
        wanted = frozenset(planned.rules) if planned is not None else frozenset()
        present = [process for process in state.markers if process.device == device]
        handled.update(process.pid for process in present)
        rules_text = " ".join(f"{d}=0x{v:04x}" for d, v in (planned.rules if planned else ()))
        if wanted and len(present) == 1 and present[0].rules == wanted and present[0].managed \
                and present[0].problem is None:
            diff.changes.append(Change("marker", "keep", device, f"pid {present[0].pid}, {len(wanted)} rules"))
            continue
        for process in present:
            script.marker_stop.extend(commands.marker_stop(marker, process.pid))
        if wanted:
            script.marker_start.append(commands.marker_start(marker, device, planned.rules if planned else ()))
            before = "; ".join(process.text() for process in present)
            diff.changes.append(Change("marker", "replace" if present else "add", device,
                                       f"{before} -> {rules_text}" if present else rules_text))
        elif present:
            diff.changes.append(Change("marker", "remove", device, "; ".join(p.text() for p in present)))
    for process in state.markers:
        if process.pid not in handled:
            script.marker_stop.extend(commands.marker_stop(marker, process.pid))
            diff.changes.append(Change("marker", "remove", f"pid {process.pid}", process.text()))
    if script.marker_start and state.marker_binary is None:
        diff.blockers.append(f"the marker executable {marker.path} is missing; build it with `install-marker`")


def _record(host: HostPlan, plan: GroupPlan, layout_name: str, marker: commands.MarkerConfig,
            qdiscs: Sequence[str]) -> dict[str, object]:
    return {"schema": commands.RECORD_SCHEMA, "host": host.name, "position": host.position,
            "layout": layout_name, "group": plan.group.label, "members": list(plan.group.members),
            "relay_egress": plan.relay_egress, "plan": plan.digest, "marker": marker.path,
            "qdiscs": sorted(qdiscs)}


def compare_up(state: HostState, host: HostPlan, plan: GroupPlan, *, layout_name: str,
               marker: commands.MarkerConfig, adopt: bool, check_hostname: bool = True) -> HostDiff:
    """What ``apply`` changes on one host to install its group's plan."""
    diff = HostDiff(host.position, host.name, plan.group.label, "up")
    if _common_blockers(diff, state, check_hostname=check_hostname):
        return diff
    script = _Script()
    removed_filters = {netdev: 0 for netdev in model.FABRIC_NETDEVS}

    # Relay filters (reserved preferences) and the ingress qdiscs they need.
    added_qdiscs = []
    for netdev in model.FABRIC_NETDEVS:
        planned = {relay_filter.pref: relay_filter for relay_filter in host.filters
                   if relay_filter.in_netdev == netdev}
        present: dict[int, list[FilterEntry]] = {}
        for entry in state.reserved_filters(netdev):
            present.setdefault(entry.pref, []).append(entry)
        if planned and state.ingress_qdisc.get(netdev) is None:
            script.qdisc_add.append(commands.qdisc_add(netdev))
            added_qdiscs.append(netdev)
            diff.changes.append(Change("qdisc", "add", netdev, "ingress"))
        for pref, relay_filter in sorted(planned.items()):
            entries = present.pop(pref, [])
            wanted = (f"0x{relay_filter.protocol:04x} -> 0x{relay_filter.new_type:04x}, dst "
                      f"{relay_filter.next_mac}, out {relay_filter.out_netdev}")
            if len(entries) == 1 and entries[0].matches(relay_filter):
                diff.changes.append(Change("filter", "keep", f"{netdev} pref {pref}", wanted))
                continue
            if entries:
                script.filter_del.extend(_filter_removal(entries))
                removed_filters[netdev] += len(entries)
            script.filter_add.append(commands.filter_add(relay_filter))
            before = "; ".join(entry.text() for entry in entries)
            diff.changes.append(Change("filter", "replace" if entries else "add", f"{netdev} pref {pref}",
                                       f"{before} -> {wanted}" if entries else wanted))
        for pref, entries in sorted(present.items()):
            script.filter_del.extend(_filter_removal(entries))
            removed_filters[netdev] += len(entries)
            diff.changes.append(Change("filter", "remove", f"{netdev} pref {pref}",
                                       "; ".join(entry.text() for entry in entries)))

    # Origin routes and their permanent neighbours.
    planned_routes = {route.destination: route for route in host.routes}
    host_routes: dict[str, list[RouteEntry]] = {}
    for entry in state.routes:
        if entry.prefixlen == 32:
            host_routes.setdefault(entry.destination, []).append(entry)
    for destination, route in planned_routes.items():
        entries = host_routes.get(destination, [])
        candidates = [entry for entry in entries if entry.owned or state.is_relay_route(entry)]
        foreign = [entry for entry in entries if entry not in candidates]
        want = f"dev {route.netdev} src {route.source} -> position {route.peer} {route.peer_netdev}"
        if foreign:
            diff.blockers.append(f"route {destination}/32 is held by a route that is not a relay route "
                                 f"({foreign[0].text()}); it is never touched")
            continue
        # `ip route replace` rewrites the metric-0 route in place; routes at other metrics are removed.
        equal = next((entry for entry in candidates
                      if _route_equal(entry, destination, route.netdev, route.source)), None)
        in_place = equal or next((entry for entry in candidates if entry.metric == 0), None)
        for entry in candidates:
            if entry is not in_place:
                script.route_del.append(_route_removal(entry))
                diff.changes.append(Change("route", "remove", f"{destination}/32", entry.text(), not entry.owned))
        if equal is not None:
            action = "keep" if equal.owned else "mark"
            diff.changes.append(Change("route", action, f"{destination}/32", want, not equal.owned))
            if action == "mark":
                script.route_set.append(commands.route_replace(destination, route.netdev, route.source))
        else:
            script.route_set.append(commands.route_replace(destination, route.netdev, route.source))
            diff.changes.append(Change("route", "replace" if in_place else "add", f"{destination}/32",
                                       f"{in_place.text()} -> {want}" if in_place else want,
                                       bool(in_place and not in_place.owned)))
    for entry in state.relay_routes():
        if entry.destination not in planned_routes:
            script.route_del.append(_route_removal(entry))
            diff.changes.append(Change("route", "remove", f"{entry.destination}/32", entry.text(), not entry.owned))

    planned_neighbours = {(route.destination, route.netdev): route.next_mac for route in host.routes}
    present_neighbours: dict[tuple[str, str], NeighbourEntry] = {}
    for entry in state.relay_neighbours():
        present_neighbours[(entry.destination, entry.dev)] = entry
    for (destination, netdev), mac in planned_neighbours.items():
        entry = present_neighbours.pop((destination, netdev), None)
        if entry is not None and entry.mac == mac.lower():
            action = "keep" if entry.owned else "mark"
            diff.changes.append(Change("neighbour", action, f"{destination} dev {netdev}", mac, not entry.owned))
            if action == "mark":
                script.neigh_set.append(commands.neigh_replace(destination, mac, netdev))
            continue
        script.neigh_set.append(commands.neigh_replace(destination, mac, netdev))
        diff.changes.append(Change("neighbour", "replace" if entry else "add", f"{destination} dev {netdev}",
                                   f"{entry.mac} -> {mac}" if entry else mac, bool(entry and not entry.owned)))
    for (destination, netdev), entry in sorted(present_neighbours.items()):
        script.neigh_del.append(commands.neigh_del(destination, netdev))
        diff.changes.append(Change("neighbour", "remove", f"{destination} dev {netdev}", entry.text(),
                                   not entry.owned))

    _marker_changes(diff, script, state, host, marker)

    # Qdiscs the record lists as added by the tool and that no filter needs any more.
    record = state.record if state.record and state.record.get("group") == plan.group.label else None
    recorded = set(record.get("qdiscs", [])) if record else set()
    kept = set(added_qdiscs) | recorded
    for netdev in sorted(recorded):
        remaining = len(state.filters.get(netdev, [])) - removed_filters[netdev]
        if netdev not in host.filter_netdevs and remaining <= 0 and state.ingress_qdisc.get(netdev) == "ingress":
            script.qdisc_del.append(commands.qdisc_del(netdev))
            kept.discard(netdev)
            diff.changes.append(Change("qdisc", "remove", netdev, "ingress qdisc added by the tool"))
    wanted_record = _record(host, plan, layout_name, marker, sorted(kept))
    if state.record != wanted_record:
        script.record.extend(commands.record_write(wanted_record))
        before = (f"group {state.record.get('group')}, plan {state.record.get('plan')}" if state.record
                  else "none")
        diff.changes.append(Change("record", "write", commands.RECORD_PATH,
                                   f"{before} -> group {plan.group.label}, layout {layout_name}, plan {plan.digest}"))
    for entry in state.foreign_tag_filters():
        diff.notes.append(f"a filter outside the reserved preferences matches relay tag 0x{entry.protocol:04x}: "
                          f"{entry.text()}")
    _adopt_blocker(diff, adopt)
    diff.commands = script.lines(host.name) if diff.pending else []
    return diff


def compare_down(state: HostState, *, group: str, marker: commands.MarkerConfig, adopt: bool,
                 check_hostname: bool = True) -> HostDiff:
    """What ``remove`` deletes from one host: every object the tool owns there."""
    diff = HostDiff(state.position, state.name, group, "down")
    if _common_blockers(diff, state, check_hostname=check_hostname):
        return diff
    script = _Script()
    removed_filters = {netdev: 0 for netdev in model.FABRIC_NETDEVS}
    for netdev in model.FABRIC_NETDEVS:
        present: dict[int, list[FilterEntry]] = {}
        for entry in state.reserved_filters(netdev):
            present.setdefault(entry.pref, []).append(entry)
        for pref, entries in sorted(present.items()):
            script.filter_del.extend(_filter_removal(entries))
            removed_filters[netdev] += len(entries)
            diff.changes.append(Change("filter", "remove", f"{netdev} pref {pref}",
                                       "; ".join(entry.text() for entry in entries)))
    for entry in state.relay_routes():
        script.route_del.append(_route_removal(entry))
        diff.changes.append(Change("route", "remove", f"{entry.destination}/32", entry.text(), not entry.owned))
    for entry in state.relay_neighbours():
        script.neigh_del.append(commands.neigh_del(entry.destination, entry.dev))
        diff.changes.append(Change("neighbour", "remove", f"{entry.destination} dev {entry.dev}", entry.text(),
                                   not entry.owned))
    _marker_changes(diff, script, state, None, marker)
    recorded = set(state.record.get("qdiscs", [])) if state.record else set()
    for netdev in sorted(recorded):
        remaining = len(state.filters.get(netdev, [])) - removed_filters.get(netdev, 0)
        if remaining <= 0 and state.ingress_qdisc.get(netdev) == "ingress":
            script.qdisc_del.append(commands.qdisc_del(netdev))
            diff.changes.append(Change("qdisc", "remove", netdev, "ingress qdisc added by the tool"))
    if state.record is not None:
        script.record.append(commands.record_remove())
        diff.changes.append(Change("record", "remove", commands.RECORD_PATH,
                                   f"group {state.record.get('group')}, plan {state.record.get('plan')}"))
    _adopt_blocker(diff, adopt)
    diff.commands = script.lines(state.name) if diff.pending else []
    return diff


def compare_boot(state: HostState, host: HostPlan, plan: GroupPlan, *, layout_name: str,
                 marker: commands.MarkerConfig, adopt: bool = False,
                 check_hostname: bool = True) -> HostDiff:
    """What ``apply --boot`` installs on one host: the parts of the plan that are missing.

    The restore semantics of SparkRing's runtime restore: add every missing
    object, replace a differing object in place at its own reserved
    preference, protocol or destination, restart markers whose rules differ or
    that are gone, rewrite the record; remove nothing. ``adopt`` is accepted
    for one signature with ``compare_up``; boot restore never removes, so
    unowned objects are only ever marked in place at their own destination.
    """
    diff = HostDiff(host.position, host.name, plan.group.label, "boot")
    if _common_blockers(diff, state, check_hostname=True):
        return diff
    script = _Script()
    if state.record is not None and state.record.get("group") != plan.group.label:
        # compare_boot keeps another group's record alone; --boot only ever restores its own group.
        return diff
    for netdev in model.FABRIC_NETDEVS:
        planned = {relay_filter.pref: relay_filter for relay_filter in host.filters
                   if relay_filter.in_netdev == netdev}
        if planned and state.ingress_qdisc.get(netdev) is None:
            script.qdisc_add.append(commands.qdisc_add(netdev))
            diff.changes.append(Change("qdisc", "add", netdev, "ingress"))
        present: dict[int, list[FilterEntry]] = {}
        for entry in state.reserved_filters(netdev):
            present.setdefault(entry.pref, []).append(entry)
        for pref, relay_filter in sorted(planned.items()):
            entries = present.get(pref, [])
            wanted = (f"0x{relay_filter.protocol:04x} -> 0x{relay_filter.new_type:04x}, dst "
                      f"{relay_filter.next_mac}, out {relay_filter.out_netdev}")
            if len(entries) == 1 and entries[0].matches(relay_filter):
                diff.changes.append(Change("filter", "keep", f"{netdev} pref {pref}", wanted))
                continue
            if entries:
                # The preference is the tool's; one wrong filter there is replaced, not left.
                script.filter_del.extend(_filter_removal(entries))
            script.filter_add.append(commands.filter_add(relay_filter))
            before = "; ".join(entry.text() for entry in entries)
            diff.changes.append(Change("filter", "replace" if entries else "add", f"{netdev} pref {pref}",
                                       f"{before} -> {wanted}" if entries else wanted))
    for route in host.routes:
        entries = [entry for entry in state.routes if entry.prefixlen == 32
                   and entry.destination == route.destination]
        equal = next((entry for entry in entries if _route_equal(entry, route.destination, route.netdev,
                                                                route.source)), None)
        if equal is not None and equal.owned:
            diff.changes.append(Change("route", "keep", f"{route.destination}/32",
                                       f"dev {route.netdev} src {route.source}"))
            continue
        if entries and not all(entry.owned or state.is_relay_route(entry) for entry in entries):
            diff.notes.append(f"route {route.destination}/32 is held by a route that is not a relay route; "
                              "boot restore leaves it alone")
            continue
        script.route_set.append(commands.route_replace(route.destination, route.netdev, route.source))
        before = "; ".join(entry.text() for entry in entries)
        diff.changes.append(Change("route", "replace" if entries else "add", f"{route.destination}/32",
                                   f"{before} -> dev {route.netdev} src {route.source}" if entries else
                                   f"dev {route.netdev} src {route.source}"))
    for route in host.routes:
        neighbours = [entry for entry in state.relay_neighbours()
                      if (entry.destination, entry.dev) == (route.destination, route.netdev)]
        if len(neighbours) == 1 and neighbours[0].mac == route.next_mac.lower():
            action = "keep" if neighbours[0].owned else "mark"
            diff.changes.append(Change("neighbour", action, f"{route.destination} dev {route.netdev}",
                                       route.next_mac, not neighbours[0].owned))
            if action == "mark":
                script.neigh_set.append(commands.neigh_replace(route.destination, route.next_mac, route.netdev))
            continue
        script.neigh_set.append(commands.neigh_replace(route.destination, route.next_mac, route.netdev))
        before = "; ".join(entry.text() for entry in neighbours)
        diff.changes.append(Change("neighbour", "replace" if neighbours else "add",
                                   f"{route.destination} dev {route.netdev}",
                                   f"{before} -> {route.next_mac}" if neighbours else route.next_mac))
    _marker_changes(diff, script, state, host, marker)
    recorded = set(state.record.get("qdiscs", [])) if state.record else set()
    wanted_record = _record(host, plan, layout_name, marker,
                            sorted(recorded | {netdev for netdev in host.filter_netdevs}))
    if state.record != wanted_record:
        script.record.extend(commands.record_write(wanted_record))
        before = (f"group {state.record.get('group')}, plan {state.record.get('plan')}" if state.record
                  else "none")
        diff.changes.append(Change("record", "write", commands.RECORD_PATH,
                                   f"{before} -> group {plan.group.label}, layout {layout_name}, "
                                   f"plan {plan.digest}"))
    diff.commands = script.lines(host.name) if diff.pending else []
    return diff
