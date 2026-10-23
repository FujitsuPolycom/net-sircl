# net-sircl

net-sircl brings SIRCL, the Switchless Inference RDMA Collective Layer, to NCCL
applications. It lets them run across NVIDIA DGX Sparks that are cabled directly
to each other, with no network switch. It is an NCCL network plugin: install it
on each Spark, describe the cabling in a topology file and point NCCL at it with
environment variables. PyTorch, vLLM, JAX and other NCCL applications then use
it with no code changes, and NCCL keeps its own collective algorithms and
CUDA-graph support.

Version **0.1.2**, wire version **6** (every host in a job must use the same
wire version). Status: **research-only**. The [qualification table](docs/qualification.md)
identifies the exact source revisions and conditions of recorded DGX Spark
results; modified source builds require separate hardware qualification.

## What it does

- **Pairs, rings, sections of a ring, and open-ended chains.** Hosts that share
  no cable reach each other through NIC relays on the Sparks between them.
- **Relays without a switch.** The bundled `fabric-tool` installs the relay
  rules, sender routes and packet marker that relayed traffic needs, and
  verifies them read-only.
- **Unmodified frameworks.** PyTorch, DeepSpeed, JAX, vLLM, SGLang,
  TensorRT-LLM and others run on it through stock NCCL; see
  [Verified with](#verified-with).
- **Bounded relay traffic.** Per-route windows keep bytes in flight below
  each relay's forwarding queue
  ([proof and assumptions](net-sircl/ROUTE-ALLOWANCE.md)).
- **Channel tuning.** A bundled tuner caps collectives at eight channels on
  topologies with more than two hosts ([channel policy](docs/channels.md)).
- **NCCL 2.27.7, 2.28.9, 2.31.2 and 2.32.3** tested (frameworks also ran
  2.30.7); ext-net v7-v12 ([compatibility](docs/compatibility.md)).

## Requirements

- DGX Sparks with ConnectX-7 NICs, cabled directly with QSFP; one GPU and one
  rank per host. No minimum DGX OS version is established.
- Linux with NVIDIA drivers and RoCE v2; IPv4 addresses on the cabled
  interfaces, plus a reachable IP network for process startup.
- To build: a C11 compiler, Make, Python 3.10+ and rdma-core headers (pytest
  for CPU tests). Vendored NCCL headers mean CUDA and NCCL are not needed to
  build.

## Build and install

```sh
sudo apt-get install build-essential python3 python3-pytest libibverbs-dev
make -j
make check
sudo make install PREFIX=/opt/net-sircl
```

This installs `/opt/net-sircl/lib/libnccl-net-sircl.so` and
`/opt/net-sircl/bin/fabric-tool`. Build on each architecture you use (aarch64
on Spark). `make emu` builds a test library that never touches a NIC;
`make help` lists the other targets.

## Quickstart: two Sparks

1. Cable host 0's port 0 to host 1's port 1. Assign an IPv4 address to each
   cabled RDMA function's interface (`rdma link show` lists them; port 0 is
   `rocep1s0f0` and `roceP2p1s0f0`, port 1 is `rocep1s0f1` and
   `roceP2p1s0f1`). RoCE v2 derives its RDMA address from it.
2. Copy [examples/pair.topology](examples/pair.topology) to
   `/etc/net-sircl.topology` on both hosts and replace `node-0 node-1` with
   their `hostname` values.
3. On each host, from this repository, with CUDA-enabled PyTorch on NCCL 2.32.3
   (`RANK` is `0` or `1`):

```sh
export NCCL_NET_PLUGIN=/opt/net-sircl/lib/libnccl-net-sircl.so
export NCCL_NET=sircl
export NET_SIRCL_TOPOLOGY=/etc/net-sircl.topology
export NCCL_SOCKET_IFNAME=BOOTSTRAP_IFACE
export NCCL_DEBUG=INFO NCCL_DEBUG_SUBSYS=INIT,NET
torchrun --nnodes=2 --nproc-per-node=1 --node-rank=RANK \
  --master-addr=MASTER_HOST --master-port=29500 examples/all_reduce.py
```

Expect `Loaded net plugin sircl (v12)`, `Using network sircl`,
`net-sircl 0.1.2 (wire version 6)`, and `PASS` from every rank.
`NCCL_SOCKET_IFNAME` takes one interface name or prefix; for NCCL's `^`, `=`
or list forms, set `NET_SIRCL_OOB_ADDR` to this host's IPv4 address instead.

## More Sparks: rings, sections and chains

1. Start from [ring4](examples/ring4.topology), [ring8](examples/ring8.topology)
   or [path4](examples/path4.topology) (an open-ended chain). List hosts in
   cable order: position i's port 0 connects to position i+1's port 1; rings
   add the last-to-first cable. To use a section of a ring, keep the full ring
   file and launch ranks only on the hosts you want.
2. If any two communicating hosts share no cable (a chain, a section, or a ring
   where non-neighbours talk), install relays with fabric-tool first:
   [fabric setup](docs/fabric.md). A pair, or a full ring where only neighbours
   talk, needs no relays.
3. Launch as in the quickstart with `NCCL_ALGO=Ring`, one rank per host in cable
   order, and `--nnodes`/`--node-rank` adjusted.

