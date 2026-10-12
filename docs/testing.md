# Testing and CI

```sh
make -j all emu
make check
make check-release
make package
```

`make check` tests core planning, verbs ordering, topology parsing, plugin
transfers from zero bytes to 4 MiB, pipelining, shared windows, lanes, peer loss,
deadlines and teardown. It also validates examples, version stamping and
archive contents. No GPU, RoCE NIC, root access or remote host is needed.

Five inherited comparisons skip outside SparkRing: three proxy-adaptation
tests require libsircl, and two plugin tests compare with the Python planner
and libsircl stand-in. Fixed route expectations and actual transfer tests run
in this repository. A missing libibverbs development package may cause the
plugin symbol test to skip; CI installs it and builds the real library first.

## NCCL end to end on one GPU

Use a CUDA-capable PyTorch environment with runtime NCCL 2.32.3. The workstation
validation used PyTorch 2.10.0+cu128 and a prebuilt unpatched NCCL 2.32.3 library
through `LD_PRELOAD`. If supplying a separate compatible NCCL library:

```sh
export LD_PRELOAD=/path/to/libnccl.so.2.32.3
make check-e2e PYTHON=/path/to/cuda-python
```

Two ranks share GPU 0 using `NCCL_MULTI_RANK_GPU_ENABLE=1`. P2P and SHM transports
are disabled so real NCCL uses the plugin, backed by shared-memory verbs.
Both plain and 128 KiB windowed/two-lane cases run. Every element is checked
for all-reduce, all-gather and broadcast from 8 KiB to 64 MiB; a 64 KiB graph
replays 20 times, followed by graph release and process-group teardown.
Logs and JSON summaries go under `test-results/` (untracked).

`make check-e2e` fails if prerequisites are missing. CI uses
`make check-e2e-if-available`: it reports an explicit skip when CUDA-enabled
PyTorch, a CUDA GPU or NCCL 2.32.3 is absent. Once those prerequisites are found,
test failures fail the job. NCCL GPU collectives cannot run on a CPU-only host;
the CPU layer tests exercise the network API there without calling NCCL.

## CI boundary

The workflow builds and tests natively on `ubuntu-24.04` (x86_64) and
`ubuntu-24.04-arm` (aarch64), labels listed in
[GitHub's runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).
Both jobs build production and emulated libraries, run CPU and release checks,
make and inspect the source archive, and check full NCCL-test availability.
Standard hosted runners lack a CUDA GPU/PyTorch test environment, so the full
NCCL case is expected to skip there. No private/self-hosted runner is selected.

CI does not measure bandwidth/latency, ConnectX-7 hairpin forwarding, relay
counters, DGX OS GID discovery, real IP routes, relaxed ordering, or physical
queue-pair failure behavior. Passing CI is not hardware release qualification.

## Source packaging

`transport-core/tools/package.py` is the archive builder. In a standalone tree,
the archive includes the root release files, docs, examples, scripts, tests,
workflow, and sibling `net-sircl` / `transport-core` trees. In SparkRing's layout
it retains the two-component extraction behavior and optional libsircl support.
Build outputs, Git metadata and local test results are excluded. Archive member
ordering, ownership, timestamps and gzip metadata are normalized.

The release-safety checker scans tracked text without printing matched content.
It rejects site-host patterns, non-documentation IPv4 addresses, user-home paths
and credential shapes; optional private site values stay outside the archive.
