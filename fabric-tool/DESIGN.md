# fabric-tool: the relay table of directly cabled DGX Sparks, standalone

Source reference: SparkRing branch `pi/sircl-fabric-tool` at
`852407bd1f946840debce36a7299d303dfceb8cb`. SparkRing paths and line numbers below
refer to that upstream checkout, not files required by this standalone package.

A standalone installer for the **relay table** of DGX Sparks joined by direct
QSFP cables: ConnectX-7 hardware hairpin forwarding rules, the routes that aim
RoCE v2 traffic at them, and the RDMA-TX rules that tag it. Paths and rings of
Sparks reach peers beyond a cable only through that table. The table exists
today in two places, both part of SparkRing:

- the SIRCL relay plan installer, `python -m sparkring_sircl.fabric`
  (`spark_transport/sircl/sparkring_sircl/fabric/`), which the NCCL network
  plugin `net-sircl`, libsircl and SIRCL all rely on;
- SparkRing's runtime restore, `runtime/host/relays.py`, which renders the
  same plan into its fabric record and reinstalls it at boot
  (`sparkring-fabric.service`, `sparkring-relay-marker.service`).

This package carries the table's derivation and installation on its own,
under Python 3's standard library alone, driven by the topology file that
`net-sircl` already reads. Anyone with directly cabled Sparks can install,
verify, remove and persist the table without SparkRing, without SIRCL's
Python package and without vLLM. SparkRing's own fabric path is unchanged;
section "How SparkRing could call this tool" records how it could adopt the
tool later.

Status: implemented, CPU-tested (unit tests and equivalence tests against
`sparkring_sircl.fabric`, plus host-command-simulator runs). The tool has not
run on hardware; `HARDWARE-TEST-PLAN.md` in this directory is the plan for
that validation.

## What SparkRing's fabric code does

Derivation, offline (`sparkring_sircl`):

| Module | What it holds | Lines |
|---|---|---|
| `routes.py` | The four ConnectX-7 function roles (`Role`, `DEFAULT_ROLES`, DGX OS names) 66–88; `role_at` 157–161; cables and fabrics (`Cable` 176, `Step` 203, `Fabric` 218, `shortest_paths` 281, `walk` 312); session layouts 331–386; lane derivation (`derive_lanes` 426, `derive_routes` 487) and route maps 459–484; relay queue load 636–652 and queue membership 671–693; lane isolation 843–861; `DEFAULT_MAX_RELAYS = 3` 59 | 59–861 |
| `ring/plan.py` | `group_layout` 309–347: a group's fabric is the whole ring, or the cables between consecutive members | 309 |
| `fabric/layouts.py` | Groups on a ring (`Group` 49–89), layouts of disjoint groups 92–148, the custom group syntax `0-3;4-7` 151–178, built-in layouts `ring8`, `2xTP4`, `4xTP2` 186–198, the relay-egress default (same for a whole-ring group, sibling otherwise) 181–183 | 41–198 |
| `fabric/plan.py` | The relay plan: origin routes, relay filters, marker rules 254–346; facts (addresses and MACs, real, from a file, or symbolic placeholders) 122–248; `build_group_plan` 497–565 carries every shortest path of both function classes and checks every SIRCL lane is carried; `trace` 594–628 walks every origin route through the plan's objects; isolation 631–673; the queue-load bound (75 % of a 512 KiB hairpin queue) 394–399 and 438–446 | 71–722 |

Installation, over SSH (`fabric/`):

| Module | What it holds | Lines |
|---|---|---|
| `commands.py` | Every shell command, one `bash -s` script per Spark: the read script with `@@section` blocks 111–135, `ip route get` checks 138–148, the hostname guard 154–159, `tc qdisc add/del ingress` 162–167, `tc filter add/del` 170–183, `ip neigh replace/del` 186–192, `ip route replace/del` 195–207, marker start/stop 210–226, the record 229–239, the marker build script 247–262 | 41–262 |
| `state.py` | Parsers for iproute2 5.x and 6.x JSON 231–398 and `HostState` 404–564: which routes, neighbours, filters, markers and qdiscs a Spark holds, and which of them the installer owns (protocol 82, reserved filter preferences 11–17, marker process name) 466–503 | 42–564 |
| `diff.py` | `compare_up` 198–320 and `compare_down` 323–360: the changes, blockers and the exact script per Spark; script order 96–116 (qdiscs, filter deletions, filter additions, neighbours, routes, marker stops, marker starts, stale route and neighbour deletions, qdisc deletions, record; 1.5 s settle after a marker start) | 42–360 |
| `ops.py` | Parallel reads 48–59, the evaluation (facts, cable subnet check 68–83, plan build, comparison) 111–153, `ip route get` lane checks 156–203, parallel apply 206–216 | 31–216 |
| `cli.py` | `plan`, `facts`, `show`, `diff`, `up`, `down`, `marker`; `up`/`down` are dry runs unless `--apply` 130–192, 195–259; exit codes 0 done, 1 changes or failure, 2 invalid input, 3 blockers 20–23 | 1–266 |

