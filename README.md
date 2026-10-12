# net-sircl

Run unmodified NVIDIA NCCL on directly cabled NVIDIA DGX Sparks, including paths
whose end peers communicate through hardware relays. `net-sircl` is an NCCL
`ext-net` plugin: NCCL keeps its algorithms and CUDA-graph support; the plugin
selects a route per peer, uses both PCI functions of each cable, and limits bytes
in flight toward relayed peers. Version **0.1.0**, wire version **2**.

Install one library and describe the cabling in a topology file. No SparkRing
Python package is needed for the plugin. A topology file describes an already
configured RoCE fabric; it does not configure interfaces, routes or relay tables.

## Supported layouts

| Layout | Plugin status on DGX Spark GB10 | Relay table |
|---|---|---|
| Pair, one cable | Measured with NCCL 2.32.3 | No |
| Ring of eight | Measured with NCCL 2.32.3 and `NCCL_ALGO=Ring` | No, when communicating ring neighbours are directly cabled |
| Ring of four | Routes covered by CPU tests; no hardware run yet | Same condition as ring of eight |
| Path of four | Measured with NCCL 2.32.3 and `NCCL_ALGO=Ring` | Required on intermediate Sparks, with endpoint routes |
| Other ring/path sizes or subsets | Topology parser supports up to 64 hosts; not hardware-qualified | Required whenever a communicating pair is not directly cabled |

Stock NCCL 2.32.3 already runs on the measured pair and full ring of eight with
cuMem enabled. The plugin helps most on paths: stock NCCL connected the measured
path of four, but relayed all-reduce and all-gather failed their time limits.
See [measurements](docs/measurements.md) for conditions and exceptions.

## Build and install

On Linux, install a C11 compiler, Make, Python 3 and rdma-core development headers.
For Ubuntu:

```sh
sudo apt-get install build-essential python3 libibverbs-dev
make -j
make check
sudo make install PREFIX=/opt/net-sircl
```

Build on each target architecture (aarch64 on Spark, x86_64 for workstation tests).
The NCCL plugin headers are included; building requires neither CUDA nor an NCCL
source checkout. Running requires compatible NCCL: **2.32.3 is the tested version**,
using the v12 network-plugin API. The production library is
`/opt/net-sircl/lib/libnccl-net-sircl.so`.

## Pair quickstart

1. Cable host 0's port 0 to host 1's port 1. Each cabled function needs an IPv4
   RoCE v2 GID. Both hosts also need a mutually reachable bootstrap IP network.
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

The bootstrap settings are ordinary NCCL/torchrun launch settings, not per-peer
route or window maps. Use a single plain interface name for `NCCL_SOCKET_IFNAME`;
the plugin does not parse NCCL's `=`, `^` or comma-list syntax. If needed, set
`NET_SIRCL_OOB_ADDR` to this host's reachable IPv4 address instead.

Expect `Loaded net plugin sircl (v12)`, `Using network sircl`,
`net-sircl 0.1.0 (wire version 2)` and a `transport-core 0.1.0 ABI 1 source ...`
line. Every rank prints `PASS`. All ranks must use the same wire version.

## Rings and paths

Use [ring4.topology](examples/ring4.topology),
[ring8.topology](examples/ring8.topology) or
[path4.topology](examples/path4.topology). Replace the host names on every rank.
Position `i`'s port 0 connects to position `i+1`'s port 1. Rings add the last-to-first
cable. Paths do not. Set `NCCL_ALGO=Ring`, launch one rank per host in cable order,
and change `--nnodes` and `--node-rank` accordingly.

**Paths need a relay table before launching NCCL.** Intermediate Sparks must
forward RoCE v2 frames (flow label 0) to the far peer on the same PCI-function
class. Endpoints need IP routes through the appropriate functions. This plugin
does not install that state. SparkRing's fabric installer provides the relay
table and endpoint routes from its own site description; see
[fabric setup](docs/fabric.md).

**Standalone fabric-tool integration: pending.** This release does not bundle
that tool. Its installation instructions and verified command examples belong
in [docs/fabric.md](docs/fabric.md) once it is available.

Topology syntax, default function names, overrides and window semantics are in
[configuration](docs/configuration.md).

## PyTorch and CUDA graphs

