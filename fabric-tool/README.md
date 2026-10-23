# fabric-tool

fabric-tool configures relays between directly cabled DGX Sparks: NIC
forwarding rules on intermediate hosts, sender IP routes and neighbour entries,
and a per-NIC marker process that tags outgoing RDMA packets with the number
of relays to cross. It reads net-sircl's topology file.
Version **0.1.2**, Apache-2.0; derived from the SparkRing project, see
[LICENSE](LICENSE) and [NOTICE](NOTICE).

Status: research-only. The tool implements relay planning, ownership checks,
hardware-offload verification and explicit boot restoration. CPU checks qualify
these software contracts; real forwarding and reboot restoration require the
[hardware procedure](HARDWARE-TEST-PLAN.md). The repository's
[qualification table](../docs/qualification.md) identifies recorded hardware
evidence separately from the source checks.
Standalone tests cover planning, facts, command forms, marker security checks,
boot previews and mocked boot operations. They do not prove real NIC forwarding
or reboot restoration.

## Run

From this directory, Python 3.10 or later runs the tool with only its standard
library. Linux hosts need iproute2, rdma-core, Bash, util-linux `setsid` with
`-f` support and noninteractive sudo;
SSH operation also needs OpenSSH. Building the marker needs a C compiler,
libibverbs and libmlx5 development headers (Ubuntu: `libibverbs-dev`, which also provides mlx5 headers).

```sh
python3 -m fabric_tool --topology /path/to/path4.topology
python3 -m fabric_tool plan --topology /path/to/path4.topology --facts facts.json --commands
```

Omitting the command selects `plan`, an offline preview. Every host-changing
command requires `--execute` to make changes. `make install` at the repository
root installs a `fabric-tool` entry point under the chosen prefix's `bin/`.
The Python package stays under that prefix's `lib/fabric-tool/`.

For the full facts/marker/apply/verify sequence and per-host boot installation,
see the repository guide `docs/fabric.md`.
The installed copy of that guide is under `share/doc/net-sircl/`.

## Commands

| Command | Behavior |
|---|---|
| `plan` (default) | Offline relay plan; `--commands` needs complete `--facts`; `--json` emits machine-readable output |
| `facts` | Read local host, or every topology host with repeated `--ssh NAME=TARGET`; `--output FILE` saves facts |
| `apply` | Preview reconciliation; `--execute` applies; `--boot` restores without removing objects |
| `verify` | Read and compare objects and resolve lane destinations with `ip route get`; zero means success |
| `status` | Show owned and unowned objects |
| `install-marker` | Preview/build `mesh_marker.c` on hosts, install root-owned at `/usr/local/libexec/fabric-tool/fabric-tool-marker` and record its SHA-256 |
| `remove` | Preview/remove owned relay objects |
| `install-boot` | Locally install `fabric-table.service`, tool copy and inputs, enable restoration and apply now |
| `remove-boot` | Locally disable/remove the unit and inputs; leave live table and tool copy |

`apply`, `verify`, `remove`, `status` and `install-marker` act locally by default.
With `--ssh`, supply one target for **every** topology host. Host names must match
`hostname`. `install-boot` and `remove-boot` run on each host, with root privileges
for execution. Facts must cover every host even for a local apply or boot install;
repeated `--facts` files merge without allowing contradictory host entries.

Exit codes: 0 success; 1 failed reads or remaining changes; 2 invalid input;
3 blockers. The tool refuses foreign ownership, contradictory facts, cabling
mismatch and non-relay routes to planned destinations. A failure during execution
may leave partial state; inspect and reconcile before using the table.

## Marker security

The marker runs as root. Before every start, the tool requires its executable,
its adjacent `.sha256` file and every parent directory through `/` to be owned
by root and not writable by group or others. It also requires the executable
to match the SHA-256 recorded at installation. These checks prevent an
unprivileged local user from replacing the code that root starts.

Logs live under `/var/lib/fabric-tool/logs`. Every existing directory from
there through `/` must also be root-owned and not writable by group or others;
missing directories are created as root. Root creates each log without following
a symbolic link. Custom `--marker` paths require the same protections;
paths under `/tmp`, `/var/tmp` and `/dev/shm` are refused.

`apply` reports observed security failures as blockers before making changes.
Generated host scripts repeat the checks at each start and exit 4 on failure.
Use `install-marker --execute` to install a missing marker or digest record;
use `--rebuild` to request a rebuild. The digest checks the installed file's
integrity against its local record; it is not a signature of trusted source.

See [DESIGN.md](DESIGN.md) for derivation and ownership, and
[HARDWARE-TEST-PLAN.md](HARDWARE-TEST-PLAN.md) for the hardware procedure.

## Script lifecycle

Every read-script section ends with a newline. A digest record without its own
trailing newline therefore cannot hide the next header or its ownership data.
This also applies to the sections used to verify IP routes.

Markers start through foreground `sudo -n setsid -f bash -c '<launch>'` with
stdin from `/dev/null`. `setsid -f` forks a new session and returns, allowing
sudo and the SSH script to finish while the marker runs. Root creates the log
and directs the marker's stdout and stderr to it. The marker keeps its process
name and command line so status and removal can still identify it. A failure
of sudo or setsid stops the host script; apply then rereads and verifies state.
