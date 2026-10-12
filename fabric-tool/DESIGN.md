# fabric-tool design

The tool converts one topology file into a relay plan, binds it to host facts,
reads host state and computes the changes needed to match the plan.

## Inputs and planning

`fabric_tool/topology.py` reads hosts, cables and function names using the
same conventions as `transport-core/src/topology.c`. `model.py` derives
shortest paths and lanes. A layout means ordered hosts and cables; a group is
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

## Execution and ownership

OFFLINE commands compute from files without host reads. READ-ONLY REMOTE
commands inspect hosts through SSH. MUTATES HOST commands preview changes
unless `--execute` is supplied. No command assigns interface addresses.

Ownership marks are routing protocol 82, ingress preferences 11â€“17, marker
process identities and `/run/fabric-table/relay-state.json`. The tool refuses
a foreign ownership record and unrelated routes to planned destinations.
Apply can leave partial state on a command failure; inspect and reconcile it.
Boot apply restores missing objects without removing any. The systemd unit
is a oneshot operation rather than marker supervision.

See the installed guide `share/doc/net-sircl/docs/fabric.md` for the relay
mechanism and command sequence. `HARDWARE-TEST-PLAN.md` describes hardware
qualification. An earlier revision ran the ring/path table exercise on four
DGX Sparks; the script fixes in this version have only been tested on CPU.
The tool remains research-only. Standalone tests cover planning, facts, command forms,
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