Ownership marks, by which the installer removes exactly its own objects
(`commands.py` 11–26): routes and permanent neighbours carry routing protocol
82; relay filters occupy the reserved preferences 11–17 of ingress chain 0 on
the four fabric network devices (preference `10 + k`, handle `k`, protocol
`TAG(k)` = `0x88b4 + k`, `TAG(0)` = IPv4 `0x0800`); marker processes are the
processes named after the marker executable (first 15 characters of its path
basename); the record `/run/sparkring-fabric/relay-state.json` names the
Spark's group, layout, plan digest and the qdiscs the installer added.
Nothing else on a Spark is ever touched: a route to a planned destination
that is not a relay route is a conflict and is left alone (`diff.py` 250–252);
unowned relay-shaped routes and neighbours change only with `--adopt`
(`diff.py` 153–157); filters outside the reserved preferences are reported,
never changed (`state.py` 495–499, `diff.py` 315–317).

SparkRing's runtime restore (`runtime/host/relays.py`) renders the same plan
into `sparkring-relay-plan/v1` (render 190–232), stores each Spark's part in
its `/etc/sparkring/fabric.json`, and reinstalls missing parts at boot
(`restore` 370–427: never removes; `tc filter replace` fixes drift in place;
`sparkring-fabric.service`), with the markers run by
`sparkring-relay-marker.service` from the package binary
(`/usr/lib/sparkring/bin/sparkring-relay-marker`, built from
`spark_transport/fabric/relay_marker.c`). `sparkring fabric verify` is
`runtime/host/fabric.py` `check_local` 228. The RDMA-TX tagging marker of the
SIRCL installer is `fabric/mesh_marker.c`: one process per RDMA device, one
flow rule per routed destination, rewriting the EtherType of RoCE v2 packets
to `TAG(k)`; rules live until the process exits.

## What the tool keeps and drops

Kept, absorbing the derivation and the command forms unchanged:

- the plan derivation: every shortest path of every ordered pair on both
  function classes, origin routes with permanent neighbours, relay filters at
  the reserved preferences, one marker process per RDMA device, the trace and
  isolation checks, the queue-load bound;
- the layout rules that decide the relay egress: `same` for a whole-cycle
  layout, `sibling` otherwise (`layouts.py` 181–183);
- every shell command form and the script order of `diff.py`, the read
  script, the ownership marks, the hostname guard, the adoption rules and the
  `ip route get` lane checks;
- the marker executable source (`mesh_marker.c`), its default path
  `/var/tmp/ring8-mesh-marker`, its process name and its per-device logs.

Dropped:

- the site file (`sircl-ring-site/v1`) and with it SparkRing's host list,
  image, control port and LAN fields; the tool takes the topology file of
  `net-sircl` plus per-host facts;
- layouts of several disjoint groups (`2xTP4`, `4xTP2`, custom `0-3;4-7`):
  the tool installs the table of the one layout its topology file names. A
  topology file that lists a subset of hosts describes exactly one path or
  cycle, which is the one group the tool plans;
- `NET_SIRCL_ROUTES`-style route-map output and the ring harness's plan
  surfaces; the tool prints the table's commands, not session plans;
- the marker process supervision of SparkRing's runtime (a separate systemd
  service with restart): the tool's apply scripts start the markers detached
  (`setsid`), as `sparkring_sircl.fabric up` does; the boot unit re-runs
  apply, which restarts markers whose rules differ or that are gone.

## The topology file is the layout

