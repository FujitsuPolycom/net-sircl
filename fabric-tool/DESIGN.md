# fabric-tool design

The tool converts one topology file into a relay plan, binds it to host facts,
reads host state and computes the changes needed to match the plan.

## Inputs and planning

`fabric_tool/topology.py` reads hosts, cables and function names using the
same conventions as `transport-core/src/topology.c`: at most 64 hosts, names
of 1 to 63 bytes, a decimal `relay_budget`, and one cable for two hosts under
`cabling ring` as under `cabling path` (a pair joined by two cables is written
with `cabling explicit`). `model.py` derives
shortest paths and lanes. Routes, filters and the delivery trace follow the
topology's cables, so `cabling explicit` may join the hosts in any order and
through either port as long as the cables form one path or one cycle. A layout means ordered hosts and cables; a group is
the hosts managed by one topology file.

`plan.py` carries every shortest path on both function classes. It derives
sender routes, neighbour entries, marker rules and intermediate NIC filters.
It checks that every lane is carried and that paths remain inside the group.
For opposite peers on an even ring the two lanes use opposite directions.
The JSON plan schema is `fabric-tool-plan/v2`; `lanes` names the lane lists.

`facts.py` reads and merges host names, interfaces, MACs, IPv4 addresses and
subnet prefixes. Contradictions and incomplete host coverage block apply.
`commands.py` produces scripts. `state.py` parses host observations;
`diff.py` compares them with the plan; `ops.py` runs the selected operations;
`report.py` formats the result.

The read script never lets a failed read pass for an empty host. It names the
tools missing from the read account's `PATH` and records the exit status of
the address, route, neighbour, qdisc, filter and marker-process reads;
`state.py` turns a missing tool, a failed `ip` read, a `tc` that could not be
executed (exit 126 or 127; other `tc` failures are not judged) and a failed
`pgrep` (exit 1 means no match) into parse problems, which block apply and
remove and fail verify. Only the first block of a section name is used, and a
repeated header is a parse problem. Marker processes are read with
`pgrep -U 0 -u 0`: only root's count.

## Execution and ownership

OFFLINE commands compute from files without host reads. READ-ONLY REMOTE
commands inspect hosts through SSH. MUTATES HOST commands preview changes
unless `--execute` is supplied. No command assigns interface addresses.

Ownership marks are routing protocol 82, ingress preferences 11–17, marker
process identities and `/run/fabric-table/relay-state.json`. The tool refuses
a foreign ownership record and unrelated routes to planned destinations.
Apply can leave partial state on a command failure; inspect and reconcile it.
Boot apply restores missing objects and replaces a planned object that
differs; it removes nothing the plan does not name, including marker
processes of a device without planned rules. The systemd unit
is a oneshot operation rather than marker supervision.

See the installed guide `share/doc/net-sircl/docs/fabric.md` for the relay
mechanism and command sequence. `HARDWARE-TEST-PLAN.md` describes hardware
qualification. Status: research-only. Recorded hardware evidence and its
source identities are listed in `../docs/qualification.md`.
Standalone tests cover planning, facts, command forms,
boot previews and mocked boot operations; full-network simulation and
comparison tests that require another project are not distributed.

## Privileged marker execution

`install-marker` compiles the bundled source, checks every existing parent of
the destination through `/`, installs the executable root-owned under
`/usr/local/libexec/fabric-tool`, and writes its SHA-256 to an adjacent record.
`state.py` reads the digest and ownership observations; `diff.py` blocks apply
when the executable, record or parents are missing, not root-owned, writable
by group or others, or the digest differs. A running marker from another
executable path is replaced during ordinary apply.

`commands.marker_start` repeats the executable and full parent checks before
each launch, including starts from offline scripts and boot restoration.
`log_dir_setup` checks every existing directory from
`/var/lib/fabric-tool/logs` through `/`, then creates missing directories as
root. The launch shell removes the old log entry and creates a new file with
noclobber, so a log symlink is not followed. Failed security checks exit the
host script with status 4. The local digest record detects changes since
installation; it does not authenticate the source or protect against root.


Read-script sections, including route checks, end with an explicit newline so
outputs without a trailing newline cannot consume the following header.
Marker starts use foreground `sudo -n setsid -f` with stdin from `/dev/null`;
the detached root shell creates the log and redirects marker stdout and stderr
there. The SSH caller can finish without waiting for the long-lived marker.
Standalone Bash regression tests check digest/ownership parsing and require
the caller's captured output to close before a simulated marker exits.

## Configuration and reconciliation ownership

Implemented: each operation resolves an immutable `FabricBindings` value before
collecting host observations. Facts, host observations and host plans retain that
value; parsing another topology does not change existing plans or reports.
Device identifiers are validated before script generation and quoted as shell
arguments. Topology parsing has no process-global side effects.

Apply and boot restoration use one resource reconciler. Its policy controls
stale-object removal and adoption requirements. Restoration retains stale
objects; apply reconciles them. Qdisc ownership records contain only interfaces
where the tool added a qdisc, plus previously recorded ownership. Removal leaves
a pre-existing unowned qdisc intact and preserves qdiscs with unrelated filters.
A script that adds qdiscs writes the record before it adds them, so a run that
fails afterwards still owns them. The record's layout name is a digest of the
topology's hosts, cables and functions, never the file's path: the workstation
and the boot unit, which read different copies, agree on it. Objects that carry
the ownership mark on a device outside the fabric's four are noted and left.

`RestoreInvocation` retains execution intent, facts, topology, marker location,
relay limit and optional host selection. The systemd unit serializes its argument
vector using systemd escaping. Offline installation uses the same reconciler
against an empty observation, rather than a separate command sequence.
