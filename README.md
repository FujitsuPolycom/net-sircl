# net-sircl

net-sircl is the Switchless Inference RDMA Collective Layer (SIRCL) for
NVIDIA NCCL applications. Current focus is two or more
NVIDIA DGX Sparks cabled directly to each other, with no network switch.
It supplies RDMA connections through NCCL's network-plugin (`ext-net`) interface.
NCCL keeps its collective algorithms and CUDA-graph support. PyTorch and other
NCCL users can load the plugin without a framework patch.

Version **0.1.1**, connection protocol (wire version) **6**: every host in a job
must use a plugin with the same wire version.

## Requirements

- Two or more DGX Sparks with ConnectX-7 NICs and direct QSFP cabling; one GPU
  and one rank per host. Hardware results cover a pair, a ring of eight and a
  path of four. This release is research-only.
- Linux with working NVIDIA drivers and RoCE v2 networking. No minimum DGX OS
  version is established by this repository's evidence.
- NCCL **2.32.3**, the tested runtime, with the v12 network-plugin API. The library also exports APIs v7â€“v11;
  CPU ABI checks do not qualify every NCCL runtime on hardware.
- A C11 compiler, Make, Python 3.10 or later, and rdma-core development headers.
  CPU tests also need pytest. CUDA-enabled PyTorch runs the example below.
- IPv4 addresses on the cabled RDMA interfaces and a reachable IP network for
  process startup. The topology file describes configured networking; it does
  not assign addresses, routes or relays.

## Four terms

- **PCI function:** a separately exposed part of the NIC. Each Spark QSFP port
  has two RDMA devices: `rocep1s0f0` and `roceP2p1s0f0` on port 0,
  `rocep1s0f1` and `roceP2p1s0f1` on port 1. They use the NIC's two PCIe links.
- **Lane:** one of the two parallel connections the plugin uses across a
  cable, one per PCI function.
- **Relay:** an intermediate Spark that forwards traffic between its NIC
  ports so hosts without a shared cable can communicate.
- **Relay window:** a cap on bytes in flight toward a peer reached through
  relays. Static route-derived source/function shares and separate ACK debt
  bound modeled occupancy to at most 458752 bytes per independent 512 KiB
  forwarding queue. See the [proof and assumptions](net-sircl/ROUTE-ALLOWANCE.md).

| Cabling and communication | What to configure |
|---|---|
| Pair, or full ring with traffic only between cabled neighbours | Plugin and topology file |
| Path, or ring with traffic between non-neighbours | Plugin, topology file and relays installed with fabric-tool |

## Build and install

On Ubuntu:

```sh
sudo apt-get install build-essential python3 python3-pytest libibverbs-dev
make -j
make check
sudo make install PREFIX=/opt/net-sircl
```

Build for each target architecture: aarch64 on Spark, x86_64 for workstation
tests. Vendored NCCL headers let the build run without CUDA or NCCL installed.
NCCL loads `/opt/net-sircl/lib/libnccl-net-sircl.so`. The install also provides
`/opt/net-sircl/bin/fabric-tool`. `make` compiles its marker program,
`fabric-tool/mesh_marker.c`, which tags outgoing RDMA packets for relaying.
`make emu` builds a separate test library that never touches a NIC.

## Pair quickstart

1. Cable host 0's port 0 to host 1's port 1. Port 0 has RDMA functions
   `rocep1s0f0` and `roceP2p1s0f0`; port 1 has `rocep1s0f1` and `roceP2p1s0f1`.
   Use `rdma link show` to identify their network interfaces and link state.
   Assign an IPv4 address to each cabled function's interface; RoCE v2 derives
   its RDMA address (GID) from that address. Both hosts also need a mutually
   reachable bootstrap IP network.
2. Copy [examples/pair.topology](examples/pair.topology) to
   `/etc/net-sircl.topology` on both hosts. Replace `node-0 node-1` with the names
   returned by `hostname`. This is the only plugin routing configuration.
3. Install the library on both hosts. With CUDA-enabled PyTorch using NCCL 2.32.3,
   run the following on each host from this repository. Replace `BOOTSTRAP_IFACE`
   with the management interface name, `MASTER_HOST` with host 0's reachable name,
   and `RANK` with `0` or `1`:

```sh
export NCCL_NET_PLUGIN=/opt/net-sircl/lib/libnccl-net-sircl.so
export NCCL_NET=sircl
export NET_SIRCL_TOPOLOGY=/etc/net-sircl.topology
export NCCL_SOCKET_IFNAME=BOOTSTRAP_IFACE
export NCCL_DEBUG=INFO
export NCCL_DEBUG_SUBSYS=INIT,NET
torchrun --nnodes=2 --nproc-per-node=1 --node-rank=RANK \
  --master-addr=MASTER_HOST --master-port=29500 examples/all_reduce.py
```

These are ordinary NCCL and torchrun settings. Set `NCCL_SOCKET_IFNAME` to one
interface name or name prefix. The plugin advertises the first IPv4 address of
the first matching interface to its peers. It does not parse NCCL's `^`
(exclude), `=` (exact match) or comma-list forms. If you need those forms, set
`NET_SIRCL_OOB_ADDR` to this host's reachable IPv4 address instead.