DDP and `torch.distributed` use the plugin through NCCL without a framework patch.
Check the NCCL INIT log for the library actually loaded; PyTorch's compiled-against
version may differ from a preloaded NCCL library.

Delete **all captured graphs referencing the communicator** before teardown:

```python
del graph  # also release any other references to captured graphs
torch.cuda.synchronize()
torch.distributed.destroy_process_group()
```

NCCL waits for graph references during communicator destruction. Keeping them
alive can hang teardown with either this plugin or NCCL's socket transport.
The [emulated test](net-sircl/tests/emulation/nccl_e2e.py) demonstrates the order.

## Measured results

Research-only measurements on DGX Spark GB10, unpatched NVIDIA NCCL 2.32.3,
nccl-tests v2.21.1, bfloat16, 256 MiB out-of-place bus bandwidth:

| Layout and condition | Shared-core plugin at `a4de4dd5` | Comparison |
|---|---|---|
| Pair, eager all-reduce, 64 channels | 21.51 GB/s | Stock 20.62 GB/s in the same session |
| Ring of eight, Ring algorithm, 4 channels, eager all-reduce | 23.95 GB/s | RC `7b81b968`: 23.95 GB/s |
| Path of four, Ring algorithm, 4 channels, two relays on closing pair | 17.67 GB/s eager; 17.73 with `-G 20` | RC: 17.70 / 17.67 GB/s in that session |

Every tested collective line was exact. Across these layouts, candidate/RC
ratios at 256 MiB were 0.986–1.003. An earlier RC path measurement was 17.74 GB/s;
the RC pair reached at least 90% of stock on 35 of 36 rows from 8–256 MiB.
These are measurements, not throughput guarantees or serving qualification.
See [measurement conditions and provenance](docs/measurements.md).

## Limitations and open items

- No ring-of-four hardware run. Other GPU/NIC models, switches and multiple GPUs
  per host are not qualified. GPU Direct RDMA, DMA-BUF and grouped receives are
  not supported; this plugin stages through host memory.
- Tree/PAT connections on paths are not qualified; use Ring. A full physical ring
  only avoids relays when NCCL's communicating neighbours follow its cables.
- Several senders sharing a relay queue need an externally planned aggregate
  budget. Automatic windows cap each local (function, peer), not every host's
  combined traffic. Do not assume arbitrary all-to-all traffic is admitted.
- The relaxed-ordering-off comparison has not been run.
- Pair 8 MiB eager all-reduce was weaker in one RC run (0.82 of stock) and near
  parity in another (0.98). The six-run-per-variant graph rerun found no lower
  candidate row under its predeclared rule; that does not resolve every eager
  performance question.
- The release stamp and packaging have local validation. Hardware evidence is
  for the source revisions named above, not a newly measured 0.1.0 binary.

## Troubleshooting

| Symptom | Action |
|---|---|
| Plugin missing / wrong network | Check library path, dependencies with `ldd`, `NCCL_NET=sircl`, and the INIT log |
| No active RoCE device / no IPv4 GID | Check link state, IPv4 addresses, rdma-core and any `NET_SIRCL_HCA` filter |
| Host absent from topology | Match `hostname` exactly or set `NET_SIRCL_POSITION` to the file's zero-based position |
| `no route from ... to ...` | Check endpoint routes, cabling and relay setup |
| Queue pair RTR fails | Check GID selection and relay tables on intermediate hosts |
| Connect timeout | Check every rank is running and bootstrap/OOB IPv4 addresses are reachable; unset interface-selection prefixes |
| Millisecond delays on a path | Verify the log selects `sircl`; stock IB transport did not sustain the measured relayed path |
| Teardown hangs after graph capture | Release captured graphs before `destroy_process_group()` |

## Tests, packaging and licensing

`make check` runs CPU transport and topology tests. `make check-e2e` runs real
NCCL through emulated verbs on one CUDA GPU, in plain and windowed modes.
`make package` creates `dist/net-sircl-0.1.0.tar.gz` with both source directories
and this repository's release files. See [testing and CI](docs/testing.md),
[release notes](docs/release-0.1.0.md) and [source provenance](SOURCE.json).

Apache-2.0: see [LICENSE](LICENSE) and [NOTICE](NOTICE), including RoCEnante and
NVIDIA NCCL attributions. All vendored NVIDIA header notices are retained.
