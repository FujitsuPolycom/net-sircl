# Plugin versus stock NCCL: hardware measurements

These are research-only communication measurements on DGX Spark GB10, not
serving qualification or throughput guarantees. The measured plugin builds
used transport C sources and NVIDIA headers byte-identical to this release
([SOURCE.json](../SOURCE.json)), with a different plugin version string.
The 0.1.0 library itself has not run on Sparks. Raw logs are not published
because they contain site-specific host information.

## Conditions

Plugin runs used unpatched NVIDIA NCCL 2.32.3, nccl-tests v2.21.1, bfloat16,
one process/GPU per host, host-memory staging, relaxed ordering enabled and
both PCI functions per cable. GPU application clocks were set to 2418 MHz.
Model-serving processes were stopped on the eight hosts.

The pair used one cable of a physical ring of eight. The path used four
consecutive hosts, with traffic between end hosts crossing two intermediate
relays. Other physical cables remained connected. Relays were configured
with a different installer from the SparkRing project, not fabric-tool.
The pair used 64 channels; ring and path used `NCCL_ALGO=Ring` and four channels.

Thirteen nccl-tests runs per layout covered an 8-byte connectivity check, then
all-reduce, all-gather and broadcast, each eager and with CUDA graphs (`-G 20`),
over 8 KiB–2 MiB and 8–256 MiB sweeps. In every plugin run, every rank exited
zero, with zero wrong values and zero out-of-bounds errors reported.

## Results

Values below are **256 MiB out-of-place bus bandwidth in GB/s**. Plugin values
are means of two runs. A dash means no stock value is reported for that
collective and mode. Stock means NCCL's own IB transport with the plugin disabled.

| Layout | Collective | net-sircl eager / CUDA graph | Stock NCCL |
|---|---|---|---|
| Pair | all-reduce | 21.51 / 21.50 | 20.62 eager, one same-session run |
| Pair | all-gather | 18.72 / 18.87 | — |
| Pair | broadcast | 23.59 / 23.44 | — |
| Ring of eight | all-reduce | 23.95 / 23.93 | 23.77 eager, separate session/build, cuMem on |
| Ring of eight | all-gather | 23.18 / 23.12 | — |
| Ring of eight | broadcast | 24.26 / 24.31 | — |
| Path of four | all-reduce | 17.67 / 17.73 | Did not complete within 75 s in a separate session/build |
| Path of four | all-gather | 18.12 / 18.06 | Did not complete within 75 s in a separate session/build |
| Path of four | broadcast, root 0 | 24.30 / 24.30 | Passed; bandwidth not reported here |

The separate stock runs also used NCCL 2.32.3 and nccl-tests v2.21.1. They are
context, not a same-binary control for the plugin's ring and path measurements.
On the ring of eight, all stock runs passed with NCCL's cuMem memory allocator
enabled (`NCCL_CUMEM_ENABLE=1`). Disabling it caused setup failures between
non-adjacent hosts used by the tree algorithm. On the path, the 8-byte check
connected, but traffic between the end hosts moved at about 0.5 MB/s.
Root-0 broadcast passed because it did not use that end-host connection.

On the pair, 8 MiB eager all-reduce reached 11.84 GB/s with the plugin versus
10.93 GB/s stock in the same session (1.08 times stock). This single comparison
does not characterise small-message performance.

PyTorch 2.13.0+cu130 with NCCL 2.32.3 loaded through `LD_PRELOAD` passed four
repetitions on the path of four: 20 DDP steps ending with identical parameters
on every rank, eager all-reduce at eight sizes, and 20 replays of a captured
64 KiB all-reduce. With all graphs released before `destroy_process_group`,
teardown took 0.17–0.31 s per rank.

## Reproduce the comparison

Use an MPI-enabled nccl-tests v2.21.1 build linked to unpatched NCCL 2.32.3.
The commands below are reproduction templates, not archived historical command
lines. Set `TESTS` to the nccl-tests build directory on every host, `HOSTS` to
comma-separated host names in cable order, and `BOOTSTRAP_IFACE` to a reachable
management interface. Use the same library paths on every host, stop competing
workloads, and record driver, firmware, DGX OS, clock and NCCL versions.
Configure relays before path or non-neighbour ring traffic.

