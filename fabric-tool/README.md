# fabric-tool

fabric-tool configures relays between directly cabled DGX Sparks: NIC
forwarding rules on intermediate hosts, sender IP routes and neighbour entries,
and a per-NIC marker process that tags outgoing RDMA packets with the number
of relays to cross. It reads net-sircl's topology file.
Version **0.1.0**, Apache-2.0; derived from the SparkRing project, see
[LICENSE](LICENSE) and [NOTICE](NOTICE).

Status: implemented and CPU-tested, research-only; not run on hardware.
Standalone tests cover planning, facts, command forms, boot previews and mocked
boot operations. They do not prove real NIC forwarding or reboot restoration.

## Run

From this directory, Python 3.10 or later runs the tool with only its standard
library. Linux hosts need iproute2, rdma-core, Bash and noninteractive sudo;
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
| `install-marker` | Preview/build `mesh_marker.c` on hosts, default binary `/var/tmp/fabric-tool-marker` |
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

See [DESIGN.md](DESIGN.md) for derivation and ownership, and
[HARDWARE-TEST-PLAN.md](HARDWARE-TEST-PLAN.md) for the hardware procedure; the tool has not run on hardware.