The tool parses the same file the plugin parses
(`spark_transport/transport-core/src/topology.c` 11–98): `hosts` in position
order, `cabling ring` / `path` / `explicit`, `cable <i>.port<p> <j>.port<q>`,
`port0 <fn> <fn>` and `port1 <fn> <fn>` (the RDMA functions of each port in
lane order; defaults `rocep1s0f0`/`roceP2p1s0f0` and `rocep1s0f1`/
`roceP2p1s0f1`), and `relay_budget` (informational for the tool; the table's
queue bound comes from the measured 512 KiB hairpin queue, `plan.py` 394).

The four functions become the tool's role table in the plugin's lane order:
port 0 lane 0 is the primary function of port 0, port 0 lane 1 its secondary,
and likewise for port 1. Every host must name the same network device for the
same function, as SIRCL requires of a fabric document (`routes.py` 93–135).

The layout the tool plans is the fabric of the topology file's cables, with
the file's hosts as positions 0–N-1:

| Topology | Fabric | Group | Relay egress |
|---|---|---|---|
| `cabling ring` over N hosts | cycle: host i's port 0 to host i+1's port 1, last to first | whole ring (`cycle:0-1-...`) | `same` |
| `cabling path` over N hosts | the same cables without the closing one | `path:0-1-...` | `sibling` |
| `explicit` cables forming a cycle over every host | those cables | whole ring | `same` |
| `explicit` cables forming a path | those cables | `path:...` | `sibling` |
| `explicit` cables that leave a host unreachable, join a host to itself, or put two cables on one port | – | refused | – |

A whole-cycle layout is the universal relay table of that ring (SparkRing's
`ring8` is its N = 8 case): every host reaches every other host's addresses,
each the way it arrives.

## Facts: identity, discovery and verification

A facts file (schema `fabric-tool-facts/v1`) holds, per host, per function
name, the network device, MAC and IPv4 address with prefix length:

```json
{"schema": "fabric-tool-facts/v1", "hosts": [
  {"name": "node-0", "functions": {
    "rocep1s0f0":  {"netdev": "enp1s0f0np0",  "mac": "0c:42:a1:...", "address": "192.0.2.1/30"},
    "roceP2p1s0f0": {"netdev": "enP2p1s0f0np0", "mac": "0c:42:a1:...", "address": "192.0.2.5/30"},
    "rocep1s0f1":  {"netdev": "enp1s0f1np1",  "mac": "0c:42:a1:...", "address": "192.0.2.10/30"},
    "roceP2p1s0f1": {"netdev": "enP2p1s0f1np1", "mac": "0c:42:a1:...", "address": "192.0.2.14/30"}}}]}
```

`fabric-tool facts` produces it by reading hosts: locally, or over SSH with
`--ssh <name>=<target>` (repeatable). Every topology function must be present
with exactly one IPv4 address; every host's function-to-device mapping must
equal every other's; addresses and MACs must be unique across the fabric; and
each cable's two ends must share one subnet per function class, otherwise the
topology's host order is not the cabling and the tool refuses (the check of
`ops.py` 68–83). Facts may also come from the apply read itself when the read
covered every topology host; a `--facts` file is then cross-checked against
what the hosts report.

A host is identified by its host name: `apply`, `verify`, `remove`, `status`
and `install-marker` refuse a host whose `hostname` is not an entry of the
topology file (preflight, and again inside every mutating script through the
guard of `commands.py` 154–159, so a wrong SSH target changes nothing).
`--host <name>` acts on one entry; the default is the entry that matches this
machine's host name.

## The command-line interface

`fabric-tool` is `python3 -m fabric_tool` from this directory (or the entry point
installed by `make install`). Safety classes follow SparkRing's
conventions: OFFLINE (nothing leaves this machine), READ-ONLY REMOTE, MUTATES
HOST. **Every command that changes a host is a dry run unless `--execute` is
given**, and a dry run prints the exact scripts it would run.