For a pair (two names in `HOSTS`), copy the matching topology to every host:

```sh
export TESTS=/opt/nccl-tests/build
export NCCL_NET_PLUGIN=/opt/net-sircl/lib/libnccl-net-sircl.so
export NCCL_NET=sircl NET_SIRCL_TOPOLOGY=/etc/net-sircl.topology
export NCCL_SOCKET_IFNAME=BOOTSTRAP_IFACE
export NCCL_MIN_NCHANNELS=64 NCCL_MAX_NCHANNELS=64
export NCCL_CUMEM_ENABLE=1 NET_SIRCL_RELAXED_ORDERING=1 NCCL_DEBUG=INFO
unset NCCL_ALGO
mpirun -np 2 --host "$HOSTS" --map-by ppr:1:node \
  -x NCCL_NET_PLUGIN -x NCCL_NET -x NET_SIRCL_TOPOLOGY -x NCCL_SOCKET_IFNAME \
  -x NCCL_MIN_NCHANNELS -x NCCL_MAX_NCHANNELS -x NCCL_CUMEM_ENABLE \
  -x NET_SIRCL_RELAXED_ORDERING -x NCCL_DEBUG \
  "$TESTS/all_reduce_perf" -b 8M -e 256M -f 2 -g 1 -d bfloat16 -c 1
```

For a ring of eight, set `HOSTS` to eight hosts, install its topology, and keep
the common plugin, bootstrap and memory settings above:

```sh
export NCCL_ALGO=Ring NCCL_MIN_NCHANNELS=4 NCCL_MAX_NCHANNELS=4
mpirun -np 8 --host "$HOSTS" --map-by ppr:1:node \
  -x NCCL_NET_PLUGIN -x NCCL_NET -x NET_SIRCL_TOPOLOGY -x NCCL_SOCKET_IFNAME \
  -x NCCL_ALGO -x NCCL_MIN_NCHANNELS -x NCCL_MAX_NCHANNELS -x NCCL_CUMEM_ENABLE \
  -x NET_SIRCL_RELAXED_ORDERING -x NCCL_DEBUG \
  "$TESTS/all_reduce_perf" -b 8M -e 256M -f 2 -g 1 -d bfloat16 -c 1
```

For a path of four, set `HOSTS` to four consecutive hosts, install its topology
and verify relays. With the common settings above:

```sh
export NCCL_ALGO=Ring NCCL_MIN_NCHANNELS=4 NCCL_MAX_NCHANNELS=4
mpirun -np 4 --host "$HOSTS" --map-by ppr:1:node \
  -x NCCL_NET_PLUGIN -x NCCL_NET -x NET_SIRCL_TOPOLOGY -x NCCL_SOCKET_IFNAME \
  -x NCCL_ALGO -x NCCL_MIN_NCHANNELS -x NCCL_MAX_NCHANNELS -x NCCL_CUMEM_ENABLE \
  -x NET_SIRCL_RELAXED_ORDERING -x NCCL_DEBUG \
  "$TESTS/all_reduce_perf" -b 8M -e 256M -f 2 -g 1 -d bfloat16 -c 1
```

Add `-G 20` for CUDA graphs. Substitute `all_gather_perf` or `broadcast_perf`
(root 0) for the other collectives. Use `-b 8K -e 2M` for the small sweep
and `-b 8 -e 8` for the connectivity check. Record each rank's exit code,
wrong-value and out-of-bounds counts, plus the out-of-place bus bandwidth.

For matched stock controls set `NCCL_NET_PLUGIN=none NCCL_NET=IB`, retaining
the same host order, channels, NCCL binary and other settings. Confirm the
INIT log selects the built-in IB transport. Run eager and graph cases twice
per layout and report individual runs as well as means. Record timeouts as
failures, not zero throughput. The separate-session stock values above are
not predictions for this matched-control procedure.

## Unmeasured conditions

No ring-of-four hardware run or measurement with relaxed ordering disabled.
No serving performance or combined multi-sender relay qualification. CPU tests
and shared-memory emulation do not establish physical forwarding, real NIC
loss recovery, reboot restoration or NIC performance. fabric-tool has not run
on hardware. The 0.1.0 binary has not run on hardware.
