# net-sircl 0.1.0

Standalone NCCL ext-net plugin for directly cabled DGX Sparks. The release
contains the plugin and shared transport-core 0.1.0 (C ABI 1), Apache-2.0
licenses and notices, topology examples, CPU tests and a single-GPU NCCL test.

- NCCL v12 plugin API; tested with unpatched NCCL 2.32.3.
- Wire version 2, unchanged from RC `7b81b968` and shared-core source `a4de4dd5`.
- Per-peer routes, dual-function lane selection and shared per-function/peer
  windows for relayed traffic. Host-memory staging; no DMA-BUF/GPU Direct RDMA.
- Initialization logs the plugin version, core version, ABI and source digest.
- Source archive supports `make`, `make check`, `make install` and `make package`
  from its root. Builds require no sibling SparkRing checkout.
- Native x86_64 and aarch64 CI builds and CPU tests; full emulated NCCL runs when
  a compatible CUDA/PyTorch/NCCL environment is available.

See [measurements](measurements.md) for research-only pair, cycle-of-eight and
path-of-four results. There is no ring-of-four hardware run and no
relaxed-ordering-off comparison. The pair 8 MiB eager all-reduce result varied
between runs. A standalone relay installer is not bundled; paths require
preconfigured relay tables and endpoint routes. Release all captured CUDA
graphs before destroying a PyTorch process group.

The standalone source is derived from SparkRing commit
`a4de4dd5280f01613889ced7873aa85ccf8dc2bb`. Transport C/header bytes are retained.
The package tool now supports both source-tree layouts and includes public
release files in the standalone layout. Because the core digest includes tools,
that packaging change gives this release a distinct source identity. It does
not imply that transport algorithms changed. See [SOURCE.json](../SOURCE.json).
