# net-sircl

net-sircl is the Switchless Inference RDMA Collective Layer (SIRCL) for
NVIDIA NCCL applications. For you, that means running across two or more
NVIDIA DGX Sparks cabled directly to each other, with no network switch.
It supplies RDMA connections through NCCL's network-plugin (`ext-net`) interface.
NCCL keeps its collective algorithms and CUDA-graph support. PyTorch and other
NCCL users can load the plugin without a framework patch.

Version **0.1.0**, connection protocol (wire version) **2**: every host in a job
must use a plugin with the same wire version.

## Requirements

- Two or more DGX Sparks with ConnectX-7 NICs and direct QSFP cabling; one GPU
  and one rank per host. Hardware results cover a pair, a ring of eight and a
  path of four. This release is research-only.
- Linux with working NVIDIA drivers and RoCE v2 networking. No minimum DGX OS
  version is established by this repository's evidence.
- NCCL **2.32.3**, the tested runtime, with the v12 network-plugin API.
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
  relays. The NIC forwarding queue holds 512 KiB and cannot slow its sender;
  the default window is 393216 bytes per local function and peer.

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
`net-sircl 0.1.0 (wire version 2)`. Every rank prints `PASS`. A further line,
`net-sircl transport-core 0.1.0 ABI 1 source <sha256>`, identifies the bundled
RDMA library's source files; see [source identity](docs/testing.md#source-identity).

## Rings and paths

Use [ring4.topology](examples/ring4.topology),
[ring8.topology](examples/ring8.topology) or [path4.topology](examples/path4.topology).
List hosts in cable order. A host's *position* is its zero-based index on the
`hosts` line. Position i's port 0 connects to position i+1's port 1. Rings add
the last-to-first cable; paths do not. Set `NCCL_ALGO=Ring`, launch one rank per
host in cable order, and adjust `--nnodes` and `--node-rank`.

Ring-of-four route derivation is CPU-tested but has not run on hardware.
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

fabric-tool is implemented and CPU-tested, research-only, and has not run on
hardware. The plugin measurements used a different relay installer from the
SparkRing project; they do not qualify fabric-tool or its boot unit.

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

## Plugin versus stock NCCL

Research-only results on DGX Spark GB10: NCCL 2.32.3, nccl-tests v2.21.1,
bfloat16, one rank/GPU per host, both cable functions, relaxed ordering on.
Values are 256 MiB out-of-place all-reduce bus bandwidth.

| Layout and condition | net-sircl | Stock NCCL (own IB transport) |
|---|---|---|
| Pair, eager, 64 channels | 21.51 GB/s | 20.62 GB/s, same session |
| Ring of eight, Ring, 4 channels, eager | 23.95 GB/s | 23.77 GB/s with cuMem enabled; separate session and NCCL build |
| Path of four, Ring, 4 channels, two relays between end hosts | 17.67 GB/s eager; 17.73 GB/s CUDA graph | All-reduce and all-gather did not finish within 75 s; separate session and NCCL build |

Every tested plugin collective returned exact results. The measured builds
used the same transport C/header bytes as this release, with a different
version string. The 0.1.0 library itself has not run on Sparks.
See [conditions, limitations and reproduction](docs/measurements.md).

## Limitations

The plugin stages data through host memory. GPU Direct RDMA, DMA-BUF and
grouped receives are unsupported. Other GPU/NIC models, switches, multiple GPUs
per host, and Tree/PAT connections on paths are not qualified; use Ring.

Several processes or hosts sending through one relay at once are unsupported.
The window limits one process's traffic through one local function toward one
peer, not several senders' combined traffic. All-to-all traffic can overrun
the forwarding queue. Performance with `NET_SIRCL_RELAXED_ORDERING=0` and
small-message performance are not characterised.

See [testing and CI](docs/testing.md), [release notes](docs/release-0.1.0.md)
and [source provenance](SOURCE.json). `make help` lists build and test targets.

Apache-2.0: [LICENSE](LICENSE) and [NOTICE](NOTICE) retain attribution to
RoCEnante (Local Inference Lab's RDMA collectives), NVIDIA NCCL (plugin headers)
and SparkRing (the project where these components were developed).
