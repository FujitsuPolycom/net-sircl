# Build, tests and source archives

## CPU checks

On Linux with a C11 compiler, Make, Python 3.10+, pytest and rdma-core headers:

```sh
make -j2 all emu
make check
make check-release
```

`make all` builds the core static library and dynamic-loader object, the
real-NIC plugin and the packet marker. `make emu` builds the shared-memory
test plugin. `make check` runs:

- transport-core: two C test executables for concurrent window arithmetic, abort/progress
  callbacks and emulated verbs, plus four Python discovery/topology tests.
- net-sircl: 24 Python tests, including multiprocess transfer, window, lane,
  teardown, peer-loss and deadline tests, fixed topology expectations and
  the real-NIC library's v7–v12 export check, ABI layout calls, route/ACK
  capacity oracle, oldest-first credit, partial completions and profiling checks. That export check needs rdma-core
  headers and `nm`; CI installs both.
- fabric-tool: standalone pytest cases for topology, plans, facts, exact
  command forms, marker ownership/digest and log-parent refusals, boot previews
  and mocked boot operations. Security tests use synthetic host observations
  and local Bash checks without privileged host changes. No external
  checkout or full-network simulator is used. These do not exercise real
  apply/verify/remove across hosts or restoration after an actual reboot.
- Release contracts: shipped C/C++/header hashes, example routes, embedded core
  identity, and archive reproducibility/completeness. The archive test creates
  an untracked file under docs and checks that the archive still contains
  exactly `git ls-files`. This test skips in an extracted archive without Git
  metadata; the other three release tests run there.

External-project comparison, adapter and host-simulator tests are not shipped.
CPU checks establish neither hardware qualification nor performance.

## Single-GPU NCCL emulation

`make check-e2e` requires CUDA-enabled PyTorch, a CUDA GPU and runtime NCCL
2.32.3. Set `PYTHON` to the desired interpreter; if needed, set `LD_PRELOAD`
to a compatible unpatched NCCL library. The test queries the loaded runtime.

```sh
make check-e2e PYTHON=python3
make check-e2e-if-available PYTHON=python3
```

The first command fails if prerequisites are absent. The second prints `SKIP`
and exits zero for absent prerequisites; once available, test failures fail
the command. Two ranks share GPU 0 with P2P and NCCL shared-memory transport
disabled, forcing NCCL traffic through the emulated network plugin. Plain
and windowed/two-lane modes each verify all-reduce, all-gather and broadcast
at six sizes from 8 KiB to 64 MiB, plus 20 CUDA-graph replays at 64 KiB.
Graphs are released before process-group destruction. Each rank must select
sircl, report seven exact-result lines and exit successfully.

Logs and JSON summaries go under `test-results/`, which Git ignores and
`make package` excludes. Logs contain this machine's host name; check them
before sharing. The emulator moves bytes in shared memory and models no NIC,
cable, physical relay queue or loss recovery.

## Native GPU backend

`net-sircl/tests/emulation/nccl_compat_e2e.cc` is an optional C++ backend
using public CUDA/NCCL APIs. Compile it with a C++ compiler and CUDA and NCCL
development headers/libraries, then pass its executable to
`net-sircl/tests/emulation/nccl_e2e.py --launch --native EXECUTABLE --plugin
ABSOLUTE_PLUGIN_PATH --out OUTPUT_DIRECTORY`. Add `--windowed` for relayed
credit emulation. `NET_SIRCL_TEST_LARGE_GRAPHS=1` adds 20 graph replays at each
size from 8 to 256 MiB, both in and out of place, with changing inputs and
exact checks; use `--timeout 900`. This backend is not required on CPU CI.

## CI

The GitHub Actions job runs on `ubuntu-24.04` and `ubuntu-24.04-arm`:

1. Install build-essential, Python 3, pytest and libibverbs-dev.
2. Run `make -j2 all emu` and `make check check-release`.
3. Run `make check-e2e-if-available`; public CPU runners normally report an
   explicit prerequisite skip rather than running NCCL.
4. Build the source archive, extract it, build all components and emulation,
   and rerun `make check`. Only the Git-index archive test skips there.
5. Stage `make install` and run the installed fabric-tool on the path-of-four
   example as an offline JSON plan.

CI does not access Sparks, run physical relay tests, install a boot unit on a
host or establish performance. See the [hardware procedure](../fabric-tool/HARDWARE-TEST-PLAN.md).

## Source archives and privacy

```sh
make package
python3 scripts/check_release_safety.py .
```

The archive is `dist/net-sircl-0.1.1.tar.gz`, built from `git ls-files` in
this repository, using current tracked-file bytes and fixed timestamps,
ownership and modes. Stage added or deleted files before packaging; a release
should be built from a clean commit. Untracked files cannot ship. A tracked
generated/private path, missing file or symlink makes packaging fail. Extraction
needs no Git checkout to build or test; repackaging requires a checkout.

The scanner rejects default Spark host names (`spark-` plus four characters),
IPv4 addresses outside documentation/loopback ranges, user-home paths and
credential-like strings. It reports locations and rule names without matched
values. `--site-values FILE` adds literal host names or addresses, one per line;
keep the file untracked (`scripts/config/site-values.txt` is ignored and
excluded). The scanner checks Git-tracked text; archive privacy is checked by
scanning its extracted text with the same rules.

## Source identity

The startup line `net-sircl transport-core 0.1.1 ABI 1 source <sha256>` contains
a digest of transport-core's C sources, headers, Python tools and VERSION.
It is not a compiled-library checksum. Removing Python adaptation tools and
changing packaging or the component version changes this digest.
[SOURCE.json](../SOURCE.json) separates shipped checksums, candidate provenance
and immutable historical 0.1.0 measured hashes. They do not qualify this binary.
See [retained compatibility code](transport-core.md#retained-compatibility-code).