Expect `Loaded net plugin sircl (v12)`, `Using network sircl` and
`net-sircl 0.1.1 (wire version 6)`. Every rank prints `PASS`. A further line,
`net-sircl transport-core 0.1.1 ABI 1 source <sha256>`, identifies the bundled
RDMA library's source files; see [source identity](docs/testing.md#source-identity).

## Rings and paths

Use [ring4.topology](examples/ring4.topology),
[ring8.topology](examples/ring8.topology) or [path4.topology](examples/path4.topology).
List hosts in cable order. A host's *position* is its zero-based index on the
`hosts` line. Position i's port 0 connects to position i+1's port 1. Rings add
the last-to-first cable; paths do not. Set `NCCL_ALGO=Ring`, launch one rank per
host in cable order, and adjust `--nnodes` and `--node-rank`.

Plugin ring-of-four route derivation is CPU-tested; ring-of-four plugin
traffic has not been measured. The fabric table exercise is described below.
Other sizes and subsets are unsupported for hardware use, although the parser
accepts up to 64 hosts. See [configuration](docs/configuration.md).

## Peers beyond a cable

**Configure relays before launching NCCL on a path**, or on a ring whose
communicating peers are not neighbours. Every intermediate Spark needs NIC
forwarding rules. Senders need IP routes toward the far host through the first
relay, and a marker program that tags packets with the remaining relay count.
The bundled fabric-tool derives these from the topology file and installs them.
Follow [fabric setup](docs/fabric.md) for facts collection, marker installation,
apply, verification, ownership and boot restoration.

fabric-tool is research-only. An earlier revision installed and verified
ring and path tables on four DGX Sparks, and plugin test arms passed on the
path table. The two script fixes included here have only been tested on CPU;
the fixed version has not yet been rerun on hardware, and boot restoration
remains unqualified. See the [release notes](docs/release-0.1.0.md).

## Troubleshooting

| Symptom | Action |
|---|---|
| Plugin missing or wrong network | Check the library path, `ldd`, `NCCL_NET=sircl` and the INIT log |
| No active RoCE device or IPv4 GID | Check link state, IPv4 addresses, rdma-core and `NET_SIRCL_HCA` |
| Host absent from topology | Match `hostname` or set `NET_SIRCL_POSITION` to its zero-based position |
| `no route from ... to ...` | Check cabling, sender IP routes and [relay setup](#peers-beyond-a-cable) |
| `queue pair to RTR on ... failed` (connection could not reach peer) | Check GIDs and relays on intermediate hosts |
| Connect timeout | Start every rank, check bootstrap IPv4 reachability, remove leading `^` or `=` from `NCCL_SOCKET_IFNAME`, or set `NET_SIRCL_OOB_ADDR` |
| Millisecond delays on a path | Check for `Using network sircl`; stock NCCL's IB transport moved relayed traffic at about 0.5 MB/s in testing |
| Teardown hangs after graph capture | Release every captured graph before `destroy_process_group()`; see [configuration](docs/configuration.md#pytorch-and-cuda-graphs) |

## Measured relayed-path behavior

On a four-host DGX Spark path, NCCL 2.32.3 Ring with eight channels reaches
14.41/14.65 GB/s out-of-place/in-place all-reduce bus bandwidth at 256 MiB
(four samples combined across rounds 9–10). The 8–256 MiB sweep returns exact
results with zero mandatory drop/error deltas. Graph L05 completes in 44.80 s.
These measurements use candidate source ee91dcc6, not the standalone release
binary. [Release notes](RELEASE_NOTES-0.1.1.md) give conditions, sample counts,
controls, other collectives and the pending final-pass section.

For relayed jobs, set `NCCL_MAX_NCHANNELS=8` on every rank before NCCL
initializes, leaving channel minima unset. The plugin emits an advisory when
no supported cap environment entry exists; it does not apply the cap itself.
Cap 8 on direct pairs and eight-rank jobs is not measured by these records.
Historical plugin-versus-stock results remain in [measurements](docs/measurements.md).

## Limitations

The plugin stages data through host memory. GPU Direct RDMA, DMA-BUF and
grouped receives are unsupported. Other GPU/NIC models, switches, multiple GPUs
per host, and Tree/PAT connections on paths are not qualified; use Ring.

The route model includes every source in the complete topology, with one
plugin process per physical host and consistent policy across overlapping
jobs. Independent queues, reciprocal forwarding and no unrelated producers
are required; physical queue independence and arbitrary multi-job traffic are
not qualified. Small-message performance is not characterised.

Known issue: on one cabled pair, one run in four of the all-gather line under
CUDA-graph capture (`-G 20`, 8-256 MiB) stopped at its first size with
`transport retry counter exceeded` on one PCI function, while the receiving
port counted six ACK timeouts. No relay was involved, and three further runs
of that line passed. Whether the cause is the cable, the port or the plugin is
not yet established.

See [testing and CI](docs/testing.md), [release notes](RELEASE_NOTES-0.1.1.md)
and [source provenance](SOURCE.json). `make help` lists build and test targets.

Apache-2.0: [LICENSE](LICENSE) and [NOTICE](NOTICE) retain attribution to
RoCEnante (Local Inference Lab's RDMA collectives), NVIDIA NCCL (plugin headers)
and SparkRing (the project where these components were developed).