Relay tables are verified read-only by fabric-tool; restoring them at boot is
not yet qualified. A ring of four is CPU-tested only, and layouts not listed in
[Verified with](#verified-with) are untested on hardware. See
[configuration](docs/configuration.md).

## Verified with

Single runs on one eight-Spark ring (GB10, ConnectX-7 RoCE at 200 Gb/s, one
GPU and rank per host, `NCCL_MAX_NCHANNELS=8`). A pass means correct results,
unchanged RDMA/Ethernet error counters and every rank exiting on its own; it is
not a throughput benchmark or a serving qualification. Rows other than
nccl-tests and PyTorch eager/graph used the 0.1.1 library, which shares wire
version 6 and transport-core 0.1.1 with this release. Every row is
**research-only**.

| Framework | Version | NCCL | Sparks |
|---|---|---|---|
| nccl-tests (all-reduce, all-gather, reduce-scatter, broadcast) | 2.21.1 | 2.32.3 | 2, 4, 8 |
| PyTorch eager and CUDA-graph all-reduce, with teardown | 2.13.0+cu130 | 2.32.3 | 4 |
| PyTorch DDP, FSDP2 | 2.14.0a0 (NGC PyTorch 26.09) | 2.31.2 | 2, 4, 6, 8 |
| DeepSpeed ZeRO-2 / ZeRO-3 | 0.19.7 | 2.31.2 | 2, 4 / 2, 4, 6, 8 |
| Hugging Face Accelerate + TRL SFT (LoRA) | Accelerate 1.15.0, TRL 1.15.0, PEFT 0.21.2 | 2.31.2 | 2, 4 |
| JAX (`jax.distributed`, `psum`) | 0.11.1.dev (NGC JAX 26.09) | 2.31.2 | 2, 4, 6, 8 |
| TensorRT-LLM (LLM API, Ray) | 1.4.0rc0 | 2.30.7 | 2, 4 |
| vLLM, Ray executor | 0.29.0 (NGC 26.09); 0.31.1rc1, 0.32.0rc1 | 2.31.2; 2.32.3 | 2, 4 |
| vLLM, native multi-node | 0.29.0 (NGC vLLM 26.09) | 2.31.2 | 4, 8 |
| SGLang (CUDA graphs disabled) | 0.5.21 | 2.30.7 | 8 |
| llm-compressor GPTQ W4A16 (local-device patch) | 0.14.0 | 2.31.2 | 2, 4 |
| NVIDIA ModelOpt FP8 calibration | 0.46.0 | 2.31.2 | 2, 4 |
| sparkrun (vLLM recipe) | 0.3.10 | 2.31.2; 2.32.3 | 2, 4 |

2 is a direct pair and 8 the full ring. 4 is an open-ended chain (path
topology, no last-to-first cable): the last rank reaches the first through
relays on the two middle Sparks. 6 is ranks on positions 0-5 of the
ring-of-eight topology.

## Performance

On a four-Spark path, NCCL 2.32.3 Ring with eight channels reached about
14.4 GB/s all-reduce bus bandwidth at 256 MiB, with exact results
([0.1.1 release notes](RELEASE_NOTES-0.1.1.md)). Stock NCCL's IB transport
moved relayed traffic at about 0.5 MB/s in testing.

The bundled tuner's eight-channel cap matched an explicit `NCCL_MAX_NCHANNELS=8`
within a few percent at 8-256 MiB on the ring of eight. It allocates 64 channels
at startup, which costs about 1.5 s, and on paths it is slower than the explicit
cap below 2 MiB. On a pair the tuner leaves NCCL's default, which reached about
half the explicit cap's throughput at 8 MiB. Set `NCCL_MAX_NCHANNELS=8` on
every rank where startup time, small messages or pair throughput matter. Details:
[channel policy](docs/channels.md), [measurements](docs/measurements.md).

## Troubleshooting

| Symptom | Action |
|---|---|
| Plugin missing or wrong network | Check the library path, `ldd`, `NCCL_NET=sircl` and the INIT log |
| No active RoCE device or IPv4 GID | Check link state, IPv4 addresses, rdma-core and `NET_SIRCL_HCA` |
| Host absent from topology | Match `hostname` or set `NET_SIRCL_POSITION` to its zero-based position |
| `no route from ... to ...` | Check cabling, sender IP routes and [relays](#more-sparks-rings-sections-and-chains) |
| `queue pair to RTR on ... failed` | Check GIDs and relays on intermediate hosts |
| Connect timeout | Start every rank, check bootstrap reachability, or set `NET_SIRCL_OOB_ADDR` |
| Millisecond delays on a path | Check for `Using network sircl` |
| Teardown hangs after graph capture | Release every captured graph before `destroy_process_group()` ([details](docs/configuration.md#pytorch-and-cuda-graphs)) |

## Limitations

- Data is staged through host memory; GPU Direct RDMA, DMA-BUF and grouped
  receives are unsupported.
- Not qualified: switches, other GPU/NIC models, multiple GPUs per host, and
  Tree/PAT on paths (use Ring).
- The relay bound assumes one plugin process per host, consistent policy across
  overlapping jobs and no unrelated traffic on the relays.
- Small-message performance is not characterised.
- Known issue: one of four CUDA-graph all-gather runs on one cabled pair stopped
  with `transport retry counter exceeded`; the cause (cable, port or plugin) is
  not established ([details](docs/release-0.1.0.md)).

See [testing and CI](docs/testing.md) and [source provenance](SOURCE.json).

Apache-2.0: [LICENSE](LICENSE) and [NOTICE](NOTICE) retain attribution to
RoCEnante (Local Inference Lab's RDMA collectives), NVIDIA NCCL (plugin headers)
and SparkRing (the project where these components were developed).