| Command | Class | What it does |
|---|---|---|
| `plan` (default) | OFFLINE | The layout's relay plan from the topology file and `--facts`; with complete facts it prints the exact install commands per host, with a facts file they are real, without one they are placeholders. `--json` prints the plan as JSON. |
| `facts` | READ-ONLY REMOTE | Read the hosts' functions and print the facts document (`--output FILE` writes it). |
| `apply` | READ-ONLY REMOTE; MUTATES HOST with `--execute` | Compare each host with its part of the plan, print the changes and scripts, and with `--execute` run them, read again and verify. `--boot` installs missing parts only and never removes (boot semantics). `--adopt` takes over unowned relay routes and neighbours. |
| `verify` | READ-ONLY REMOTE | Read every host, compare with the plan, and resolve every lane destination with `ip route get`. Exit 0 when every host holds exactly its part and every lane resolves. |
| `remove` | READ-ONLY REMOTE; MUTATES HOST with `--execute` | Remove exactly the owned objects: protocol-82 relay routes and neighbours, reserved-preference filters and the qdiscs the record lists, the markers, the record. Unowned objects need `--adopt`. |
| `status` | READ-ONLY REMOTE | What each host holds: routes, neighbours, markers, filters, qdiscs, record, labelled owned/unowned. |
| `install-marker` | READ-ONLY REMOTE; MUTATES HOST with `--execute` | Compile `mesh_marker.c` on the host (rdma-core headers needed) and install it at the marker path. `status` reports a missing marker; `apply` refuses to start one that is missing. |
| `install-boot` | MUTATES HOST with `--execute` | Write `/etc/fabric-table/{topology,facts.json}` and the systemd unit `fabric-table.service` (oneshot, `After=network-online.target`, `WantedBy=multi-user.target`, `ExecStart=fabric-tool apply --boot ...`), enable it, and run one apply to install the table now. Refuses placeholder facts. |
| `remove-boot` | MUTATES HOST with `--execute` | Disable and remove the unit and `/etc/fabric-table/`. The relay table itself stays until `remove`. |

Exit codes: 0 done (verify: hosts hold their part); 1 a read failed, apply or
verify found remaining changes or failed checks; 2 invalid input; 3 blockers
stopped a run or `--execute`.

Refusals, before anything is changed:

- a host that is not a topology entry, or a topology host that cannot be
  reached and identified (hostname mismatch, unreachable);
- facts that are incomplete, place another host's name on a position, or
  place two functions on one address or MAC;
