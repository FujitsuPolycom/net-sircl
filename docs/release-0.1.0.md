# net-sircl 0.1.0

Standalone NCCL ext-net plugin for directly cabled DGX Sparks. The release
contains the plugin, shared transport-core 0.1.0 (C ABI 1) and fabric-tool 0.1.0, Apache-2.0
licenses and notices, topology examples, CPU tests and a single-GPU NCCL test.

- NCCL v12 plugin API; tested with unpatched NCCL 2.32.3.
- Wire version 2, unchanged from RC `7b81b968` and shared-core source `a4de4dd5`.
- Per-peer routes, dual-function lane selection and shared per-function/peer
  windows for relayed traffic. Host-memory staging; no DMA-BUF/GPU Direct RDMA.
- Initialization logs the plugin version, core version, ABI and source digest.
- The standalone fabric tool plans by default, requires `--execute` to change
  hosts, verifies the table and installs a per-host boot unit. Its CPU tests
  establish equivalence with SparkRing's planner; hardware validation is pending.
- Source archive supports `make`, `make check`, `make install` and `make package`
  from its root. Builds require no sibling SparkRing checkout.
- Native x86_64 and aarch64 CI builds and CPU tests; full emulated NCCL runs when
  a compatible CUDA/PyTorch/NCCL environment is available.

See [measurements](measurements.md) for research-only pair, cycle-of-eight and
path-of-four results. There is no ring-of-four hardware run and no
relaxed-ordering-off comparison. The pair 8 MiB eager all-reduce result varied
between runs. Paths require relay tables and endpoint routes installed with
the bundled tool or an existing fabric provider. Release all captured CUDA
graphs before destroying a PyTorch process group.

The standalone source is derived from SparkRing commit
`a4de4dd5280f01613889ced7873aa85ccf8dc2bb`. Transport C/header bytes are retained.
The package tool now supports both source-tree layouts and includes public
release files in the standalone layout. Because the core digest includes tools,
that packaging change gives this release a distinct source identity. It does
not imply that transport algorithms changed. See [SOURCE.json](../SOURCE.json).

Fabric-tool comes from SparkRing branch `pi/sircl-fabric-tool` at
`852407bd1f946840debce36a7299d303dfceb8cb`. Public examples and fixtures use
placeholder hosts and documentation addresses. Integration fixes cover boot CLI
defaults, per-host route checks, complete persisted facts and subnet prefixes, ownership preflight
and checked systemd operations. No DGX Spark was used to validate these fixes.