- a cabling whose subnets disagree with the topology's host order;
- a Spark whose record names another group of the same tool (one group's
  operation never changes another group's objects), or whose
  `/run/sparkring-fabric/relay-state.json` exists (SparkRing's installer owns
  objects there; remove its table with SparkRing's own command first);
- a route to a planned destination held by something that is not a relay
  route (never touched);
- unowned relay routes or neighbours without `--adopt`;
- a plan that fails its trace, isolation or relay-limit checks, or a lane
  longer than `--max-relays` (default 3, the qualified limit).

Never touched: any network device outside the topology's four functions (the
command builders refuse other device names, `commands.py` 96–105), any
address, link, qdisc, filter, route or neighbour outside the plan's objects
and the ownership marks, and any route whose destination the plan does not
name.

## Byte-identical effect

For the same facts, the tool's plan objects and commands are the ones
`sparkring_sircl.fabric` produces for the same layout:

- the derivation is the absorbed `build_group_plan`: same carried paths, same
  tags (`TAG(k) = 0x88b4 + k`, `TAG(0)` = IPv4), same preference `10 + k` and
  handle `k`, same route/neighbour/marker objects, same ordering;
- the commands are the absorbed `commands.py` forms: `tc filter add ... flower
  skip_sw action pedit ... pipe action pedit ... pipe action mirred egress
  redirect ...`, `ip route replace .../32 dev ... src ... scope link proto 82`,
  `ip neigh replace ... nud permanent proto 82`, `tc qdisc add/del dev ...
  ingress`, the same marker invocation (`setsid ... --device ... --rule ...
  --managed`), the same read and apply script order and the same settle;
- the default marker path `/var/tmp/ring8-mesh-marker` is SparkRing's
  installer's default, so the marker objects are interchangeable;
- the tests compare, for a ring of four, a ring of eight, a path of four and
  a path of three, the planned rule and route sets and the exact command
  strings of both tools (tests `test_equivalence.py`).

What differs on purpose, none of it forwarding state: the record lives at
`/run/fabric-table/relay-state.json` with schema `fabric-table-record/v1`
(SparkRing's installer uses `/run/sparkring-fabric/relay-state.json`), and
each tool blocks on the other's record, so both never run on the same Spark
at once. The record is bookkeeping for `remove` and the qdisc decisions; the
table's effect on traffic is the routes, neighbours, filters and markers,
which are identical.

## Boot persistence

`install-boot` writes the unit:

```ini
[Unit]
Description=Fabric relay table (fabric-tool)
Documentation=file:///usr/local/lib/fabric-tool/README.md
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/fabric-tool apply --boot --topology /etc/fabric-table/topology --facts /etc/fabric-table/facts.json
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
```

`apply --boot` is the restore semantics of SparkRing's runtime restore
(`relays.py` 370–427): it adds every missing object of this Spark's part
(qdiscs, `tc filter replace` for each planned filter, routes, neighbours),
restarts the markers whose rules differ or that are gone, and rewrites the
record; it removes nothing. After a reboot (`/run` is cleared, markers are
gone, filters and routes are gone with the interfaces) that reinstalls the
whole table; while the table stands it changes nothing. `install-boot` runs
on the host itself (it refuses `--ssh`): it copies the tool package to
`/usr/local/lib/fabric-tool`, writes the entry point `/usr/local/bin/fabric-tool`
that imports the copy, writes the unit and the inputs, enables the unit, and
applies the table now. `remove-boot` removes the unit and the copied inputs;
`remove` removes the table.

## Packaging

`transport-core/tools/package.py` at the standalone repository root adds this directory as
`fabric-tool` to the standalone archive, so one tarball carries the plugin
`net-sircl`, the shared core `transport-core` and `fabric-tool` (plus
`libsircl` with `--include-libsircl`). The archive keeps LICENSE, NOTICE and
VERSION of each tree. NOTICE attributes the absorbed code to SparkRing's
SIRCL (`sparkring_sircl.routes`, `sparkring_sircl.fabric`, `mesh_marker.c`)
and records the measured constraints the derivation encodes.

Layout:

```text
fabric-tool/
  DESIGN.md  README.md  HARDWARE-TEST-PLAN.md  LICENSE  NOTICE  VERSION
  Makefile  fabric-tool  mesh_marker.c
  fabric_tool/  topology.py model.py plan.py facts.py commands.py state.py
                diff.py ops.py report.py boot.py cli.py __init__.py __main__.py
  tests/        test_topology.py test_plan.py test_facts.py
                test_commands.py test_boot.py test_equivalence.py test_apply.py
```

## How SparkRing could call this tool

Design only; SparkRing's fabric path is unchanged in this work. SparkRing's
runtime could replace its own derivation with one call:

- `runtime/host/relays.py` `render` builds `sparkring-relay-plan/v1` from
  `sircl_plan_of` (a `GroupPlan` of the whole fabric). The tool's `plan
  --json` produces the same objects from a topology file and a facts file;
  a thin adapter could map a fabric document to a topology file (the
  document's positions and function names already carry the same facts) and
  consume the tool's JSON instead of calling into `sparkring_sircl`.
- SparkRing's `restore` (never-remove boot install) is the tool's `apply
  --boot`; SparkRing's unit could call the tool's binary with `--boot` and
  keep its own supervision of the markers.
- The ownership marks are shared by design, so the transition needs no
  migration of installed state: a table installed by either tool is a table
  the other can verify, adopt and remove. The two records stay distinct, and
  each tool refuses to run while the other's record stands.

## Tests

- `test_topology.py`: the parser's forms and refusals, the layout mapping
  table above, the role defaults and overrides.
- `test_plan.py`: fabrics, shortest paths, lane
  derivation, plan objects for a cycle of four and eight and paths of three
  and four (symbolic facts), trace delivery, isolation refusals, queue-load
  bound, tags and preferences.
- `test_commands.py`: every command form, the guard, refusals of
  placeholders and of non-fabric devices, the script order.
- `test_facts.py`: the facts schema, merging, identity and cabling checks.
- `test_equivalence.py`: for a ring of four, a ring of eight, a path of four
  and a path of three, build the plan with `fabric_tool` and with
  `sparkring_sircl.fabric` from identical facts and compare the route sets,
  filter sets, marker rules and the exact command strings. Skips where
  SparkRing's SIRCL package is not beside the tool (the standalone archive).
- `test_apply.py`: the dry-run and apply flow against the host-command
  simulator (`sparkring_sircl.testing.relay_hosts`), including verify, remove,
  adoption, refusals and boot restore. Skips outside the repository.
- `test_boot.py`: the unit text and the install/remove file operations
  against a fake root, non-mutating CLI previews and failed systemd enable.

The standalone integration supplies the boot CLI's local defaults and apply
options, persists all merged facts and subnet prefixes, checks ownership before writing the unit,
and reports failed systemd operations. Route checks cover only the hosts being
compared, allowing a unit to apply locally using facts for the whole topology.
The host simulator exercises boot installation and verification on one host.
